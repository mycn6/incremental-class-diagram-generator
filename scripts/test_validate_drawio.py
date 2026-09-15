"""In-memory regression tests for generation, styling, layout, and validation."""

from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import fields
from io import StringIO
from pathlib import Path
import re
import sys
from typing import Iterator
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

import diagram_gen
import facts_io
from diagram_gen import (
    ChangedFile,
    ClassInfo,
    build_drawio,
    build_facts,
    class_sections,
    export_diagram,
    extract_python,
    facts_to_classes,
    main,
)
from layout_drawio import ENGINE, SUPPORT_GAP, LayoutIssue, check_document, optimize
from layout_drawio import main as layout_main
from normalize_drawio import normalize
from style_drawio import apply, set_style
from style_drawio import main as style_main
from text_layout import (
    ADVANCE_EM,
    MAX_CLASS_WIDTH,
    MIN_CLASS_WIDTH,
    SectionSpec,
    advance_width,
    fit_members,
    hard_wrap,
    logical_lines,
    page_class_width,
    plan_class_box,
    rendered_lines,
    wrap_text,
)
from validate_drawio import plain_label, text_overflow, validate_bytes, validate_document, visible


class DiagramContractTests(unittest.TestCase):
    def setUp(self) -> None:
        path = Path(__file__).parents[1] / "assets" / "class-diagram-example.drawio"
        self.document = ET.parse(path).getroot()
        apply(self.document)
        self.root = self.document.find("diagram/mxGraphModel/root")
        assert self.root is not None

    def obj(self, identifier: str) -> ET.Element:
        return next(item for item in self.root if item.get("id") == identifier)

    def errors(self) -> list[str]:
        return validate_document(self.document)

    def test_example_satisfies_contract(self) -> None:
        self.assertEqual([], self.errors())

    def test_legend_requires_semantic_role(self) -> None:
        self.obj("legend").set("role", "decoration")
        self.assertTrue(any("role=legend" in error for error in self.errors()))

    def test_legend_requires_every_real_uml_type(self) -> None:
        self.root.remove(self.obj("legend-uml-interface"))
        self.assertTrue(any("UML type keys mismatch" in error for error in self.errors()))

    def test_legend_uml_type_requires_compartments(self) -> None:
        self.root.remove(self.obj("legend-uml-interface--separator"))
        self.assertTrue(any("header, body, and native separator" in error for error in self.errors()))

    def test_legend_abstract_header_must_be_italic(self) -> None:
        cell = self.obj("legend-uml-abstract--header").find("mxCell")
        assert cell is not None
        set_style(cell, {"fontStyle": "1"})
        self.assertTrue(any("abstract UML legend header must be italic" in error for error in self.errors()))

    def test_legend_relationship_must_use_real_arrow_style(self) -> None:
        cell = self.obj("legend-relation-inheritance").find("mxCell")
        assert cell is not None
        set_style(cell, {"endArrow": "open"})
        self.assertTrue(any("relationship legend arrow" in error for error in self.errors()))

    def test_legend_change_swatch_must_match_real_state_color(self) -> None:
        cell = self.obj("legend-change-added").find("mxCell")
        assert cell is not None
        set_style(cell, {"fillColor": "#FFFFFF"})
        self.assertTrue(any("change legend style" in error for error in self.errors()))

    def test_example_satisfies_geometry_contract(self) -> None:
        optimize(self.document)
        self.assertEqual([], check_document(self.document))

    def test_layout_is_idempotent(self) -> None:
        optimize(self.document)
        once = ET.tostring(self.document)
        optimize(self.document)
        self.assertEqual(once, ET.tostring(self.document))

    def test_geometry_check_requires_explicit_ports(self) -> None:
        cell = self.obj("relation-2").find("mxCell")
        assert cell is not None
        set_style(cell, {}, clear={"exitX", "exitY", "entryX", "entryY"})
        self.assertTrue(any(issue.kind == "missing-port" for issue in check_document(self.document)))

    def test_geometry_check_detects_edge_through_class(self) -> None:
        edge = self.obj("relation-2")
        cell = edge.find("mxCell")
        assert cell is not None
        cell.set("target", "class-5")
        set_style(
            cell,
            {"exitX": "1", "exitY": "0.5", "entryX": "0", "entryY": "0.5"},
        )
        geometry = cell.find("mxGeometry")
        assert geometry is not None
        for child in list(geometry):
            geometry.remove(child)
        issues = check_document(self.document)
        self.assertTrue(any(issue.kind == "edge-node" for issue in issues))

    def test_geometry_check_detects_overlapping_relationships(self) -> None:
        first = self.obj("relation-2").find("mxCell")
        second = self.obj("relation-1").find("mxCell")
        assert first is not None and second is not None
        first.set("target", "class-5")
        second.set("source", "class-1")
        second.set("target", "class-3")
        for cell in (first, second):
            set_style(
                cell,
                {"exitX": "1", "exitY": "0.5", "entryX": "0", "entryY": "0.5"},
            )
            geometry = cell.find("mxGeometry")
            assert geometry is not None
            for child in list(geometry):
                geometry.remove(child)
        self.assertTrue(any(issue.kind == "edge-overlap" for issue in check_document(self.document)))

    def test_layout_routes_long_edge_around_intermediate_classes(self) -> None:
        edge = self.obj("relation-2")
        cell = edge.find("mxCell")
        assert cell is not None
        cell.set("target", "class-5")
        optimize(self.document)
        self.assertEqual([], check_document(self.document))
        self.assertEqual(ENGINE, edge.get("layout_engine"))
        self.assertEqual("outer", edge.get("layout_route"))
        geometry = cell.find("mxGeometry")
        assert geometry is not None
        points = geometry.find("Array[@as='points']")
        self.assertIsNotNone(points)
        assert points is not None
        self.assertGreaterEqual(len(points), 2)

    def test_missing_kind_is_rejected(self) -> None:
        self.obj("class-1").attrib.pop("kind")
        self.assertTrue(any("class kind" in error for error in self.errors()))

    def test_change_color_must_match_git_state(self) -> None:
        cell = self.obj("class-3").find("mxCell")
        assert cell is not None
        set_style(cell, {"fillColor": "#F3F4F6", "strokeColor": "#475569"})
        self.assertTrue(any("change style" in error for error in self.errors()))

    def test_class_requires_header_compartment(self) -> None:
        self.root.remove(self.obj("class-1--header"))
        self.assertTrue(any("exactly one class-header" in error for error in self.errors()))

    def test_class_compartment_must_belong_to_outer_class(self) -> None:
        cell = self.obj("class-1--header").find("mxCell")
        assert cell is not None
        cell.set("parent", "1")
        self.assertTrue(any("class compartment must belong" in error for error in self.errors()))

    def test_enum_requires_literals_compartment(self) -> None:
        self.root.remove(self.obj("class-4--literals"))
        self.assertTrue(any("exactly one class-literals" in error for error in self.errors()))

    def test_separator_must_be_native_line(self) -> None:
        cell = self.obj("class-1--separator-1").find("mxCell")
        assert cell is not None
        set_style(cell, {"shape": "rectangle"})
        self.assertTrue(any("separator must be a native line" in error for error in self.errors()))

    def test_abstract_header_must_be_italic(self) -> None:
        cell = self.obj("class-1--header").find("mxCell")
        assert cell is not None
        set_style(cell, {"fontStyle": "1"})
        self.assertTrue(any("abstract class header must be italic" in error for error in self.errors()))

    def test_unchanged_class_has_no_visible_prefix(self) -> None:
        header = self.obj("class-5--header")
        self.assertEqual("«interface»\nRepository", header.get("label"))
        header.set("label", "«interface»\n[未变] Repository")
        self.assertTrue(any("without [未变]" in error for error in self.errors()))

    def test_relationship_arrow_must_match_semantics(self) -> None:
        cell = self.obj("relation-1").find("mxCell")
        assert cell is not None
        set_style(cell, {"dashed": "1"})
        self.assertTrue(any("arrow style" in error for error in self.errors()))

    def test_styling_is_idempotent_and_preserves_geometry(self) -> None:
        edges_before = [
            ET.tostring(item.find("mxCell"))
            for item in self.root
            if item.get("role") == "relationship"
        ]
        apply(self.document)
        once = ET.tostring(self.document)
        apply(self.document)
        self.assertEqual(once, ET.tostring(self.document))
        edges_after = [
            ET.tostring(item.find("mxCell"))
            for item in self.root
            if item.get("role") == "relationship"
        ]
        self.assertEqual(edges_before, edges_after)

    def test_generator_does_not_infer_unchanged_types_or_review_issues(self) -> None:
        classes = [
            ClassInfo(
                name="OrderService",
                kind="class",
                source="src/order.py",
                change="modified",
                fields=["- repository: OrderRepository"],
                methods=["+ create_order(command): Order"],
                relations=[("dependency", "OrderRepository")],
            )
        ]
        document = build_drawio(classes, "test-base..test-head")
        self.assertEqual([], validate_document(document))
        self.assertEqual([], check_document(document))
        generated = document.find("diagram/mxGraphModel/root")
        assert generated is not None
        class_nodes = [item for item in generated if item.get("role") == "class"]
        relationships = [item for item in generated if item.get("role") == "relationship"]
        self.assertEqual(1, len(class_nodes))
        self.assertEqual([], relationships)
        self.assertTrue(all(item.get("change") == "modified" for item in class_nodes))

    def test_generator_handles_a_dependency_cycle(self) -> None:
        classes = [
            ClassInfo(
                name="First",
                kind="class",
                source="first.py",
                change="modified",
                relations=[("dependency", "Second")],
            ),
            ClassInfo(
                name="Second",
                kind="class",
                source="second.py",
                change="modified",
                relations=[("dependency", "First")],
            ),
        ]
        document = build_drawio(classes, "cycle")
        self.assertEqual([], check_document(document))

    def test_python_extractor_does_not_infer_analysis_flags(self) -> None:
        source = """
class Service(Protocol):
    def run(self, value: str) -> bool:
        ...
"""
        classes = extract_python(source, "service.py", "added")
        self.assertEqual(1, len(classes))
        self.assertEqual("interface", classes[0].kind)

    def test_cjk_text_rejects_a_latin_only_font(self) -> None:
        cell = self.obj("class-1--header").find("mxCell")
        assert cell is not None
        set_style(cell, {"fontFamily": "Consolas"})
        self.assertTrue(any("needs a CJK-safe fontFamily" in error for error in self.errors()))

    def test_cjk_text_accepts_a_font_stack_with_coverage(self) -> None:
        cell = self.obj("class-1--header").find("mxCell")
        assert cell is not None
        set_style(cell, {"fontFamily": "Consolas, Microsoft YaHei"})
        self.assertEqual([], self.errors())

    def test_cjk_text_requires_an_explicit_font(self) -> None:
        cell = self.obj("class-4--header").find("mxCell")
        assert cell is not None
        set_style(cell, {}, clear={"fontFamily"})
        self.assertTrue(any("needs a CJK-safe fontFamily" in error for error in self.errors()))

    def test_latin_only_labels_may_omit_a_font(self) -> None:
        cell = self.obj("class-5--header").find("mxCell")
        assert cell is not None
        set_style(cell, {}, clear={"fontFamily"})
        self.assertEqual([], self.errors())

    def test_encoding_loss_in_a_label_is_rejected(self) -> None:
        self.obj("scope").set("label", "??：example-base..example-head")
        self.assertTrue(any("encoding loss" in error for error in self.errors()))

    def test_ascii_question_mark_is_not_treated_as_encoding_loss(self) -> None:
        self.obj("scope").set("label", "+ find(key?): Result")
        self.assertEqual([], self.errors())

    def test_bytes_must_decode_as_strict_utf8(self) -> None:
        source = "<?xml version='1.0' encoding='utf-8'?><mxfile><diagram name='增量'/></mxfile>"
        errors = validate_bytes(source.encode("gbk"))
        self.assertTrue(any("not valid UTF-8" in error for error in errors))

    def test_example_bytes_round_trip_through_validate_bytes(self) -> None:
        path = Path(__file__).parents[1] / "assets" / "class-diagram-example.drawio"
        self.assertEqual([], validate_bytes(path.read_bytes()))

    def test_generator_warns_but_still_writes_on_layout_conflicts(self) -> None:
        classes = [ClassInfo(name="Service", kind="class", source="src/service.py", change="modified")]
        conflict = [LayoutIssue("edge-crossing", "relation-1", "relation-2", "near (10, 10)")]
        stderr = StringIO()
        with patch("diagram_gen.optimize_layout", return_value=conflict):
            with redirect_stderr(stderr):
                document = build_drawio(classes, "strict-mode")
        self.assertEqual("mxfile", document.tag)
        self.assertIn("edge-crossing", stderr.getvalue())
        self.assertIn("layout_drawio.py --check", stderr.getvalue())

    def test_generator_strict_mode_refuses_to_write_layout_conflicts(self) -> None:
        classes = [ClassInfo(name="Service", kind="class", source="src/service.py", change="modified")]
        conflict = [LayoutIssue("edge-crossing", "relation-1", "relation-2", "near (10, 10)")]
        with patch("diagram_gen.optimize_layout", return_value=conflict):
            with self.assertRaises(ValueError) as caught:
                build_drawio(classes, "strict-mode", strict=True)
        self.assertIn("still has conflicts", str(caught.exception))

    def test_mutations_do_not_leak_between_documents(self) -> None:
        clone = deepcopy(self.document)
        self.obj("class-1--header").set("label", "changed")
        self.assertNotEqual(ET.tostring(self.document), ET.tostring(clone))
        self.assertEqual([], validate_document(clone))

    def test_example_compartment_too_short_for_its_members_is_rejected(self) -> None:
        geometry = self.obj("class-1--attributes").find("mxCell/mxGeometry")
        assert geometry is not None
        geometry.set("height", "20")
        self.assertTrue(any("rendered lines" in error for error in self.errors()))

    def test_example_legend_header_tolerates_one_pixel_and_not_two(self) -> None:
        geometry = self.obj("legend-uml-interface--header").find("mxCell/mxGeometry")
        assert geometry is not None
        geometry.set("height", "33")
        self.assertEqual([], self.errors())
        geometry.set("height", "32")
        self.assertTrue(any("rendered lines" in error for error in self.errors()))


class TextMetricsTests(unittest.TestCase):
    def test_cjk_advance_is_a_full_em_and_outranks_latin(self) -> None:
        cjk = advance_width("中", 13.0)
        self.assertEqual(13.0, cjk)
        # YaHei is metrically one em square, so a CJK glyph ties the widest
        # Latin class rather than beating it.
        self.assertEqual(cjk, advance_width("m", 13.0))
        self.assertGreater(cjk, advance_width("x", 13.0))
        self.assertGreater(advance_width("x", 13.0), advance_width("i", 13.0))

    def test_every_advance_class_stays_at_or_above_the_true_advance(self) -> None:
        # Guards the one-sided property. Dropping any bucket below the real
        # advance would let the validator pass a box that renders overflowing.
        self.assertEqual(1.00, ADVANCE_EM["cjk"])
        self.assertGreaterEqual(ADVANCE_EM["wide"], 0.83)
        self.assertGreaterEqual(ADVANCE_EM["upper"], 0.68)
        self.assertGreaterEqual(ADVANCE_EM["default"], 0.55)
        self.assertGreaterEqual(ADVANCE_EM["narrow"], 0.28)

    def test_bold_text_measures_wider_than_the_same_regular_text(self) -> None:
        self.assertGreater(advance_width("Repository", 14.0, bold=True), advance_width("Repository", 14.0))

    def test_wrapped_lines_never_exceed_the_width_they_were_wrapped_for(self) -> None:
        text = "+ create_manual_risk(company_id, enterprise_profile_id, creator_id, level, content): dict"
        lines = wrap_text(text, 269.0, 13.0)
        self.assertGreater(len(lines), 1)
        for line in lines:
            self.assertLessEqual(advance_width(line, 13.0), 269.0)

    def test_a_token_with_no_break_opportunity_is_broken_character_by_character(self) -> None:
        text = "+ test_invalid_workbook_does_not_persist_any_row_or_file()"
        lines = wrap_text(text, 269.0, 13.0)
        self.assertGreater(len(lines), 1)
        for line in lines:
            self.assertLessEqual(advance_width(line, 13.0), 269.0)

    def test_explicit_newlines_survive_wrapping(self) -> None:
        self.assertEqual(
            ["«interface»", "Repository"],
            wrap_text("«interface»\nRepository", 400.0, 14.0, bold=True),
        )

    def test_cjk_wraps_into_at_least_as_many_lines_as_ascii(self) -> None:
        self.assertGreaterEqual(
            rendered_lines("中" * 40, 200.0, 13.0),
            rendered_lines("x" * 40, 200.0, 13.0),
        )

    def test_page_width_is_empty_safe_and_clamped(self) -> None:
        self.assertEqual(MIN_CLASS_WIDTH, page_class_width([]))
        self.assertEqual(MIN_CLASS_WIDTH, page_class_width([("ok", 13.0, False)]))
        self.assertEqual(MAX_CLASS_WIDTH, page_class_width([("x" * 4000, 13.0, False)]))

    def test_page_width_uses_a_percentile_rather_than_the_maximum(self) -> None:
        measured = [("x" * 100, 1.0, False)] * 9 + [("x" * 1000, 1.0, False)]
        self.assertEqual(58.0, page_class_width(measured, minimum=0.0, maximum=10**6))

    def test_page_width_includes_the_label_padding_of_its_cells(self) -> None:
        measured = [("x" * 100, 1.0, False)]
        self.assertEqual(58.0, page_class_width(measured, minimum=0.0, maximum=10**6))
        self.assertEqual(74.0, page_class_width(measured, pad=16.0, minimum=0.0, maximum=10**6))

    def test_page_width_measures_the_widest_line_of_a_multiline_label(self) -> None:
        one = page_class_width([("x" * 50, 1.0, False)], minimum=0.0, maximum=10**6)
        two = page_class_width([("x" * 50 + "\n" + "x" * 10, 1.0, False)], minimum=0.0, maximum=10**6)
        self.assertEqual(one, two)

    def test_hard_wrap_makes_the_logical_line_count_the_rendered_line_count(self) -> None:
        for text in (
            "+ " + "x" * 200 + "(a, b): None",
            "- 字段：一个很长的中文属性名称用于测试换行边界",
            "+ save_certificate(manual_fields, uploads, doc_type, company_id): Result",
            "short",
        ):
            wrapped = hard_wrap(text, 269.0, 13.0)
            self.assertEqual(
                logical_lines(wrapped), rendered_lines(wrapped, 269.0, 13.0)
            )
            for line in wrapped.split("\n"):
                self.assertLessEqual(advance_width(line, 13.0), 269.0)

    def test_hard_wrap_is_idempotent_and_keeps_explicit_breaks(self) -> None:
        wrapped = hard_wrap("+" + "x" * 200, 100.0, 13.0)
        self.assertEqual(wrapped, hard_wrap(wrapped, 100.0, 13.0))
        self.assertEqual(["+ a", "+ b"], hard_wrap("+ a\n+ b", 100.0, 13.0).split("\n"))


class ClassBoxPlanningTests(unittest.TestCase):
    def test_sparse_content_reproduces_the_contract_example_geometry(self) -> None:
        # Pins the invariant that keeps assets/class-diagram-example.drawio a
        # valid baseline: when nothing wraps, the planned bands must land on the
        # hand-written 52 / 70 / 88 and 51 / 121 exactly.
        box = plan_class_box(
            "OrderService",
            (
                SectionSpec("attributes", "class-attributes", ("- repository: UserRepository",), "（未自动提取属性）"),
                SectionSpec("operations", "class-operations", ("+ create_user(command): User",), "（未自动提取操作）"),
            ),
            285.0,
        )
        self.assertEqual((285.0, 210.0, 52.0), (box.width, box.height, box.header_height))
        self.assertEqual((51.0, 121.0), box.separators)
        self.assertEqual((52.0, 70.0), (box.sections[0].y, box.sections[0].height))
        self.assertEqual((122.0, 88.0), (box.sections[1].y, box.sections[1].height))

    def test_long_signatures_grow_their_compartment_past_the_old_fixed_height(self) -> None:
        # The old generator gave every operations compartment 88px regardless of
        # content. Four 90-character signatures at the old 285px width do not
        # come close to fitting that, which is the defect this fixes.
        member = "+ create_manual_risk(company_id, enterprise_profile_id, creator_id, level, content): dict"
        section = plan_class_box(
            "X", (SectionSpec("operations", "class-operations", (member,) * 4, "e"),), 285.0
        ).sections[0]
        self.assertGreater(section.height, 88.0)
        self.assertGreaterEqual(
            section.height,
            rendered_lines(section.label, 285.0 - 16, 13.0) * 1.25 * 13.0,
        )

    def test_the_line_budget_caps_a_long_compartment_and_counts_what_it_dropped(self) -> None:
        members = tuple(f"- member_{index}: int" for index in range(40))
        section = plan_class_box("X", (SectionSpec("operations", "class-operations", members, "e"),), 355.0).sections[0]
        self.assertLess(section.kept, 40)
        self.assertEqual(40 - section.kept, section.dropped)
        self.assertEqual(40 - section.kept, int(re.search(r"另有 (\d+) 项", section.label).group(1)))

    def test_the_ellipsis_line_is_charged_to_the_same_budget_as_a_member(self) -> None:
        label, kept, dropped = fit_members(
            [f"- m{index}" for index in range(6)], "e", max_width=10**6, font_size=13.0, budget=5
        )
        self.assertEqual((4, 2), (kept, dropped))
        self.assertEqual("- m0\n- m1\n- m2\n- m3\n… 另有 2 项", label)

    def test_a_compartment_that_exactly_fills_its_budget_hides_nothing(self) -> None:
        label, kept, dropped = fit_members(
            [f"- m{index}" for index in range(5)], "e", max_width=10**6, font_size=13.0, budget=5
        )
        self.assertEqual((5, 0), (kept, dropped))
        self.assertNotIn("另有", label)

    def test_the_visible_count_and_the_dropped_count_never_disagree(self) -> None:
        signatures = (
            "- m{i}",
            "- save_certificate(manual_fields, uploads, doc_type, company_id, certificate_id, force): Result",
        )
        for template in signatures:
            for total in range(1, 40):
                members = [template.format(i=index) for index in range(total)]
                label, kept, dropped = fit_members(members, "e", max_width=269.0, font_size=13.0)
                self.assertEqual(total - kept, dropped)
                # The kept members appear verbatim and in order; the only extra
                # line is the ellipsis, and it is always last.
                shown = "\n".join(hard_wrap(member, 269.0, 13.0) for member in members[:kept])
                if dropped:
                    self.assertEqual(f"{shown}\n… 另有 {dropped} 项", label)
                else:
                    self.assertEqual(shown, label)
                    self.assertNotIn("另有", label)
                # Every emitted line must fit the width it was wrapped for.
                for line in label.split("\n"):
                    self.assertLessEqual(advance_width(line, 13.0), 269.0)

    def test_a_single_oversized_member_is_kept_instead_of_becoming_an_ellipsis(self) -> None:
        label, kept, dropped = fit_members(["+" + "x" * 300], "e", max_width=100.0, font_size=13.0, budget=4)
        self.assertEqual((1, 0), (kept, dropped))
        self.assertNotIn("另有", label)

    def test_every_empty_compartment_keeps_its_authored_label(self) -> None:
        expected = {
            "class": ("（未自动提取属性）", "（未自动提取操作）"),
            "abstract": ("（未自动提取属性）", "（未自动提取操作）"),
            "struct": ("（未自动提取属性）", "（未自动提取操作）"),
            "interface": ("（未自动提取操作）",),
            "enum": ("（未自动提取枚举常量）", "（无相关操作）"),
        }
        for kind, labels in expected.items():
            info = ClassInfo(name="X", kind=kind, source="x.py", change="modified")
            box = plan_class_box("X", class_sections(info), 355.0)
            self.assertEqual(labels, tuple(section.label for section in box.sections))
            for section in box.sections:
                self.assertNotIn("另有", section.label)


class GeneratedSizingTests(unittest.TestCase):
    def test_generated_boxes_pass_both_checks_across_content_shapes(self) -> None:
        for count in (0, 1, 5, 20, 40):
            for members in (
                [f"+ m{index}(a, b): R" for index in range(count)],
                [f"- 字段{index}：值" for index in range(count)],
            ):
                document = build_drawio(
                    [ClassInfo(name="S", kind="class", source="s.py", change="modified", methods=members)],
                    "shapes",
                )
                self.assertEqual([], validate_document(document))
                self.assertEqual([], check_document(document))

    def test_an_unbreakable_signature_is_wrapped_rather_than_left_overflowing(self) -> None:
        for member in (
            "+ test_invalid_workbook_does_not_persist_any_row_or_file(alpha, beta): Result",
            "+ " + "x" * 200 + "(a): R",
        ):
            document = build_drawio(
                [ClassInfo(name="S", kind="class", source="s.py", change="modified", methods=[member])],
                "unbreakable",
            )
            self.assertEqual([], validate_document(document))

    def test_every_generated_label_has_its_breaks_already_written_in(self) -> None:
        # The renderer lets a token with no break opportunity run past the box
        # edge instead of breaking it, so the generator must never rely on
        # whiteSpace=wrap. Every emitted label has to satisfy the validator's own
        # width rule on its explicit lines alone.
        member = "+ " + "x" * 200 + "(a, b): None"
        document = build_drawio(
            [ClassInfo(name="S", kind="class", source="s.py", change="modified", methods=[member])],
            "hard-wrapped",
        )
        root = document.find("diagram/mxGraphModel/root")
        assert root is not None

        checked = 0
        for item in root:
            role = item.get("role") or ""
            if not role.startswith("class-") or role == "class-separator":
                continue
            label = visible(item.get("label"))
            if role == "class-operations":
                self.assertGreater(logical_lines(label), 1, "the long member was never hard-wrapped")
            cell = item.find("mxCell")
            assert cell is not None
            self.assertIsNone(text_overflow(role, label, cell), f"{role} overflows its own box")
            checked += 1
        self.assertGreater(checked, 0)

    def test_mixed_class_kinds_share_one_page_without_conflicts(self) -> None:
        classes = [
            ClassInfo(name="A", kind="class", source="a.py", change="added",
                      fields=["- x: int"], methods=["+ go(self) -> None"]),
            ClassInfo(name="B", kind="abstract", source="b.py", change="modified",
                      methods=["+ run(self) -> None"]),
            ClassInfo(name="C", kind="interface", source="c.py", change="modified",
                      methods=["+ save(x) -> None"]),
            ClassInfo(name="D", kind="enum", source="d.py", change="added",
                      fields=["A = 1"], methods=["+ label(self) -> str"]),
            ClassInfo(name="E", kind="struct", source="e.py", change="added", fields=["- v: int"]),
        ]
        document = build_drawio(classes, "kinds")
        self.assertEqual([], validate_document(document))
        self.assertEqual([], check_document(document))

    def test_every_class_on_a_page_shares_one_width(self) -> None:
        classes = [
            ClassInfo(
                name=f"Type{index}", kind="class", source=f"t{index}.py", change="modified",
                fields=[f"- value_{index}: int"],
                methods=[f"+ get_{index}(self) -> int"] if index else [],
            )
            for index in range(5)
        ]
        root = build_drawio(classes, "shared-width").find("diagram/mxGraphModel/root")
        assert root is not None
        widths = {
            item.find("mxCell/mxGeometry").get("width")
            for item in root
            if item.get("role") == "class"
        }
        self.assertEqual(1, len(widths))

    def test_compartment_structure_follows_the_class_kind(self) -> None:
        for kind, suffixes, separators in (
            ("class", ("attributes", "operations"), 2),
            ("abstract", ("attributes", "operations"), 2),
            ("struct", ("attributes", "operations"), 2),
            ("interface", ("operations",), 1),
            ("enum", ("literals", "operations"), 2),
        ):
            info = ClassInfo(
                name="X", kind=kind, source="x.py", change="modified",
                fields=[] if kind == "interface" else ["- a: int"],
                methods=["+ go(self) -> None"],
            )
            document = build_drawio([info], "kind-structure")
            root = document.find("diagram/mxGraphModel/root")
            assert root is not None
            roles = [item.get("role") for item in root if (item.get("role") or "").startswith("class-")]
            self.assertEqual(
                ["class-header", *(f"class-{suffix}" for suffix in suffixes)],
                [role for role in roles if role != "class-separator"],
            )
            self.assertEqual(separators, roles.count("class-separator"))
            self.assertEqual([], validate_document(document))

    def test_a_taller_class_pushes_the_legend_below_the_whole_class_region(self) -> None:
        classes = [
            ClassInfo(name="Tiny", kind="class", source="a.py", change="modified"),
            ClassInfo(name="Huge", kind="class", source="b.py", change="modified",
                      methods=[f"+ method_{index}(alpha, beta, gamma, delta): Result" for index in range(30)]),
        ]
        document = build_drawio(classes, "tall-row")
        root = document.find("diagram/mxGraphModel/root")
        assert root is not None
        optimize(document)
        bottoms = [
            float(geometry.get("y")) + float(geometry.get("height"))
            for item in root
            if item.get("role") == "class"
            and (geometry := item.find("mxCell/mxGeometry")) is not None
        ]
        legend = next(item for item in root if item.get("role") == "legend")
        legend_geometry = legend.find("mxCell/mxGeometry")
        assert legend_geometry is not None
        self.assertGreaterEqual(float(legend_geometry.get("y")), max(bottoms) + SUPPORT_GAP)

    def test_the_page_covers_every_object_it_contains(self) -> None:
        classes = [
            ClassInfo(name=f"T{index}", kind="class", source=f"t{index}.py", change="modified",
                      methods=[f"+ m{index}(a): R" for _ in range(10)])
            for index in range(6)
        ]
        document = build_drawio(classes, "page-cover")
        root = document.find("diagram/mxGraphModel/root")
        assert root is not None
        model = document.find("diagram/mxGraphModel")
        assert model is not None
        rights, bottoms = [], []
        for item in root:
            geometry = item.find("mxCell/mxGeometry")
            if item.tag != "object" or geometry is None:
                continue
            # Edges carry relative geometry with no coordinates of their own.
            if geometry.get("x") is None or geometry.get("width") is None:
                continue
            rights.append(float(geometry.get("x")) + float(geometry.get("width")))
            bottoms.append(float(geometry.get("y")) + float(geometry.get("height")))
        self.assertGreaterEqual(float(model.get("pageWidth")), max(rights))
        self.assertGreaterEqual(float(model.get("pageHeight")), max(bottoms))


class RelationResolutionTests(unittest.TestCase):
    """Two files may both define a User. The edge must never guess which one."""

    def edges(self, document: ET.Element) -> list[tuple[str, str]]:
        root = document.find("diagram/mxGraphModel/root")
        assert root is not None
        symbols = {item.get("id"): item.get("symbol") for item in root if item.get("role") == "class"}
        result = []
        for item in root:
            if item.get("role") != "relationship":
                continue
            cell = item.find("mxCell")
            assert cell is not None
            result.append((symbols.get(cell.get("source")), symbols.get(cell.get("target"))))
        return result

    def build(self, classes: list[ClassInfo]) -> tuple[ET.Element, str]:
        stderr = StringIO()
        with redirect_stderr(stderr):
            document = build_drawio(classes, "names")
        return document, stderr.getvalue()

    def test_an_ambiguous_short_name_is_refused_and_reported_rather_than_guessed(self) -> None:
        document, warnings = self.build(
            [
                ClassInfo(name="User", kind="class", source="models.py", change="modified"),
                ClassInfo(name="User", kind="class", source="schemas.py", change="modified"),
                ClassInfo(
                    name="Admin", kind="class", source="admin.py", change="added",
                    relations=[("inheritance", "User")],
                ),
            ]
        )
        self.assertEqual([], self.edges(document))
        self.assertIn("relation not drawn", warnings)
        self.assertIn("admin.py: Admin declares User", warnings)
        self.assertIn("claimed by 2 classes", warnings)
        # The page itself is still valid; the edge is missing, not malformed.
        self.assertEqual([], validate_document(document))

    def test_a_qualified_name_picks_the_class_it_names(self) -> None:
        document, warnings = self.build(
            [
                ClassInfo(name="User", kind="class", source="models.py", change="modified"),
                ClassInfo(name="schemas.User", kind="class", source="schemas.py", change="modified"),
                ClassInfo(
                    name="Admin", kind="class", source="admin.py", change="added",
                    relations=[("inheritance", "schemas.User")],
                ),
            ]
        )
        self.assertEqual([("Admin", "schemas.User")], self.edges(document))
        self.assertEqual("", warnings)

    def test_a_unique_short_name_still_resolves(self) -> None:
        document, warnings = self.build(
            [
                ClassInfo(name="Base", kind="class", source="base.py", change="modified"),
                ClassInfo(
                    name="Impl", kind="class", source="impl.py", change="added",
                    relations=[("inheritance", "Base")],
                ),
            ]
        )
        self.assertEqual([("Impl", "Base")], self.edges(document))
        self.assertEqual("", warnings)

    def test_a_target_off_the_page_stays_silent(self) -> None:
        # Framework bases are the documented exclusion, not a defect. Warning on
        # them would bury the ambiguity reports under dozens of noise lines.
        document, warnings = self.build(
            [
                ClassInfo(
                    name="Model", kind="class", source="model.py", change="added",
                    relations=[("inheritance", "BaseModel"), ("inheritance", "ABC")],
                ),
            ]
        )
        self.assertEqual([], self.edges(document))
        self.assertEqual("", warnings)

    def test_the_evidence_string_always_names_the_class_the_edge_points_at(self) -> None:
        # The failure this guards against: an edge drawn to the wrong class while
        # its evidence still named the right one. A reviewer reads the evidence,
        # so a mismatch between the two would be invisible.
        document = build_drawio(
            [
                ClassInfo(name="Base", kind="class", source="base.py", change="modified"),
                ClassInfo(name="Repo", kind="class", source="repo.py", change="modified"),
                ClassInfo(
                    name="OrderService", kind="class", source="svc.py", change="added",
                    relations=[("inheritance", "Base"), ("dependency", "Repo")],
                ),
            ],
            "names",
        )
        root = document.find("diagram/mxGraphModel/root")
        assert root is not None
        symbols = {item.get("id"): item.get("symbol") for item in root if item.get("role") == "class"}
        checked = 0
        for item in root:
            if item.get("role") != "relationship":
                continue
            cell = item.find("mxCell")
            assert cell is not None
            declared = item.get("evidence").rsplit("declares ", 1)[1]
            self.assertEqual(declared, symbols[cell.get("target")])
            checked += 1
        self.assertEqual(2, checked)


class TextFitRuleTests(unittest.TestCase):
    BODY = "shape=rectangle;rounded=0;whiteSpace=wrap;html=0;fontSize=13;spacing=8;"
    HTML = "shape=rectangle;rounded=0;whiteSpace=wrap;html=1;fontSize=13;spacing=8;"

    def cell(self, width: float, height: float, style: str) -> ET.Element:
        cell = ET.Element("mxCell", {"vertex": "1", "parent": "1", "style": style})
        ET.SubElement(
            cell,
            "mxGeometry",
            {"as": "geometry", "x": "0", "y": "0", "width": str(width), "height": str(height)},
        )
        return cell

    def test_tolerance_accepts_one_pixel_and_rejects_ten(self) -> None:
        label = "+ a(): R"
        self.assertIsNone(text_overflow("c", label, self.cell(200, 31, self.BODY)))
        self.assertIsNotNone(text_overflow("c", label, self.cell(200, 21, self.BODY)))

    def test_a_box_narrower_than_a_single_character_is_reported(self) -> None:
        error = text_overflow("c", "中", self.cell(22, 90, self.BODY))
        self.assertIsNotNone(error)
        self.assertIn("a label line is", error)

    def test_a_cell_with_no_room_left_for_its_padding_is_reported(self) -> None:
        error = text_overflow("c", "x", self.cell(10, 10, self.BODY))
        self.assertIsNotNone(error)
        self.assertIn("padding", error)

    def test_the_real_overflow_that_motivated_this_rule_is_reported(self) -> None:
        # Signatures copied from a generated diagram that rendered its text
        # outside the box: nine members in a 285x88 compartment.
        members = (
            "+ __init__(db)",
            "# _manual_risk_dict(item): dict",
            "+ list_manual_risks(company_id, enterprise_profile_id): list[dict]",
            "+ create_manual_risk(company_id, enterprise_profile_id, creator_id, level, content): dict",
            "+ update_manual_risk(company_id, enterprise_profile_id, item_id, level, content): dict",
            "+ delete_manual_risk(company_id, enterprise_profile_id, item_id): None",
            "+ get_dashboard(company_id, enterprise_profile_id, include_recommendations): dict",
            "+ get_recommendations(company_id, enterprise_profile_id, refresh): list[dict]",
        )
        label = "\n".join(members)
        error = text_overflow("class-5--operations", label, self.cell(285, 88, self.BODY))
        self.assertIsNotNone(error)
        self.assertIn("a label line is", error)

        # The same members, hard-wrapped so no line is too wide, only move the
        # failure to the height rule: 88px holds four 13px lines.
        wrapped = hard_wrap(label, 269.0, 13.0)
        self.assertGreater(logical_lines(wrapped), 4)
        error = text_overflow("class-5--operations", wrapped, self.cell(285, 88, self.BODY))
        self.assertIsNotNone(error)
        self.assertIn("rendered lines", error)

        section = plan_class_box(
            "X", (SectionSpec("operations", "class-operations", members, "e"),), 355.0
        ).sections[0]
        self.assertIsNone(
            text_overflow("class-5--operations", section.label, self.cell(355, section.height, self.BODY))
        )

    def test_blank_labels_are_never_reported(self) -> None:
        for label in (None, "", "   ", "\n"):
            self.assertIsNone(text_overflow("c", label, self.cell(200, 90, self.BODY)))

    def test_a_cell_without_geometry_is_never_reported(self) -> None:
        cell = ET.Element("mxCell", {"vertex": "1", "style": self.BODY})
        self.assertIsNone(text_overflow("c", "x" * 500, cell))

    def test_html_off_measures_generic_type_arguments_verbatim(self) -> None:
        label = "+ save(items: list[Result<T>]): None"
        self.assertEqual(label, plain_label(label, {"html": "0"}))
        self.assertEqual(advance_width(label, 13.0), advance_width(plain_label(label, {"html": "0"}), 13.0))

    def test_html_on_strips_markup_before_measuring(self) -> None:
        self.assertEqual("Order", plain_label("<b>Order</b>", {"html": "1"}))
        self.assertIsNone(text_overflow("c", "<b>Order</b>", self.cell(200, 90, self.HTML)))

    def test_entity_decoding_happens_exactly_once(self) -> None:
        # ElementTree already decoded the attribute while parsing, so neither the
        # fit check nor the semantic checks may unescape a second time.
        self.assertEqual("a &amp; b", plain_label("a &amp; b", {}))
        self.assertEqual("a &amp; b", visible("a &amp; b"))


ROOT = Path(__file__).parents[1]


@contextmanager
def quiet() -> Iterator[None]:
    """Swallow both streams for a run whose output the test does not assert on.

    A passing suite should print nothing but its own summary, so the messages a
    command-line run emits on purpose cannot be mistaken for failures.
    """
    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
        yield

_CLASS_FIELDS = frozenset(field.name for field in fields(facts_io.ClassFact))
_RELATION_FIELDS = frozenset(field.name for field in fields(facts_io.RelationFact))


def facts_with(
    classes: list[facts_io.ClassFact],
    relations: list[facts_io.RelationFact] | None = None,
    files: list[str] | None = None,
) -> facts_io.Facts:
    return facts_io.Facts(
        scope={"label": "test", "base": "HEAD", "head": "WORKTREE", "window": ""},
        classes=classes,
        relations=relations or [],
        files=files if files is not None else [],
    )


def simple_class(identifier: str, **overrides) -> facts_io.ClassFact:
    payload = {
        "id": identifier,
        "name": identifier,
        "kind": "class",
        "change": "modified",
        "source": "src/app.py",
        "origin": "scan",
    }
    payload.update(overrides)
    # Anything the dataclass does not declare is an unknown key by definition,
    # which is exactly the case `extra` exists to carry.
    extra = {key: payload.pop(key) for key in list(payload) if key not in _CLASS_FIELDS}
    return facts_io.ClassFact(**payload, extra=extra)


def gutter_fixture() -> list[ClassInfo]:
    """Two edges leaving one column and entering one target from the same side.

    The router only reaches the outer gutter channel when both endpoints are
    boxed in vertically, so a class needs a neighbour above and below it in its
    own column before the top and bottom channels are refused. That is what
    makes the smallest shape that shows the defect eight classes rather than
    two.
    """
    info = {
        name: ClassInfo(
            name=name,
            kind="class",
            source="src/outer-lane.py",
            change="added",
            fields=[f"- {name.lower()}: int"],
        )
        for name in ("A", "X", "Y", "E", "M", "P", "Z", "Q")
    }

    def link(source: str, target: str, kind: str = "association") -> None:
        key = (kind, target)
        info[source].relations.append(key)
        info[source].relation_evidence[key] = f"src/outer-lane.py: {source} uses {target}"
        info[source].relation_targets[key] = info[target]

    for name in ("A", "X", "Y", "E"):
        link(name, "M")
    for name in ("P", "Z", "Q"):
        link("M", name)
    link("X", "Z", "dependency")
    link("Y", "Z", "dependency")
    return list(info.values())


def simple_relation(**overrides) -> facts_io.RelationFact:
    payload = {
        "source_id": "OrderService",
        "target_id": "Repo",
        "target_declared": "Repo",
        "kind": "association",
        "evidence": "svc.py: OrderService stores Repo",
        "origin": "agent",
    }
    payload.update(overrides)
    extra = {key: payload.pop(key) for key in list(payload) if key not in _RELATION_FIELDS}
    return facts_io.RelationFact(**payload, extra=extra)


class FactsSchemaTests(unittest.TestCase):
    """The inventory carries the deliverable, so its own rules have to hold."""

    def test_a_round_trip_preserves_structure_and_bytes(self) -> None:
        original = facts_with([simple_class("OrderService")], [simple_relation()])
        reloaded = facts_io.loads(facts_io.dumps(original))
        self.assertEqual(facts_io.to_payload(original), facts_io.to_payload(reloaded))
        self.assertEqual(facts_io.dumps(original), facts_io.dumps(reloaded))

    def test_dump_is_deterministic(self) -> None:
        first = facts_io.dumps(facts_with([simple_class("A"), simple_class("B")]))
        second = facts_io.dumps(facts_with([simple_class("A"), simple_class("B")]))
        self.assertEqual(first, second)

    def test_a_relation_to_an_unknown_class_is_refused(self) -> None:
        errors, _ = facts_io.validate_facts(
            facts_with([simple_class("OrderService")], [simple_relation(target_id="Ghost")])
        )
        self.assertTrue(any("Ghost" in error and "not a class" in error for error in errors), errors)

    def test_an_unresolved_relation_must_still_name_its_declared_target(self) -> None:
        errors, _ = facts_io.validate_facts(
            facts_with(
                [simple_class("OrderService")],
                [simple_relation(target_id=None, target_declared="")],
            )
        )
        self.assertTrue(any("must still name" in error for error in errors), errors)

    def test_empty_evidence_is_refused(self) -> None:
        errors, _ = facts_io.validate_facts(
            facts_with([simple_class("OrderService")], [simple_relation(evidence="   ")])
        )
        self.assertTrue(any("evidence must not be empty" in error for error in errors), errors)

    def test_a_source_outside_the_repository_is_refused(self) -> None:
        for source in ("", "/etc/passwd", "C:/tmp/x.py", "../outside.py"):
            with self.subTest(source=source):
                errors, _ = facts_io.validate_facts(facts_with([simple_class("A", source=source)]))
                self.assertTrue(errors, f"{source!r} should be refused")

    def test_an_off_page_target_is_allowed_and_stays_quiet(self) -> None:
        # A framework base is the documented exclusion. Refusing it, or warning
        # on it, would make every ordinary scan look broken.
        errors, warnings = facts_io.validate_facts(
            facts_with(
                [simple_class("OrderService")],
                [simple_relation(target_id=None, target_declared="BaseModel",
                                 evidence="svc.py: OrderService extends BaseModel")],
            )
        )
        self.assertEqual([], errors)
        self.assertEqual([], warnings)

    def test_evidence_that_omits_the_target_warns_without_failing(self) -> None:
        # Evidence may name the target through a variable, so this cannot be an
        # error - an over-strict validator sends the author back to hand-editing
        # the exported XML.
        errors, warnings = facts_io.validate_facts(
            facts_with([simple_class("OrderService"), simple_class("Repo")],
                       [simple_relation(evidence="svc.py: OrderService stores the gateway")]
            )
        )
        self.assertEqual([], errors)
        self.assertTrue(any("does not mention" in warning for warning in warnings), warnings)

    def test_unknown_keys_survive_a_round_trip(self) -> None:
        original = facts_with([simple_class("A", note="keep me")])
        reloaded = facts_io.loads(facts_io.dumps(original))
        self.assertEqual({"note": "keep me"}, reloaded.classes[0].extra)
        self.assertEqual(facts_io.dumps(original), facts_io.dumps(reloaded))

    def test_two_classes_with_one_name_get_distinct_ids(self) -> None:
        taken: set[str] = set()
        first = facts_io.derive_id("User", taken)
        taken.add(first)
        second = facts_io.derive_id("User", taken)
        self.assertEqual(("User", "User#2"), (first, second))


class FactsMergeTests(unittest.TestCase):
    """Re-scanning must refresh the scan, not destroy the review."""

    def test_reviewed_entries_survive_and_scanned_ones_are_replaced(self) -> None:
        existing = facts_with(
            [simple_class("Keep", origin="agent", change="unchanged"), simple_class("Stale")],
            [simple_relation(source_id="Keep", target_id="Keep", target_declared="Keep")],
            files=["src/app.py"],
        )
        fresh = facts_with([simple_class("Stale", change="added")], files=["src/app.py"])
        merged = facts_io.merge_facts(existing, fresh)

        self.assertEqual(["Stale", "Keep"], [fact.id for fact in merged.classes])
        self.assertEqual("added", merged.classes[0].change)
        self.assertEqual("agent", merged.classes[1].origin)
        self.assertEqual(1, len(merged.relations))

    def test_a_reviewed_class_the_scan_now_covers_is_reported(self) -> None:
        existing = facts_with([simple_class("Orders", origin="agent", change="unchanged")])
        fresh = facts_with([simple_class("Orders", change="modified")])
        merged = facts_io.merge_facts(existing, fresh)
        self.assertEqual(1, len(merged.classes))
        self.assertEqual("scan", merged.classes[0].origin)
        self.assertTrue(any("now in the change set" in warning for warning in merged.warnings),
                        merged.warnings)

    def test_scope_matching_ignores_the_timestamp_but_not_the_range(self) -> None:
        current = {"base": "HEAD", "head": "WORKTREE", "window": "", "generated_at": "noon"}
        self.assertTrue(facts_io.scope_matches({**current, "generated_at": "dawn"}, current))
        self.assertFalse(facts_io.scope_matches({**current, "window": "2020-01-01"}, current))
        self.assertFalse(facts_io.scope_matches({**current, "head": "HEAD~3"}, current))


class FactsExportTests(unittest.TestCase):
    """The .drawio is a pure function of the inventory, never read back in."""

    def edges(self, document: ET.Element) -> list[tuple[str, str, str, str]]:
        root = document.find("diagram/mxGraphModel/root")
        assert root is not None
        symbols = {item.get("id"): item.get("symbol") for item in root if item.get("role") == "class"}
        result = []
        for item in root:
            if item.get("role") != "relationship":
                continue
            cell = item.find("mxCell")
            assert cell is not None
            result.append((
                symbols.get(cell.get("source")),
                symbols.get(cell.get("target")),
                item.get("relation"),
                item.get("evidence"),
            ))
        return result

    def test_the_example_inventory_exports_a_contract_valid_diagram(self) -> None:
        path = Path(__file__).parents[1] / "assets" / "class-facts-example.json"
        facts = facts_io.load_facts(path)
        errors, warnings = facts_io.validate_facts(facts)
        self.assertEqual([], errors)
        self.assertEqual([], warnings)
        document = build_drawio(facts_to_classes(facts), "example")
        self.assertEqual([], validate_document(document))
        self.assertEqual([], check_document(document))

    def test_a_reviewed_relation_exports_with_its_evidence_verbatim(self) -> None:
        facts = facts_with(
            [simple_class("OrderService"), simple_class("Repo")],
            [simple_relation(evidence="svc.py: OrderService.__init__ stores Repo")],
        )
        edges = self.edges(build_drawio(facts_to_classes(facts), "test"))
        self.assertEqual(
            [("OrderService", "Repo", "association", "svc.py: OrderService.__init__ stores Repo")],
            edges,
        )

    def test_a_reviewed_unchanged_class_has_no_visible_status_prefix(self) -> None:
        facts = facts_with([simple_class("Repository", kind="interface", change="unchanged",
                                         origin="agent")])
        document = build_drawio(facts_to_classes(facts), "test")
        root = document.find("diagram/mxGraphModel/root")
        assert root is not None
        header = next(item for item in root if item.get("role") == "class-header")
        self.assertEqual("«interface»\nRepository", header.get("label"))

    def test_a_pinned_target_disambiguates_two_classes_that_share_a_name(self) -> None:
        # A name lookup cannot express this; the inventory's id can, and the
        # export must honour it rather than falling back to guessing.
        classes = [
            simple_class("User", name="User", source="models.py"),
            simple_class("User#2", name="User", source="schemas.py"),
            simple_class("Admin", name="Admin", change="added"),
        ]
        facts = facts_with(
            classes,
            [simple_relation(source_id="Admin", target_id="User#2", target_declared="User",
                             kind="dependency", evidence="admin.py: Admin validates schemas.User")],
        )
        edges = self.edges(build_drawio(facts_to_classes(facts), "test"))
        self.assertEqual([("Admin", "User", "dependency", "admin.py: Admin validates schemas.User")],
                         edges)

    def test_the_full_member_list_survives_when_the_export_truncates(self) -> None:
        methods = [f"+ method_{number}(value: int): None" for number in range(40)]
        facts = facts_with([simple_class("Big", methods=methods)])
        self.assertEqual(40, len(facts.classes[0].methods))

        document = build_drawio(facts_to_classes(facts), "test")
        root = document.find("diagram/mxGraphModel/root")
        assert root is not None
        operations = next(item for item in root if item.get("role") == "class-operations")
        label = operations.get("label") or ""
        self.assertIn("… 另有", label)
        dropped = int(re.search(r"… 另有 (\d+) 项", label).group(1))
        kept = sum(1 for line in label.splitlines() if line.startswith("+ method_"))
        self.assertEqual(40, kept + dropped)

    def test_an_inventory_with_no_changed_classes_refuses_to_export(self) -> None:
        # Writing it would produce a document validate_drawio.py rejects, which
        # is a worse failure than refusing up front.
        facts = facts_with([simple_class("Only", change="unchanged", origin="agent")])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "facts.json"
            facts_io.dump_facts(facts, path)
            with self.assertRaises(facts_io.FactsError):
                export_diagram(facts, Path(folder) / "direct.drawio", strict=False)
            with quiet():
                code = main(["--repo", str(ROOT), "--from-facts", str(path),
                             "--output", str(Path(folder) / "out.drawio")])
            self.assertEqual(2, code)
            self.assertFalse((Path(folder) / "direct.drawio").exists())
            self.assertFalse((Path(folder) / "out.drawio").exists())


class FactsCommandLineTests(unittest.TestCase):
    """Which artifacts a run produces, and how it fails."""

    def test_no_diagram_is_requested_unless_output_is_given(self) -> None:
        args = diagram_gen.parse_args(["--repo", str(ROOT), "--days", "3"])
        self.assertIsNone(args.output)

    def test_scanning_without_output_writes_no_diagram(self) -> None:
        with tempfile.TemporaryDirectory() as folder, \
                patch("diagram_gen.collect_changes",
                      return_value=([ChangedFile("a.py", "modified")], "HEAD..HEAD")), \
                patch("diagram_gen.source_at_revision", return_value="class A: pass"), \
                patch("diagram_gen.write_document") as writer, quiet():
            code = main(["--repo", str(ROOT), "--base", "HEAD", "--head", "HEAD",
                         "--facts", str(Path(folder) / "facts.json")])
        self.assertEqual(0, code)
        writer.assert_not_called()

    def test_a_scope_mismatch_refuses_to_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "facts.json"
            facts_io.dump_facts(
                facts_io.Facts(scope={"base": "OLD", "head": "HEAD", "window": ""}), target
            )
            with patch("diagram_gen.collect_changes",
                       return_value=([ChangedFile("a.py", "modified")], "HEAD..HEAD")), \
                    patch("diagram_gen.source_at_revision", return_value="class A: pass"), \
                    quiet():
                code = main(["--repo", str(ROOT), "--base", "HEAD", "--head", "HEAD",
                             "--facts", str(target)])
            self.assertEqual(3, code)
            self.assertEqual("OLD", facts_io.load_facts(target).scope["base"])

    def test_strict_layout_conflicts_exit_two_without_a_traceback(self) -> None:
        # build_drawio raises on --strict, and an uncaught raise here would print
        # a traceback next to three other paths that report cleanly.
        stderr = StringIO()
        with tempfile.TemporaryDirectory() as folder, \
                patch("diagram_gen.collect_changes",
                      return_value=([ChangedFile("a.py", "modified")], "HEAD..HEAD")), \
                patch("diagram_gen.source_at_revision", return_value="class A: pass"), \
                patch("diagram_gen.optimize_layout", return_value=["edge crosses node"]), \
                redirect_stderr(stderr), redirect_stdout(StringIO()):
            code = main(["--repo", str(ROOT), "--base", "HEAD", "--head", "HEAD",
                         "--facts", str(Path(folder) / "facts.json"),
                         "--output", str(Path(folder) / "out.drawio"), "--strict"])
        self.assertEqual(2, code)
        self.assertIn("conflicts", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


class ExportLineEndingTests(unittest.TestCase):
    """Every .drawio write path emits LF, whatever the host platform is.

    Handing `ET` a filename opens the file in text mode, where "\n" becomes
    os.linesep: the same input would produce different bytes on Windows and on
    Linux. Each writer opens a binary handle instead, so these assertions hold
    on every platform rather than only on the one the suite runs on.
    """

    def assert_lf(self, path: Path) -> None:
        raw = path.read_bytes()
        self.assertNotIn(b"\r", raw, f"{path.name} was written with CRLF")
        self.assertIn(b"\n", raw, f"{path.name} was not written at all")

    def test_the_generator_writes_lf(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "generated.drawio"
            with quiet():
                code = main(["--repo", str(ROOT), "--from-facts",
                             str(ROOT / "assets" / "class-facts-example.json"),
                             "--output", str(output)])
            self.assertEqual(0, code)
            self.assert_lf(output)

    def test_the_layout_command_writes_lf(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "laid-out.drawio"
            with patch.object(sys, "argv", [
                "layout_drawio.py", str(ROOT / "assets" / "class-diagram-example.drawio"),
                "--output", str(output),
            ]), quiet():
                code = layout_main()
            self.assertEqual(0, code)
            self.assert_lf(output)

    def test_the_style_command_writes_lf(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "styled.drawio"
            with patch.object(sys, "argv", [
                "style_drawio.py", str(ROOT / "assets" / "class-diagram-example.drawio"),
                "--output", str(output),
            ]), quiet():
                code = style_main()
            self.assertEqual(0, code)
            self.assert_lf(output)

    def test_the_normalizer_rewrites_only_line_endings(self) -> None:
        source = (ROOT / "assets" / "class-diagram-example.drawio").read_bytes()
        self.assertNotIn(b"\r", source, "the fixture itself drifted back to CRLF")
        self.assertEqual(source, normalize(source))
        self.assertEqual(source, normalize(source.replace(b"\n", b"\r\n")))

    def test_a_bare_cr_is_refused_rather_than_guessed_at(self) -> None:
        with self.assertRaises(ValueError):
            normalize(b"<mxfile/>\r")


class OuterLaneRegressionTests(unittest.TestCase):
    """A documented limitation, pinned so a fix has an objective pass/fail.

    `route_edges` assigns an outer lane by counting per channel name, and the
    count only ever moves a horizontal line's y. The x of a gutter vertical
    comes from a box edge alone, so two edges whose relevant box edges line up
    are drawn on the same x and overlap instead of being spread apart. See
    AGENTS.md「已知限制：外围通道的竖折线不分配 x 车道」.
    """

    @unittest.expectedFailure
    def test_two_edges_entering_one_target_share_a_gutter_lane(self) -> None:
        # X and Y both enter Z from the left, and both leave the same column,
        # so all four of their gutter verticals derive from a box edge. Two
        # classes in that column and one target column are enough to collide.
        with quiet():
            document = build_drawio(gutter_fixture(), "outer-lane")
        reported = {
            frozenset((issue.edge, issue.obstacle))
            for issue in check_document(document)
        }
        self.assertNotIn(frozenset(("relation-2", "relation-6")), reported)


if __name__ == "__main__":
    unittest.main()

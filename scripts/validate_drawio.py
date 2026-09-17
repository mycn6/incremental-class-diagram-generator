#!/usr/bin/env python3
"""Validate incremental class diagrams against the documented XML contract."""

from __future__ import annotations

import argparse
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable

from style_drawio import (
    CHANGE_LEGEND_LABELS,
    CHANGE_STYLES,
    CJK_FONTS,
    CLASS_KIND_LEGEND_LABELS,
    CLASS_KINDS,
    RELATION_LEGEND_LABELS,
    RELATION_STYLES,
    style_dict,
)
from text_layout import LINE_RATIO, logical_lines, widest_line


CHANGE_LABELS = {
    "added": "[新增]",
    "modified": "[修改]",
    "removed": "[删除]",
    "unchanged": "",
}

CJK_TEXT = re.compile(r"[一-鿿]")

# Heuristic trace of a lossy write: a Western codec with errors=replace turns
# every unmappable character into "?", so runs of them - or a "?" touching CJK -
# are evidence that text was re-encoded outside the UTF-8 pipeline.
LOST_TEXT = re.compile(r"\?\?|[?][一-鿿]|[一-鿿][?]")

# Markup removal for html=1 labels. The lookarounds keep generic type arguments
# like list[Result<T>] from being mistaken for tags.
TAGS = re.compile(r"(?<!<)<[^<>]*>(?!>)")

# Absorbs the generator's ceiling rounding and the tightest authored vertex in
# the contract example, whose legend UML header is exactly 34px tall for two
# 12px lines. Real overflows are an order of magnitude larger (the smallest one
# observed was 24px), so this cannot mask a genuine defect.
TOLERANCE_PX = 1.5

VERTEX_ROLES = {
    "class", "class-header", "class-attributes", "class-operations",
    "class-literals", "class-separator", "package", "note", "decoration",
    "legend", "legend-item", "legend-item-section", "legend-text",
}


def visible(value: str | None) -> str:
    """Label text with html=1 markup removed.

    ElementTree already decoded the attribute while parsing, so this must not
    unescape a second time: doing so turns a label that literally reads "&amp;"
    into a bare "&".
    """
    return TAGS.sub("", value or "")


def flattened(value: str | None) -> str:
    """Label text with markup and hard-wrapped line breaks removed.

    The generator inserts line breaks to keep text inside its box, so a long
    symbol can legitimately be split across lines. The semantic check is that its
    characters are still present in order, not that they sit on one line.
    """
    return visible(value).replace("\n", "")


def plain_label(value: str | None, styles: dict[str, str]) -> str:
    """The characters the renderer actually has to draw.

    Markup is only stripped when the cell opts into html=1. An html=0 label is
    measured verbatim, which is what keeps generic type arguments such as
    list[Result<T>] from being mistaken for tags and under-measured.
    """
    text = value or ""
    return TAGS.sub("", text) if styles.get("html", "0") == "1" else text


def font_style_bits(style: str | None) -> int:
    try:
        return int(style_dict(style).get("fontStyle", "0"))
    except ValueError:
        return 0


def has_italic(style: str | None) -> bool:
    return bool(font_style_bits(style) & 2)


def has_bold(style: str | None) -> bool:
    return bool(font_style_bits(style) & 1)


def style_number(styles: dict[str, str], name: str, default: float) -> float:
    try:
        return float(styles.get(name, default))
    except ValueError:
        return default


def text_overflow(identifier: str, label: str | None, cell: ET.Element) -> str | None:
    """Report the first way this label fails to fit its own cell, or None.

    Measures the authored label against the cell's own geometry, so it sees an
    overflow that no mxGeometry-to-mxGeometry check can: a label that renders
    past its compartment changes no coordinate at all.

    The model assumes the renderer does not wrap anything: `\\n` is where the
    generator put a break, and a token that does not fit is a defect rather than
    something the renderer will fold. Observed draw.io renders let an unbreakable
    token run straight past the box edge, so a line that is too wide is a real
    overflow and not a hypothetical one.
    """
    geometry = cell.find("mxGeometry")
    geometry = cell.find("mxGeometry")
    if geometry is None:
        return None
    try:
        width = float(geometry.get("width", "0"))
        height = float(geometry.get("height", "0"))
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None

    styles = style_dict(cell.get("style"))
    text = plain_label(label, styles)
    if not text.strip():
        return None

    font_size = style_number(styles, "fontSize", 12.0)
    spacing = style_number(styles, "spacing", 2.0)
    bold = has_bold(cell.get("style"))
    usable_width = width - 2 * spacing
    usable_height = height - 2 * spacing
    if usable_width <= 0 or usable_height <= 0:
        return f"{identifier}: cell padding of {spacing:g}px leaves no room for text"

    lines = logical_lines(text)
    widest = widest_line(text, font_size, bold=bold)
    if widest > usable_width + TOLERANCE_PX:
        return (
            f"{identifier}: a label line is {widest:.0f}px wide but the box allows {usable_width:.0f}px; "
            f"the renderer does not break a token that has no break opportunity, so insert a line break "
            f"or widen the node"
        )

    needed_height = lines * LINE_RATIO * font_size
    if needed_height > usable_height + TOLERANCE_PX:
        return (
            f"{identifier}: label needs {lines} rendered lines ({needed_height:.0f}px) but the box is "
            f"{usable_height:.0f}px tall; raise its height to at least "
            f"{needed_height + 2 * spacing:.0f}px"
        )
    return None


def cjk_safe_font(value: str | None) -> bool:
    families = {part.strip().strip("\"'") for part in (value or "").split(",")}
    return bool(families & CJK_FONTS)


def label_of(item: ET.Element) -> str:
    return item.get("label") if item.tag != "mxCell" else item.get("value")


def encoding_loss(identifier: str, item: ET.Element, cell: ET.Element, fail: Callable[[str], None]) -> None:
    for source in (item, cell):
        for name, value in source.attrib.items():
            if LOST_TEXT.search(value):
                fail(f"{identifier}: attribute {name} shows encoding loss: {value[:60]!r}")
    for text in item.itertext():
        if LOST_TEXT.search(text):
            fail(f"{identifier}: text shows encoding loss: {text.strip()[:60]!r}")


def is_descendant(
    identifier: str,
    ancestor: str,
    items: dict[str, tuple[ET.Element, ET.Element]],
) -> bool:
    visited: set[str] = set()
    parent = items[identifier][1].get("parent", "")
    while parent and parent not in visited:
        if parent == ancestor:
            return True
        visited.add(parent)
        parent_item = items.get(parent)
        if parent_item is None:
            return False
        parent = parent_item[1].get("parent", "")
    return False


def validate_graphic_legend(
    items: dict[str, tuple[ET.Element, ET.Element]],
    legend_id: str,
    fail: Callable[[str], None],
) -> None:
    descendants = {
        identifier: pair
        for identifier, pair in items.items()
        if identifier != legend_id and is_descendant(identifier, legend_id, items)
    }
    for identifier, (item, _) in items.items():
        if item.get("role") in {"legend-item", "legend-item-section", "legend-edge", "legend-text"}:
            if identifier not in descendants:
                fail(f"{identifier}: legend graphic must be grouped under {legend_id}")

    def keyed(role: str, group: str) -> dict[str, list[tuple[str, ET.Element, ET.Element]]]:
        result: dict[str, list[tuple[str, ET.Element, ET.Element]]] = {}
        for identifier, (item, cell) in descendants.items():
            if item.get("role") == role and item.get("legend_group") == group:
                result.setdefault(item.get("legend_key", ""), []).append((identifier, item, cell))
        return result

    def exact_entries(
        entries: dict[str, list[tuple[str, ET.Element, ET.Element]]],
        expected: set[str],
        description: str,
    ) -> None:
        if set(entries) != expected:
            missing = sorted(expected - set(entries))
            extra = sorted(set(entries) - expected)
            fail(f"legend: {description} keys mismatch; missing={missing}, extra={extra}")
        for key, values in entries.items():
            if len(values) != 1:
                fail(f"legend: {description} {key} must appear exactly once")

    uml_entries = keyed("legend-item", "uml-type")
    exact_entries(uml_entries, set(CLASS_KINDS), "UML type")
    for kind, values in uml_entries.items():
        if len(values) != 1 or kind not in CLASS_KINDS:
            continue
        identifier, _, cell = values[0]
        styles = style_dict(cell.get("style"))
        if (
            styles.get("shape") != "rectangle"
            or styles.get("rounded", "0") != "0"
            or styles.get("container") != "1"
        ):
            fail(f"{identifier}: UML legend item must use a compartment container")
        sections = [
            (child_id, child, child_cell)
            for child_id, (child, child_cell) in descendants.items()
            if child.get("role") == "legend-item-section" and child_cell.get("parent") == identifier
        ]
        parts = {child.get("legend_part", ""): (child_id, child, child_cell) for child_id, child, child_cell in sections}
        if set(parts) != {"header", "body", "separator"} or len(sections) != 3:
            fail(f"{identifier}: UML legend item needs header, body, and native separator sections")
            continue
        header_id, header, header_cell = parts["header"]
        header_label = visible(header.get("label"))
        if CLASS_KIND_LEGEND_LABELS[kind] not in header_label:
            fail(f"{header_id}: UML legend header must show {CLASS_KIND_LEGEND_LABELS[kind]}")
        stereotype = CLASS_KINDS[kind]
        if stereotype and stereotype not in header_label:
            fail(f"{header_id}: UML legend header must show {stereotype}")
        if kind == "abstract" and not has_italic(header_cell.get("style")):
            fail(f"{header_id}: abstract UML legend header must be italic")
        separator_id, _, separator_cell = parts["separator"]
        if style_dict(separator_cell.get("style")).get("shape") != "line":
            fail(f"{separator_id}: UML legend separator must be a native line")

    change_entries = keyed("legend-item", "change")
    exact_entries(change_entries, set(CHANGE_STYLES), "change state")
    for change, values in change_entries.items():
        if len(values) != 1 or change not in CHANGE_STYLES:
            continue
        identifier, item, cell = values[0]
        styles = style_dict(cell.get("style"))
        if styles.get("shape") != "rectangle":
            fail(f"{identifier}: change legend item must be a native rectangle")
        for name, expected in CHANGE_STYLES[change].items():
            if styles.get(name) != expected:
                fail(f"{identifier}: change legend style disagrees with {change}")
                break
        if CHANGE_LEGEND_LABELS[change] not in visible(item.get("label")):
            fail(f"{identifier}: change legend item must show {CHANGE_LEGEND_LABELS[change]}")

    relation_entries = keyed("legend-edge", "relationship")
    exact_entries(relation_entries, set(RELATION_STYLES), "relationship")
    for relation, values in relation_entries.items():
        if len(values) != 1 or relation not in RELATION_STYLES:
            continue
        identifier, item, cell = values[0]
        if cell.get("edge") != "1":
            fail(f"{identifier}: relationship legend must be a real edge")
        geometry = cell.find("mxGeometry")
        if (
            geometry is None
            or geometry.get("relative") != "1"
            or geometry.find("mxPoint[@as='sourcePoint']") is None
            or geometry.find("mxPoint[@as='targetPoint']") is None
        ):
            fail(f"{identifier}: relationship legend needs editable source and target points")
        styles = style_dict(cell.get("style"))
        for name, expected in RELATION_STYLES[relation].items():
            if styles.get(name) != expected:
                fail(f"{identifier}: relationship legend arrow disagrees with {relation}")
                break
        if RELATION_LEGEND_LABELS[relation] not in visible(item.get("label")):
            fail(f"{identifier}: relationship legend must show {RELATION_LEGEND_LABELS[relation]}")

def validate_document(document: ET.Element) -> list[str]:
    errors: list[str] = []
    if document.tag != "mxfile" or not document.findall("diagram"):
        return ["Expected mxfile with at least one diagram"]

    for page in document.findall("diagram"):
        page_name = page.get("name", "unnamed")

        def fail(message: str) -> None:
            errors.append(f"{page_name}: {message}")

        root = page.find("mxGraphModel/root")
        if root is None:
            fail("Expected uncompressed mxGraphModel/root")
            continue

        items: dict[str, tuple[ET.Element, ET.Element]] = {}
        for item in root:
            cell = item if item.tag == "mxCell" else item.find("mxCell")
            identifier = item.get("id")
            if cell is None or not identifier:
                fail(f"Missing cell or ID: {identifier}")
                continue
            if identifier in items:
                fail(f"Duplicate ID: {identifier}")
                continue
            items[identifier] = (item, cell)

        if "0" not in items or "1" not in items or items["1"][1].get("parent") != "0":
            fail("Required root/layer cells 0 and 1 are missing or invalid")

        class_ids: set[str] = set()
        class_keys: set[tuple[str, str]] = set()
        changed_classes = 0
        legends: list[ET.Element] = []

        for identifier, (item, cell) in items.items():
            role = item.get("role")
            parent = cell.get("parent")
            if identifier != "0" and parent not in items:
                fail(f"{identifier}: unknown parent {parent}")

            encoding_loss(identifier, item, cell, fail)

            label = visible(label_of(item))
            if label and CJK_TEXT.search(label):
                font = style_dict(cell.get("style")).get("fontFamily")
                if not cjk_safe_font(font):
                    fail(
                        f"{identifier}: cell showing CJK text needs a CJK-safe "
                        f"fontFamily, found {font!r}"
                    )

            is_vertex = cell.get("vertex") == "1"
            is_edge = cell.get("edge") == "1"
            if is_vertex and is_edge:
                fail(f"{identifier}: cannot be both vertex and edge")

            if is_vertex:
                if role not in VERTEX_ROLES:
                    fail(f"{identifier}: missing or unsupported vertex role")
                geometry = cell.find("mxGeometry")
                try:
                    dimensions = (
                        [float(geometry.get(key, "0")) for key in ("width", "height")]
                        if geometry is not None
                        else []
                    )
                    if len(dimensions) != 2 or min(dimensions) <= 0:
                        raise ValueError
                except (TypeError, ValueError):
                    fail(f"{identifier}: invalid vertex dimensions")
                else:
                    overflow = text_overflow(identifier, label_of(item), cell)
                    if overflow:
                        fail(overflow)

            if role == "legend":
                legends.append(item)

            if role in {
                "class-header", "class-attributes", "class-operations",
                "class-literals", "class-separator",
            }:
                owner = items.get(parent or "")
                if owner is None or owner[0].get("role") != "class" or item.get("for") != parent:
                    fail(f"{identifier}: class compartment must belong to its class")

            if role == "class":
                class_ids.add(identifier)
                kind = item.get("kind")
                change = item.get("change")
                symbol = (item.get("symbol") or "").strip()
                source = (item.get("source") or "").strip()

                if kind not in CLASS_KINDS:
                    fail(f"{identifier}: invalid or missing class kind")

                if change not in CHANGE_LABELS:
                    fail(f"{identifier}: invalid or missing change status")

                if not symbol:
                    fail(f"{identifier}: missing symbol metadata")
                if not source:
                    fail(f"{identifier}: missing source evidence")
                key = (source, symbol)
                if key in class_keys:
                    fail(f"{identifier}: duplicate class source/symbol pair")
                class_keys.add(key)

                styles = style_dict(cell.get("style"))
                if (
                    styles.get("shape") != "rectangle"
                    or styles.get("rounded", "0") != "0"
                    or styles.get("container") != "1"
                ):
                    fail(f"{identifier}: class must use a native compartment container")
                if change in CHANGE_STYLES:
                    for key_name, expected in CHANGE_STYLES[change].items():
                        if styles.get(key_name) != expected:
                            fail(f"{identifier}: change style disagrees with {change}")
                            break

                if change in {"added", "modified", "removed"}:
                    changed_classes += 1

            if role == "relationship":
                if not is_edge:
                    fail(f"{identifier}: relationship must be an edge")
                relation = item.get("relation")
                if relation not in RELATION_STYLES:
                    fail(f"{identifier}: invalid or missing relationship type")
                if not (item.get("evidence") or "").strip():
                    fail(f"{identifier}: relationship needs evidence")
                topic = item.get("topic", "")
                topic_label = item.get("topic_label", "")
                if bool(topic) != bool(topic_label):
                    fail(f"{identifier}: topic and topic_label must appear together")
                if topic_label and CJK_TEXT.search(topic_label) is None:
                    fail(f"{identifier}: topic_label must contain a Chinese business description")
                geometry = cell.find("mxGeometry")
                if geometry is None or geometry.get("relative") != "1":
                    fail(f"{identifier}: edge needs relative geometry")
                if relation in RELATION_STYLES:
                    expected_label = RELATION_LEGEND_LABELS[relation]
                    if visible(item.get("label")).strip() != expected_label:
                        fail(f"{identifier}: visible relationship label must be {expected_label}")
                    styles = style_dict(cell.get("style"))
                    for key_name, expected in RELATION_STYLES[relation].items():
                        if styles.get(key_name) != expected:
                            fail(f"{identifier}: arrow style disagrees with {relation}")
                            break

            if is_edge and role not in {"relationship", "legend-edge"}:
                fail(f"{identifier}: edges must use role=relationship or role=legend-edge")

        page_relationships = [item for item, _ in items.values() if item.get("role") == "relationship"]
        if page_name.startswith("关系详图｜"):
            labels = {item.get("topic_label", "") for item in page_relationships} - {""}
            if labels and not any(label in page_name for label in labels):
                fail("relationship detail page name must include a reviewed Chinese business topic")
            expected_topics = sorted({item.get("topic", "") for item in page_relationships} - {""})
            if expected_topics and page.get("relation_topics", "").split(",") != expected_topics:
                fail("relationship detail page relation_topics metadata is incomplete")

        for identifier in class_ids:
            class_item, _ = items[identifier]
            kind = class_item.get("kind", "")
            change = class_item.get("change", "")
            symbol = class_item.get("symbol", "")
            children = [
                (child_id, child, child_cell)
                for child_id, (child, child_cell) in items.items()
                if child_cell.get("parent") == identifier
                and child.get("role", "").startswith("class-")
            ]
            roles: dict[str, list[tuple[str, ET.Element, ET.Element]]] = {}
            for child_id, child, child_cell in children:
                roles.setdefault(child.get("role", ""), []).append((child_id, child, child_cell))
                if child.get("for") != identifier:
                    fail(f"{child_id}: class compartment must reference its parent class")

            expected_single = {"class-header", "class-operations"}
            if kind == "enum":
                expected_single.add("class-literals")
            elif kind != "interface":
                expected_single.add("class-attributes")
            expected_separators = 1 if kind == "interface" else 2
            allowed_roles = expected_single | {"class-separator"}
            for role in expected_single:
                if len(roles.get(role, [])) != 1:
                    fail(f"{identifier}: requires exactly one {role} compartment")
            if len(roles.get("class-separator", [])) != expected_separators:
                fail(f"{identifier}: requires exactly {expected_separators} native separator line(s)")
            extras = sorted(set(roles) - allowed_roles)
            if extras:
                fail(f"{identifier}: unsupported class compartments {extras}")

            headers = roles.get("class-header", [])
            if len(headers) == 1:
                header_id, header, header_cell = headers[0]
                label = flattened(header.get("label"))
                if symbol not in label:
                    fail(f"{header_id}: class symbol must be visible in the header")
                stereotype = CLASS_KINDS.get(kind, "")
                if stereotype and stereotype not in label:
                    fail(f"{header_id}: class header must show {stereotype}")
                change_label = CHANGE_LABELS.get(change)
                if change_label and change_label not in label:
                    fail(f"{header_id}: class header must show {change_label}")
                if change == "unchanged" and "[未变]" in label:
                    fail(f"{header_id}: unchanged class must display its name without [未变]")
                if kind == "abstract" and not has_italic(header_cell.get("style")):
                    fail(f"{header_id}: abstract class header must be italic")

            for separator_id, _, separator_cell in roles.get("class-separator", []):
                if style_dict(separator_cell.get("style")).get("shape") != "line":
                    fail(f"{separator_id}: class separator must be a native line")

        for identifier, (item, cell) in items.items():
            if item.get("role") != "relationship":
                continue
            for endpoint in ("source", "target"):
                if cell.get(endpoint) not in class_ids:
                    fail(f"{identifier}: {endpoint} must refer to a class node")

        if changed_classes == 0:
            fail("Diagram must contain at least one added, modified, or removed class")

        if len(legends) != 1:
            fail("Diagram must contain exactly one editable role=legend object")
        else:
            validate_graphic_legend(items, legends[0].get("id", ""), fail)

    return errors


def validate_bytes(data: bytes) -> list[str]:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return [f"file is not valid UTF-8: {exc}"]
    try:
        # ElementTree rejects a str that carries an encoding declaration, so the
        # raw bytes go straight in.
        document = ET.fromstring(data)
    except ET.ParseError as exc:
        return [str(exc)]
    return validate_document(document)


def validate(path: Path) -> list[str]:
    try:
        return validate_bytes(path.read_bytes())
    except OSError as exc:
        return [str(exc)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    errors = validate(args.path)
    for error in errors:
        print(f"ERROR: {error}")
    if not errors:
        print(
            "PASS: XML metadata, UML compartments, Git states, relationships, graphical legend, "
            "and measured text fit satisfy the contract."
        )
        print(
            "Text fit is estimated from a one-sided advance-width model, so it never reports a "
            "passing box that actually overflows; the residual risk is font substitution."
        )
        print("Source truth, review judgment, and rendered layout still require separate review.")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())

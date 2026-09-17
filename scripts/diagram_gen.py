#!/usr/bin/env python3
"""Generate a reviewable draw.io class-diagram baseline from a Git change set.

The scanner is deliberately conservative. It extracts class-like declarations,
Python members, and explicit inheritance/implementation clauses. Review and
complete the generated diagram against the source code before delivery.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter
import copy
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Sequence

import facts_io
from layout_drawio import LayoutIssue, optimize as optimize_layout, optimize_page
from style_drawio import (
    CHANGE_LEGEND_LABELS,
    CHANGE_STYLES,
    CLASS_KIND_LEGEND_LABELS,
    CLASS_KINDS,
    RELATION_LEGEND_LABELS,
    RELATION_STYLES,
    TEXT_FONT,
    apply as apply_styles,
)
from text_layout import (
    BODY_FONT_SIZE,
    BODY_SPACING,
    HEADER_FONT_SIZE,
    HEADER_MIN_HEIGHT,
    SECTION_MIN_HEIGHT,
    ClassBox,
    SectionSpec,
    page_class_width,
    plan_class_box,
)


SUPPORTED_EXTENSIONS = {
    ".py",
    ".java",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".vue",
    ".go",
    ".rb",
    ".php",
}

CHANGE_LABELS = {
    "added": "新增",
    "modified": "修改",
    "removed": "删除",
    "unchanged": "",
}

# One order for both the inventory and the page, so a re-scan of unchanged
# source produces the same bytes and the same node numbers.
CHANGE_ORDER = {"added": 0, "modified": 1, "removed": 2, "unchanged": 3}

IGNORED_BASES = {
    "ABC",
    "Enum",
    "Generic",
    "object",
    "Protocol",
    "TypedDict",
}

# The initial page grid only supplies deterministic input coordinates. The
# layout pass later packs the header, routed content, and legend from their
# actual bounds and derives the final page size.
CLASS_ORIGIN_X = 40.0
CLASS_ORIGIN_Y = 175.0
CLASS_COLUMN_GAP = 40.0
CLASS_ROW_GAP = 50.0
CLASSES_PER_ROW = 4
LEGEND_X = 40.0
LEGEND_WIDTH = 1280.0
LEGEND_HEIGHT = 315.0
PAGE_MARGIN = 80.0

# Height of a class box whose compartments are empty, i.e. the minimum every
# classifier has. Also what the grid assumes when a page has no classes at all.
EMPTY_CLASS_HEIGHT = (
    HEADER_MIN_HEIGHT + SECTION_MIN_HEIGHT["class-attributes"] + SECTION_MIN_HEIGHT["class-operations"]
)


@dataclass
class ChangedFile:
    path: str
    change: str


@dataclass
class ClassInfo:
    name: str
    kind: str
    source: str
    change: str
    fields: list[str] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)
    relations: list[tuple[str, str]] = field(default_factory=list)
    # Review authors prose evidence ("stores", "injects"); the scanner only ever
    # synthesizes a "declares" string. Keyed by the same (relation, target) pair
    # that names the relation, so an empty map reproduces the synthesized form.
    relation_evidence: dict[tuple[str, str], str] = field(default_factory=dict)
    # Reviewed Chinese business topics drive semantic pagination and visible
    # page titles. The scanner leaves this empty rather than guessing meaning
    # from an English class name.
    relation_topics: dict[tuple[str, str], tuple[str, str]] = field(default_factory=dict)
    # A relation already resolved by the inventory. Pinning the exact class is
    # how an id that disambiguates two identically-named classes survives the
    # trip back into the diagram, which a name lookup alone cannot express.
    relation_targets: dict[tuple[str, str], "ClassInfo"] = field(default_factory=dict)


def run_git(repo: Path, arguments: Sequence[str], check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "unknown Git error"
        raise RuntimeError(message)
    return result.stdout


def parse_name_status(text: str, extensions: set[str]) -> list[ChangedFile]:
    changes: dict[str, str] = {}
    priority = {"modified": 1, "added": 2, "removed": 2}

    for raw_line in text.splitlines():
        parts = raw_line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0]
        code = status[0]
        path = parts[-1].replace("\\", "/")
        if Path(path).suffix.lower() not in extensions:
            continue
        change = {
            "A": "added",
            "C": "added",
            "D": "removed",
            "M": "modified",
            "R": "modified",
            "T": "modified",
        }.get(code)
        if change is None:
            continue
        previous = changes.get(path)
        if previous is None or priority[change] > priority[previous]:
            changes[path] = change

    return [ChangedFile(path, change) for path, change in sorted(changes.items())]


def normalize_repo_path(repo: Path, value: str) -> str:
    candidate = (repo / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
    try:
        relative = candidate.relative_to(repo)
    except ValueError as exc:
        raise ValueError(f"File is outside the repository: {value}") from exc
    return relative.as_posix()


def collect_changes(args: argparse.Namespace, extensions: set[str]) -> tuple[list[ChangedFile], str]:
    repo = args.repo
    if args.files:
        changes = []
        for value in args.files:
            path = normalize_repo_path(repo, value)
            if Path(path).suffix.lower() in extensions:
                changes.append(ChangedFile(path, args.change))
        return changes, "explicit files"

    if args.base:
        if args.head == "WORKTREE":
            output = run_git(repo, ["diff", "--name-status", "--find-renames", args.base, "--"])
            changes = parse_name_status(output, extensions)
            untracked = run_git(repo, ["ls-files", "--others", "--exclude-standard"])
            known = {item.path for item in changes}
            for raw_path in untracked.splitlines():
                path = raw_path.replace("\\", "/")
                if Path(path).suffix.lower() in extensions and path not in known:
                    changes.append(ChangedFile(path, "added"))
            return sorted(changes, key=lambda item: item.path), f"{args.base}..WORKTREE"

        output = run_git(
            repo,
            ["diff", "--name-status", "--find-renames", args.base, args.head, "--"],
        )
        return parse_name_status(output, extensions), f"{args.base}..{args.head}"

    output = run_git(
        repo,
        [
            "log",
            f"--since={args.days} days ago",
            "--name-status",
            "--format=",
            "--find-renames",
            "--diff-filter=ACMRDT",
            "HEAD",
            "--",
        ],
    )
    return parse_name_status(output, extensions), f"commits from the last {args.days} day(s)"


def source_at_revision(
    repo: Path,
    changed: ChangedFile,
    base: str | None,
    head: str,
    explicit_files: bool,
) -> str | None:
    if changed.change == "removed":
        if not base:
            print(f"WARN: skipped removed file without a base revision: {changed.path}", file=sys.stderr)
            return None
        try:
            return run_git(repo, ["show", f"{base}:{changed.path}"])
        except RuntimeError as exc:
            print(f"WARN: cannot read removed file {changed.path}: {exc}", file=sys.stderr)
            return None

    if explicit_files or head == "WORKTREE":
        path = repo / changed.path
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            print(f"WARN: cannot read {changed.path}: {exc}", file=sys.stderr)
            return None

    try:
        return run_git(repo, ["show", f"{head}:{changed.path}"])
    except RuntimeError as exc:
        print(f"WARN: cannot read {changed.path} at {head}: {exc}", file=sys.stderr)
        return None


def unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def visibility(name: str) -> str:
    if name.startswith("__") and name.endswith("__"):
        return "+"
    if name.startswith("__"):
        return "-"
    if name.startswith("_"):
        return "#"
    return "+"


def annotation_text(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except (AttributeError, ValueError):
        return ""


def python_method(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    positional = [*node.args.posonlyargs, *node.args.args]
    names = [argument.arg for argument in positional if argument.arg not in {"self", "cls"}]
    if node.args.vararg:
        names.append(f"*{node.args.vararg.arg}")
    names.extend(argument.arg for argument in node.args.kwonlyargs)
    if node.args.kwarg:
        names.append(f"**{node.args.kwarg.arg}")
    returns = annotation_text(node.returns)
    suffix = f": {returns}" if returns else ""
    return f"{visibility(node.name)} {node.name}({', '.join(names)}){suffix}"


def extract_python(text: str, source: str, change: str) -> list[ClassInfo]:
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        print(f"WARN: Python parse failed for {source}: {exc}", file=sys.stderr)
        return []

    classes: list[ClassInfo] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            name = ".".join([*self.scope, node.name])
            fields: list[str] = []
            methods: list[str] = []
            relations: list[tuple[str, str]] = []
            kind = "class"

            for base in node.bases:
                base_name = annotation_text(base)
                if base_name:
                    relations.append(("inheritance", base_name))
                if base_name.rsplit(".", 1)[-1] in {"ABC", "Protocol"}:
                    kind = "abstract" if base_name.endswith("ABC") else "interface"

            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.append(python_method(child))
                    if child.name == "__init__":
                        for descendant in ast.walk(child):
                            if isinstance(descendant, (ast.Assign, ast.AnnAssign)):
                                targets = descendant.targets if isinstance(descendant, ast.Assign) else [descendant.target]
                                for target in targets:
                                    if (
                                        isinstance(target, ast.Attribute)
                                        and isinstance(target.value, ast.Name)
                                        and target.value.id == "self"
                                    ):
                                        annotation = annotation_text(getattr(descendant, "annotation", None))
                                        suffix = f": {annotation}" if annotation else ""
                                        fields.append(f"{visibility(target.attr)} {target.attr}{suffix}")
                elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                    annotation = annotation_text(child.annotation)
                    fields.append(f"{visibility(child.target.id)} {child.target.id}: {annotation}")
                elif isinstance(child, ast.Assign):
                    for target in child.targets:
                        if isinstance(target, ast.Name):
                            fields.append(f"{visibility(target.id)} {target.id}")

            classes.append(
                ClassInfo(
                    name=name,
                    kind=kind,
                    source=source,
                    change=change,
                    fields=unique(fields),
                    methods=unique(methods),
                    relations=unique(relations),
                )
            )
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

    Visitor().visit(tree)
    return classes


BRACED_CLASS_RE = re.compile(
    r"(?m)^\s*(?:export\s+)?(?:default\s+)?(?:public\s+|protected\s+|private\s+|abstract\s+|final\s+)*"
    r"(class|interface|enum|record)\s+([A-Za-z_$][\w$]*)"
    r"(?:\s*<[^\n{]+>)?"
    r"(?:\s+extends\s+([^\n{]+?))?(?=\s+implements|\s*\{)"
    r"(?:\s+implements\s+([^\n{]+?))?\s*\{"
)


def split_types(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def extract_braced_classes(text: str, source: str, change: str) -> list[ClassInfo]:
    classes: list[ClassInfo] = []
    for match in BRACED_CLASS_RE.finditer(text):
        raw_kind, name, extends, implements = match.groups()
        kind = {"record": "class"}.get(raw_kind, raw_kind)
        relations = [("inheritance", target) for target in split_types(extends)]
        relations.extend(("implementation", target) for target in split_types(implements))
        classes.append(
            ClassInfo(
                name=name,
                kind=kind,
                source=source,
                change=change,
                relations=relations,
            )
        )
    return classes


RUBY_CLASS_RE = re.compile(r"(?m)^\s*class\s+([A-Z][\w:]*)\s*(?:<\s*([A-Z][\w:]*))?")
GO_STRUCT_RE = re.compile(r"(?m)^\s*type\s+([A-Za-z_]\w*)\s+struct\b")
GO_METHOD_RE = re.compile(r"(?m)^\s*func\s*\(\s*\w+\s+\*?([A-Za-z_]\w*)\s*\)\s*([A-Za-z_]\w*)\s*\(")


def extract_ruby(text: str, source: str, change: str) -> list[ClassInfo]:
    result = []
    for match in RUBY_CLASS_RE.finditer(text):
        name, base = match.groups()
        relations = [("inheritance", base)] if base else []
        result.append(ClassInfo(name, "class", source, change, relations=relations))
    return result


def extract_go(text: str, source: str, change: str) -> list[ClassInfo]:
    methods: dict[str, list[str]] = {}
    for owner, method in GO_METHOD_RE.findall(text):
        methods.setdefault(owner, []).append(f"+ {method}()")
    return [
        ClassInfo(name, "struct", source, change, methods=unique(methods.get(name, [])))
        for name in GO_STRUCT_RE.findall(text)
    ]


def extract_classes(text: str, source: str, change: str) -> list[ClassInfo]:
    extension = Path(source).suffix.lower()
    if extension == ".py":
        return extract_python(text, source, change)
    if extension == ".rb":
        return extract_ruby(text, source, change)
    if extension == ".go":
        return extract_go(text, source, change)
    return extract_braced_classes(text, source, change)


def qualified_type_name(value: str) -> str:
    """The declared type with generics and trailing prose removed, dots kept."""
    value = re.sub(r"<.*>|\[.*\]", "", value or "").strip()
    return value.split()[0] if value else ""


def short_type_name(value: str) -> str:
    return re.split(r"[.\\:]", qualified_type_name(value))[-1]


def resolve_relation(
    raw_target: str,
    source_id: str,
    index: dict[str, list[str]],
) -> tuple[str | None, str | None]:
    """Find the class node a declared relation points at.

    Returns (target_id, reason). A resolved relation is (id, None); a target
    that is simply not on this page is (None, None), which is the documented
    default - most declared bases are framework or standard-library types that
    the contract excludes. (None, "why") is reserved for the one case worth
    reporting: a name claimed by several classes on the page. Guessing between
    them would draw the edge to the wrong class while its own evidence string
    still named the right one, and a reviewer reading the evidence cannot tell.
    """
    for key in dict.fromkeys((qualified_type_name(raw_target), short_type_name(raw_target))):
        if not key:
            continue
        remaining = [value for value in index.get(key, []) if value != source_id]
        if len(remaining) == 1:
            return remaining[0], None
        if len(remaining) > 1:
            return None, f"{key} is claimed by {len(remaining)} classes on this page"
    return None, None


def class_header_label(info: ClassInfo) -> str:
    status = CHANGE_LABELS[info.change]
    header = f"[{status}] {info.name}" if status else info.name
    stereotype = CLASS_KINDS.get(info.kind)
    if stereotype:
        header = f"{stereotype}\n{header}"
    return header


def class_sections(info: ClassInfo) -> tuple[SectionSpec, ...]:
    """The compartments of one classifier, in draw.io z-order.

    Compartment order and the empty labels are authored text preserved from the
    hand-written contract example; how many members actually fit is decided later
    by text_layout, against the measured width and the box line budget.
    """
    if info.kind == "interface":
        return (
            SectionSpec("operations", "class-operations", tuple(info.methods), "（本类未声明显式操作）"),
        )
    if info.kind == "enum":
        return (
            SectionSpec("literals", "class-literals", tuple(info.fields), "（本枚举未声明显式常量）"),
            SectionSpec("operations", "class-operations", tuple(info.methods), "（本类未声明显式操作）"),
        )
    return (
        SectionSpec("attributes", "class-attributes", tuple(info.fields), "（本类未声明显式属性）"),
        SectionSpec("operations", "class-operations", tuple(info.methods), "（本类未声明显式操作）"),
    )


def _dim(value: float) -> str:
    """Render a coordinate. Integral values stay integral so geometry stays diff-friendly."""
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:g}"


def add_vertex(
    root: ET.Element,
    identifier: str,
    label: str,
    role: str,
    style: str,
    x: float,
    y: float,
    width: float,
    height: float,
    metadata: dict[str, str] | None = None,
    parent: str = "1",
) -> None:
    attributes = {"id": identifier, "label": label, "role": role}
    if metadata:
        attributes.update(metadata)
    wrapper = ET.SubElement(root, "object", attributes)
    cell = ET.SubElement(wrapper, "mxCell", {"vertex": "1", "parent": parent, "style": style})
    ET.SubElement(
        cell,
        "mxGeometry",
        {
            "as": "geometry",
            "x": _dim(x),
            "y": _dim(y),
            "width": _dim(width),
            "height": _dim(height),
        },
    )


def add_class_node(
    root: ET.Element,
    identifier: str,
    info: ClassInfo,
    x: float,
    y: float,
    box: ClassBox,
) -> None:
    """Emit one classifier container with the geometry text_layout planned for it."""
    add_vertex(
        root,
        identifier,
        "",
        "class",
        "shape=rectangle;rounded=0;whiteSpace=wrap;html=0;strokeWidth=2;",
        x,
        y,
        box.width,
        box.height,
        {
            "kind": info.kind,
            "change": info.change,
            "symbol": info.name,
            "source": info.source,
        },
    )
    add_vertex(
        root,
        f"{identifier}--header",
        box.header,
        "class-header",
        "shape=rectangle;rounded=0;whiteSpace=wrap;html=0;fillColor=none;strokeColor=none;",
        0,
        0,
        box.width,
        box.header_height,
        {"for": identifier},
        parent=identifier,
    )
    for section in box.sections:
        add_vertex(
            root,
            f"{identifier}--{section.suffix}",
            section.label,
            section.role,
            "shape=rectangle;rounded=0;whiteSpace=wrap;html=0;fillColor=none;strokeColor=none;",
            0,
            section.y,
            box.width,
            section.height,
            {"for": identifier},
            parent=identifier,
        )
    for index, separator_y in enumerate(box.separators, start=1):
        add_vertex(
            root,
            f"{identifier}--separator-{index}",
            "",
            "class-separator",
            "shape=line;direction=east;strokeWidth=1;connectable=0;",
            0,
            separator_y,
            box.width,
            1,
            {"for": identifier},
            parent=identifier,
        )


def add_relationship(
    root: ET.Element,
    identifier: str,
    source: str,
    target: str,
    relation: str,
    evidence: str,
    topic: str = "",
    topic_label: str = "",
) -> None:
    metadata = {
        "id": identifier,
        "label": RELATION_LEGEND_LABELS[relation],
        "role": "relationship",
        "relation": relation,
        "evidence": evidence,
    }
    if topic:
        metadata["topic"] = topic
        metadata["topic_label"] = topic_label
    wrapper = ET.SubElement(
        root,
        "object",
        metadata,
    )
    cell = ET.SubElement(
        wrapper,
        "mxCell",
        {
            "edge": "1",
            "parent": "1",
            "source": source,
            "target": target,
            "style": "edgeStyle=orthogonalEdgeStyle;rounded=0;html=0;strokeWidth=2;",
        },
    )
    ET.SubElement(cell, "mxGeometry", {"as": "geometry", "relative": "1"})


def style_string(base: dict[str, str], additions: dict[str, str] | None = None) -> str:
    values = {**base, **(additions or {})}
    return ";".join(f"{key}={value}" for key, value in values.items()) + ";"


def add_legend_edge(
    root: ET.Element,
    identifier: str,
    relation: str,
    x: int,
    y: int,
    width: int = 145,
) -> None:
    wrapper = ET.SubElement(
        root,
        "object",
        {
            "id": identifier,
            "label": RELATION_LEGEND_LABELS[relation],
            "role": "legend-edge",
            "legend_group": "relationship",
            "legend_key": relation,
        },
    )
    cell = ET.SubElement(
        wrapper,
        "mxCell",
        {
            "edge": "1",
            "parent": "legend",
            "style": style_string(
                {
                    "edgeStyle": "orthogonalEdgeStyle",
                    "rounded": "0",
                    "html": "0",
                    "strokeWidth": "2",
                    "fontFamily": TEXT_FONT,
                    "fontSize": "14",
                    "labelBackgroundColor": "#FFFFFF",
                },
                RELATION_STYLES[relation],
            ),
        },
    )
    geometry = ET.SubElement(cell, "mxGeometry", {"as": "geometry", "relative": "1"})
    ET.SubElement(geometry, "mxPoint", {"as": "sourcePoint", "x": str(x), "y": str(y)})
    ET.SubElement(geometry, "mxPoint", {"as": "targetPoint", "x": str(x + width), "y": str(y)})


def add_graphic_legend(root: ET.Element, y: float) -> None:
    add_vertex(
        root,
        "legend",
        "",
        "legend",
        "rounded=1;whiteSpace=wrap;html=0;fillColor=#F8FAFC;strokeColor=#CBD5E1;",
        LEGEND_X,
        y,
        LEGEND_WIDTH,
        LEGEND_HEIGHT,
    )
    add_vertex(
        root,
        "legend-title",
        "类图图例｜所有图形均为原生可编辑对象",
        "legend-text",
        f"text;html=0;align=left;verticalAlign=middle;fontFamily={TEXT_FONT};fontSize=18;fontStyle=1;fontColor=#172554;",
        20,
        12,
        1235,
        30,
        {"legend_group": "heading", "legend_key": "title"},
        parent="legend",
    )
    add_vertex(
        root,
        "legend-uml-heading",
        "UML 类型",
        "legend-text",
        f"text;html=0;align=left;verticalAlign=middle;fontFamily={TEXT_FONT};fontSize=15;fontStyle=1;fontColor=#334155;",
        20,
        48,
        160,
        24,
        {"legend_group": "heading", "legend_key": "uml-type"},
        parent="legend",
    )
    for index, (kind, chinese) in enumerate(CLASS_KIND_LEGEND_LABELS.items()):
        stereotype = CLASS_KINDS[kind]
        item_id = f"legend-uml-{kind}"
        add_vertex(
            root,
            item_id,
            "",
            "legend-item",
            "shape=rectangle;rounded=0;whiteSpace=wrap;html=0;container=1;collapsible=0;fillColor=#FFFFFF;strokeColor=#64748B;strokeWidth=2;",
            20 + index * 154,
            78,
            140,
            66,
            {"legend_group": "uml-type", "legend_key": kind},
            parent="legend",
        )
        header = f"{stereotype}\n{chinese}" if stereotype else chinese
        add_vertex(
            root,
            f"{item_id}--header",
            header,
            "legend-item-section",
            f"shape=rectangle;rounded=0;whiteSpace=wrap;html=0;fillColor=none;strokeColor=none;fontFamily={TEXT_FONT};fontSize=12;"
            f"fontStyle={'3' if kind == 'abstract' else '1'};align=center;verticalAlign=middle;",
            0,
            0,
            140,
            34,
            {"legend_group": "uml-type", "legend_key": kind, "legend_part": "header"},
            parent=item_id,
        )
        body_label = "常量 / 操作" if kind == "enum" else ("操作" if kind == "interface" else "属性 / 操作")
        add_vertex(
            root,
            f"{item_id}--body",
            body_label,
            "legend-item-section",
            f"shape=rectangle;rounded=0;whiteSpace=wrap;html=0;fillColor=none;strokeColor=none;fontFamily={TEXT_FONT};fontSize=11;align=center;verticalAlign=middle;",
            0,
            34,
            140,
            32,
            {"legend_group": "uml-type", "legend_key": kind, "legend_part": "body"},
            parent=item_id,
        )
        add_vertex(
            root,
            f"{item_id}--separator",
            "",
            "legend-item-section",
            "shape=line;direction=east;strokeColor=#64748B;strokeWidth=1;connectable=0;",
            0,
            33,
            140,
            1,
            {"legend_group": "uml-type", "legend_key": kind, "legend_part": "separator"},
            parent=item_id,
        )

    add_vertex(
        root,
        "legend-change-heading",
        "Git 状态",
        "legend-text",
        f"text;html=0;align=left;verticalAlign=middle;fontFamily={TEXT_FONT};fontSize=15;fontStyle=1;fontColor=#334155;",
        810,
        48,
        160,
        24,
        {"legend_group": "heading", "legend_key": "change"},
        parent="legend",
    )
    for index, (change, chinese) in enumerate(CHANGE_LEGEND_LABELS.items()):
        add_vertex(
            root,
            f"legend-change-{change}",
            chinese,
            "legend-item",
            style_string(
                {
                    "shape": "rectangle",
                    "rounded": "0",
                    "whiteSpace": "wrap",
                    "html": "0",
                    "align": "center",
                    "verticalAlign": "middle",
                    "fontFamily": TEXT_FONT,
                    "fontSize": "14",
                    "strokeWidth": "2",
                },
                CHANGE_STYLES[change],
            ),
            810 + index * 108,
            86,
            96,
            42,
            {"legend_group": "change", "legend_key": change},
            parent="legend",
        )

    add_vertex(
        root,
        "legend-relation-heading",
        "类间关系",
        "legend-text",
        f"text;html=0;align=left;verticalAlign=middle;fontFamily={TEXT_FONT};fontSize=15;fontStyle=1;fontColor=#334155;",
        20,
        155,
        160,
        24,
        {"legend_group": "heading", "legend_key": "relationship"},
        parent="legend",
    )
    for index, relation in enumerate(RELATION_LEGEND_LABELS):
        column = index % 3
        row = index // 3
        add_legend_edge(root, f"legend-relation-{relation}", relation, 30 + column * 245, 202 + row * 68)



def _class_order(info: ClassInfo) -> tuple[int, str]:
    return (CHANGE_ORDER[info.change], info.name)


def _page_cell(item: ET.Element) -> ET.Element | None:
    return item if item.tag == "mxCell" else item.find("mxCell")


def _page_view(
    source: ET.Element,
    relationship_ids: set[str],
    *,
    keep_all_classes: bool,
    page_number: int,
    page_name: str,
) -> ET.Element:
    """Clone one generated page into an overview or relationship detail view."""
    page = copy.deepcopy(source)
    page.set("id", f"incremental-class-diagram-{page_number}")
    page.set("name", page_name)
    root = page.find("mxGraphModel/root")
    if root is None:
        raise ValueError("Generated page has no mxGraphModel/root")

    relationships = {
        item.get("id", ""): item
        for item in root
        if item.get("role") == "relationship"
    }
    needed_classes: set[str] = set()
    for identifier in relationship_ids:
        item = relationships.get(identifier)
        cell = _page_cell(item) if item is not None else None
        if cell is not None:
            needed_classes.update((cell.get("source", ""), cell.get("target", "")))

    removed_classes = {
        item.get("id", "")
        for item in root
        if item.get("role") == "class"
        and not keep_all_classes
        and item.get("id", "") not in needed_classes
    }
    for item in list(root):
        role = item.get("role")
        identifier = item.get("id", "")
        cell = _page_cell(item)
        parent = cell.get("parent", "") if cell is not None else ""
        if role == "relationship" and identifier not in relationship_ids:
            root.remove(item)
        elif identifier in removed_classes or item.get("for", "") in removed_classes or parent in removed_classes:
            root.remove(item)

    # Routing consumes relationships in XML order. Reorder the surviving edges
    # by semantic content so a harmless inventory reorder cannot change which
    # edge receives the first lane and, through that, the chosen pagination.
    page_relationships = [item for item in root if item.get("role") == "relationship"]
    if page_relationships:
        first_index = min(list(root).index(item) for item in page_relationships)
        for item in page_relationships:
            root.remove(item)

        def relationship_key(item: ET.Element) -> tuple[str, ...]:
            cell = _page_cell(item)
            return (
                item.get("topic", "~"),
                cell.get("source", "") if cell is not None else "",
                cell.get("target", "") if cell is not None else "",
                item.get("relation", ""),
                item.get("evidence", ""),
                item.get("id", ""),
            )

        for offset, item in enumerate(sorted(page_relationships, key=relationship_key)):
            root.insert(first_index + offset, item)

    title = next((item for item in root if item.get("id") == "title"), None)
    if title is not None:
        title.set("label", f"增量类图｜{page_name}")
    scope = next((item for item in root if item.get("id") == "scope"), None)
    if scope is not None:
        scope.set(
            "label",
            f"{scope.get('label', '')}\n页面：{page_name}；类型总览不画关系，关系详图可重复展示端点类型。",
        )
        scope_cell = _page_cell(scope)
        scope_geometry = scope_cell.find("mxGeometry") if scope_cell is not None else None
        if scope_geometry is not None:
            scope_geometry.set("height", _dim(max(float(scope_geometry.get("height", "0")), 80.0)))
    return page


def _route_issues(issues: list[LayoutIssue]) -> list[LayoutIssue]:
    return [
        issue
        for issue in issues
        if getattr(issue, "kind", "") in {"edge-node", "edge-crossing", "edge-overlap"}
    ]


def _relation_sort_key(
    identifier: str,
    relationships: dict[str, ET.Element],
    classes: dict[str, ET.Element],
) -> tuple[str, ...]:
    item = relationships[identifier]
    cell = _page_cell(item)
    source = cell.get("source", "") if cell is not None else ""
    target = cell.get("target", "") if cell is not None else ""
    return (
        item.get("topic", "~"),
        classes.get(source, ET.Element("missing")).get("symbol", source),
        classes.get(target, ET.Element("missing")).get("symbol", target),
        item.get("relation", ""),
        item.get("evidence", ""),
        identifier,
    )


def _relation_affinity(
    first_id: str,
    second_id: str,
    relationships: dict[str, ET.Element],
    classes: dict[str, ET.Element],
) -> int:
    """Rank evidence-backed reasons for keeping two relations on one page."""
    first = relationships[first_id]
    second = relationships[second_id]
    first_cell = _page_cell(first)
    second_cell = _page_cell(second)
    if first_cell is None or second_cell is None:
        return 0
    score = 0
    first_topic = first.get("topic", "")
    if first_topic and first_topic == second.get("topic", ""):
        score += 1000
    first_nodes = {first_cell.get("source", ""), first_cell.get("target", "")}
    second_nodes = {second_cell.get("source", ""), second_cell.get("target", "")}
    score += 200 * len((first_nodes & second_nodes) - {""})
    first_sources = {
        classes[node].get("source", "")
        for node in first_nodes
        if node in classes
    }
    second_sources = {
        classes[node].get("source", "")
        for node in second_nodes
        if node in classes
    }
    if (first_sources & second_sources) - {""}:
        score += 40
    first_dirs = {str(Path(value).parent).replace("\\", "/") for value in first_sources if value}
    second_dirs = {str(Path(value).parent).replace("\\", "/") for value in second_sources if value}
    if first_dirs & second_dirs:
        score += 20
    if first.get("relation") == second.get("relation"):
        score += 5
    return score


def _initial_relation_units(
    relation_ids: list[str],
    relationships: dict[str, ET.Element],
    classes: dict[str, ET.Element],
) -> list[frozenset[str]]:
    """Keep reviewed topics intact; cluster unreviewed edges by structural affinity."""
    topic_groups: dict[str, set[str]] = {}
    unreviewed: set[str] = set()
    for identifier in relation_ids:
        topic = relationships[identifier].get("topic", "")
        if topic:
            topic_groups.setdefault(topic, set()).add(identifier)
        else:
            unreviewed.add(identifier)

    units = [frozenset(group) for _, group in sorted(topic_groups.items())]
    while unreviewed:
        seed = min(unreviewed, key=lambda value: _relation_sort_key(value, relationships, classes))
        unreviewed.remove(seed)
        component = {seed}
        changed = True
        while changed:
            changed = False
            for candidate in sorted(
                unreviewed,
                key=lambda value: _relation_sort_key(value, relationships, classes),
            ):
                if any(
                    _relation_affinity(candidate, member, relationships, classes) >= 20
                    for member in component
                ):
                    component.add(candidate)
                    unreviewed.remove(candidate)
                    changed = True
        units.append(frozenset(component))
    return sorted(
        units,
        key=lambda group: min(
            _relation_sort_key(identifier, relationships, classes)
            for identifier in group
        ),
    )


def _semantic_bisect(
    relation_ids: frozenset[str],
    relationships: dict[str, ET.Element],
    classes: dict[str, ET.Element],
) -> tuple[frozenset[str], frozenset[str]]:
    """Cut a conflicted topic at its weakest structural seam, not its id midpoint."""
    ordered = sorted(
        relation_ids,
        key=lambda value: _relation_sort_key(value, relationships, classes),
    )
    if len(ordered) == 2:
        return frozenset([ordered[0]]), frozenset([ordered[1]])
    seed_pairs = [
        (
            _relation_affinity(first, second, relationships, classes),
            _relation_sort_key(first, relationships, classes),
            _relation_sort_key(second, relationships, classes),
            first,
            second,
        )
        for index, first in enumerate(ordered)
        for second in ordered[index + 1:]
    ]
    _, _, _, left_seed, right_seed = min(seed_pairs)
    left = {left_seed}
    right = {right_seed}
    remaining = [value for value in ordered if value not in {left_seed, right_seed}]
    remaining.sort(
        key=lambda value: (
            -max(
                _relation_affinity(value, left_seed, relationships, classes),
                _relation_affinity(value, right_seed, relationships, classes),
            ),
            _relation_sort_key(value, relationships, classes),
        )
    )
    for identifier in remaining:
        left_score = sum(
            _relation_affinity(identifier, member, relationships, classes)
            for member in left
        ) / len(left)
        right_score = sum(
            _relation_affinity(identifier, member, relationships, classes)
            for member in right
        ) / len(right)
        if left_score > right_score or (left_score == right_score and len(left) <= len(right)):
            left.add(identifier)
        else:
            right.add(identifier)
    return frozenset(left), frozenset(right)


def _group_affinity(
    first: frozenset[str],
    second: frozenset[str],
    relationships: dict[str, ET.Element],
    classes: dict[str, ET.Element],
) -> int:
    return sum(
        _relation_affinity(left, right, relationships, classes)
        for left in first
        for right in second
    )


def _page_topic_name(
    relation_ids: frozenset[str],
    relationships: dict[str, ET.Element],
) -> str:
    topic_counts = Counter(
        (relationships[identifier].get("topic", ""), relationships[identifier].get("topic_label", ""))
        for identifier in relation_ids
        if relationships[identifier].get("topic", "")
    )
    if topic_counts:
        topics = sorted(topic_counts, key=lambda value: (-topic_counts[value], value[0], value[1]))
        label = "、".join(value[1] for value in topics[:2])
        if len(topics) > 2:
            label += "等"
        return f"关系详图｜{label}"

    kind_counts = Counter(relationships[identifier].get("relation", "") for identifier in relation_ids)
    kinds = sorted(kind_counts, key=lambda value: (-kind_counts[value], value))
    if len(kinds) <= 2:
        label = "、".join(RELATION_LEGEND_LABELS.get(value, "类间") for value in kinds)
        return f"关系详图｜{label}关系"
    return "关系详图｜类间关系"


def _rename_relation_page(
    page: ET.Element,
    page_number: int,
    page_name: str,
    relation_ids: frozenset[str],
    relationships: dict[str, ET.Element],
    classes: dict[str, ET.Element],
) -> None:
    page.set("id", f"incremental-class-diagram-{page_number}")
    page.set("name", page_name)
    topics = sorted({relationships[value].get("topic", "") for value in relation_ids} - {""})
    if topics:
        page.set("relation_topics", ",".join(topics))
    degrees: Counter[str] = Counter()
    for identifier in relation_ids:
        cell = _page_cell(relationships[identifier])
        if cell is not None:
            degrees.update((cell.get("source", ""), cell.get("target", "")))
    anchors = sorted(
        (identifier for identifier in degrees if identifier in classes),
        key=lambda value: (-degrees[value], classes[value].get("symbol", value)),
    )[:2]
    if anchors:
        page.set("anchor_types", ",".join(classes[value].get("symbol", value) for value in anchors))

    root = page.find("mxGraphModel/root")
    if root is None:
        return
    title = next((item for item in root if item.get("id") == "title"), None)
    if title is not None:
        title.set("label", f"增量类图｜{page_name}")
    scope = next((item for item in root if item.get("id") == "scope"), None)
    if scope is not None:
        prefix = scope.get("label", "").rsplit("\n页面：", 1)[0]
        scope.set(
            "label",
            f"{prefix}\n页面：{page_name}；类型总览不画关系，关系详图可重复展示端点类型。",
        )


def split_conflicting_page(mxfile: ET.Element) -> tuple[ET.Element, list[LayoutIssue]]:
    """Replace a conflicted page with an overview and semantic, compact detail pages."""
    source = mxfile.find("diagram")
    if source is None:
        return mxfile, []
    root = source.find("mxGraphModel/root")
    if root is None:
        return mxfile, []
    relationships = {
        item.get("id", ""): item
        for item in root
        if item.get("role") == "relationship"
    }
    classes = {
        item.get("id", ""): item
        for item in root
        if item.get("role") == "class"
    }
    relation_ids = sorted(
        relationships,
        key=lambda value: _relation_sort_key(value, relationships, classes),
    )
    if len(relation_ids) < 2:
        return mxfile, optimize_layout(mxfile)

    overview = _page_view(
        source,
        set(),
        keep_all_classes=True,
        page_number=1,
        page_name="类型总览",
    )
    overview_issues = optimize_page(overview)

    cache: dict[frozenset[str], tuple[ET.Element, list[LayoutIssue]]] = {}

    def candidate(group: frozenset[str]) -> tuple[ET.Element, list[LayoutIssue]]:
        cached = cache.get(group)
        if cached is not None:
            return copy.deepcopy(cached[0]), list(cached[1])
        candidate = _page_view(
            source,
            set(group),
            keep_all_classes=False,
            page_number=2,
            page_name="关系详图",
        )
        issues = optimize_page(candidate, max_iterations=2)
        cache[group] = (copy.deepcopy(candidate), list(issues))
        return candidate, issues

    def feasible_units(group: frozenset[str]) -> list[frozenset[str]]:
        _, issues = candidate(group)
        if not _route_issues(issues) or len(group) == 1:
            return [group]
        left, right = _semantic_bisect(group, relationships, classes)
        return [*feasible_units(left), *feasible_units(right)]

    units: list[frozenset[str]] = []
    for group in _initial_relation_units(relation_ids, relationships, classes):
        units.extend(feasible_units(group))

    def normalize(groups: list[frozenset[str]]) -> tuple[frozenset[str], ...]:
        return tuple(sorted(groups, key=lambda group: tuple(sorted(group))))

    def partition_rank(groups: tuple[frozenset[str], ...]) -> tuple[object, ...]:
        cohesion = sum(
            _relation_affinity(first, second, relationships, classes)
            for group in groups
            for index, first in enumerate(sorted(group))
            for second in sorted(group)[index + 1:]
        )
        return (len(groups), -cohesion, tuple(tuple(sorted(group)) for group in groups))

    memo: dict[tuple[frozenset[str], ...], tuple[frozenset[str], ...]] = {}
    state_budget = max(32, min(128, len(units) * len(units) * 4))
    visited = 0

    def merge_search(state: tuple[frozenset[str], ...]) -> tuple[frozenset[str], ...]:
        nonlocal visited
        if state in memo:
            return memo[state]
        visited += 1
        best = state
        if visited > state_budget:
            memo[state] = best
            return best
        pairs = [
            (
                -_group_affinity(state[left], state[right], relationships, classes),
                -len(state[left] | state[right]),
                tuple(sorted(state[left])),
                tuple(sorted(state[right])),
                left,
                right,
            )
            for left in range(len(state))
            for right in range(left + 1, len(state))
        ]
        for _, _, _, _, left, right in sorted(pairs):
            merged = state[left] | state[right]
            _, issues = candidate(merged)
            if _route_issues(issues):
                continue
            next_state = normalize([
                group
                for index, group in enumerate(state)
                if index not in {left, right}
            ] + [merged])
            result = merge_search(next_state)
            if partition_rank(result) < partition_rank(best):
                best = result
            # The all-relations detail page was already attempted before this
            # fallback. Once two clean pages exist, no deeper branch can improve
            # the page count and further geometry trials only add latency.
            if len(best) <= 2:
                break
        memo[state] = best
        return best

    all_relations = frozenset(relation_ids)
    _, all_relation_issues = candidate(all_relations)
    groups = (
        [all_relations]
        if not _route_issues(all_relation_issues)
        else list(merge_search(normalize(units)))
    )
    groups.sort(key=lambda group: (_page_topic_name(group, relationships), tuple(sorted(group))))

    base_names = [_page_topic_name(group, relationships) for group in groups]
    name_counts = Counter(base_names)
    used_names: Counter[str] = Counter()
    accepted: list[ET.Element] = []
    accepted_issues: list[LayoutIssue] = []
    for index, (group, base_name) in enumerate(zip(groups, base_names), start=1):
        page, issues = candidate(group)
        name = base_name
        if name_counts[base_name] > 1:
            kind_counts = Counter(relationships[value].get("relation", "") for value in group)
            dominant = sorted(kind_counts, key=lambda value: (-kind_counts[value], value))[0]
            name = f"{base_name}｜{RELATION_LEGEND_LABELS.get(dominant, '类间')}关系"
        used_names[name] += 1
        if used_names[name] > 1:
            name = f"{name}（第{used_names[name]}组）"
        _rename_relation_page(page, index + 1, name, group, relationships, classes)
        accepted.append(page)
        accepted_issues.extend(issues)

    result = ET.Element("mxfile", dict(mxfile.attrib))
    result.append(overview)
    result.extend(accepted)
    return result, [*overview_issues, *accepted_issues]


def build_drawio(classes: list[ClassInfo], scope_label: str, strict: bool = False) -> ET.Element:
    classes.sort(key=_class_order)

    # Measure and size every box before anything is positioned. The layout pass
    # reads these dimensions, and while relationship ports are stored as
    # fractions that follow a resize, waypoints are absolute and do not.
    measured: list[tuple[str, float, bool]] = []
    for info in classes:
        measured.append((class_header_label(info), HEADER_FONT_SIZE, True))
        for section in class_sections(info):
            measured.extend((member, BODY_FONT_SIZE, False) for member in section.members)
    class_width = page_class_width(measured, pad=2 * BODY_SPACING)
    boxes = [plan_class_box(class_header_label(info), class_sections(info), class_width) for info in classes]

    rows = max(1, (len(classes) + CLASSES_PER_ROW - 1) // CLASSES_PER_ROW)
    columns = min(max(len(classes), 1), CLASSES_PER_ROW)
    row_pitch = max((box.height for box in boxes), default=EMPTY_CLASS_HEIGHT) + CLASS_ROW_GAP
    column_pitch = class_width + CLASS_COLUMN_GAP
    legend_y = CLASS_ORIGIN_Y + rows * row_pitch
    class_right = CLASS_ORIGIN_X + (columns - 1) * column_pitch + class_width

    mxfile = ET.Element("mxfile", {"host": "app.diagrams.net", "agent": "incremental-class-diagram-generator"})
    diagram = ET.SubElement(mxfile, "diagram", {"id": "incremental-class-diagram", "name": "增量类图"})
    model = ET.SubElement(
        diagram,
        "mxGraphModel",
        {
            "grid": "1",
            "gridSize": "10",
            "page": "1",
            "pageScale": "1",
            "pageWidth": _dim(max(LEGEND_X + LEGEND_WIDTH, class_right) + PAGE_MARGIN),
            "pageHeight": _dim(legend_y + LEGEND_HEIGHT + PAGE_MARGIN),
            "math": "0",
            "shadow": "0",
        },
    )
    root = ET.SubElement(model, "root")
    ET.SubElement(root, "mxCell", {"id": "0"})
    ET.SubElement(root, "mxCell", {"id": "1", "parent": "0"})

    add_vertex(
        root,
        "title",
        "增量类图｜自动扫描初稿",
        "decoration",
        f"text;html=0;align=left;verticalAlign=middle;fontFamily={TEXT_FONT};fontSize=26;fontStyle=1;fontColor=#172554;",
        40,
        20,
        1280,
        45,
    )
    add_vertex(
        root,
        "scope",
        f"范围：{scope_label}｜生成日期：{date.today().isoformat()}\n自动扫描仅提取声明、Python 成员和显式继承／实现；不自动补充未变类型或代码问题，其他成员与关系必须结合源码复核。",
        "note",
        f"rounded=1;whiteSpace=wrap;html=0;align=left;verticalAlign=middle;fillColor=#EFF6FF;strokeColor=#93C5FD;fontFamily={TEXT_FONT};fontSize=14;spacing=10;",
        40,
        75,
        1280,
        70,
    )

    # Every class claims both its full name and its short name, and a name may
    # have several claimants: two files each defining a User, or two Configs.
    # Keeping them all is what lets a lookup tell "unique" from "ambiguous".
    index: dict[str, list[str]] = {}
    placed: list[tuple[ClassInfo, str]] = []
    placement: dict[int, str] = {}
    for number, (info, box) in enumerate(zip(classes, boxes), start=1):
        identifier = f"class-{number}"
        placed.append((info, identifier))
        placement[id(info)] = identifier
        for key in dict.fromkeys((info.name, info.name.rsplit(".", 1)[-1])):
            index.setdefault(key, []).append(identifier)
        column = (number - 1) % CLASSES_PER_ROW
        row = (number - 1) // CLASSES_PER_ROW
        add_class_node(
            root,
            identifier,
            info,
            CLASS_ORIGIN_X + column * column_pitch,
            CLASS_ORIGIN_Y + row * row_pitch,
            box,
        )

    edge_index = 1
    for info, source_id in placed:
        for relation, raw_target in info.relations:
            key = (relation, raw_target)
            pinned = info.relation_targets.get(key)
            if pinned is not None:
                # The inventory already decided which class this points at.
                target_id, reason = placement[id(pinned)], None
            else:
                target_id, reason = resolve_relation(raw_target, source_id, index)
            if target_id is None:
                # Ambiguity is reported to stderr, never into the diagram: the
                # reader can resolve a named edge, but the artifact must not
                # carry the scanner's run state. An off-page target is the
                # documented default and stays silent.
                if reason:
                    print(
                        f"WARN: relation not drawn: {info.source}: {info.name} declares "
                        f"{raw_target} ({reason})",
                        file=sys.stderr,
                    )
                continue
            add_relationship(
                root,
                f"relation-{edge_index}",
                source_id,
                target_id,
                relation,
                info.relation_evidence.get(key)
                or f"{info.source}: {info.name} declares {raw_target}",
                *info.relation_topics.get(key, ("", "")),
            )
            edge_index += 1

    add_graphic_legend(root, legend_y)

    apply_styles(mxfile)
    # Candidate relationship pages must start from the same styled, unlaid-out
    # template. Reusing the failed all-types layout would make pagination depend
    # on stale coordinates from classes that the candidate page removed.
    pagination_source = copy.deepcopy(mxfile)
    layout_issues = optimize_layout(mxfile)
    if _route_issues(layout_issues):
        mxfile, layout_issues = split_conflicting_page(pagination_source)
    if layout_issues:
        details = "; ".join(str(issue) for issue in layout_issues[:5])
        if strict:
            raise ValueError(f"Generated XML geometry still has conflicts: {details}")
        # The diagram is a baseline for human review, not a finished artifact.
        # Failing here would push the caller into ad-hoc writers, so report the
        # conflicts and let layout_drawio.py --check remain the gate.
        print(
            f"WARN: generated XML geometry still has {len(layout_issues)} conflict(s): {details}",
            file=sys.stderr,
        )
        print(
            "WARN: adjust spacing or split pages, then run layout_drawio.py --check.",
            file=sys.stderr,
        )
    return mxfile


def write_document(mxfile: ET.Element, output: Path) -> None:
    """Serialize one diagram. The caller owns the tree."""
    output.parent.mkdir(parents=True, exist_ok=True)
    tree = ET.ElementTree(mxfile)
    ET.indent(tree, space="  ")
    # Hand ET a binary handle, never a path: given a filename it opens the file
    # in text mode and translates "\n" to os.linesep, which would make the
    # export's line endings depend on the host platform.
    with open(output, "wb") as handle:
        tree.write(handle, encoding="utf-8", xml_declaration=True)


def write_drawio(
    classes: list[ClassInfo],
    output: Path,
    scope_label: str,
    strict: bool = False,
) -> None:
    write_document(build_drawio(list(classes), scope_label, strict=strict), output)


def build_facts(
    classes: list[ClassInfo],
    scope: dict[str, str],
    files: list[str],
) -> facts_io.Facts:
    """Turn one scan into the inventory.

    Relations resolve here through the same function the diagram uses, against
    inventory ids instead of node numbers, so the two can never disagree about
    which target is ambiguous.
    """
    ordered = sorted(classes, key=_class_order)
    facts: list[facts_io.ClassFact] = []
    taken: set[str] = set()
    for info in ordered:
        identifier = facts_io.derive_id(info.name, taken)
        taken.add(identifier)
        facts.append(
            facts_io.ClassFact(
                id=identifier,
                name=info.name,
                kind=info.kind,
                change=info.change,
                source=info.source,
                origin="scan",
                fields=list(info.fields),
                methods=list(info.methods),
            )
        )

    index: dict[str, list[str]] = {}
    for fact in facts:
        for key in dict.fromkeys((fact.name, fact.name.rsplit(".", 1)[-1])):
            index.setdefault(key, []).append(fact.id)

    relations: list[facts_io.RelationFact] = []
    warnings: list[str] = []
    for fact, info in zip(facts, ordered):
        for relation, raw_target in info.relations:
            target_id, reason = resolve_relation(raw_target, fact.id, index)
            relations.append(
                facts_io.RelationFact(
                    source_id=fact.id,
                    target_id=target_id,
                    target_declared=raw_target,
                    kind=relation,
                    evidence=f"{info.source}: {info.name} declares {raw_target}",
                    origin="scan",
                )
            )
            if reason:
                warnings.append(
                    f"relation not drawn: {info.source}: {info.name} declares "
                    f"{raw_target} ({reason})"
                )
    return facts_io.Facts(
        scope=dict(scope),
        classes=facts,
        relations=relations,
        files=list(files),
        warnings=warnings,
    )


def facts_to_classes(facts: facts_io.Facts) -> list[ClassInfo]:
    """Restore the scanner's record shape, carrying the review's additions.

    A relation the inventory left unresolved is not drawn even when a class of
    that name happens to be on the page: `to: null` is the inventory's own
    statement that this edge has no target here.
    """
    classes = [
        ClassInfo(
            fact.name,
            fact.kind,
            fact.source,
            fact.change,
            fields=list(fact.fields),
            methods=list(fact.methods),
        )
        for fact in facts.classes
    ]
    by_id = {fact.id: info for fact, info in zip(facts.classes, classes)}

    for relation in facts.relations:
        source = by_id.get(relation.source_id)
        target = by_id.get(relation.target_id) if relation.target_id else None
        if source is None or target is None:
            continue
        key = (relation.kind, relation.target_declared or relation.target_id)
        source.relations.append(key)
        source.relation_evidence[key] = relation.evidence
        if relation.topic:
            source.relation_topics[key] = (relation.topic, relation.topic_label)
        source.relation_targets[key] = target
    return classes


def _slug(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "-", value or "").strip("-").lower()[:40] or "ref"


def default_facts_path(args: argparse.Namespace) -> Path:
    """Where the inventory for this invocation belongs.

    A base..head range is a stable identity, so re-running the same range lands
    on the same file and merges into it. "--days" and "--files" are not
    identities - the same arguments mean different commits tomorrow - so those
    are dated, and a later run starts a new file instead of folding unrelated
    facts into an old one.
    """
    if args.base:
        stem = f"{_slug(args.base)}-{_slug(args.head)}"
    elif args.files:
        stem = f"{date.today().isoformat()}-files"
    else:
        stem = f"{date.today().isoformat()}-last-{args.days}-day"
    return Path("docs/code-review") / f"{stem}-facts.json"


def _extensions(spec: str) -> set[str]:
    values = {value.strip().lower() for value in spec.split(",") if value.strip()}
    return {value if value.startswith(".") else f".{value}" for value in values}


def _resolve(path: Path, repo: Path) -> Path:
    return (path if path.is_absolute() else repo / path).resolve()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="Git repository root (default: current directory)")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--base", help="Base Git revision; combine with --head")
    scope.add_argument("--days", type=int, default=None, help="Collect files changed by commits in the last N days")
    scope.add_argument("--files", nargs="+", help="Explicit repository-relative source files")
    parser.add_argument("--head", default="HEAD", help="Target revision or WORKTREE (default: HEAD)")
    parser.add_argument(
        "--change",
        choices=("added", "modified", "removed"),
        default="modified",
        help="Status assigned to --files entries",
    )
    parser.add_argument("--ext", default=",".join(sorted(SUPPORTED_EXTENSIONS)), help="Comma-separated extensions")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail instead of writing when XML geometry conflicts remain",
    )
    parser.add_argument(
        "--facts",
        type=Path,
        default=None,
        help="Inventory path to write (default: docs/code-review/<scope>-facts.json)",
    )
    parser.add_argument(
        "--from-facts",
        type=Path,
        default=None,
        help="Read an existing inventory as the source of truth instead of scanning",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Also export a .drawio diagram here; omit it to build no diagram at all",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an inventory whose Git range differs from this run",
    )
    args = parser.parse_args(argv)
    args.repo = args.repo.resolve()
    if args.from_facts is not None:
        for name in ("base", "days", "files"):
            if getattr(args, name):
                parser.error(f"--from-facts reads an existing inventory, so --{name} does not apply")
        if args.facts is not None:
            parser.error("--facts writes an inventory and --from-facts reads one; use one of them")
    else:
        if args.days is None and not args.base and not args.files:
            args.days = 1
        if args.days is not None and args.days < 1:
            parser.error("--days must be at least 1")
        if args.head == "WORKTREE" and not args.base:
            parser.error("--head WORKTREE requires --base")
    if not args.repo.is_dir():
        parser.error(f"Repository directory does not exist: {args.repo}")
    return args


def export_diagram(facts: facts_io.Facts, output: Path, strict: bool) -> int:
    """Write the diagram the inventory describes.

    The export is a pure function of the inventory: geometry is measured against
    the members as they stand, so nothing here can go stale, and no exported XML
    is ever read back in.
    """
    if not any(fact.change != "unchanged" for fact in facts.classes):
        raise facts_io.FactsError(
            "the inventory has no added, modified, or removed classes, so an "
            "incremental diagram would be empty and would fail validation"
        )
    classes = facts_to_classes(facts)
    scope_label = str(facts.scope.get("label") or "explicit files")
    write_drawio(classes, output, scope_label, strict=strict)
    print(f"Wrote {len(classes)} class(es) to {output}")
    print("Check the export with validate_drawio.py and layout_drawio.py --check.")
    return 0


def export_from_inventory(args: argparse.Namespace) -> int:
    source = _resolve(args.from_facts, args.repo)
    facts = facts_io.load_facts(source)
    errors, warnings = facts_io.validate_facts(facts)
    for warning in warnings:
        print(f"WARN: {warning}", file=sys.stderr)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 2
    if args.output is None:
        print(f"{source} satisfies the inventory contract.")
        return 0
    return export_diagram(facts, _resolve(args.output, args.repo), args.strict)


def scan_into_inventory(args: argparse.Namespace) -> int:
    try:
        changes, scope_label = collect_changes(args, _extensions(args.ext))
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if not changes:
        print("No matching changed files were found.", file=sys.stderr)
        return 1

    classes: list[ClassInfo] = []
    explicit_files = bool(args.files)
    for changed in changes:
        text = source_at_revision(args.repo, changed, args.base, args.head, explicit_files)
        if text is not None:
            classes.extend(extract_classes(text, changed.path, changed.change))
    if not classes:
        print("No supported class-like declarations were found in the changed files.", file=sys.stderr)
        return 1

    fresh = build_facts(
        classes,
        scope={
            "label": scope_label,
            "base": args.base or "",
            "head": args.head if args.base else "",
            # Scopes that two revisions do not name are pinned to the day they
            # were collected, so tomorrow's run is a new file rather than a
            # merge with a range it never shared.
            "window": "" if args.base else date.today().isoformat(),
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        },
        files=[changed.path for changed in changes],
    )

    target = _resolve(args.facts or default_facts_path(args), args.repo)
    if target.exists() and not args.force:
        existing = facts_io.load_facts(target)
        if not facts_io.scope_matches(existing.scope, fresh.scope):
            print(
                f"ERROR: {target} already holds a different Git range "
                f"({existing.scope.get('base') or existing.scope.get('label')!r}). "
                "Pass --force to overwrite it and drop the reviewed entries it holds.",
                file=sys.stderr,
            )
            return 3
        # Re-scanning the same range is the normal way to refresh the scanned
        # half of the inventory. The reviewed half is not re-derivable, so it
        # survives; dropping it is what made the old hand-edited XML fragile.
        # merge_facts folds its notices into the result's warnings, so the loop
        # below is the only place that reports them.
        fresh = facts_io.merge_facts(existing, fresh)

    errors, warnings = facts_io.validate_facts(fresh)
    for warning in list(fresh.warnings) + warnings:
        print(f"WARN: {warning}", file=sys.stderr)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 2

    facts_io.dump_facts(fresh, target)
    print(f"Wrote {len(fresh.classes)} class(es) and {len(fresh.relations)} relation(s) to {target}")

    if args.output is None:
        print("No diagram requested; pass --output <path> to export one.")
        return 0
    return export_diagram(fresh, _resolve(args.output, args.repo), args.strict)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.from_facts is not None:
            return export_from_inventory(args)
        return scan_into_inventory(args)
    # build_drawio raises on --strict conflicts and the export refuses an
    # all-unchanged inventory. Both are ordinary failures, not tracebacks.
    except (facts_io.FactsError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

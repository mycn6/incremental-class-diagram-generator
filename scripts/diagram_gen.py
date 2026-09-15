#!/usr/bin/env python3
"""Generate a reviewable draw.io class-diagram baseline from a Git change set.

The scanner is deliberately conservative. It extracts class-like declarations,
Python members, and explicit inheritance/implementation clauses. Review and
complete the generated diagram against the source code before delivery.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterable, Sequence

from layout_drawio import optimize as optimize_layout
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

IGNORED_BASES = {
    "ABC",
    "Enum",
    "Generic",
    "object",
    "Protocol",
    "TypedDict",
}

# Page grid. The origin, gaps, and legend box are fixed; every pitch, the row
# count, and the page size are derived from measured box heights so a taller box
# can never collide with the legend below it or run off the page.
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
            SectionSpec("operations", "class-operations", tuple(info.methods), "（未自动提取操作）"),
        )
    if info.kind == "enum":
        return (
            SectionSpec("literals", "class-literals", tuple(info.fields), "（未自动提取枚举常量）"),
            SectionSpec("operations", "class-operations", tuple(info.methods), "（无相关操作）"),
        )
    return (
        SectionSpec("attributes", "class-attributes", tuple(info.fields), "（未自动提取属性）"),
        SectionSpec("operations", "class-operations", tuple(info.methods), "（未自动提取操作）"),
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
) -> None:
    wrapper = ET.SubElement(
        root,
        "object",
        {
            "id": identifier,
            "label": relation,
            "role": "relationship",
            "relation": relation,
            "evidence": evidence,
        },
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



def build_drawio(classes: list[ClassInfo], scope_label: str, strict: bool = False) -> ET.Element:
    classes.sort(key=lambda item: ({"added": 0, "modified": 1, "removed": 2, "unchanged": 3}[item.change], item.name))

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
    for number, (info, box) in enumerate(zip(classes, boxes), start=1):
        identifier = f"class-{number}"
        placed.append((info, identifier))
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
                f"{info.source}: {info.name} declares {raw_target}",
            )
            edge_index += 1

    add_graphic_legend(root, legend_y)

    apply_styles(mxfile)
    layout_issues = optimize_layout(mxfile)
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


def write_drawio(
    classes: list[ClassInfo],
    output: Path,
    scope_label: str,
    strict: bool = False,
) -> None:
    mxfile = build_drawio(classes, scope_label, strict=strict)
    output.parent.mkdir(parents=True, exist_ok=True)
    tree = ET.ElementTree(mxfile)
    ET.indent(tree, space="  ")
    tree.write(output, encoding="utf-8", xml_declaration=True)


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
        "--output",
        type=Path,
        default=Path("docs/diagrams/incremental-class-diagram.drawio"),
        help="Output .drawio path, relative to --repo by default",
    )
    args = parser.parse_args(argv)
    args.repo = args.repo.resolve()
    if args.days is None and not args.base and not args.files:
        args.days = 1
    if args.days is not None and args.days < 1:
        parser.error("--days must be at least 1")
    if args.head == "WORKTREE" and not args.base:
        parser.error("--head WORKTREE requires --base")
    if not args.repo.is_dir():
        parser.error(f"Repository directory does not exist: {args.repo}")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    extensions = {value.strip().lower() for value in args.ext.split(",") if value.strip()}
    extensions = {value if value.startswith(".") else f".{value}" for value in extensions}

    try:
        changes, scope_label = collect_changes(args, extensions)
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    classes: list[ClassInfo] = []
    explicit_files = bool(args.files)
    for changed in changes:
        text = source_at_revision(args.repo, changed, args.base, args.head, explicit_files)
        if text is not None:
            classes.extend(extract_classes(text, changed.path, changed.change))

    if not changes:
        print("No matching changed files were found.", file=sys.stderr)
        return 1
    if not classes:
        print("No supported class-like declarations were found in the changed files.", file=sys.stderr)
        return 1

    output = args.output if args.output.is_absolute() else args.repo / args.output
    write_drawio(classes, output.resolve(), scope_label, strict=args.strict)
    print(f"Wrote {len(classes)} changed class(es) to {output.resolve()}")
    print("Review the diagram against source code, then run validate_drawio.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

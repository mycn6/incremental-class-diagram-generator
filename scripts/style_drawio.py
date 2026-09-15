#!/usr/bin/env python3
"""Apply deterministic styles to semantic incremental class-diagram XML.

The script consumes explicit metadata. It does not infer class semantics,
relationships, evidence, coordinates, or edge routes.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import xml.etree.ElementTree as ET


TEXT_FONT = "Microsoft YaHei"

# Fonts with CJK coverage. Text carrying Chinese must name one of these so the
# renderer never falls back to a Latin-only face.
CJK_FONTS = frozenset({
    TEXT_FONT,
    "SimSun",
    "SimHei",
    "Noto Sans CJK SC",
    "Source Han Sans SC",
    "PingFang SC",
    "Hiragino Sans GB",
})

CLASS_KINDS = {
    "class": "",
    "interface": "«interface»",
    "abstract": "«abstract»",
    "enum": "«enumeration»",
    "struct": "«struct»",
}

CLASS_KIND_LEGEND_LABELS = {
    "class": "类",
    "interface": "接口",
    "abstract": "抽象类",
    "enum": "枚举",
    "struct": "结构体",
}

CHANGE_STYLES = {
    "added": {"fillColor": "#DCFCE7", "strokeColor": "#16A34A", "dashed": "0"},
    "modified": {"fillColor": "#FEF3C7", "strokeColor": "#D97706", "dashed": "0"},
    "removed": {"fillColor": "#FEE2E2", "strokeColor": "#DC2626", "dashed": "1"},
    "unchanged": {"fillColor": "#F3F4F6", "strokeColor": "#6B7280", "dashed": "0"},
}

CHANGE_LEGEND_LABELS = {
    "added": "新增",
    "modified": "修改",
    "removed": "删除",
    "unchanged": "未变",
}

RELATION_STYLES = {
    "inheritance": {
        "startArrow": "none", "endArrow": "block", "endFill": "0", "dashed": "0",
    },
    "implementation": {
        "startArrow": "none", "endArrow": "block", "endFill": "0", "dashed": "1",
    },
    "association": {
        "startArrow": "none", "endArrow": "open", "endFill": "0", "dashed": "0",
    },
    "aggregation": {
        "startArrow": "diamond", "startFill": "0", "endArrow": "none", "dashed": "0",
    },
    "composition": {
        "startArrow": "diamond", "startFill": "1", "endArrow": "none", "dashed": "0",
    },
    "dependency": {
        "startArrow": "none", "endArrow": "open", "endFill": "0", "dashed": "1",
    },
}

RELATION_LEGEND_LABELS = {
    "inheritance": "继承",
    "implementation": "实现",
    "association": "关联",
    "aggregation": "聚合",
    "composition": "组合",
    "dependency": "依赖",
}

RELATION_STYLE_KEYS = {
    "startArrow", "startFill", "endArrow", "endFill", "dashed",
}


def style_dict(value: str | None) -> dict[str, str]:
    return dict(
        part.split("=", 1) if "=" in part else (part, "")
        for part in (value or "").split(";")
        if part
    )


def set_style(cell: ET.Element, values: dict[str, str], clear: set[str] | None = None) -> None:
    result = style_dict(cell.get("style"))
    for key in clear or set():
        result.pop(key, None)
    result.update(values)
    cell.set("style", ";".join(f"{key}={value}" if value else key for key, value in result.items()) + ";")


def apply(document: ET.Element) -> ET.Element:
    if document.tag != "mxfile" or not document.findall("diagram"):
        raise ValueError("Expected mxfile with uncompressed diagrams")

    for page in document.findall("diagram"):
        root = page.find("mxGraphModel/root")
        if root is None:
            raise ValueError(f"Compressed or missing graph: {page.get('name', '')}")

        items = {item.get("id", ""): item for item in root if item.get("id")}
        for item in list(root):
            role = item.get("role")
            cell = item.find("mxCell") if item.tag != "mxCell" else item

            if role == "class":
                kind = item.get("kind")
                change = item.get("change")
                if kind not in CLASS_KINDS:
                    raise ValueError(f"{item.get('id')}: explicit supported kind required")
                if change not in CHANGE_STYLES:
                    raise ValueError(f"{item.get('id')}: explicit supported change required")
                if cell is None:
                    raise ValueError(f"{item.get('id')}: missing mxCell")

                set_style(
                    cell,
                    {
                        "shape": "rectangle",
                        "rounded": "0",
                        "container": "1",
                        "collapsible": "0",
                        "recursiveResize": "0",
                        "whiteSpace": "wrap",
                        "html": "0",
                        "align": "center",
                        "verticalAlign": "middle",
                        "spacing": "0",
                        "strokeWidth": "2",
                        **CHANGE_STYLES[change],
                    },
                )

            if role in {
                "class-header", "class-attributes", "class-operations",
                "class-literals", "class-separator",
            }:
                if cell is None:
                    raise ValueError(f"{item.get('id')}: missing mxCell")
                parent_id = cell.get("parent", "")
                parent = items.get(parent_id)
                if parent is None or parent.get("role") != "class" or item.get("for") != parent_id:
                    raise ValueError(f"{item.get('id')}: class compartment must belong to its class")
                kind = parent.get("kind", "")
                change = parent.get("change", "")
                if kind not in CLASS_KINDS or change not in CHANGE_STYLES:
                    raise ValueError(f"{item.get('id')}: invalid parent class metadata")
                if role == "class-header":
                    set_style(
                        cell,
                        {
                            "shape": "rectangle",
                            "rounded": "0",
                            "whiteSpace": "wrap",
                            "html": "0",
                            "fillColor": "none",
                            "strokeColor": "none",
                            "fontFamily": TEXT_FONT,
                            "fontSize": "14",
                            "fontStyle": "3" if kind == "abstract" else "1",
                            "align": "center",
                            "verticalAlign": "middle",
                            "spacing": "4",
                            "connectable": "0",
                        },
                    )
                elif role == "class-separator":
                    set_style(
                        cell,
                        {
                            "shape": "line",
                            "direction": "east",
                            "strokeColor": CHANGE_STYLES[change]["strokeColor"],
                            "strokeWidth": "1",
                            "fillColor": "none",
                            "connectable": "0",
                        },
                    )
                else:
                    set_style(
                        cell,
                        {
                            "shape": "rectangle",
                            "rounded": "0",
                            "whiteSpace": "wrap",
                            "html": "0",
                            "fillColor": "none",
                            "strokeColor": "none",
                            "fontFamily": TEXT_FONT,
                            "fontSize": "13",
                            "fontStyle": "0",
                            "align": "left",
                            "verticalAlign": "top",
                            "spacing": "8",
                            "connectable": "0",
                        },
                    )

            if role == "relationship":
                relation = item.get("relation")
                if relation not in RELATION_STYLES:
                    raise ValueError(f"{item.get('id')}: explicit supported relation required")
                if cell is None:
                    raise ValueError(f"{item.get('id')}: missing mxCell")
                set_style(
                    cell,
                    {
                        "edgeStyle": "orthogonalEdgeStyle",
                        "rounded": "0",
                        "html": "0",
                        "strokeWidth": "2",
                        **RELATION_STYLES[relation],
                    },
                    clear=RELATION_STYLE_KEYS,
                )

    return document


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    document = ET.parse(args.path).getroot()
    apply(document)
    ET.indent(document)
    # Binary handle, not a path: ET would translate "\n" to os.linesep and make
    # the export's line endings platform dependent.
    with open(args.output or args.path, "wb") as handle:
        ET.ElementTree(document).write(handle, encoding="utf-8", xml_declaration=True)
    print("Styled from explicit metadata; semantic and visual review still required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

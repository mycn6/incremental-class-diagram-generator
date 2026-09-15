#!/usr/bin/env python3
"""Lay out class nodes and route relationships using XML geometry heuristics.

The optimizer is deterministic and dependency-free. It reduces crossings from
explicit draw.io coordinates, but it cannot prove rendered visual correctness.
"""

from __future__ import annotations

import argparse
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
import xml.etree.ElementTree as ET

from style_drawio import set_style, style_dict


ENGINE = "incremental-class-layered-v1"
HORIZONTAL_GAP = 150.0
VERTICAL_GAP = 90.0
MAX_ROWS_PER_LAYER = 4
PORT_MIN = 0.22
PORT_MAX = 0.78
GUTTER = 28.0
OUTER_MARGIN = 48.0
OUTER_LANE = 26.0
SUPPORT_GAP = 36.0
EPSILON = 0.01


@dataclass(frozen=True)
class Point:
    x: float
    y: float


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    width: float
    height: float

    @property
    def left(self) -> float:
        return self.x

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def top(self) -> float:
        return self.y

    @property
    def bottom(self) -> float:
        return self.y + self.height

    @property
    def center(self) -> Point:
        return Point(self.x + self.width / 2, self.y + self.height / 2)


@dataclass(frozen=True)
class LayoutIssue:
    kind: str
    edge: str
    obstacle: str
    detail: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.edge} / {self.obstacle}: {self.detail}"


@dataclass
class RoutePlan:
    identifier: str
    item: ET.Element
    cell: ET.Element
    source: str
    target: str
    source_side: str
    target_side: str
    outer: bool
    outer_channel: str = ""
    source_fraction: float = 0.5
    target_fraction: float = 0.5


def number(value: str | None, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except ValueError:
        return default


def cell_for(item: ET.Element) -> ET.Element | None:
    return item if item.tag == "mxCell" else item.find("mxCell")


def geometry_for(item: ET.Element) -> ET.Element | None:
    cell = cell_for(item)
    return cell.find("mxGeometry") if cell is not None else None


def rect_for(item: ET.Element) -> Rect | None:
    geometry = geometry_for(item)
    if geometry is None:
        return None
    width = number(geometry.get("width"))
    height = number(geometry.get("height"))
    if width <= 0 or height <= 0:
        return None
    return Rect(number(geometry.get("x")), number(geometry.get("y")), width, height)


def page_root(page: ET.Element) -> ET.Element:
    root = page.find("mxGraphModel/root")
    if root is None:
        raise ValueError(f"{page.get('name', 'unnamed')}: expected uncompressed mxGraphModel/root")
    return root


def page_items(root: ET.Element) -> dict[str, ET.Element]:
    return {item.get("id", ""): item for item in root if item.get("id")}


def class_items(root: ET.Element) -> dict[str, ET.Element]:
    return {
        item.get("id", ""): item
        for item in root
        if item.get("id") and item.get("role") == "class" and rect_for(item) is not None
    }


def relationship_items(root: ET.Element) -> list[ET.Element]:
    return sorted(
        (item for item in root if item.get("role") == "relationship"),
        key=lambda item: item.get("id", ""),
    )


def strongly_connected_components(
    nodes: list[str], edges: list[tuple[str, str]],
) -> tuple[list[list[str]], dict[str, int]]:
    graph: dict[str, list[str]] = {node: [] for node in nodes}
    for source, target in edges:
        if source in graph and target in graph:
            graph[source].append(target)
    for targets in graph.values():
        targets.sort()

    index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[list[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)

        for target in graph[node]:
            if target not in indices:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[target])

        if lowlinks[node] == indices[node]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node:
                    break
            components.append(sorted(component))

    for node in sorted(nodes):
        if node not in indices:
            visit(node)

    component_of = {
        member: component_index
        for component_index, component in enumerate(components)
        for member in component
    }
    return components, component_of


def dependency_layers(
    nodes: list[str], edges: list[tuple[str, str]],
) -> tuple[dict[str, int], dict[str, list[str]]]:
    components, component_of = strongly_connected_components(nodes, edges)
    outgoing: dict[int, set[int]] = {index: set() for index in range(len(components))}
    indegree = {index: 0 for index in range(len(components))}
    for source, target in edges:
        if source not in component_of or target not in component_of:
            continue
        source_component = component_of[source]
        target_component = component_of[target]
        if source_component != target_component and target_component not in outgoing[source_component]:
            outgoing[source_component].add(target_component)
            indegree[target_component] += 1

    queue = deque(sorted(index for index, degree in indegree.items() if degree == 0))
    component_rank = {index: 0 for index in range(len(components))}
    while queue:
        current = queue.popleft()
        for target in sorted(outgoing[current]):
            component_rank[target] = max(component_rank[target], component_rank[current] + 1)
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)

    ranks = {node: component_rank[component_of[node]] for node in nodes}
    layers: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        layers[str(ranks[node])].append(node)
    return ranks, layers


def order_layers(
    layers: dict[str, list[str]], ranks: dict[str, int], edges: list[tuple[str, str]],
) -> dict[int, list[str]]:
    ordered = {
        int(rank): sorted(nodes)
        for rank, nodes in layers.items()
    }
    predecessors: dict[str, list[str]] = defaultdict(list)
    successors: dict[str, list[str]] = defaultdict(list)
    for source, target in edges:
        if source in ranks and target in ranks and ranks[source] != ranks[target]:
            successors[source].append(target)
            predecessors[target].append(source)

    for _ in range(4):
        positions = {
            node: position
            for nodes in ordered.values()
            for position, node in enumerate(nodes)
        }
        for rank in sorted(ordered):
            ordered[rank].sort(
                key=lambda node: (
                    sum(positions.get(peer, 0) for peer in predecessors[node])
                    / max(1, len(predecessors[node])),
                    node,
                )
            )
        positions = {
            node: position
            for nodes in ordered.values()
            for position, node in enumerate(nodes)
        }
        for rank in sorted(ordered, reverse=True):
            ordered[rank].sort(
                key=lambda node: (
                    sum(positions.get(peer, 0) for peer in successors[node])
                    / max(1, len(successors[node])),
                    node,
                )
            )
    return ordered


def move_geometry(item: ET.Element, x: float, y: float) -> None:
    geometry = geometry_for(item)
    if geometry is None:
        raise ValueError(f"{item.get('id')}: missing geometry")
    geometry.set("x", compact(x))
    geometry.set("y", compact(y))


def compact(value: float) -> str:
    return str(int(value)) if abs(value - round(value)) < EPSILON else f"{value:.2f}".rstrip("0").rstrip(".")


def arrange_classes(
    page: ET.Element,
    root: ET.Element,
    classes: dict[str, ET.Element],
    ordered: dict[int, list[str]],
    relationship_count: int,
) -> tuple[dict[str, Rect], dict[str, int], dict[str, int], float]:
    original_rects = {identifier: rect_for(item) for identifier, item in classes.items()}
    rects = {identifier: rect for identifier, rect in original_rects.items() if rect is not None}
    start_x = min(rect.left for rect in rects.values())
    original_start_y = min(rect.top for rect in rects.values())
    original_bottom = max(rect.bottom for rect in rects.values())
    max_width = max(rect.width for rect in rects.values())
    max_height = max(rect.height for rect in rects.values())
    rank_positions: dict[str, int] = {}
    layer_positions: dict[str, int] = {}

    header_bottom = max(
        (
            rect.bottom
            for item in root
            if (
                (cell := cell_for(item)) is not None
                and cell.get("vertex") == "1"
                and cell.get("parent") == "1"
                and item.get("role") != "class"
                and (rect := rect_for(item)) is not None
                and rect.bottom <= original_start_y
            )
        ),
        default=0.0,
    )
    top_reserve = 36.0 + OUTER_MARGIN + max(1, relationship_count) * OUTER_LANE
    start_y = max(original_start_y, header_bottom + top_reserve)

    x = start_x
    for rank in sorted(ordered):
        nodes = ordered[rank]
        chunks = max(1, (len(nodes) + MAX_ROWS_PER_LAYER - 1) // MAX_ROWS_PER_LAYER)
        for position, identifier in enumerate(nodes):
            chunk = position // MAX_ROWS_PER_LAYER
            row = position % MAX_ROWS_PER_LAYER
            move_geometry(
                classes[identifier],
                x + chunk * (max_width + HORIZONTAL_GAP),
                start_y + row * (max_height + VERTICAL_GAP),
            )
            rank_positions[identifier] = rank
            layer_positions[identifier] = position
        x += chunks * max_width + chunks * HORIZONTAL_GAP

    arranged = {
        identifier: rect
        for identifier, item in classes.items()
        if (rect := rect_for(item)) is not None
    }
    new_bottom = max(rect.bottom for rect in arranged.values())
    new_right = max(rect.right for rect in arranged.values())

    support_items: list[tuple[ET.Element, Rect]] = []
    for item in root:
        cell = cell_for(item)
        rect = rect_for(item)
        if (
            cell is not None
            and rect is not None
            and cell.get("vertex") == "1"
            and cell.get("parent") == "1"
            and item.get("role") != "class"
            and rect.top >= original_bottom + SUPPORT_GAP
        ):
            support_items.append((item, rect))

    route_reserve = OUTER_MARGIN + max(1, relationship_count) * OUTER_LANE
    if support_items:
        first_support = min(rect.top for _, rect in support_items)
        desired_support = new_bottom + route_reserve
        shift = max(0.0, desired_support - first_support)
        for item, rect in support_items:
            move_geometry(item, rect.x, rect.y + shift)

    model = page.find("mxGraphModel")
    if model is not None:
        model.set("layoutEngine", ENGINE)
        all_rects = [rect_for(item) for item in root]
        visible_rects = [rect for rect in all_rects if rect is not None]
        required_height = max((rect.bottom for rect in visible_rects), default=new_bottom) + 80
        model.set("pageHeight", compact(max(number(model.get("pageHeight"), 0), required_height)))
        model.set("pageWidth", compact(max(number(model.get("pageWidth"), 0), new_right + 80)))

    return arranged, rank_positions, layer_positions, new_bottom


def side_point(rect: Rect, side: str, fraction: float) -> Point:
    if side == "left":
        return Point(rect.left, rect.top + rect.height * fraction)
    if side == "right":
        return Point(rect.right, rect.top + rect.height * fraction)
    if side == "top":
        return Point(rect.left + rect.width * fraction, rect.top)
    if side == "bottom":
        return Point(rect.left + rect.width * fraction, rect.bottom)
    raise ValueError(f"Unsupported side: {side}")


def port_style(side: str, fraction: float, prefix: str) -> dict[str, str]:
    coordinates = {
        "left": (0.0, fraction),
        "right": (1.0, fraction),
        "top": (fraction, 0.0),
        "bottom": (fraction, 1.0),
    }[side]
    return {
        f"{prefix}X": compact(coordinates[0]),
        f"{prefix}Y": compact(coordinates[1]),
        f"{prefix}Perimeter": "1",
    }


def replace_points(cell: ET.Element, points: list[Point]) -> None:
    geometry = cell.find("mxGeometry")
    if geometry is None:
        geometry = ET.SubElement(cell, "mxGeometry", {"as": "geometry", "relative": "1"})
    for child in list(geometry):
        if child.tag == "Array" and child.get("as") == "points":
            geometry.remove(child)
    if points:
        array = ET.SubElement(geometry, "Array", {"as": "points"})
        for point in points:
            ET.SubElement(array, "mxPoint", {"x": compact(point.x), "y": compact(point.y)})


def assign_port_fractions(plans: list[RoutePlan], rects: dict[str, Rect]) -> None:
    groups: dict[tuple[str, str], list[tuple[RoutePlan, bool, float]]] = defaultdict(list)
    for plan in plans:
        source_other = rects[plan.target].center
        target_other = rects[plan.source].center
        groups[(plan.source, plan.source_side)].append(
            (plan, True, source_other.x if plan.source_side in {"top", "bottom"} else source_other.y)
        )
        groups[(plan.target, plan.target_side)].append(
            (plan, False, target_other.x if plan.target_side in {"top", "bottom"} else target_other.y)
        )

    for entries in groups.values():
        entries.sort(key=lambda entry: (entry[2], entry[0].identifier))
        count = len(entries)
        for index, (plan, is_source, _) in enumerate(entries):
            fraction = 0.5 if count == 1 else PORT_MIN + (PORT_MAX - PORT_MIN) * index / (count - 1)
            if is_source:
                plan.source_fraction = fraction
            else:
                plan.target_fraction = fraction


def make_route_plans(
    relationships: list[ET.Element],
    rects: dict[str, Rect],
    ranks: dict[str, int],
    positions: dict[str, int],
    forced_outer: set[str],
) -> list[RoutePlan]:
    def vertically_clear(identifier: str, direction: str) -> bool:
        candidate = rects[identifier]
        for other_id, other in rects.items():
            if other_id == identifier:
                continue
            horizontally_overlaps = max(candidate.left, other.left) < min(candidate.right, other.right)
            if not horizontally_overlaps:
                continue
            if direction == "top" and other.bottom <= candidate.top + EPSILON:
                return False
            if direction == "bottom" and other.top >= candidate.bottom - EPSILON:
                return False
        return True

    plans: list[RoutePlan] = []
    for item in relationships:
        cell = cell_for(item)
        if cell is None:
            continue
        identifier = item.get("id", "")
        source = cell.get("source", "")
        target = cell.get("target", "")
        if source not in rects or target not in rects:
            continue
        rank_gap = ranks[target] - ranks[source]
        same_layer_adjacent = ranks[source] == ranks[target] and abs(positions[source] - positions[target]) == 1
        outer = identifier in forced_outer or rank_gap > 1 or rank_gap < 0

        outer_channel = ""
        if rank_gap == 1 and not outer:
            source_side, target_side = "right", "left"
        elif same_layer_adjacent and not outer:
            if rects[target].center.y >= rects[source].center.y:
                source_side, target_side = "bottom", "top"
            else:
                source_side, target_side = "top", "bottom"
        else:
            outer = True
            if vertically_clear(source, "top") and vertically_clear(target, "top"):
                source_side, target_side = "top", "top"
                outer_channel = "top"
            elif vertically_clear(source, "bottom") and vertically_clear(target, "bottom"):
                source_side, target_side = "bottom", "bottom"
                outer_channel = "bottom"
            elif rects[target].center.x >= rects[source].center.x:
                source_side, target_side = "right", "left"
                outer_channel = "gutter"
            else:
                source_side, target_side = "left", "right"
                outer_channel = "gutter"

        plans.append(
            RoutePlan(
                identifier, item, cell, source, target,
                source_side, target_side, outer, outer_channel,
            )
        )
    assign_port_fractions(plans, rects)
    return plans


def route_edges(
    relationships: list[ET.Element],
    rects: dict[str, Rect],
    ranks: dict[str, int],
    positions: dict[str, int],
    class_top: float,
    class_bottom: float,
    forced_outer: set[str],
) -> None:
    plans = make_route_plans(relationships, rects, ranks, positions, forced_outer)
    outer_indices = {"top": 0, "bottom": 0, "gutter": 0}
    for plan in plans:
        source_rect = rects[plan.source]
        target_rect = rects[plan.target]
        source_point = side_point(source_rect, plan.source_side, plan.source_fraction)
        target_point = side_point(target_rect, plan.target_side, plan.target_fraction)
        set_style(
            plan.cell,
            {
                **port_style(plan.source_side, plan.source_fraction, "exit"),
                **port_style(plan.target_side, plan.target_fraction, "entry"),
            },
        )

        points: list[Point]
        if plan.outer:
            outer_index = outer_indices[plan.outer_channel]
            outer_indices[plan.outer_channel] += 1
            if plan.outer_channel == "top":
                channel_y = class_top - OUTER_MARGIN - outer_index * OUTER_LANE
                points = [Point(source_point.x, channel_y), Point(target_point.x, channel_y)]
            elif plan.outer_channel == "bottom":
                channel_y = class_bottom + OUTER_MARGIN + outer_index * OUTER_LANE
                points = [Point(source_point.x, channel_y), Point(target_point.x, channel_y)]
            else:
                channel_y = class_bottom + OUTER_MARGIN + outer_index * OUTER_LANE
                if plan.source_side == "right":
                    source_gutter = source_rect.right + GUTTER
                    target_gutter = target_rect.left - GUTTER
                else:
                    source_gutter = source_rect.left - GUTTER
                    target_gutter = target_rect.right + GUTTER
                points = [
                    Point(source_gutter, source_point.y),
                    Point(source_gutter, channel_y),
                    Point(target_gutter, channel_y),
                    Point(target_gutter, target_point.y),
                ]
            route_kind = "outer"
        elif plan.source_side in {"left", "right"}:
            if abs(source_point.y - target_point.y) < EPSILON:
                points = []
                route_kind = "straight"
            else:
                channel_x = (source_point.x + target_point.x) / 2
                points = [Point(channel_x, source_point.y), Point(channel_x, target_point.y)]
                route_kind = "orthogonal"
        else:
            if abs(source_point.x - target_point.x) < EPSILON:
                points = []
                route_kind = "straight"
            else:
                channel_y = (source_point.y + target_point.y) / 2
                points = [Point(source_point.x, channel_y), Point(target_point.x, channel_y)]
                route_kind = "orthogonal"

        replace_points(plan.cell, points)
        plan.item.set("layout_engine", ENGINE)
        plan.item.set("layout_route", route_kind)


def style_port_point(cell: ET.Element, rect: Rect, prefix: str) -> Point:
    styles = style_dict(cell.get("style"))
    x = number(styles.get(f"{prefix}X"), 0.5)
    y = number(styles.get(f"{prefix}Y"), 0.5)
    return Point(rect.left + rect.width * x, rect.top + rect.height * y)


def edge_path(item: ET.Element, rects: dict[str, Rect]) -> list[Point] | None:
    cell = cell_for(item)
    if cell is None:
        return None
    source = cell.get("source", "")
    target = cell.get("target", "")
    if source not in rects or target not in rects:
        return None
    result = [style_port_point(cell, rects[source], "exit")]
    geometry = cell.find("mxGeometry")
    if geometry is not None:
        points = geometry.find("Array[@as='points']")
        if points is not None:
            result.extend(Point(number(point.get("x")), number(point.get("y"))) for point in points)
    result.append(style_port_point(cell, rects[target], "entry"))
    return [point for index, point in enumerate(result) if index == 0 or point != result[index - 1]]


def segments(points: list[Point]) -> list[tuple[Point, Point]]:
    return list(zip(points, points[1:]))


def axis_aligned(first: Point, second: Point) -> bool:
    return abs(first.x - second.x) < EPSILON or abs(first.y - second.y) < EPSILON


def segment_hits_rect(first: Point, second: Point, rect: Rect) -> bool:
    if abs(first.x - second.x) < EPSILON:
        x = first.x
        low, high = sorted((first.y, second.y))
        return rect.left + EPSILON < x < rect.right - EPSILON and max(low, rect.top) < min(high, rect.bottom)
    if abs(first.y - second.y) < EPSILON:
        y = first.y
        low, high = sorted((first.x, second.x))
        return rect.top + EPSILON < y < rect.bottom - EPSILON and max(low, rect.left) < min(high, rect.right)
    return True


def same_point(first: Point, second: Point) -> bool:
    return abs(first.x - second.x) < EPSILON and abs(first.y - second.y) < EPSILON


def segment_intersection(
    first_a: Point, first_b: Point, second_a: Point, second_b: Point,
) -> tuple[str, Point] | None:
    first_vertical = abs(first_a.x - first_b.x) < EPSILON
    second_vertical = abs(second_a.x - second_b.x) < EPSILON
    if not axis_aligned(first_a, first_b) or not axis_aligned(second_a, second_b):
        return None

    if first_vertical != second_vertical:
        vertical_a, vertical_b = (first_a, first_b) if first_vertical else (second_a, second_b)
        horizontal_a, horizontal_b = (second_a, second_b) if first_vertical else (first_a, first_b)
        point = Point(vertical_a.x, horizontal_a.y)
        if (
            min(vertical_a.y, vertical_b.y) - EPSILON <= point.y <= max(vertical_a.y, vertical_b.y) + EPSILON
            and min(horizontal_a.x, horizontal_b.x) - EPSILON <= point.x <= max(horizontal_a.x, horizontal_b.x) + EPSILON
        ):
            endpoints = (first_a, first_b, second_a, second_b)
            if sum(same_point(point, endpoint) for endpoint in endpoints) >= 2:
                return None
            return "edge-crossing", point
        return None

    if first_vertical:
        if abs(first_a.x - second_a.x) >= EPSILON:
            return None
        low = max(min(first_a.y, first_b.y), min(second_a.y, second_b.y))
        high = min(max(first_a.y, first_b.y), max(second_a.y, second_b.y))
        if high - low > EPSILON:
            return "edge-overlap", Point(first_a.x, (low + high) / 2)
    else:
        if abs(first_a.y - second_a.y) >= EPSILON:
            return None
        low = max(min(first_a.x, first_b.x), min(second_a.x, second_b.x))
        high = min(max(first_a.x, first_b.x), max(second_a.x, second_b.x))
        if high - low > EPSILON:
            return "edge-overlap", Point((low + high) / 2, first_a.y)
    return None


def page_geometry_issues(page: ET.Element) -> list[LayoutIssue]:
    root = page_root(page)
    items = page_items(root)
    classes = class_items(root)
    class_rects = {
        identifier: rect
        for identifier, item in classes.items()
        if (rect := rect_for(item)) is not None
    }
    obstacles = {
        identifier: rect
        for identifier, item in items.items()
        if (
            (cell := cell_for(item)) is not None
            and cell.get("vertex") == "1"
            and cell.get("parent") == "1"
            and (rect := rect_for(item)) is not None
        )
    }
    relationships = relationship_items(root)
    issues: list[LayoutIssue] = []
    paths: dict[str, list[Point]] = {}

    for item in relationships:
        identifier = item.get("id", "")
        cell = cell_for(item)
        if cell is None:
            continue
        styles = style_dict(cell.get("style"))
        required_ports = ("exitX", "exitY", "entryX", "entryY")
        if any(key not in styles for key in required_ports):
            issues.append(LayoutIssue("missing-port", identifier, "edge", "explicit entry/exit ports are required"))
        path = edge_path(item, class_rects)
        if path is None:
            continue
        paths[identifier] = path
        for first, second in segments(path):
            if not axis_aligned(first, second):
                issues.append(LayoutIssue("non-orthogonal", identifier, "edge", f"{first} -> {second}"))
        source = cell.get("source", "")
        target = cell.get("target", "")
        for obstacle_id, rect in obstacles.items():
            if obstacle_id in {source, target}:
                continue
            if any(segment_hits_rect(first, second, rect) for first, second in segments(path)):
                issues.append(LayoutIssue("edge-node", identifier, obstacle_id, "route crosses vertex bounds"))

    edge_ids = sorted(paths)
    for first_index, first_id in enumerate(edge_ids):
        first_item = items[first_id]
        first_cell = cell_for(first_item)
        if first_cell is None:
            continue
        first_endpoints = {first_cell.get("source", ""), first_cell.get("target", "")}
        for second_id in edge_ids[first_index + 1:]:
            second_item = items[second_id]
            second_cell = cell_for(second_item)
            if second_cell is None:
                continue
            shared_nodes = first_endpoints & {second_cell.get("source", ""), second_cell.get("target", "")}
            found: tuple[str, Point] | None = None
            for first_segment in segments(paths[first_id]):
                for second_segment in segments(paths[second_id]):
                    candidate = segment_intersection(*first_segment, *second_segment)
                    if candidate is None:
                        continue
                    kind, point = candidate
                    if shared_nodes and any(
                        rect.left - EPSILON <= point.x <= rect.right + EPSILON
                        and rect.top - EPSILON <= point.y <= rect.bottom + EPSILON
                        for node in shared_nodes
                        if (rect := class_rects.get(node)) is not None
                    ):
                        continue
                    found = candidate
                    break
                if found:
                    break
            if found:
                kind, point = found
                issues.append(
                    LayoutIssue(kind, first_id, second_id, f"near ({compact(point.x)}, {compact(point.y)})")
                )
    return issues


def check_document(document: ET.Element) -> list[LayoutIssue]:
    if document.tag != "mxfile" or not document.findall("diagram"):
        return [LayoutIssue("structure", "document", "mxfile", "expected uncompressed diagrams")]
    issues: list[LayoutIssue] = []
    for page in document.findall("diagram"):
        try:
            page_issues = page_geometry_issues(page)
        except ValueError as exc:
            page_issues = [LayoutIssue("structure", page.get("name", "unnamed"), "page", str(exc))]
        issues.extend(page_issues)
    return issues


def optimize_page(page: ET.Element, max_iterations: int = 8) -> list[LayoutIssue]:
    root = page_root(page)
    classes = class_items(root)
    relationships = relationship_items(root)
    if not classes:
        return []
    edge_pairs = []
    for item in relationships:
        cell = cell_for(item)
        if cell is not None:
            edge_pairs.append((cell.get("source", ""), cell.get("target", "")))
    ranks, layers = dependency_layers(sorted(classes), edge_pairs)
    ordered = order_layers(layers, ranks, edge_pairs)
    rects, ranks, positions, class_bottom = arrange_classes(
        page, root, classes, ordered, len(relationships)
    )

    forced_outer: set[str] = set()
    for _ in range(max(1, max_iterations)):
        route_edges(
            relationships, rects, ranks, positions,
            min(rect.top for rect in rects.values()), class_bottom, forced_outer,
        )
        issues = page_geometry_issues(page)
        conflict_edges: set[str] = set()
        for issue in issues:
            if issue.kind not in {"edge-node", "edge-crossing", "edge-overlap"}:
                continue
            candidates = [
                identifier
                for identifier in (issue.edge, issue.obstacle)
                if identifier in {item.get("id", "") for item in relationships}
                and identifier not in forced_outer
            ]
            if candidates:
                conflict_edges.add(sorted(candidates)[-1])
        if not conflict_edges:
            return issues
        forced_outer.update(conflict_edges)
    route_edges(
        relationships, rects, ranks, positions,
        min(rect.top for rect in rects.values()), class_bottom, forced_outer,
    )
    return page_geometry_issues(page)


def optimize(document: ET.Element, max_iterations: int = 8) -> list[LayoutIssue]:
    if document.tag != "mxfile" or not document.findall("diagram"):
        raise ValueError("Expected mxfile with uncompressed diagrams")
    issues: list[LayoutIssue] = []
    for page in document.findall("diagram"):
        issues.extend(optimize_page(page, max_iterations=max_iterations))
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", action="store_true", help="Check geometry without changing the file")
    parser.add_argument("--max-iterations", type=int, default=8)
    args = parser.parse_args()

    try:
        document = ET.parse(args.path).getroot()
        if args.check:
            issues = check_document(document)
        else:
            issues = optimize(document, max_iterations=args.max_iterations)
            ET.indent(document, space="  ")
            ET.ElementTree(document).write(
                args.output or args.path, encoding="utf-8", xml_declaration=True
            )
    except (ET.ParseError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1

    for issue in issues:
        print(f"ERROR: {issue}")
    if not issues:
        print("PASS: explicit ports and XML geometry have no detected edge-node, edge-edge, or overlap conflicts.")
        print("Rendered text, labels, arrows, scale, and visual meaning still require manual review.")
    return int(bool(issues))


if __name__ == "__main__":
    raise SystemExit(main())

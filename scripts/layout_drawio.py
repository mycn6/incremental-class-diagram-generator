#!/usr/bin/env python3
"""Lay out class nodes and route relationships using XML geometry heuristics.

The optimizer is deterministic and dependency-free. It reduces crossings from
explicit draw.io coordinates, but it cannot prove rendered visual correctness.
"""

from __future__ import annotations

import argparse
from collections import defaultdict, deque
from dataclasses import dataclass
import heapq
from pathlib import Path
import xml.etree.ElementTree as ET

from style_drawio import set_style, style_dict


ENGINE = "incremental-class-layered-v6"
HORIZONTAL_GAP = 150.0
VERTICAL_GAP = 90.0
MAX_ROWS_PER_LAYER = 4
PORT_MIN = 0.22
PORT_MAX = 0.78
GUTTER = 28.0
OUTER_MARGIN = 48.0
OUTER_LANE = 26.0
ROUTE_CLEARANCE = 48.0
PREFERRED_ROUTE_CLEARANCE = 80.0
SUPPORT_GAP = 36.0
PAGE_MARGIN = 80.0
EPSILON = 0.01
MAZE_EDGE_LIMIT = 4


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


def weakly_connected_components(
    nodes: list[str], edges: list[tuple[str, str]],
) -> list[list[str]]:
    adjacency: dict[str, set[str]] = {node: set() for node in nodes}
    for source, target in edges:
        if source in adjacency and target in adjacency:
            adjacency[source].add(target)
            adjacency[target].add(source)
    components: list[list[str]] = []
    remaining = set(nodes)
    while remaining:
        start = min(remaining)
        queue = deque([start])
        remaining.remove(start)
        component: list[str] = []
        while queue:
            node = queue.popleft()
            component.append(node)
            for peer in sorted(adjacency[node]):
                if peer in remaining:
                    remaining.remove(peer)
                    queue.append(peer)
        components.append(sorted(component))
    return components


def routing_capacity(edge_pairs: list[tuple[str, str]]) -> tuple[int, int, int]:
    """Derive endpoint lanes, outer tracks, and maze lanes from graph pressure."""
    degree: dict[str, int] = defaultdict(int)
    for source, target in edge_pairs:
        degree[source] += 1
        degree[target] += 1
    edge_count = len(edge_pairs)
    max_degree = max(degree.values(), default=1)
    endpoint_lanes = max(3, max_degree)
    outer_tracks = max(4, (edge_count + 1) // 2 + 2, max_degree + 1)
    maze_lanes = max(5, (edge_count + 1) // 2 + 3, max_degree + 2)
    return endpoint_lanes, outer_tracks, maze_lanes


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
    components: list[list[str]],
    connected: set[str],
    edge_pairs: list[tuple[str, str]],
    relationship_count: int,
) -> tuple[dict[str, Rect], dict[str, int], dict[str, int], float]:
    original_rects = {identifier: rect_for(item) for identifier, item in classes.items()}
    rects = {identifier: rect for identifier, rect in original_rects.items() if rect is not None}
    original_start_x = min(rect.left for rect in rects.values())
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
    _, _, page_lane_count = routing_capacity(edge_pairs)
    side_reserve = OUTER_MARGIN + (page_lane_count + 2) * OUTER_LANE
    start_x = max(original_start_x, side_reserve)
    top_reserve = 36.0 + side_reserve
    start_y = max(original_start_y, header_bottom + top_reserve)

    row_pitch = max_height + VERTICAL_GAP
    column_pitch = max_width + HORIZONTAL_GAP
    rank_of = {
        identifier: rank
        for rank, nodes in ordered.items()
        for identifier in nodes
    }
    band_y = start_y
    previous_reserve = 0.0
    routed_components = [component for component in components if any(node in connected for node in component)]
    for component in routed_components:
        component_set = set(component)
        edge_count = sum(
            source in component_set and target in component_set
            for source, target in edge_pairs
        )
        component_pairs = [
            (source, target)
            for source, target in edge_pairs
            if source in component_set and target in component_set
        ]
        _, _, lane_count = routing_capacity(component_pairs)
        route_reserve = OUTER_MARGIN + (lane_count + 1) * OUTER_LANE
        if previous_reserve:
            band_y += previous_reserve + route_reserve + VERTICAL_GAP
        by_rank: dict[int, list[str]] = defaultdict(list)
        for identifier in component:
            rank = rank_of[identifier]
            by_rank[rank].append(identifier)
            rank_positions[identifier] = rank
        local_ranks = {identifier: rank_of[identifier] for identifier in component}
        local_layers = {
            str(rank): list(nodes)
            for rank, nodes in by_rank.items()
        }
        local_edges = component_pairs
        local_order = order_layers(local_layers, local_ranks, local_edges)
        band_rows = max(len(nodes) for nodes in by_rank.values())
        for rank, nodes in sorted(local_order.items()):
            for position, identifier in enumerate(nodes):
                move_geometry(
                    classes[identifier],
                    start_x + rank * column_pitch,
                    band_y + position * row_pitch,
                )
                layer_positions[identifier] = position
        band_y += band_rows * row_pitch
        previous_reserve = route_reserve

    isolated = sorted(set(classes) - connected)
    if isolated:
        if routed_components:
            band_y += previous_reserve + VERTICAL_GAP
        for position, identifier in enumerate(isolated):
            column = position % MAX_ROWS_PER_LAYER
            row = position // MAX_ROWS_PER_LAYER
            move_geometry(
                classes[identifier],
                start_x + column * column_pitch,
                band_y + row * row_pitch,
            )
            rank_positions[identifier] = 0
            layer_positions[identifier] = position

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

    route_reserve = side_reserve
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
        required_height = max((rect.bottom for rect in visible_rects), default=new_bottom) + PAGE_MARGIN
        model.set("pageHeight", compact(max(number(model.get("pageHeight"), 0), required_height)))
        model.set("pageWidth", compact(max(number(model.get("pageWidth"), 0), new_right + PAGE_MARGIN)))

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


def simplify_path(points: list[Point]) -> list[Point]:
    """Remove duplicate and redundant collinear points without changing a route."""
    result: list[Point] = []
    for point in points:
        if result and same_point(result[-1], point):
            continue
        result.append(point)
        while len(result) >= 3:
            first, middle, last = result[-3:]
            if (
                abs(first.x - middle.x) < EPSILON
                and abs(middle.x - last.x) < EPSILON
            ) or (
                abs(first.y - middle.y) < EPSILON
                and abs(middle.y - last.y) < EPSILON
            ):
                result.pop(-2)
            else:
                break
    return result


def paths_intersect(
    first: list[Point],
    second: list[Point],
    shared_rects: list[Rect] | None = None,
) -> bool:
    """Return whether two routes conflict outside their shared endpoint boxes."""
    for first_segment in segments(first):
        for second_segment in segments(second):
            candidate = segment_intersection(*first_segment, *second_segment)
            if candidate is None:
                continue
            _, point = candidate
            if shared_rects and any(
                rect.left - EPSILON <= point.x <= rect.right + EPSILON
                and rect.top - EPSILON <= point.y <= rect.bottom + EPSILON
                for rect in shared_rects
            ):
                continue
            return True
    return False


def point_on_segment(point: Point, first: Point, second: Point) -> bool:
    if abs(first.x - second.x) < EPSILON:
        return (
            abs(point.x - first.x) < EPSILON
            and min(first.y, second.y) - EPSILON <= point.y <= max(first.y, second.y) + EPSILON
        )
    if abs(first.y - second.y) < EPSILON:
        return (
            abs(point.y - first.y) < EPSILON
            and min(first.x, second.x) - EPSILON <= point.x <= max(first.x, second.x) + EPSILON
        )
    return False


def paths_touch(
    first: list[Point],
    second: list[Point],
    shared_rects: list[Rect] | None = None,
) -> bool:
    """Stricter routing-time check that also reserves existing bend points."""
    if paths_intersect(first, second, shared_rects):
        return True
    for first_segment in segments(first):
        for second_segment in segments(second):
            touching = [
                point
                for point in (*first_segment, *second_segment)
                if point_on_segment(point, *first_segment)
                and point_on_segment(point, *second_segment)
            ]
            for point in touching:
                if shared_rects and any(
                    rect.left - EPSILON <= point.x <= rect.right + EPSILON
                    and rect.top - EPSILON <= point.y <= rect.bottom + EPSILON
                    for rect in shared_rects
                ):
                    continue
                return True
    return False


def path_hits_other_class(path: list[Point], plan: RoutePlan, rects: dict[str, Rect]) -> bool:
    return any(
        any(
            segment_hits_rect(first, second, expand_rect(rect, ROUTE_CLEARANCE))
            for first, second in segments(path)
        )
        for identifier, rect in rects.items()
        if identifier not in {plan.source, plan.target}
    )


def path_near_other_classes(
    path: list[Point],
    plan: RoutePlan,
    rects: dict[str, Rect],
    clearance: float,
) -> int:
    """Count unrelated classes whose visual clearance area a route enters."""
    return sum(
        any(
            segment_hits_rect(first, second, expand_rect(rect, clearance))
            for first, second in segments(path)
        )
        for identifier, rect in rects.items()
        if identifier not in {plan.source, plan.target}
    )


def path_length(path: list[Point]) -> float:
    return sum(
        abs(second.x - first.x) + abs(second.y - first.y)
        for first, second in segments(path)
    )


def path_self_intersects(path: list[Point]) -> bool:
    route_segments = segments(path)
    for index, first in enumerate(route_segments):
        for other_index, second in enumerate(route_segments[index + 2:], start=index + 2):
            if other_index == index + 1:
                continue
            if segment_intersection(*first, *second) is not None:
                return True
    return False


def gutter_x(rect: Rect, side: str, lane: int) -> float:
    offset = GUTTER + lane * OUTER_LANE
    if side == "right":
        return rect.right + offset
    if side == "left":
        return rect.left - offset
    raise ValueError(f"Gutter x requires a horizontal side, got {side}")


def gutter_y(rect: Rect, side: str, lane: int) -> float:
    offset = GUTTER + lane * OUTER_LANE
    if side == "top":
        return rect.top - offset
    if side == "bottom":
        return rect.bottom + offset
    raise ValueError(f"Gutter y requires a vertical side, got {side}")


def add_candidate(candidates: list[list[Point]], seen: set[tuple[Point, ...]], points: list[Point]) -> None:
    candidate = simplify_path(points)
    key = tuple(candidate)
    if len(candidate) >= 2 and key not in seen and not path_self_intersects(candidate):
        seen.add(key)
        candidates.append(candidate)


def route_candidates(
    plan: RoutePlan,
    rects: dict[str, Rect],
    class_top: float,
    class_bottom: float,
    endpoint_lane_count: int,
    track_count: int,
) -> list[list[Point]]:
    """Build deterministic direct, independent-gutter, and dogleg candidates."""
    source_rect = rects[plan.source]
    target_rect = rects[plan.target]
    source = side_point(source_rect, plan.source_side, plan.source_fraction)
    target = side_point(target_rect, plan.target_side, plan.target_fraction)
    candidates: list[list[Point]] = []
    seen: set[tuple[Point, ...]] = set()

    if not plan.outer and plan.source_side in {"left", "right"}:
        middle_x = (source.x + target.x) / 2
        add_candidate(candidates, seen, [source, Point(middle_x, source.y), Point(middle_x, target.y), target])
    elif not plan.outer:
        middle_y = (source.y + target.y) / 2
        add_candidate(candidates, seen, [source, Point(source.x, middle_y), Point(target.x, middle_y), target])

    if plan.source_side in {"left", "right"} and plan.target_side in {"left", "right"}:
        lane_pairs = sorted(
            ((source_lane, target_lane) for source_lane in range(endpoint_lane_count)
             for target_lane in range(endpoint_lane_count)),
            key=lambda pair: (sum(pair), abs(pair[0] - pair[1]), pair),
        )
        tracks = [
            *(class_bottom + OUTER_MARGIN + index * OUTER_LANE for index in range(track_count)),
            *(class_top - OUTER_MARGIN - index * OUTER_LANE for index in range(track_count)),
        ]
        class_left = min(rect.left for rect in rects.values())
        class_right = max(rect.right for rect in rects.values())
        if plan.outer and plan.source_side == plan.target_side:
            side_outer_xs = (
                [class_left - OUTER_MARGIN - index * OUTER_LANE for index in range(endpoint_lane_count)]
                if plan.source_side == "left"
                else [class_right + OUTER_MARGIN + index * OUTER_LANE for index in range(endpoint_lane_count)]
            )
            side_outer_xs = [value for value in side_outer_xs if value >= GUTTER]
            perimeter_tracks = [
                *(class_top - OUTER_MARGIN - index * OUTER_LANE for index in range(track_count)),
                *(class_bottom + OUTER_MARGIN + index * OUTER_LANE for index in range(track_count)),
            ]
            for outer_x in side_outer_xs:
                for track_y in perimeter_tracks:
                    for source_lane, target_lane in lane_pairs:
                        source_x = gutter_x(source_rect, plan.source_side, source_lane)
                        target_x = gutter_x(target_rect, plan.target_side, target_lane)
                        add_candidate(
                            candidates,
                            seen,
                            [
                                source,
                                Point(source_x, source.y),
                                Point(outer_x, source.y),
                                Point(outer_x, track_y),
                                Point(target_x, track_y),
                                Point(target_x, target.y),
                                target,
                            ],
                        )
        for track_y in tracks:
            for source_lane, target_lane in lane_pairs:
                source_x = gutter_x(source_rect, plan.source_side, source_lane)
                target_x = gutter_x(target_rect, plan.target_side, target_lane)
                add_candidate(
                    candidates,
                    seen,
                    [
                        source,
                        Point(source_x, source.y),
                        Point(source_x, track_y),
                        Point(target_x, track_y),
                        Point(target_x, target.y),
                        target,
                    ],
                )

        outer_xs = [value for value in [
            *(class_left - OUTER_MARGIN - index * OUTER_LANE for index in range(endpoint_lane_count)),
            *(class_right + OUTER_MARGIN + index * OUTER_LANE for index in range(endpoint_lane_count)),
        ] if value >= GUTTER]
        # Separate source and target horizontal tracks. The intermediate x lane
        # is the dogleg that resolves incompatible lane order at the two ends.
        paired_tracks = []
        for index in range(track_count - 1):
            bottom_near = class_bottom + OUTER_MARGIN + index * OUTER_LANE
            bottom_far = bottom_near + OUTER_LANE
            top_near = class_top - OUTER_MARGIN - index * OUTER_LANE
            top_far = top_near - OUTER_LANE
            paired_tracks.extend(
                ((bottom_near, bottom_far), (bottom_far, bottom_near),
                 (top_near, top_far), (top_far, top_near))
            )
        for source_track, target_track in paired_tracks:
            for middle_x in outer_xs:
                for source_lane, target_lane in lane_pairs[:endpoint_lane_count]:
                    source_x = gutter_x(source_rect, plan.source_side, source_lane)
                    target_x = gutter_x(target_rect, plan.target_side, target_lane)
                    add_candidate(
                        candidates,
                        seen,
                        [
                            source,
                            Point(source_x, source.y),
                            Point(source_x, source_track),
                            Point(middle_x, source_track),
                            Point(middle_x, target_track),
                            Point(target_x, target_track),
                            Point(target_x, target.y),
                            target,
                        ],
                    )
    elif (
        plan.source_side in {"left", "right"}
        and plan.target_side in {"top", "bottom"}
    ):
        lane_pairs = sorted(
            ((source_lane, target_lane) for source_lane in range(endpoint_lane_count)
             for target_lane in range(endpoint_lane_count)),
            key=lambda pair: (sum(pair), abs(pair[0] - pair[1]), pair),
        )
        class_left = min(rect.left for rect in rects.values())
        class_right = max(rect.right for rect in rects.values())
        outer_xs = (
            [class_left - OUTER_MARGIN - index * OUTER_LANE for index in range(endpoint_lane_count)]
            if plan.source_side == "left"
            else [class_right + OUTER_MARGIN + index * OUTER_LANE for index in range(endpoint_lane_count)]
        )
        for outer_x in (value for value in outer_xs if value >= GUTTER):
            for source_lane, target_lane in lane_pairs:
                source_x = gutter_x(source_rect, plan.source_side, source_lane)
                target_y = gutter_y(target_rect, plan.target_side, target_lane)
                add_candidate(
                    candidates,
                    seen,
                    [
                        source,
                        Point(source_x, source.y),
                        Point(outer_x, source.y),
                        Point(outer_x, target_y),
                        Point(target.x, target_y),
                        target,
                    ],
                )
    else:
        # Top/bottom endpoints already have independent x coordinates. Offer
        # both outer bands; reversed track order is handled by candidate search.
        for index in range(track_count):
            for track_y in (
                class_top - OUTER_MARGIN - index * OUTER_LANE,
                class_bottom + OUTER_MARGIN + index * OUTER_LANE,
            ):
                add_candidate(
                    candidates,
                    seen,
                    [source, Point(source.x, track_y), Point(target.x, track_y), target],
                )
        class_left = min(rect.left for rect in rects.values())
        class_right = max(rect.right for rect in rects.values())
        outer_xs = [value for value in [
            *(class_left - OUTER_MARGIN - index * OUTER_LANE for index in range(endpoint_lane_count)),
            *(class_right + OUTER_MARGIN + index * OUTER_LANE for index in range(endpoint_lane_count)),
        ] if value >= GUTTER]

        if plan.source_side in {"top", "bottom"} and plan.target_side in {"top", "bottom"}:
            lane_pairs = sorted(
                ((source_lane, target_lane) for source_lane in range(endpoint_lane_count)
                 for target_lane in range(endpoint_lane_count)),
                key=lambda pair: (sum(pair), abs(pair[0] - pair[1]), pair),
            )
            for outer_x in outer_xs:
                for source_lane, target_lane in lane_pairs:
                    source_y = gutter_y(source_rect, plan.source_side, source_lane)
                    target_y = gutter_y(target_rect, plan.target_side, target_lane)
                    add_candidate(
                        candidates,
                        seen,
                        [
                            source,
                            Point(source.x, source_y),
                            Point(outer_x, source_y),
                            Point(outer_x, target_y),
                            Point(target.x, target_y),
                            target,
                        ],
                    )
    return candidates


def candidate_conflicts(
    candidate: list[Point],
    plan: RoutePlan,
    occupied: list[tuple[RoutePlan, list[Point]]],
    rects: dict[str, Rect],
) -> int:
    conflicts = int(path_hits_other_class(candidate, plan, rects))
    for other, path in occupied:
        shared = {plan.source, plan.target} & {other.source, other.target}
        shared_rects = [rects[identifier] for identifier in shared]
        conflicts += int(paths_touch(candidate, path, shared_rects))
    return conflicts


def candidate_quality(
    candidate: list[Point],
    plan: RoutePlan,
    occupied: list[tuple[RoutePlan, list[Point]]],
    rects: dict[str, Rect],
) -> tuple[int, int, int, float]:
    """Rank every candidate by safety first, then by visual economy."""
    return (
        candidate_conflicts(candidate, plan, occupied, rects),
        path_near_other_classes(candidate, plan, rects, PREFERRED_ROUTE_CLEARANCE),
        max(0, len(candidate) - 2),
        path_length(candidate),
    )


def maze_route(
    plan: RoutePlan,
    rects: dict[str, Rect],
    occupied: list[tuple[RoutePlan, list[Point]]],
    maze_lane_count: int,
    route_top: float,
    route_bottom: float,
) -> list[Point] | None:
    """Find a zero-conflict orthogonal route on a deterministic sparse grid."""
    source = side_point(rects[plan.source], plan.source_side, plan.source_fraction)
    target = side_point(rects[plan.target], plan.target_side, plan.target_fraction)
    lane_count = maze_lane_count
    xs = {source.x, target.x}
    ys = {source.y, target.y}
    for rect in rects.values():
        xs.update((rect.left, rect.right, rect.center.x))
        ys.update((rect.top, rect.bottom, rect.center.y))
        for lane in range(lane_count):
            offset = GUTTER + lane * OUTER_LANE
            xs.update((rect.left - offset, rect.right + offset))
            ys.update((rect.top - offset, rect.bottom + offset))

    class_left = min(rect.left for rect in rects.values())
    class_right = max(rect.right for rect in rects.values())
    route_reserve = OUTER_MARGIN + (lane_count + 1) * OUTER_LANE
    for lane in range(lane_count + 2):
        offset = OUTER_MARGIN + lane * OUTER_LANE
        xs.update((class_left - offset, class_right + offset))
        ys.update((route_top - offset, route_bottom + offset))

    endpoint_xs = {source.x, target.x}
    x_values = sorted(value for value in xs if value >= GUTTER or value in endpoint_xs)
    minimum_y = max(0.0, route_top - route_reserve)
    maximum_y = route_bottom + route_reserve
    y_values = sorted(value for value in ys if minimum_y <= value <= maximum_y)
    x_index = {value: index for index, value in enumerate(x_values)}
    y_index = {value: index for index, value in enumerate(y_values)}
    start = (x_index[source.x], y_index[source.y], "")
    goal_xy = (x_index[target.x], y_index[target.y])
    queue: list[tuple[float, float, int, int, int, str]] = []
    heapq.heappush(queue, (0.0, 0.0, 0, start[0], start[1], start[2]))
    distance: dict[tuple[int, int, str], tuple[float, int]] = {start: (0.0, 0)}
    previous: dict[tuple[int, int, str], tuple[int, int, str]] = {}
    clear_cache: dict[tuple[int, int, int, int], bool] = {}

    shared_by_other = {
        other.identifier: [
            rects[identifier]
            for identifier in ({plan.source, plan.target} & {other.source, other.target})
        ]
        for other, _ in occupied
    }

    def clear(first: Point, second: Point) -> bool:
        key = (
            x_index[first.x], y_index[first.y],
            x_index[second.x], y_index[second.y],
        )
        reverse = (key[2], key[3], key[0], key[1])
        if key in clear_cache:
            return clear_cache[key]
        if reverse in clear_cache:
            return clear_cache[reverse]
        result = not any(
            segment_hits_rect(
                first,
                second,
                rect if identifier in {plan.source, plan.target}
                else expand_rect(rect, ROUTE_CLEARANCE),
            )
            for identifier, rect in rects.items()
        )
        if result:
            for other, path in occupied:
                if paths_touch([first, second], path, shared_by_other[other.identifier]):
                    result = False
                    break
        clear_cache[key] = result
        return result

    final_state: tuple[int, int, str] | None = None
    while queue:
        _, cost, bends, x_pos, y_pos, direction = heapq.heappop(queue)
        state = (x_pos, y_pos, direction)
        if distance.get(state) != (cost, bends):
            continue
        if (x_pos, y_pos) == goal_xy:
            final_state = state
            break
        neighbours = []
        if x_pos > 0:
            neighbours.append((x_pos - 1, y_pos, "h"))
        if x_pos + 1 < len(x_values):
            neighbours.append((x_pos + 1, y_pos, "h"))
        if y_pos > 0:
            neighbours.append((x_pos, y_pos - 1, "v"))
        if y_pos + 1 < len(y_values):
            neighbours.append((x_pos, y_pos + 1, "v"))
        current = Point(x_values[x_pos], y_values[y_pos])
        for next_x, next_y, next_direction in neighbours:
            point = Point(x_values[next_x], y_values[next_y])
            if not clear(current, point):
                continue
            bend = int(bool(direction) and direction != next_direction)
            next_bends = bends + bend
            visual_penalty = path_near_other_classes(
                [current, point], plan, rects, PREFERRED_ROUTE_CLEARANCE
            )
            next_cost = (
                cost
                + abs(point.x - current.x)
                + abs(point.y - current.y)
                + bend * OUTER_LANE * 2
                + visual_penalty * OUTER_LANE * 4
            )
            next_state = (next_x, next_y, next_direction)
            best = distance.get(next_state)
            if best is not None and best <= (next_cost, next_bends):
                continue
            distance[next_state] = (next_cost, next_bends)
            previous[next_state] = state
            heuristic = abs(point.x - target.x) + abs(point.y - target.y)
            heapq.heappush(
                queue,
                (next_cost + heuristic, next_cost, next_bends, next_x, next_y, next_direction),
            )

    if final_state is None:
        return None
    states = [final_state]
    while states[-1] != start:
        states.append(previous[states[-1]])
    states.reverse()
    return simplify_path([Point(x_values[x], y_values[y]) for x, y, _ in states])


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
            # The style serializer writes at most two decimals. Route geometry
            # must use that same value or the last segment can become diagonal
            # when the rendered port is reconstructed from XML.
            fraction = float(compact(fraction))
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
        row_gap = abs(positions[source] - positions[target])
        outer = (
            identifier in forced_outer
            or rank_gap > 1
            or rank_gap < 0
            or (rank_gap == 1 and row_gap > 1)
        )

        long_adjacent_relation = rank_gap == 1 and row_gap > 1
        if long_adjacent_relation:
            source_side = "left" if row_gap % 2 == 0 else "right"
            target_side = "top" if source_side == "left" else "bottom"
        elif rank_gap == 1 and not outer:
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
            elif vertically_clear(source, "bottom") and vertically_clear(target, "bottom"):
                source_side, target_side = "bottom", "bottom"
            elif rects[target].center.x >= rects[source].center.x:
                source_side, target_side = "right", "left"
            else:
                source_side, target_side = "left", "right"

        plans.append(
            RoutePlan(
                identifier, item, cell, source, target,
                source_side, target_side, outer,
            )
        )
    assign_port_fractions(plans, rects)
    return plans


def route_edges(
    relationships: list[ET.Element],
    rects: dict[str, Rect],
    ranks: dict[str, int],
    positions: dict[str, int],
    forced_outer: set[str],
    routing_attempt: int = 0,
    reroute_ids: set[str] | None = None,
    conflict_priority: dict[str, int] | None = None,
) -> None:
    plans = make_route_plans(relationships, rects, ranks, positions, forced_outer)
    occupied: list[tuple[RoutePlan, list[Point]]] = []
    if reroute_ids is not None:
        for plan in plans:
            if plan.identifier in reroute_ids:
                continue
            path = edge_path(plan.item, rects)
            if path is not None:
                occupied.append((plan, path))
    route_components = weakly_connected_components(
        sorted(rects), [(plan.source, plan.target) for plan in plans]
    )
    component_of = {
        identifier: index
        for index, component in enumerate(route_components)
        for identifier in component
    }
    component_bounds = {
        index: (
            min(rects[identifier].top for identifier in component),
            max(rects[identifier].bottom for identifier in component),
        )
        for index, component in enumerate(route_components)
    }
    component_pairs: dict[int, list[tuple[str, str]]] = defaultdict(list)
    for plan in plans:
        component_pairs[component_of[plan.source]].append((plan.source, plan.target))
    component_capacity = {
        component: routing_capacity(pairs)
        for component, pairs in component_pairs.items()
    }
    # Preserve short local routes first; longer/outer relations then select
    # channels around the segments already committed to the page.
    def route_order(plan: RoutePlan) -> tuple[object, ...]:
        source = side_point(rects[plan.source], plan.source_side, plan.source_fraction)
        target = side_point(rects[plan.target], plan.target_side, plan.target_fraction)
        strategy = routing_attempt % 7
        if strategy == 1:
            detail = (source.y, target.y)
        elif strategy == 2:
            detail = (-source.y, -target.y)
        elif strategy == 3:
            detail = (target.y, source.y)
        elif strategy == 4:
            detail = (-target.y, -source.y)
        elif strategy == 5:
            detail = (abs(target.x - source.x) + abs(target.y - source.y), source.y)
        elif strategy == 6:
            detail = (-abs(target.x - source.x), -abs(target.y - source.y))
        else:
            detail = (-abs(target.x - source.x) - abs(target.y - source.y), source.y)
        priority = (conflict_priority or {}).get(plan.identifier, 0)
        priority_key = -priority if routing_attempt % 2 else priority
        return (plan.outer, priority_key, *detail, plan.identifier)

    selected = plans if reroute_ids is None else [plan for plan in plans if plan.identifier in reroute_ids]
    ordered = sorted(selected, key=route_order)
    for plan in ordered:
        source_rect = rects[plan.source]
        target_rect = rects[plan.target]
        component = component_of[plan.source]
        route_top, route_bottom = component_bounds[component]
        edge_count = len(component_pairs[component])
        endpoint_lanes, track_count, maze_lanes = component_capacity[component]
        set_style(
            plan.cell,
            {
                **port_style(plan.source_side, plan.source_fraction, "exit"),
                **port_style(plan.target_side, plan.target_fraction, "entry"),
            },
        )
        candidates = route_candidates(
            plan, rects, route_top, route_bottom, endpoint_lanes, track_count
        )
        if not candidates:
            source = side_point(source_rect, plan.source_side, plan.source_fraction)
            target = side_point(target_rect, plan.target_side, plan.target_fraction)
            candidates = [[source, target]]
        best: tuple[tuple[int, int, int, float], int, list[Point]] | None = None
        for index, candidate in enumerate(candidates):
            quality = candidate_quality(candidate, plan, occupied, rects)
            if best is None or (quality, index) < (best[0], best[1]):
                best = (quality, index, candidate)
        assert best is not None
        best_quality, _, path = best
        if best_quality[0] and edge_count <= MAZE_EDGE_LIMIT:
            searched = maze_route(
                plan, rects, occupied, maze_lanes, route_top, route_bottom
            )
            if searched is not None:
                path = searched
        replace_points(plan.cell, path[1:-1])
        occupied.append((plan, path))
        plan.item.set("layout_engine", ENGINE)
        plan.item.set(
            "layout_route",
            "outer-dogleg" if plan.outer and len(path) > 6 else "outer" if plan.outer else "orthogonal",
        )


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


def expand_page_to_routes(page: ET.Element, relationships: list[ET.Element], rects: dict[str, Rect]) -> None:
    """Keep every explicit route inside the declared draw.io page bounds."""
    model = page.find("mxGraphModel")
    if model is None:
        return
    points = [
        point
        for item in relationships
        if (path := edge_path(item, rects)) is not None
        for point in path
    ]
    if not points:
        return
    model.set("pageWidth", compact(max(number(model.get("pageWidth")), max(point.x for point in points) + PAGE_MARGIN)))
    model.set("pageHeight", compact(max(number(model.get("pageHeight")), max(point.y for point in points) + PAGE_MARGIN)))


def compact_vertical_flow(page: ET.Element) -> None:
    """Pack header, routed class content, and support objects without stale gaps."""
    model = page.find("mxGraphModel")
    if model is None:
        return
    root = page_root(page)
    classes = class_items(root)
    relationships = relationship_items(root)
    rects = {
        identifier: rect
        for identifier, item in classes.items()
        if (rect := rect_for(item)) is not None
    }
    if not rects:
        return

    items = page_items(root)
    header_rects = [
        rect
        for identifier in ("title", "scope")
        if (item := items.get(identifier)) is not None
        and (rect := rect_for(item)) is not None
    ]
    header_bottom = max((rect.bottom for rect in header_rects), default=0.0)
    paths = [
        path
        for relationship in relationships
        if (path := edge_path(relationship, rects)) is not None
    ]
    content_top = min(
        [rect.top for rect in rects.values()]
        + [point.y for path in paths for point in path]
    )
    delta_y = header_bottom + SUPPORT_GAP - content_top
    if abs(delta_y) >= EPSILON:
        for identifier, item in classes.items():
            rect = rects.get(identifier)
            if rect is not None:
                move_geometry(item, rect.x, rect.y + delta_y)
        for relationship in relationships:
            cell = cell_for(relationship)
            geometry = cell.find("mxGeometry") if cell is not None else None
            points = geometry.find("Array[@as='points']") if geometry is not None else None
            if points is not None:
                for point in points:
                    point.set("y", compact(number(point.get("y")) + delta_y))

    rects = {
        identifier: rect
        for identifier, item in classes.items()
        if (rect := rect_for(item)) is not None
    }
    paths = [
        path
        for relationship in relationships
        if (path := edge_path(relationship, rects)) is not None
    ]
    content_bottom = max(
        [rect.bottom for rect in rects.values()]
        + [point.y for path in paths for point in path]
    )

    legend = items.get("legend")
    legend_rect = rect_for(legend) if legend is not None else None
    if legend is not None and legend_rect is not None:
        move_geometry(
            legend,
            legend_rect.x,
            content_bottom + SUPPORT_GAP,
        )

    top_level_bottoms = []
    for item in root:
        cell = cell_for(item)
        rect = rect_for(item)
        if (
            cell is not None
            and rect is not None
            and cell.get("vertex") == "1"
            and cell.get("parent") == "1"
        ):
            top_level_bottoms.append(rect.bottom)
    page_bottom = max(top_level_bottoms + [content_bottom])
    model.set("pageHeight", compact(page_bottom + PAGE_MARGIN))


def stretch_page_support_widths(page: ET.Element) -> None:
    """Match title, scope, and legend widths to the final usable page width."""
    model = page.find("mxGraphModel")
    if model is None:
        return
    root = page_root(page)
    items = page_items(root)
    usable_right = max(PAGE_MARGIN, number(model.get("pageWidth")) - PAGE_MARGIN)
    for identifier in ("title", "scope", "legend"):
        item = items.get(identifier)
        rect = rect_for(item) if item is not None else None
        geometry = geometry_for(item) if item is not None else None
        if rect is not None and geometry is not None:
            geometry.set("width", compact(max(rect.width, usable_right - rect.left)))

    legend = items.get("legend")
    legend_title = items.get("legend-title")
    legend_rect = rect_for(legend) if legend is not None else None
    title_rect = rect_for(legend_title) if legend_title is not None else None
    title_geometry = geometry_for(legend_title) if legend_title is not None else None
    if legend_rect is not None and title_rect is not None and title_geometry is not None:
        title_geometry.set(
            "width",
            compact(max(title_rect.width, legend_rect.width - title_rect.left - 20.0)),
        )


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


def expand_rect(rect: Rect, padding: float) -> Rect:
    return Rect(
        rect.x - padding,
        rect.y - padding,
        rect.width + padding * 2,
        rect.height + padding * 2,
    )


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
            obstacle = expand_rect(rect, ROUTE_CLEARANCE) if obstacle_id in class_rects else rect
            if any(segment_hits_rect(first, second, obstacle) for first, second in segments(path)):
                detail = (
                    f"route enters the {compact(ROUTE_CLEARANCE)} px class clearance area"
                    if obstacle_id in class_rects
                    else "route crosses vertex bounds"
                )
                issues.append(LayoutIssue("edge-node", identifier, obstacle_id, detail))

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
    connected = {
        identifier
        for pair in edge_pairs
        for identifier in pair
        if identifier in classes
    }
    ordered = order_layers(layers, ranks, edge_pairs)
    components = weakly_connected_components(sorted(classes), edge_pairs)
    rects, ranks, positions, _ = arrange_classes(
        page, root, classes, ordered, components, connected, edge_pairs, len(relationships)
    )

    effective_iterations = min(max_iterations, 2 if len(relationships) > 6 else 4)
    forced_outer: set[str] = set()
    best_page: bytes | None = None
    best_count: int | None = None
    route_edges(
        relationships, rects, ranks, positions, forced_outer,
        routing_attempt=0,
    )
    for attempt in range(max(1, effective_iterations)):
        issues = page_geometry_issues(page)
        if best_count is None or len(issues) < best_count:
            best_count = len(issues)
            best_page = ET.tostring(page)
        conflict_edges: set[str] = set()
        conflict_priority: dict[str, int] = defaultdict(int)
        has_route_conflict = False
        for issue in issues:
            if issue.kind not in {"edge-node", "edge-crossing", "edge-overlap"}:
                continue
            has_route_conflict = True
            candidates = [
                identifier
                for identifier in (issue.edge, issue.obstacle)
                if identifier in {item.get("id", "") for item in relationships}
            ]
            conflict_edges.update(candidates)
            for identifier in candidates:
                conflict_priority[identifier] += 1
        if not has_route_conflict:
            expand_page_to_routes(page, relationships, rects)
            compact_vertical_flow(page)
            stretch_page_support_widths(page)
            return page_geometry_issues(page)
        forced_outer.update(conflict_edges)
        if attempt + 1 < max(1, effective_iterations):
            # Alternate between a true rip-up/reroute pass that preserves
            # conflict-free paths and a full reorder that can release an early
            # route which monopolised a critical channel.
            reroute_ids = conflict_edges if attempt % 2 == 0 else None
            route_edges(
                relationships,
                rects,
                ranks,
                positions,
                forced_outer,
                routing_attempt=attempt + 1,
                reroute_ids=reroute_ids,
                conflict_priority=conflict_priority,
            )
    assert best_page is not None
    restored = ET.fromstring(best_page)
    page.attrib.clear()
    page.attrib.update(restored.attrib)
    page.text = restored.text
    page[:] = list(restored)
    root = page_root(page)
    relationships = relationship_items(root)
    classes = class_items(root)
    rects = {
        identifier: rect
        for identifier, item in classes.items()
        if (rect := rect_for(item)) is not None
    }
    expand_page_to_routes(page, relationships, rects)
    compact_vertical_flow(page)
    stretch_page_support_widths(page)
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
            # Binary handle, not a path: ET would translate "\n" to os.linesep
            # and make the export's line endings platform dependent.
            with open(args.output or args.path, "wb") as handle:
                ET.ElementTree(document).write(
                    handle, encoding="utf-8", xml_declaration=True
                )
    except (ET.ParseError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1

    for issue in issues:
        print(f"ERROR: {issue}")
    if not issues:
        print(
            "PASS: explicit ports and XML geometry have no detected edge-node, "
            "class-clearance, edge-edge, or overlap conflicts."
        )
        print("Rendered text, labels, arrows, scale, and visual meaning still require manual review.")
    return int(bool(issues))


if __name__ == "__main__":
    raise SystemExit(main())

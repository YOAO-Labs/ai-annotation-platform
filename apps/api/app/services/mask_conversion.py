from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.utils.raster_mask_rle import (
    coco_rle_bbox_norm,
    decode_coco_rle,
    encode_coco_rle,
    validate_coco_rle,
)

Point = tuple[float, float]
PixelPoint = tuple[int, int]
DirectedEdge = tuple[PixelPoint, PixelPoint]


@dataclass(frozen=True)
class MaskShapeStats:
    area: int
    components: int
    holes: int


@dataclass(frozen=True)
class ConversionMetrics:
    source_area_pixels: int
    target_area_pixels: int
    changed_pixels: int
    source_components: int
    target_components: int
    source_holes: int
    target_holes: int
    source_vertices: int
    target_vertices: int
    lossy: bool
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_area_pixels": self.source_area_pixels,
            "target_area_pixels": self.target_area_pixels,
            "changed_pixels": self.changed_pixels,
            "source_components": self.source_components,
            "target_components": self.target_components,
            "source_holes": self.source_holes,
            "target_holes": self.target_holes,
            "source_vertices": self.source_vertices,
            "target_vertices": self.target_vertices,
            "lossy": self.lossy,
            "reasons": list(self.reasons),
        }


def _rings_of(geometry: dict[str, Any]) -> list[tuple[list[Point], list[list[Point]]]]:
    geometry_type = geometry.get("type")
    if geometry_type == "polygon":
        return [
            (
                [tuple(point) for point in geometry.get("points") or []],
                [
                    [tuple(point) for point in hole]
                    for hole in geometry.get("holes") or []
                ],
            )
        ]
    if geometry_type == "multi_polygon":
        return [
            (
                [tuple(point) for point in polygon.get("points") or []],
                [
                    [tuple(point) for point in hole]
                    for hole in polygon.get("holes") or []
                ],
            )
            for polygon in geometry.get("polygons") or []
        ]
    raise ValueError("geometry must be polygon or multi_polygon")


def _point_in_ring(x: float, y: float, ring: list[Point]) -> bool:
    inside = False
    previous = len(ring) - 1
    for index, (xi, yi) in enumerate(ring):
        xj, yj = ring[previous]
        if (yi > y) != (yj > y) and x < ((xj - xi) * (y - yi)) / (yj - yi) + xi:
            inside = not inside
        previous = index
    return inside


def rasterize_region_geometry(
    geometry: dict[str, Any], width: int, height: int
) -> dict[str, Any]:
    if width <= 0 or height <= 0:
        raise ValueError("image dimensions must be positive")
    components = _rings_of(geometry)
    if not components or any(len(outer) < 3 for outer, _ in components):
        raise ValueError("region geometry must contain a valid outer ring")
    pixels = bytearray(width * height)
    for y in range(height):
        normalized_y = (y + 0.5) / height
        for x in range(width):
            normalized_x = (x + 0.5) / width
            for outer, holes in components:
                if not _point_in_ring(normalized_x, normalized_y, outer):
                    continue
                if any(
                    _point_in_ring(normalized_x, normalized_y, hole) for hole in holes
                ):
                    continue
                pixels[y * width + x] = 1
                break
    rle = encode_coco_rle(pixels, width, height)
    if not any(pixels):
        raise ValueError("conversion produced an empty mask")
    return rle


def _component_stats(
    pixels: bytearray, width: int, height: int, *, foreground: bool
) -> tuple[int, int]:
    parent: list[int] = []
    rank: list[int] = []
    touches_boundary: list[bool] = []
    rows: list[list[tuple[int, int, int]]] = []
    area = 0

    def add_node(touches: bool) -> int:
        node = len(parent)
        parent.append(node)
        rank.append(0)
        touches_boundary.append(touches)
        return node

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        if rank[left_root] < rank[right_root]:
            left_root, right_root = right_root, left_root
        parent[right_root] = left_root
        touches_boundary[left_root] = (
            touches_boundary[left_root] or touches_boundary[right_root]
        )
        if rank[left_root] == rank[right_root]:
            rank[left_root] += 1

    for y in range(height):
        spans: list[tuple[int, int, int]] = []
        x = 0
        while x < width:
            matches = bool(pixels[y * width + x]) == foreground
            if not matches:
                x += 1
                continue
            start = x
            while x + 1 < width and bool(pixels[y * width + x + 1]) == foreground:
                x += 1
            end = x
            area += end - start + 1
            spans.append(
                (
                    start,
                    end,
                    add_node(y in {0, height - 1} or start == 0 or end == width - 1),
                )
            )
            x += 1
        if rows:
            previous = rows[-1]
            left = right = 0
            while left < len(previous) and right < len(spans):
                previous_start, previous_end, previous_node = previous[left]
                current_start, current_end, current_node = spans[right]
                if previous_end < current_start:
                    left += 1
                elif current_end < previous_start:
                    right += 1
                else:
                    union(previous_node, current_node)
                    if previous_end <= current_end:
                        left += 1
                    if current_end <= previous_end:
                        right += 1
        rows.append(spans)

    roots = {find(node) for node in range(len(parent))}
    if foreground:
        return area, len(roots)
    holes = sum(1 for root in roots if not touches_boundary[find(root)])
    return area, holes


def analyze_mask(rle: dict[str, Any]) -> MaskShapeStats:
    height, width, _ = validate_coco_rle(rle)
    pixels = decode_coco_rle(rle)
    area, components = _component_stats(pixels, width, height, foreground=True)
    _, holes = _component_stats(pixels, width, height, foreground=False)
    return MaskShapeStats(area=area, components=components, holes=holes)


def _boundary_edges(pixels: bytearray, width: int, height: int) -> list[DirectedEdge]:
    def solid(x: int, y: int) -> bool:
        return 0 <= x < width and 0 <= y < height and bool(pixels[y * width + x])

    edges: list[DirectedEdge] = []
    for y in range(height):
        for x in range(width):
            if not solid(x, y):
                continue
            if not solid(x, y - 1):
                edges.append(((x, y), (x + 1, y)))
            if not solid(x + 1, y):
                edges.append(((x + 1, y), (x + 1, y + 1)))
            if not solid(x, y + 1):
                edges.append(((x + 1, y + 1), (x, y + 1)))
            if not solid(x - 1, y):
                edges.append(((x, y + 1), (x, y)))
    return edges


def _direction(edge: DirectedEdge) -> int:
    (from_x, from_y), (to_x, to_y) = edge
    delta_x = to_x - from_x
    delta_y = to_y - from_y
    if delta_x == 1:
        return 0
    if delta_y == 1:
        return 1
    if delta_x == -1:
        return 2
    return 3


def _trace_boundary_rings(
    pixels: bytearray, width: int, height: int
) -> list[list[PixelPoint]]:
    edges = _boundary_edges(pixels, width, height)
    outgoing: dict[PixelPoint, list[int]] = {}
    for index, edge in enumerate(edges):
        outgoing.setdefault(edge[0], []).append(index)
    used = bytearray(len(edges))
    rings: list[list[PixelPoint]] = []
    turn_priority = [1, 0, 3, 2]

    for start_index, start in enumerate(edges):
        if used[start_index]:
            continue
        ring = [start[0]]
        current_index = start_index
        for _ in range(len(edges) + 1):
            current = edges[current_index]
            used[current_index] = 1
            if current[1] == start[0]:
                break
            ring.append(current[1])
            candidates = [
                index for index in outgoing.get(current[1], []) if not used[index]
            ]
            if not candidates:
                raise ValueError("mask boundary is open")
            current_direction = _direction(current)
            candidates.sort(
                key=lambda index: turn_priority.index(
                    (_direction(edges[index]) - current_direction + 4) % 4
                )
            )
            current_index = candidates[0]
        if len(ring) >= 4:
            rings.append(ring)
    return rings


def _signed_area(ring: list[Point]) -> float:
    return (
        sum(
            ring[index][0] * ring[(index + 1) % len(ring)][1]
            - ring[(index + 1) % len(ring)][0] * ring[index][1]
            for index in range(len(ring))
        )
        / 2
    )


def mask_to_region_geometry(rle: dict[str, Any]) -> dict[str, Any]:
    height, width, _ = validate_coco_rle(rle)
    pixels = decode_coco_rle(rle)
    rings = _trace_boundary_rings(pixels, width, height)
    outers = [ring for ring in rings if _signed_area(ring) > 0]
    holes = [ring for ring in rings if _signed_area(ring) < 0]
    if not outers:
        raise ValueError("empty mask cannot be converted to polygon")

    polygons: list[dict[str, Any]] = [
        {
            "type": "polygon",
            "points": [[x / width, y / height] for x, y in outer],
            "holes": [],
        }
        for outer in outers
    ]
    for hole in holes:
        normalized = [[x / width, y / height] for x, y in hole]
        sample_x, sample_y = normalized[0]
        owner = next(
            (
                polygon
                for polygon in polygons
                if _point_in_ring(
                    sample_x,
                    sample_y,
                    [tuple(point) for point in polygon["points"]],
                )
            ),
            None,
        )
        if owner is None:
            raise ValueError("mask hole has no containing component")
        owner["holes"].append(normalized)
    for polygon in polygons:
        if not polygon["holes"]:
            polygon.pop("holes")
    if len(polygons) == 1:
        return polygons[0]
    return {"type": "multi_polygon", "polygons": polygons}


def _region_stats(geometry: dict[str, Any]) -> tuple[int, int, int]:
    rings = _rings_of(geometry)
    return (
        len(rings),
        sum(len(holes) for _, holes in rings),
        sum(len(outer) + sum(len(hole) for hole in holes) for outer, holes in rings),
    )


def _pixel_delta(left: dict[str, Any], right: dict[str, Any]) -> tuple[int, int]:
    left_pixels = decode_coco_rle(left)
    right_pixels = decode_coco_rle(right)
    if len(left_pixels) != len(right_pixels):
        raise ValueError("mask sizes must match")
    changed = 0
    dropped = 0
    for before, after in zip(left_pixels, right_pixels, strict=True):
        if bool(before) != bool(after):
            changed += 1
        if before and not after:
            dropped += 1
    return changed, dropped


def region_to_mask_conversion(
    geometry: dict[str, Any], width: int, height: int
) -> tuple[dict[str, Any], ConversionMetrics]:
    rle = rasterize_region_geometry(geometry, width, height)
    mask_stats = analyze_mask(rle)
    source_components, source_holes, source_vertices = _region_stats(geometry)
    topology_changed = (
        source_components != mask_stats.components or source_holes != mask_stats.holes
    )
    return rle, ConversionMetrics(
        source_area_pixels=mask_stats.area,
        target_area_pixels=mask_stats.area,
        changed_pixels=0,
        source_components=source_components,
        target_components=mask_stats.components,
        source_holes=source_holes,
        target_holes=mask_stats.holes,
        source_vertices=source_vertices,
        target_vertices=0,
        lossy=topology_changed,
        reasons=("topology_changed_on_rasterization",) if topology_changed else (),
    )


def mask_to_region_conversion(
    rle: dict[str, Any],
) -> tuple[dict[str, Any], ConversionMetrics]:
    height, width, _ = validate_coco_rle(rle)
    source_stats = analyze_mask(rle)
    geometry = mask_to_region_geometry(rle)
    round_trip = rasterize_region_geometry(geometry, width, height)
    target_stats = analyze_mask(round_trip)
    changed, dropped = _pixel_delta(rle, round_trip)
    target_components, target_holes, target_vertices = _region_stats(geometry)
    reasons: tuple[str, ...] = ("pixel_xor_changed",) if changed else ()
    if dropped:
        reasons = (*reasons, "source_pixels_dropped")
    return geometry, ConversionMetrics(
        source_area_pixels=source_stats.area,
        target_area_pixels=target_stats.area,
        changed_pixels=changed,
        source_components=source_stats.components,
        target_components=target_components,
        source_holes=source_stats.holes,
        target_holes=target_holes,
        source_vertices=0,
        target_vertices=target_vertices,
        lossy=changed > 0,
        reasons=reasons,
    )


def mask_to_bbox_conversion(
    rle: dict[str, Any], *, video_frame_index: int | None = None
) -> tuple[dict[str, Any], ConversionMetrics]:
    height, width, _ = validate_coco_rle(rle)
    source_stats = analyze_mask(rle)
    bbox = coco_rle_bbox_norm(rle)
    if not bbox:
        raise ValueError("empty mask cannot be converted to bbox")
    min_x = round(float(bbox["x"]) * width)
    min_y = round(float(bbox["y"]) * height)
    box_width = round(float(bbox["w"]) * width)
    box_height = round(float(bbox["h"]) * height)
    target_area = box_width * box_height
    changed = target_area - source_stats.area
    geometry: dict[str, Any] = {
        "type": "video_bbox" if video_frame_index is not None else "bbox",
        **({"frame_index": video_frame_index} if video_frame_index is not None else {}),
        **bbox,
    }
    if min_x < 0 or min_y < 0 or changed < 0:
        raise ValueError("invalid tight mask bbox")
    return geometry, ConversionMetrics(
        source_area_pixels=source_stats.area,
        target_area_pixels=target_area,
        changed_pixels=changed,
        source_components=source_stats.components,
        target_components=1,
        source_holes=source_stats.holes,
        target_holes=0,
        source_vertices=0,
        target_vertices=4,
        lossy=changed > 0,
        reasons=("bbox_includes_background",) if changed else (),
    )

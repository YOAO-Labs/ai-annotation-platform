"""Partition a simple ring along an open polyline without a geometry dependency.

The two crossings split the Jordan boundary into complementary arcs. The simple
interior cut joins those arcs in opposite directions, so the two resulting simple
rings partition the source. Check every premise and conserved signed area; never
accept caller-supplied result geometry. Tests use GEOS as an independent oracle.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

Point = tuple[float, float]
EPS = 1e-12
MAX_INTERSECTION_CHECKS = 1_000_000


class PolygonSliceGeometryError(ValueError):
    pass


def _reject(message: str) -> None:
    raise PolygonSliceGeometryError(message)


def _cross(a: Point, b: Point) -> float:
    return a[0] * b[1] - a[1] * b[0]


def _sub(a: Point, b: Point) -> Point:
    return a[0] - b[0], a[1] - b[1]


def _same(a: Point, b: Point) -> bool:
    return abs(a[0] - b[0]) <= EPS and abs(a[1] - b[1]) <= EPS


def _lerp(a: Point, b: Point, t: float) -> Point:
    return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t


def signed_area(points: list[Point]) -> float:
    return math.fsum(_cross(a, b) for a, b in zip(points, points[1:] + points[:1])) / 2


def _on_segment(p: Point, a: Point, b: Point) -> bool:
    return (
        abs(_cross(_sub(p, a), _sub(b, a))) <= EPS
        and min(a[0], b[0]) - EPS <= p[0] <= max(a[0], b[0]) + EPS
        and min(a[1], b[1]) - EPS <= p[1] <= max(a[1], b[1]) + EPS
    )


def _intersection(a: Point, b: Point, c: Point, d: Point):
    """None, a point's segment parameters, or 'overlap' (positive length)."""
    r, s, ca = _sub(b, a), _sub(d, c), _sub(c, a)
    denominator = _cross(r, s)
    if abs(denominator) <= EPS:
        if abs(_cross(ca, r)) > EPS:
            return None
        axis = 0 if abs(r[0]) >= abs(r[1]) else 1
        if abs(r[axis]) <= EPS:
            _reject("切线或边界包含重复点")
        lo, hi = sorted(((c[axis] - a[axis]) / r[axis], (d[axis] - a[axis]) / r[axis]))
        lo, hi = max(0.0, lo), min(1.0, hi)
        if hi < lo - EPS:
            return None
        if hi - lo > EPS:
            return "overlap"
        p = _lerp(a, b, lo)
        other_axis = 0 if abs(s[0]) >= abs(s[1]) else 1
        if abs(s[other_axis]) <= EPS:
            _reject("切线或边界包含重复点")
        return lo, (p[other_axis] - c[other_axis]) / s[other_axis]
    t, u = _cross(ca, s) / denominator, _cross(ca, r) / denominator
    if -EPS <= t <= 1 + EPS and -EPS <= u <= 1 + EPS:
        return max(0.0, min(1.0, t)), max(0.0, min(1.0, u))
    return None


def _simple(points: list[Point], *, closed: bool) -> None:
    edges = list(zip(points, points[1:] + (points[:1] if closed else [])))
    ordered = sorted(
        enumerate(edges), key=lambda item: min(item[1][0][0], item[1][1][0])
    )
    active: list[tuple[int, tuple[Point, Point]]] = []
    checks = 0
    for i, (a, b) in ordered:
        if _same(a, b):
            _reject("切线或边界包含重复点")
        active = [
            (j, edge)
            for j, edge in active
            if max(edge[0][0], edge[1][0]) >= min(a[0], b[0]) - EPS
        ]
        for j, (c, d) in active:
            checks += 1
            if checks > MAX_INTERSECTION_CHECKS:
                _reject("边界过于复杂，超出切割校验预算")
            if (
                max(a[1], b[1]) < min(c[1], d[1]) - EPS
                or max(c[1], d[1]) < min(a[1], b[1]) - EPS
            ):
                continue
            hit = _intersection(a, b, c, d)
            adjacent = abs(i - j) == 1 or (closed and {i, j} == {0, len(edges) - 1})
            if hit is not None and (not adjacent or hit == "overlap"):
                _reject("切线或边界不能自交、回折或重叠")
        active.append((i, (a, b)))


def _location(point: Point, ring: list[Point]) -> int:
    """-1 outside, 0 boundary, 1 inside."""
    inside = False
    x, y = point
    for a, b in zip(ring, ring[1:] + ring[:1]):
        if _on_segment(point, a, b):
            return 0
        if (a[1] > y) != (b[1] > y) and x < (b[0] - a[0]) * (y - a[1]) / (
            b[1] - a[1]
        ) + a[0]:
            inside = not inside
    return 1 if inside else -1


def _points(value, *, ring: bool = False) -> list[Point]:
    if not isinstance(value, (list, tuple)):
        _reject("切线和边界必须是归一化坐标列表")
    result: list[Point] = []
    for point in value:
        if (
            not isinstance(point, (list, tuple))
            or len(point) != 2
            or any(
                isinstance(v, bool)
                or not isinstance(v, (int, float))
                or not math.isfinite(v)
                or not 0 <= v <= 1
                for v in point
            )
        ):
            _reject("切线和边界必须是有限的归一化坐标")
        result.append((float(point[0]), float(point[1])))
    if ring and len(result) > 1 and _same(result[0], result[-1]):
        result.pop()
    return result


@dataclass(frozen=True)
class _Hit:
    path: float
    boundary: float
    point: Point


def _at(points: list[Point], position: float) -> Point:
    index = min(int(position), len(points) - 2)
    return _lerp(points[index], points[index + 1], position - index)


def _arc(ring: list[Point], start: _Hit, end: _Hit) -> list[Point]:
    stop = end.boundary
    if stop <= start.boundary:
        stop += len(ring)
    return [
        start.point,
        *[
            ring[index % len(ring)]
            for index in range(math.floor(start.boundary) + 1, math.ceil(stop))
        ],
        end.point,
    ]


def _centroid(points: list[Point]) -> Point:
    area6 = signed_area(points) * 6
    return tuple(
        math.fsum(
            (a[axis] + b[axis]) * _cross(a, b)
            for a, b in zip(points, points[1:] + points[:1])
        )
        / area6
        for axis in (0, 1)
    )


def slice_polygon(geometry: dict, cut_path) -> tuple[dict, dict]:
    if geometry.get("type") != "polygon" or geometry.get("holes"):
        _reject("切割仅支持不含孔洞的单外环多边形")
    ring, path = _points(geometry.get("points"), ring=True), _points(cut_path)
    if len(ring) < 3 or not 2 <= len(path) <= 256:
        _reject("多边形至少需要三个点；切线需要 2–256 个点")
    _simple(ring, closed=True)
    _simple(path, closed=False)
    source_area = abs(signed_area(ring))
    if source_area <= EPS:
        _reject("来源多边形面积为零")
    if _location(path[0], ring) == 1 or _location(path[-1], ring) == 1:
        _reject("切线端点应在对象外或边界上")
    if (len(path) - 1) * len(ring) > MAX_INTERSECTION_CHECKS:
        _reject("边界过于复杂，超出切割校验预算")
    hits: list[_Hit] = []
    for i, (a, b) in enumerate(zip(path, path[1:])):
        for j, (c, d) in enumerate(zip(ring, ring[1:] + ring[:1])):
            hit = _intersection(a, b, c, d)
            if hit == "overlap":
                _reject("切线不能沿边界重合")
            if hit is None:
                continue
            t, u = hit
            hits.append(_Hit(i + t, (j + u) % len(ring), _lerp(a, b, t)))
    hits.sort(key=lambda hit: hit.path)
    unique: list[_Hit] = []
    for hit in hits:
        if not unique or abs(hit.path - unique[-1].path) > EPS:
            unique.append(hit)
    if len(unique) != 2:
        _reject("切线必须恰好穿越边界两次，不能相切或多次进出")
    start, end = unique
    stops = sorted(
        {
            0.0,
            float(len(path) - 1),
            *map(float, range(1, len(path) - 1)),
            start.path,
            end.path,
        }
    )
    for left, right in zip(stops, stops[1:]):
        if right - left <= EPS:
            continue
        midpoint = (left + right) / 2
        expected = 1 if start.path < midpoint < end.path else -1
        if _location(_at(path, midpoint), ring) != expected:
            _reject("切线必须穿越边界，不能相切或沿边界移动")
    interior = [
        start.point,
        *[
            path[index]
            for index in range(math.floor(start.path) + 1, math.ceil(end.path))
        ],
        end.point,
    ]
    first = _arc(ring, start, end) + list(reversed(interior))[1:-1]
    second = _arc(ring, end, start) + interior[1:-1]
    for result in (first, second):
        _simple(result, closed=True)
        if len(result) < 3 or abs(signed_area(result)) <= EPS:
            _reject("切割不能产生空区域或零面积区域")
        if signed_area(result) * signed_area(ring) <= 0:
            _reject("切割结果方向不一致")
    areas = [abs(signed_area(first)), abs(signed_area(second))]
    if abs(sum(areas) - source_area) > max(1e-10, source_area * 1e-8):
        _reject("切割结果未保持来源面积")
    if areas[1] > areas[0] + EPS or (
        abs(areas[0] - areas[1]) <= EPS and _centroid(second) < _centroid(first)
    ):
        first, second = second, first
    return tuple(
        {"type": "polygon", "points": [list(point) for point in result]}
        for result in (first, second)
    )

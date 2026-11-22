import type { Annotation, Geometry, PolygonGeometry } from "@/types";
import type { Pt } from "../../polygonGeom";
import { validatePolygonPartition } from "./polygonOps";

const EPS = 1e-12;
const MAX_INTERSECTION_CHECKS = 1_000_000;
export const POLYGON_SLICE_POINT_LIMIT = 256;
const cross = (a: Pt, b: Pt) => a[0] * b[1] - a[1] * b[0];
const sub = (a: Pt, b: Pt): Pt => [a[0] - b[0], a[1] - b[1]];
const same = (a: Pt, b: Pt) => Math.abs(a[0] - b[0]) <= EPS && Math.abs(a[1] - b[1]) <= EPS;
const lerp = (a: Pt, b: Pt, t: number): Pt => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
export const sliceSignedArea = (points: Pt[]) =>
  points.reduce((sum, a, i) => sum + cross(a, points[(i + 1) % points.length]), 0) / 2;
const reject = (message: string): never => {
  throw new Error(message);
};

function intersection(a: Pt, b: Pt, c: Pt, d: Pt): [number, number] | "overlap" | null {
  const r = sub(b, a),
    s = sub(d, c),
    ca = sub(c, a);
  const denominator = cross(r, s);
  if (Math.abs(denominator) <= EPS) {
    if (Math.abs(cross(ca, r)) > EPS) return null;
    const axis = Math.abs(r[0]) >= Math.abs(r[1]) ? 0 : 1;
    if (Math.abs(r[axis]) <= EPS) return reject("切线或边界包含重复点");
    const ts = [(c[axis] - a[axis]) / r[axis], (d[axis] - a[axis]) / r[axis]].sort((x, y) => x - y);
    const lo = Math.max(0, ts[0]),
      hi = Math.min(1, ts[1]);
    if (hi < lo - EPS) return null;
    if (hi - lo > EPS) return "overlap";
    const point = lerp(a, b, lo),
      other = Math.abs(s[0]) >= Math.abs(s[1]) ? 0 : 1;
    if (Math.abs(s[other]) <= EPS) return reject("切线或边界包含重复点");
    return [lo, (point[other] - c[other]) / s[other]];
  }
  const t = cross(ca, s) / denominator,
    u = cross(ca, r) / denominator;
  return t >= -EPS && t <= 1 + EPS && u >= -EPS && u <= 1 + EPS
    ? [Math.max(0, Math.min(1, t)), Math.max(0, Math.min(1, u))]
    : null;
}

function simple(points: Pt[], closed: boolean) {
  const edges = points
    .slice(0, closed ? undefined : -1)
    .map((a, i) => ({ a, b: points[(i + 1) % points.length], i }));
  const ordered = [...edges].sort((a, b) => Math.min(a.a[0], a.b[0]) - Math.min(b.a[0], b.b[0]));
  let active: typeof edges = [],
    checks = 0;
  for (const edge of ordered) {
    const { a, b, i } = edge;
    if (same(a, b)) reject("切线或边界包含重复点");
    active = active.filter(({ a: c, b: d }) => Math.max(c[0], d[0]) >= Math.min(a[0], b[0]) - EPS);
    for (const { a: c, b: d, i: j } of active) {
      if (++checks > MAX_INTERSECTION_CHECKS) reject("边界过于复杂，超出切割校验预算");
      if (
        Math.max(a[1], b[1]) < Math.min(c[1], d[1]) - EPS ||
        Math.max(c[1], d[1]) < Math.min(a[1], b[1]) - EPS
      )
        continue;
      const hit = intersection(a, b, c, d);
      const adjacent =
        Math.abs(i - j) === 1 ||
        (closed && Math.min(i, j) === 0 && Math.max(i, j) === edges.length - 1);
      if (hit !== null && (!adjacent || hit === "overlap"))
        reject("切线或边界不能自交、回折或重叠");
    }
    active.push(edge);
  }
}

function location(point: Pt, ring: Pt[]): -1 | 0 | 1 {
  let inside = false;
  const [x, y] = point;
  for (let i = 0; i < ring.length; i++) {
    const a = ring[i],
      b = ring[(i + 1) % ring.length];
    if (
      Math.abs(cross(sub(point, a), sub(b, a))) <= EPS &&
      x >= Math.min(a[0], b[0]) - EPS &&
      x <= Math.max(a[0], b[0]) + EPS &&
      y >= Math.min(a[1], b[1]) - EPS &&
      y <= Math.max(a[1], b[1]) + EPS
    )
      return 0;
    if (a[1] > y !== b[1] > y && x < ((b[0] - a[0]) * (y - a[1])) / (b[1] - a[1]) + a[0])
      inside = !inside;
  }
  return inside ? 1 : -1;
}

function checkedPoints(points: Pt[], closed = false): Pt[] {
  if (
    points.some(
      (point) =>
        point.length !== 2 ||
        point.some((value) => !Number.isFinite(value) || value < 0 || value > 1),
    )
  )
    reject("切线和边界必须是有限的归一化坐标");
  return closed && points.length > 1 && same(points[0], points[points.length - 1])
    ? points.slice(0, -1)
    : [...points];
}

export function polygonSliceUnavailableReason(
  annotation: Annotation,
  annotations: Annotation[],
): string | null {
  if (
    annotation.id.startsWith("tmp_") ||
    !Number.isInteger(annotation.version) ||
    (annotation.version ?? 0) < 1
  )
    return "请先保存来源对象";
  if (annotation.is_locked) return "请先解锁来源对象";
  if (annotations.some((item) => item.parent_annotation_id === annotation.id))
    return "有活动子对象的标注不能切割";
  if (annotation.geometry?.type !== "polygon" || annotation.geometry.holes?.length)
    return "切割仅支持不含孔洞的单外环多边形";
  return null;
}

type Hit = { path: number; boundary: number; point: Pt };
function arc(ring: Pt[], start: Hit, end: Hit): Pt[] {
  const stop = end.boundary <= start.boundary ? end.boundary + ring.length : end.boundary;
  const points = [start.point];
  for (let i = Math.floor(start.boundary) + 1; i < Math.ceil(stop); i++)
    points.push(ring[i % ring.length]);
  points.push(end.point);
  return points;
}
function centroid(points: Pt[]): Pt {
  const area6 = sliceSignedArea(points) * 6;
  return [0, 1].map(
    (axis) =>
      points.reduce((sum, a, i) => {
        const b = points[(i + 1) % points.length];
        return sum + (a[axis] + b[axis]) * cross(a, b);
      }, 0) / area6,
  ) as Pt;
}

export function slicePolygon(
  geometry: Geometry,
  cutPath: Pt[],
): [PolygonGeometry, PolygonGeometry] {
  if (geometry.type !== "polygon" || geometry.holes?.length)
    return reject("切割仅支持不含孔洞的单外环多边形");
  const ring = checkedPoints(geometry.points, true),
    path = checkedPoints(cutPath);
  if (ring.length < 3 || path.length < 2 || path.length > POLYGON_SLICE_POINT_LIMIT)
    reject("多边形至少需要三个点；切线需要 2–256 个点");
  simple(ring, true);
  simple(path, false);
  if (Math.abs(sliceSignedArea(ring)) <= EPS) reject("来源多边形面积为零");
  if (location(path[0], ring) === 1 || location(path[path.length - 1], ring) === 1)
    reject("切线端点应在对象外或边界上");
  if ((path.length - 1) * ring.length > MAX_INTERSECTION_CHECKS)
    reject("边界过于复杂，超出切割校验预算");
  const hits: Hit[] = [];
  for (let i = 0; i < path.length - 1; i++) {
    for (let j = 0; j < ring.length; j++) {
      const hit = intersection(path[i], path[i + 1], ring[j], ring[(j + 1) % ring.length]);
      if (hit === "overlap") return reject("切线不能沿边界重合");
      if (hit)
        hits.push({
          path: i + hit[0],
          boundary: (j + hit[1]) % ring.length,
          point: lerp(path[i], path[i + 1], hit[0]),
        });
    }
  }
  hits.sort((a, b) => a.path - b.path);
  const unique = hits.filter((hit, i) => !i || Math.abs(hit.path - hits[i - 1].path) > EPS);
  if (unique.length !== 2) reject("切线必须恰好穿越边界两次，不能相切或多次进出");
  const [start, end] = unique;
  const stops = [...new Set([...path.map((_, i) => i), start.path, end.path])].sort(
    (a, b) => a - b,
  );
  for (let i = 0; i < stops.length - 1; i++) {
    const left = stops[i],
      right = stops[i + 1];
    if (right - left <= EPS) continue;
    const midpoint = (left + right) / 2,
      index = Math.min(Math.floor(midpoint), path.length - 2);
    const expected = start.path < midpoint && midpoint < end.path ? 1 : -1;
    if (location(lerp(path[index], path[index + 1], midpoint - index), ring) !== expected)
      reject("切线必须穿越边界，不能相切或沿边界移动");
  }
  const interior = [
    start.point,
    ...path.slice(Math.floor(start.path) + 1, Math.ceil(end.path)),
    end.point,
  ];
  let first = [...arc(ring, start, end), ...interior.slice(1, -1).reverse()];
  let second = [...arc(ring, end, start), ...interior.slice(1, -1)];
  for (const result of [first, second]) {
    simple(result, true);
    if (result.length < 3 || Math.abs(sliceSignedArea(result)) <= EPS)
      reject("切割不能产生空区域或零面积区域");
    if (sliceSignedArea(result) * sliceSignedArea(ring) <= 0) reject("切割结果方向不一致");
  }
  const a = Math.abs(sliceSignedArea(first)),
    b = Math.abs(sliceSignedArea(second));
  const ca = centroid(first),
    cb = centroid(second);
  if (
    b > a + EPS ||
    (Math.abs(a - b) <= EPS && (cb[0] < ca[0] || (cb[0] === ca[0] && cb[1] < ca[1])))
  )
    [first, second] = [second, first];
  const parts: [PolygonGeometry, PolygonGeometry] = [
    { type: "polygon", points: first },
    { type: "polygon", points: second },
  ];
  if (!validatePolygonPartition(geometry, parts)) reject("切割结果未保持来源区域");
  return parts;
}

import type { Geometry } from "@/types";
import { isSelfIntersecting, type Pt } from "../../polygonGeom";
import { samePolygonPoint } from "../../polygonAutoPoints";
import { supportsSingleRingPolygonEdit } from "./geometryEditPolicy";

export type BoundaryHit = { point: Pt; position: number; distance: number };
export type BoundaryArc = { points: Pt[]; length: number };
export type BoundaryPaths = { clockwise: BoundaryArc; counterclockwise: BoundaryArc };
export type BoundaryDirection = keyof BoundaryPaths;

export function traceUnsupportedReason(geometry: Geometry | undefined): string | null {
  if (!supportsSingleRingPolygonEdit(geometry))
    return "仅支持简单单外环多边形；带孔和多外环对象暂不支持边界追踪。";
  if (geometry?.type !== "polygon") return "请选择已保存的多边形边界。";
  const ring = traceRing(geometry.points);
  if (
    ring.length < 3 ||
    ring.some((point) =>
      point.some((value) => !Number.isFinite(value) || value < 0 || value > 1),
    ) ||
    !isSelfIntersecting(ring).ok ||
    Math.abs(signedArea(ring)) < 1e-12
  )
    return "来源边界退化或自相交，无法追踪；请先修正来源对象。";
  return null;
}

export function traceRing(points: Pt[]): Pt[] {
  const ring = points.filter((point, i) => i === 0 || !samePolygonPoint(point, points[i - 1]));
  if (ring.length > 1 && samePolygonPoint(ring[0], ring[ring.length - 1])) return ring.slice(0, -1);
  return ring;
}

const distance = (a: Pt, b: Pt, width: number, height: number) =>
  Math.hypot((a[0] - b[0]) * width, (a[1] - b[1]) * height);

export function pickBoundary(
  ring: Pt[],
  point: Pt,
  width: number,
  height: number,
  threshold = 8,
): BoundaryHit | null {
  if (ring.length < 2 || width <= 0 || height <= 0) return null;
  let vertex: BoundaryHit | null = null;
  ring.forEach((candidate, position) => {
    const d = distance(point, candidate, width, height);
    if (d <= threshold && (!vertex || d < vertex.distance))
      vertex = { point: candidate, position, distance: d };
  });
  if (vertex) return vertex;
  let nearest: BoundaryHit | null = null;
  ring.forEach((a, i) => {
    const b = ring[(i + 1) % ring.length];
    const dx = (b[0] - a[0]) * width;
    const dy = (b[1] - a[1]) * height;
    const norm = dx * dx + dy * dy;
    if (!norm) return;
    const t = Math.max(
      0,
      Math.min(1, ((point[0] - a[0]) * width * dx + (point[1] - a[1]) * height * dy) / norm),
    );
    const projected: Pt = [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
    const d = distance(point, projected, width, height);
    if (d <= threshold && (!nearest || d < nearest.distance))
      nearest = { point: projected, position: (i + t) % ring.length, distance: d };
  });
  return nearest;
}

function signedArea(ring: Pt[]) {
  return (
    ring.reduce((area, a, i) => {
      const b = ring[(i + 1) % ring.length];
      return area + a[0] * b[1] - b[0] * a[1];
    }, 0) / 2
  );
}

function forwardArc(ring: Pt[], start: BoundaryHit, end: BoundaryHit): Pt[] {
  const finish = end.position > start.position ? end.position : end.position + ring.length;
  const result: Pt[] = [start.point];
  for (let i = Math.floor(start.position) + 1; i < finish; i++) result.push(ring[i % ring.length]);
  result.push(end.point);
  return result.filter((point, i) => i === 0 || !samePolygonPoint(point, result[i - 1]));
}

export function boundaryPaths(
  ring: Pt[],
  start: BoundaryHit,
  end: BoundaryHit,
  width: number,
  height: number,
): BoundaryPaths | null {
  if (samePolygonPoint(start.point, end.point)) return null;
  const forward = forwardArc(ring, start, end);
  const backward = forwardArc(ring, end, start).reverse();
  const arc = (points: Pt[]): BoundaryArc => ({
    points,
    length: points.reduce(
      (sum, point, i) => sum + (i ? distance(points[i - 1], point, width, height) : 0),
      0,
    ),
  });
  return signedArea(ring) > 0
    ? { clockwise: arc(forward), counterclockwise: arc(backward) }
    : { clockwise: arc(backward), counterclockwise: arc(forward) };
}

export function appendBoundaryArc(draft: Pt[], arc: Pt[]): Pt[] {
  return draft.length && arc.length && samePolygonPoint(draft[draft.length - 1], arc[0])
    ? arc.slice(1)
    : arc;
}

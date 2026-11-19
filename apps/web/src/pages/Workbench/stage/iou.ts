import polygonClipping, { type MultiPolygon, type Ring } from "polygon-clipping";

export type Geom = { x: number; y: number; w: number; h: number };

/** 用于 IoU 计算的形状：bbox 必填，polygon 可选；提供 polygon 时走精确算法。 */
export type ShapeForIoU = Geom & { polygon?: [number, number][] };

/** axis-aligned IoU on normalized geometry. 0 when 不重叠。 */
export function iou(a: Geom, b: Geom): number {
  const ix1 = Math.max(a.x, b.x);
  const iy1 = Math.max(a.y, b.y);
  const ix2 = Math.min(a.x + a.w, b.x + b.w);
  const iy2 = Math.min(a.y + a.h, b.y + b.h);
  const iw = ix2 - ix1;
  const ih = iy2 - iy1;
  if (iw <= 0 || ih <= 0) return 0;
  const inter = iw * ih;
  const union = a.w * a.h + b.w * b.h - inter;
  return union <= 0 ? 0 : inter / union;
}

// ── polygon helpers ────────────────────────────────────────────────────────

function bboxToRing(g: Geom): Ring {
  return [
    [g.x, g.y],
    [g.x + g.w, g.y],
    [g.x + g.w, g.y + g.h],
    [g.x, g.y + g.h],
    [g.x, g.y],
  ];
}

function pointsToRing(points: [number, number][]): Ring | null {
  if (points.length < 3) return null;
  const ring: Ring = points.map(([x, y]) => [x, y]);
  const first = ring[0];
  const last = ring[ring.length - 1];
  if (first[0] !== last[0] || first[1] !== last[1]) ring.push([first[0], first[1]]);
  return ring;
}

/** shoelace 面积（带闭合环：最后一点 == 首点）。 */
function ringArea(ring: Ring): number {
  let a = 0;
  for (let i = 0; i < ring.length - 1; i++) {
    a += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1];
  }
  return Math.abs(a) / 2;
}

function multiArea(mp: MultiPolygon): number {
  let total = 0;
  for (const poly of mp) {
    // 多边形 = 外环 - 所有内环（孔）。polygon-clipping 已把孔正确组织到 polygon[1..]。
    if (poly.length === 0) continue;
    total += ringArea(poly[0]);
    for (let i = 1; i < poly.length; i++) total -= ringArea(poly[i]);
  }
  return Math.max(0, total);
}

function shapeToMulti(s: ShapeForIoU): MultiPolygon | null {
  if (s.polygon && s.polygon.length >= 3) {
    const ring = pointsToRing(s.polygon);
    return ring ? [[ring]] : null;
  }
  if (s.w <= 0 || s.h <= 0) return null;
  return [[bboxToRing(s)]];
}

/**
 * 形状无关 IoU：
 * - bbox vs bbox：精确（走原 iou()）
 * - 任一为 polygon：用 polygon-clipping 求精确交并
 *
 * polygon-clipping 库失败 / 退化时回落到包围盒近似。
 */
export function iouShape(a: ShapeForIoU, b: ShapeForIoU): number {
  const aIsPoly = !!(a.polygon && a.polygon.length >= 3);
  const bIsPoly = !!(b.polygon && b.polygon.length >= 3);
  if (!aIsPoly && !bIsPoly) return iou(a, b);

  const ma = shapeToMulti(a);
  const mb = shapeToMulti(b);
  if (!ma || !mb) return iou(a, b);

  try {
    const inter = polygonClipping.intersection(ma, mb);
    const interA = multiArea(inter);
    if (interA <= 0) return 0;
    const aArea = multiArea(ma);
    const bArea = multiArea(mb);
    const unionA = aArea + bArea - interA;
    return unionA <= 0 ? 0 : interA / unionA;
  } catch {
    return iou(a, b);
  }
}

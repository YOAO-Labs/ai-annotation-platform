/**
 * Magic Box 用 bbox helper.
 *
 * 给定 SAM 返回的 polygon 顶点 (归一化坐标 [0..1]), 计算紧凑外接矩形
 * (axis-aligned bounding box).
 */

export interface NormBbox {
  x: number;
  y: number;
  w: number;
  h: number;
}

/**
 * 从 polygon 顶点列表派生紧凑外接矩形.
 * 输入 / 输出均归一化坐标 [0..1]; 顶点不足 1 个时返回 null.
 */
export function tightenBboxFromPolygon(
  points: ReadonlyArray<readonly [number, number]> | ReadonlyArray<number[]>,
): NormBbox | null {
  if (!points || points.length === 0) return null;
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const p of points) {
    if (!p || p.length < 2) continue;
    const x = p[0];
    const y = p[1];
    if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
    if (x < minX) minX = x;
    if (y < minY) minY = y;
    if (x > maxX) maxX = x;
    if (y > maxY) maxY = y;
  }
  if (!Number.isFinite(minX) || maxX <= minX || maxY <= minY) return null;
  // clamp 到 [0, 1] 避免越界 (SAM polygon 偶发 -0.0001)
  const x = Math.max(0, minX);
  const y = Math.max(0, minY);
  const w = Math.min(1, maxX) - x;
  const h = Math.min(1, maxY) - y;
  if (w <= 0 || h <= 0) return null;
  return { x, y, w, h };
}

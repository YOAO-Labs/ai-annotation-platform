export interface NormBbox {
  x: number;
  y: number;
  w: number;
  h: number;
}

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
  const x = Math.max(0, minX);
  const y = Math.max(0, minY);
  const w = Math.min(1, maxX) - x;
  const h = Math.min(1, maxY) - y;
  if (w <= 0 || h <= 0) return null;
  return { x, y, w, h };
}

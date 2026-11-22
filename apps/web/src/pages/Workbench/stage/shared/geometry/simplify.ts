import type { Pt } from "./polygon";

function perpDistSq(p: Pt, a: Pt, b: Pt): number {
  const dx = b[0] - a[0];
  const dy = b[1] - a[1];
  if (dx === 0 && dy === 0) {
    const ex = p[0] - a[0];
    const ey = p[1] - a[1];
    return ex * ex + ey * ey;
  }
  const t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy);
  const cx = a[0] + t * dx;
  const cy = a[1] + t * dy;
  const ex = p[0] - cx;
  const ey = p[1] - cy;
  return ex * ex + ey * ey;
}

function rdpOpen(points: Pt[], epsilonSq: number): Pt[] {
  if (points.length < 3) return points.slice();
  const keep = new Uint8Array(points.length);
  keep[0] = 1;
  keep[points.length - 1] = 1;
  const stack: Array<[number, number]> = [[0, points.length - 1]];
  while (stack.length) {
    const [lo, hi] = stack.pop()!;
    if (hi - lo < 2) continue;
    let maxIdx = -1;
    let maxDistSq = 0;
    for (let i = lo + 1; i < hi; i++) {
      const d = perpDistSq(points[i], points[lo], points[hi]);
      if (d > maxDistSq) {
        maxDistSq = d;
        maxIdx = i;
      }
    }
    if (maxIdx >= 0 && maxDistSq > epsilonSq) {
      keep[maxIdx] = 1;
      stack.push([lo, maxIdx]);
      stack.push([maxIdx, hi]);
    }
  }
  const out: Pt[] = [];
  for (let i = 0; i < points.length; i++) {
    if (keep[i]) out.push(points[i]);
  }
  return out;
}

/**
 * 简化闭合多边形（隐式闭合）。
 *
 * 实现：把环 ring 拆成两段（[0..mid], [mid..n-1, 0]）分别 RDP，再合并；
 * 保证首末点稳定（避免环裂开成开链）。
 * @param points 原始顶点（归一化 [0,1] 或像素均可，单位与 epsilon 一致）
 * @param epsilon 容忍偏差，单位同 points
 */
export function simplifyPolygon(points: Pt[], epsilon: number): Pt[] {
  const n = points.length;
  if (n < 4 || epsilon <= 0) return points.slice();
  const epsilonSq = epsilon * epsilon;
  let anchor = 0;
  let maxDsq = 0;
  for (let i = 1; i < n; i++) {
    const dx = points[i][0] - points[0][0];
    const dy = points[i][1] - points[0][1];
    const dsq = dx * dx + dy * dy;
    if (dsq > maxDsq) {
      maxDsq = dsq;
      anchor = i;
    }
  }
  if (anchor === 0) return points.slice();
  const first = points.slice(0, anchor + 1);
  const second = points.slice(anchor).concat([points[0]]);
  const r1 = rdpOpen(first, epsilonSq);
  const r2 = rdpOpen(second, epsilonSq);
  const merged = r1.concat(r2.slice(1, -1));
  return merged.length >= 3 ? merged : points.slice();
}

export function epsilonForScale(scale: number, imageDimensionPx: number): number {
  if (scale <= 0 || imageDimensionPx <= 0) return 0;
  return 1 / (scale * imageDimensionPx);
}

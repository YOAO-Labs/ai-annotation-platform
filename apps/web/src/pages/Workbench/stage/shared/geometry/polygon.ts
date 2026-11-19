// 多边形几何辅助：自相交检测、边到点的最近投影、顶点级编辑工具。
// 所有坐标为图像归一化空间 [0,1]。

export type Pt = [number, number];

const EPS = 1e-9;

/** 判断两条线段是否真相交（不算共端点）。 */
function segmentsProperlyIntersect(p1: Pt, p2: Pt, q1: Pt, q2: Pt): boolean {
  const d1 = cross(sub(q2, q1), sub(p1, q1));
  const d2 = cross(sub(q2, q1), sub(p2, q1));
  const d3 = cross(sub(p2, p1), sub(q1, p1));
  const d4 = cross(sub(p2, p1), sub(q2, p1));
  if (
    ((d1 > EPS && d2 < -EPS) || (d1 < -EPS && d2 > EPS)) &&
    ((d3 > EPS && d4 < -EPS) || (d3 < -EPS && d4 > EPS))
  ) {
    return true;
  }
  return false;
}

function sub(a: Pt, b: Pt): Pt {
  return [a[0] - b[0], a[1] - b[1]];
}
function cross(a: Pt, b: Pt): number {
  return a[0] * b[1] - a[1] * b[0];
}

/**
 * 检测多边形（隐式闭合）的边是否自相交。返回首对违规边的索引（若有）。
 * O(n²) 暴力，n 通常 < 50；对工作台用例足够。加载已有 polygon 一次性校验时使用。
 */
export function isSelfIntersecting(points: Pt[]): { ok: boolean; edges?: [number, number] } {
  const n = points.length;
  if (n < 4) return { ok: true };
  for (let i = 0; i < n; i++) {
    const a1 = points[i];
    const a2 = points[(i + 1) % n];
    for (let j = i + 2; j < n; j++) {
      // 跳过相邻边（共顶点不算交）；最后一条边与第一条边相邻
      if (i === 0 && j === n - 1) continue;
      const b1 = points[j];
      const b2 = points[(j + 1) % n];
      if (segmentsProperlyIntersect(a1, a2, b1, b2)) {
        return { ok: false, edges: [i, j] };
      }
    }
  }
  return { ok: true };
}

/**
 * 增量自相交检测：只检查与顶点 `changedIdx` 相邻的 2 条边
 * （边 (changedIdx-1, changedIdx) 与 (changedIdx, changedIdx+1)）是否与
 * 其它非相邻边相交。O(n)，用于顶点拖拽 / 增删时的实时校验。
 *
 * 不检测"非相邻边对"之间是否本身已经相交；调用方应在 polygon 已知非自相交时使用，
 * 或在最终 commit 时配合 `isSelfIntersecting()` 做一次全量兜底。
 */
export function isSelfIntersectingIncremental(
  points: Pt[],
  changedIdx: number,
): { ok: boolean; edges?: [number, number] } {
  const n = points.length;
  if (n < 4) return { ok: true };
  if (changedIdx < 0 || changedIdx >= n) return { ok: true };
  const prev = (changedIdx - 1 + n) % n;
  // 受影响的两条边：edge1 = (prev, changedIdx)，edge2 = (changedIdx, prev+2 mod n)
  const touched: Array<[number, number]> = [
    [prev, changedIdx],
    [changedIdx, (changedIdx + 1) % n],
  ];
  for (const [ia, ib] of touched) {
    const a1 = points[ia];
    const a2 = points[ib];
    for (let j = 0; j < n; j++) {
      const k = (j + 1) % n;
      if (j === ia || k === ia || j === ib || k === ib) continue;
      const b1 = points[j];
      const b2 = points[k];
      if (segmentsProperlyIntersect(a1, a2, b1, b2)) {
        return { ok: false, edges: [ia, j] };
      }
    }
  }
  return { ok: true };
}

/** 点到线段的垂足（不超出端点）+ 距离。 */
export function projectOnSegment(p: Pt, a: Pt, b: Pt): { proj: Pt; t: number; dist: number } {
  const dx = b[0] - a[0];
  const dy = b[1] - a[1];
  const len2 = dx * dx + dy * dy;
  if (len2 < EPS) return { proj: [a[0], a[1]], t: 0, dist: Math.hypot(p[0] - a[0], p[1] - a[1]) };
  let t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / len2;
  t = Math.max(0, Math.min(1, t));
  const proj: Pt = [a[0] + t * dx, a[1] + t * dy];
  return { proj, t, dist: Math.hypot(p[0] - proj[0], p[1] - proj[1]) };
}

/** 找出离点最近的边（隐式闭合环）；返回边起点索引 + 投影点 + 距离。 */
export function nearestEdge(points: Pt[], p: Pt): { edge: number; proj: Pt; dist: number } | null {
  const n = points.length;
  if (n < 3) return null;
  let best: { edge: number; proj: Pt; dist: number } | null = null;
  for (let i = 0; i < n; i++) {
    const a = points[i];
    const b = points[(i + 1) % n];
    const r = projectOnSegment(p, a, b);
    if (!best || r.dist < best.dist) best = { edge: i, proj: r.proj, dist: r.dist };
  }
  return best;
}

/** 在边 i 后插入新顶点 v（即变成 i, v, i+1, ...）。返回新数组。 */
export function insertVertex(points: Pt[], edgeIdx: number, v: Pt): Pt[] {
  const out = points.slice();
  out.splice(edgeIdx + 1, 0, [v[0], v[1]]);
  return out;
}

/** 删除顶点 i。≤3 顶点时拒绝（返回原数组）。 */
export function removeVertex(points: Pt[], idx: number): Pt[] {
  if (points.length <= 3) return points;
  const out = points.slice();
  out.splice(idx, 1);
  return out;
}

/** 移动顶点 i 到新位置（clamp 到 [0,1]）。 */
export function moveVertex(points: Pt[], idx: number, to: Pt): Pt[] {
  const out = points.slice();
  out[idx] = [Math.max(0, Math.min(1, to[0])), Math.max(0, Math.min(1, to[1]))];
  return out;
}

export type Pt = [number, number];

const EPS = 1e-9;

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

export function isSelfIntersecting(points: Pt[]): { ok: boolean; edges?: [number, number] } {
  const n = points.length;
  if (n < 4) return { ok: true };
  for (let i = 0; i < n; i++) {
    const a1 = points[i];
    const a2 = points[(i + 1) % n];
    for (let j = i + 2; j < n; j++) {
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

export function isSelfIntersectingIncremental(
  points: Pt[],
  changedIdx: number,
): { ok: boolean; edges?: [number, number] } {
  const n = points.length;
  if (n < 4) return { ok: true };
  if (changedIdx < 0 || changedIdx >= n) return { ok: true };
  const prev = (changedIdx - 1 + n) % n;
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

export function insertVertex(points: Pt[], edgeIdx: number, v: Pt): Pt[] {
  const out = points.slice();
  out.splice(edgeIdx + 1, 0, [v[0], v[1]]);
  return out;
}

export function removeVertex(points: Pt[], idx: number): Pt[] {
  if (points.length <= 3) return points;
  const out = points.slice();
  out.splice(idx, 1);
  return out;
}

export function moveVertex(points: Pt[], idx: number, to: Pt): Pt[] {
  const out = points.slice();
  out[idx] = [Math.max(0, Math.min(1, to[0])), Math.max(0, Math.min(1, to[1]))];
  return out;
}

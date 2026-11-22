import type { SensorCalibration } from "@/types";

export interface DepthRaster {
  cols: number;
  rows: number;
  cell: number;
  width: number;
  height: number;
  depth: Float32Array;
  x: Float32Array;
  y: Float32Array;
  z: Float32Array;
  u: Float32Array;
  v: Float32Array;
  minDepth: number;
  maxDepth: number;
}

export interface GpuDepthRaster {
  cols: number;
  rows: number;
  depth: Float32Array;
}

function projectOne(
  x: number,
  y: number,
  z: number,
  calib: SensorCalibration,
): { u: number; v: number; depth: number } {
  const { extrinsic: e, intrinsic: k, rect } = calib;
  let c0 = e[0] * x + e[1] * y + e[2] * z + e[3];
  let c1 = e[4] * x + e[5] * y + e[6] * z + e[7];
  let c2 = e[8] * x + e[9] * y + e[10] * z + e[11];
  if (rect) {
    const r0 = rect[0] * c0 + rect[1] * c1 + rect[2] * c2 + rect[3];
    const r1 = rect[4] * c0 + rect[5] * c1 + rect[6] * c2 + rect[7];
    const r2 = rect[8] * c0 + rect[9] * c1 + rect[10] * c2 + rect[11];
    c0 = r0;
    c1 = r1;
    c2 = r2;
  }
  const uu = k[0] * c0 + k[1] * c1 + k[2] * c2;
  const vv = k[3] * c0 + k[4] * c1 + k[5] * c2;
  const ww = k[6] * c0 + k[7] * c1 + k[8] * c2;
  if (ww === 0) return { u: NaN, v: NaN, depth: 0 };
  return { u: uu / ww, v: vv / ww, depth: ww };
}

export function buildDepthRaster(
  positions: Float32Array,
  calib: SensorCalibration,
  width: number,
  height: number,
  cell = 8,
): DepthRaster {
  const cols = Math.max(1, Math.ceil(width / cell));
  const rows = Math.max(1, Math.ceil(height / cell));
  const depth = new Float32Array(cols * rows).fill(Infinity);
  const x = new Float32Array(cols * rows);
  const y = new Float32Array(cols * rows);
  const z = new Float32Array(cols * rows);
  const u = new Float32Array(cols * rows);
  const v = new Float32Array(cols * rows);
  let minDepth = Infinity;
  let maxDepth = -Infinity;

  const n = (positions.length / 3) | 0;
  for (let i = 0; i < n; i++) {
    const px = positions[3 * i];
    const py = positions[3 * i + 1];
    const pz = positions[3 * i + 2];
    const p = projectOne(px, py, pz, calib);
    if (p.depth <= 0) continue;
    if (p.u < 0 || p.u >= width || p.v < 0 || p.v >= height) continue;
    const col = Math.min(cols - 1, Math.floor(p.u / cell));
    const row = Math.min(rows - 1, Math.floor(p.v / cell));
    const c = row * cols + col;
    if (p.depth < depth[c]) {
      depth[c] = p.depth;
      x[c] = px;
      y[c] = py;
      z[c] = pz;
      u[c] = p.u;
      v[c] = p.v;
    }
    if (p.depth < minDepth) minDepth = p.depth;
    if (p.depth > maxDepth) maxDepth = p.depth;
  }

  return { cols, rows, cell, width, height, depth, x, y, z, u, v, minDepth, maxDepth };
}

export function buildGpuDepthRaster(
  positions: Float32Array,
  calib: SensorCalibration,
  width: number,
  height: number,
  cell = 8,
): GpuDepthRaster {
  const cols = Math.max(1, Math.ceil(width / cell));
  const rows = Math.max(1, Math.ceil(height / cell));
  const depth = new Float32Array(cols * rows).fill(1e20);

  const n = (positions.length / 3) | 0;
  for (let i = 0; i < n; i += 1) {
    const p = projectOne(positions[3 * i], positions[3 * i + 1], positions[3 * i + 2], calib);
    if (p.depth <= 0 || p.u < 0 || p.u >= width || p.v < 0 || p.v >= height) continue;
    const col = Math.min(cols - 1, Math.floor(p.u / cell));
    const row = Math.min(rows - 1, Math.floor(p.v / cell));
    const index = row * cols + col;
    if (p.depth < depth[index]) depth[index] = p.depth;
  }

  return { cols, rows, depth };
}

export function sampleDepth(
  raster: DepthRaster,
  u: number,
  v: number,
): { depth: number; point: [number, number, number] } | null {
  const { cols, rows, cell, depth, x, y, z } = raster;
  const col = Math.floor(u / cell);
  const row = Math.floor(v / cell);
  let best = -1;
  let bestDepth = Infinity;
  for (let dr = -1; dr <= 1; dr++) {
    for (let dc = -1; dc <= 1; dc++) {
      const r = row + dr;
      const cc = col + dc;
      if (r < 0 || r >= rows || cc < 0 || cc >= cols) continue;
      const idx = r * cols + cc;
      if (depth[idx] < bestDepth) {
        bestDepth = depth[idx];
        best = idx;
      }
    }
  }
  if (best < 0 || !isFinite(bestDepth)) return null;
  return { depth: bestDepth, point: [x[best], y[best], z[best]] };
}

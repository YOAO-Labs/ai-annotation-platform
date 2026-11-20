/**
 * 点云 → 相机深度栅格(C2 深度联动)。
 *
 * 把点云逐点投到某相机像面(与 colorize/projection 同口径行主序投影),按像素网格(cell)
 * 聚合:每格保留**最近**(depth 最小)的那个点的深度 + 其 3D 坐标 + 投影像素。
 *   - 用途 1:相机图 hover → sampleDepth 查光标所在格的深度/3D 坐标读数。
 *   - 用途 2:深度热力图 → 遍历非空格,在 (u,v) 画按 depth 着色的点。
 * 建栅格 O(点数)一次(开关/换帧时),hover 查 O(1)。深度 = 相机系 z(intrinsic 末行 [0,0,1])。
 */
import type { SensorCalibration } from "@/types";

export interface DepthRaster {
  cols: number;
  rows: number;
  cell: number;
  width: number;
  height: number;
  /** 每格最近点深度(米);未命中为 Infinity。长度 cols*rows。 */
  depth: Float32Array;
  /** 每格最近点的 3D 坐标(lidar/world 系)与投影像素。 */
  x: Float32Array;
  y: Float32Array;
  z: Float32Array;
  u: Float32Array;
  v: Float32Array;
  /** 命中格的深度范围(供热力图归一化);无命中时 min>max。 */
  minDepth: number;
  maxDepth: number;
}

/** WebGPU camera colorization only needs the nearest depth per low-resolution cell. */
export interface GpuDepthRaster {
  cols: number;
  rows: number;
  depth: Float32Array;
}

/** 单点投影到相机:{u, v, depth};depth = 相机系 z(w)。与 colorize.projectOne 同。 */
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

/** 建相机深度栅格。cell = 网格边长(原图像素),默认 8。 */
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

/**
 * Build the compact depth texture payload used by GPU camera colorization.
 * Empty cells use a finite sentinel so the main thread can upload the array directly.
 */
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

/**
 * 查像素 (u,v) 处最近点的深度与 3D 坐标。先查所在格,空则向外扩 1 环(共 3×3)取最近。
 * 命中返回 {depth, point};无命中(光标在无点区)返回 null。
 */
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

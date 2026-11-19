/**
 * 相机图「2D 框种 3D 框」视锥选点纯几何(与 WebGL / renderer 无关, jsdom 可单测)。
 *
 * 在相机投影视图上拖一个像素矩形，选出投影落在矩形内的点云点。
 * 上层 autofit.psrFromPoints 用这些点拟合 box_3d 初值。
 *
 * ── 选点不建 4 个视锥平面,而是复用前向投影(projection.projectPoints)──
 *   对每个点前向投影到像素 + 相机系深度;保留「相机前方 且 像素落在矩形内」者。O(N),与
 *   overlay 画框线、相机上色、深度栅格同一条投影链(坐标系一致,标定不准时三处一起偏,可调试)。
 *
 * ── 坐标约定 ──────────────────────────────────────────────────────────
 *   - positions: 点云 lidar/world 系 (Z-up, 米), xyz 连续 Float32Array。
 *   - rect: **natural 像素系** (基于内参原图分辨率), 调用方须把显示坐标除以 sx/sy 换算回来。
 *   - calib.extrinsic(16)/rect(16) 行主序 4x4, intrinsic(9) 行主序 3x3 (与 projection.ts 同)。
 */
import * as THREE from "three";

import type { SensorCalibration } from "@/types";

import { projectPoints } from "./projection";

type Vec3 = [number, number, number];

/** 种框矩形(natural 像素系, 已规整为 x0≤x1 / y0≤y1)。 */
export interface SeedRect {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

/** selectPointsInRect 结果:命中点索引 + 各自相机系深度(供 depthGate 取最近簇)。 */
export interface RectSelection {
  indices: number[];
  depths: number[];
}

/** 把任意两角点规整为 x0≤x1 / y0≤y1 的矩形。 */
export function normalizeRect(ax: number, ay: number, bx: number, by: number): SeedRect {
  return {
    x0: Math.min(ax, bx),
    y0: Math.min(ay, by),
    x1: Math.max(ax, bx),
    y1: Math.max(ay, by),
  };
}

/**
 * 选出投影落在矩形内的点(前向投影 + 相机前方 + 像素∈rect)。
 * 返回命中索引(点序号, 非数组下标)与其相机系深度。
 */
export function selectPointsInRect(
  positions: Float32Array,
  rect: SeedRect,
  calib: SensorCalibration,
): RectSelection {
  const n = Math.floor(positions.length / 3);
  // 复用 projectPoints:接受 [x,y,z][] 或 Vector3[];这里直接喂元组数组。
  const tuples: Vec3[] = new Array(n);
  for (let i = 0; i < n; i++) {
    tuples[i] = [positions[i * 3], positions[i * 3 + 1], positions[i * 3 + 2]];
  }
  const { pixels, visible, depths } = projectPoints(tuples, calib);
  const indices: number[] = [];
  const outDepths: number[] = [];
  for (let i = 0; i < n; i++) {
    if (!visible[i]) continue; // 相机后方 / 不可见
    const [u, v] = pixels[i];
    if (u >= rect.x0 && u <= rect.x1 && v >= rect.y0 && v <= rect.y1) {
      indices.push(i);
      outDepths.push(depths[i]);
    }
  }
  return { indices, depths: outDepths };
}

/**
 * 深度门控:视锥内可能前景目标 + 背景墙都被框住。按相机系深度取**最近簇**——
 * 保留 [d_min, d_min + bandMeters] 内的点,排除更远的背景。
 *
 * MVP 用「最近点 + 固定带宽」简化(直方图找密集峰留 Phase 2)。band 默认 8m(覆盖一辆车纵深
 * + 余量);前后严重重叠仍可能混簇,靠收紧矩形重试 + Phase 2 微调兜底。
 */
export function depthGate(sel: RectSelection, opts?: { bandMeters?: number }): number[] {
  if (sel.indices.length === 0) return [];
  const band = opts?.bandMeters ?? 8;
  let dMin = Infinity;
  for (const d of sel.depths) {
    if (d < dMin) dMin = d;
  }
  const cutoff = dMin + band;
  const kept: number[] = [];
  for (let i = 0; i < sel.indices.length; i++) {
    if (sel.depths[i] <= cutoff) kept.push(sel.indices[i]);
  }
  return kept;
}

/**
 * 把点索引收成 Float32Array(len=3K, lidar 系 world 坐标),喂 autofit.psrFromPoints。
 */
export function gatherPoints(positions: Float32Array, indices: number[]): Float32Array {
  const out = new Float32Array(indices.length * 3);
  for (let i = 0; i < indices.length; i++) {
    const p = indices[i] * 3;
    out[i * 3] = positions[p];
    out[i * 3 + 1] = positions[p + 1];
    out[i * 3 + 2] = positions[p + 2];
  }
  return out;
}

/**
 * 空簇 fallback:矩形中心像素 → lidar 系射线(origin + 单位 direction)。
 * 上层沿射线取估计深度放一个默认尺寸框(criteria 2)。
 *
 * 反算链:pixel → inv(intrinsic) 得相机系射线方向 dCam → 经 inv(rect·extrinsic) 转 lidar 系。
 * extrinsic/rect/intrinsic 为行主序,故用 THREE 的 `.set(...)`(行主序入参)构造再求逆;
 * direction 用 transformDirection(只取旋转 + 归一),origin 为相机光心在 lidar 系坐标。
 */
export function centralRay(
  rect: SeedRect,
  calib: SensorCalibration,
): { origin: Vec3; direction: Vec3 } {
  const cx = (rect.x0 + rect.x1) / 2;
  const cy = (rect.y0 + rect.y1) / 2;
  const K = calib.intrinsic;
  const invK = new THREE.Matrix3()
    .set(K[0], K[1], K[2], K[3], K[4], K[5], K[6], K[7], K[8])
    .invert();
  const dCam = new THREE.Vector3(cx, cy, 1).applyMatrix3(invK);
  const e = calib.extrinsic;
  // M = lidar→camera(行主序)。有 rect 矫正时 M = rect · extrinsic。
  const M = new THREE.Matrix4().set(
    e[0],
    e[1],
    e[2],
    e[3],
    e[4],
    e[5],
    e[6],
    e[7],
    e[8],
    e[9],
    e[10],
    e[11],
    e[12],
    e[13],
    e[14],
    e[15],
  );
  if (calib.rect) {
    const r = calib.rect;
    M.premultiply(
      new THREE.Matrix4().set(
        r[0],
        r[1],
        r[2],
        r[3],
        r[4],
        r[5],
        r[6],
        r[7],
        r[8],
        r[9],
        r[10],
        r[11],
        r[12],
        r[13],
        r[14],
        r[15],
      ),
    );
  }
  const invM = M.clone().invert();
  const origin = new THREE.Vector3(0, 0, 0).applyMatrix4(invM);
  const direction = dCam.clone().transformDirection(invM); // 旋转 + 归一
  return {
    origin: [origin.x, origin.y, origin.z],
    direction: [direction.x, direction.y, direction.z],
  };
}

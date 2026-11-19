/**
 * 3D→2D 相机投影纯函数内核 (世界/lidar 点 → 相机像素)。
 *
 * 与 WebGL/renderer/浏览器无关 —— 只做矩阵·向量算术, 可在 jsdom 下单测。
 * 供图像 overlay 把点云 / 3D 框角点投到相机图上画框线 (角点经 box3d.ts
 * `psrToCorners` 得到世界系 8 角点, 再喂本模块)。
 *
 * ── 坐标约定 ──────────────────────────────────────────────────────────
 *   - 输入点为点云/lidar 世界系, Z-up, 单位米。
 *   - extrinsic: lidar→camera 外参, 行主序 4x4 (长度 16), 末行 [0,0,0,1]。
 *   - rect (可选): KITTI 矫正矩阵, 行主序 4x4 (长度 16)。
 *   - intrinsic: 相机内参, 行主序 3x3 (长度 9), 末行通常 [0,0,1]。
 *   - 输出 pixel: 基于内参原图分辨率, 像素原点在左上角 (u 向右, v 向下)。
 *
 * ── 投影链 (与 SUSTechPOINTS `image.js#points3d_homo_to_image2d`
 *    + `util.js#matmul` / `vector4to3` / `vector3_nomalize` 逐字对齐) ──
 *   对世界点 p = [x, y, z]:
 *     1. 齐次化 [x, y, z, 1], p_cam = extrinsic(4x4) · [x,y,z,1]ᵀ
 *        (标准矩阵·列向量; extrinsic 末行 [0,0,0,1] ⇒ p_cam.w = 1)。
 *     2. 若有 rect: p_cam = rect(4x4) · p_cam。
 *     3. 取相机系 xyz = p_cam 前三分量 (丢弃齐次 w, 因 w=1)。
 *     4. [u, v, w] = intrinsic(3x3) · xyz。
 *     5. pixel = [u/w, v/w] (透视除法)。
 *     6. visible = w > 0 (相机前方; 内参末行 [0,0,1] ⇒ w == 相机系深度 xyz.z)。
 *
 * ── 行主序矩阵·向量 ───────────────────────────────────────────────────
 *   result[r] = Σ_i m[r*N + i] · v[i]  (N=4 或 3)。
 *   这正是 SUSTech `matmul(m, x, vl)` 的行向量乘法 (ret^T = m·x^T = m·列向量)。
 *
 *   注意: 此处**手写**行主序乘法, 不借道 THREE.Matrix4 —— 因为
 *   `THREE.Matrix4.elements` 是**列主序**, 把行主序标定直接喂进去会被转置而出错。
 *   手写最安全且与 SUSTech 一致。
 *
 * ── 欧拉角顺序差异 (本期无影响) ───────────────────────────────────────
 *   SUSTech `euler_angle_to_rotate_matrix` 默认 order="ZYX", box3d.ts 用
 *   THREE 的 "XYZ"。两者仅在 pitch/roll 非零时不同; 本平台 7-DoF 只编辑
 *   yaw (rotation[2], 绕 Z), rx=ry=0 时两种顺序都退化为纯 Rz, 角点一致。
 *   故本模块只负责「世界点 → 像素」的投影算术, 不涉及 PSR→角点; 上游用
 *   box3d.ts 出角点即可。投影链本身与欧拉顺序无关。
 */
import type { SensorCalibration } from "@/types";
import * as THREE from "three";

export interface ProjectionResult {
  /** 每个输入点的像素坐标 (基于内参原图分辨率, 像素原点左上)。 */
  pixels: [number, number][];
  /** 该点是否在相机前方 (w > 0)。 */
  visible: boolean[];
  /**
   * 该点的相机系深度 (= 透视除法的 w; 内参末行 [0,0,1] 时即相机系 z, 单位米)。
   * 供视锥选点的深度门控 (frustum.depthGate) 取最近簇用; 相机后方点为负。
   */
  depths: number[];
}

/**
 * 12 条边线的角点索引表 (供 overlay 画框线)。
 * 基于 box3d.ts 的角点顺序: 底面环 0-1-2-3-0、顶面环 4-5-6-7-4、竖棱 0-4/1-5/2-6/3-7。
 */
export const BOX_EDGES: ReadonlyArray<readonly [number, number]> = [
  [0, 1],
  [1, 2],
  [2, 3],
  [3, 0], // 底面
  [4, 5],
  [5, 6],
  [6, 7],
  [7, 4], // 顶面
  [0, 4],
  [1, 5],
  [2, 6],
  [3, 7], // 竖棱
];

/**
 * 行主序 N×N 矩阵 · N 维列向量。
 * result[r] = Σ_i m[r*N + i] · v[i]。与 SUSTech `matmul` 一致。
 */
function matVec(m: ArrayLike<number>, v: ArrayLike<number>, n: number): number[] {
  const out = new Array<number>(n);
  for (let r = 0; r < n; r++) {
    let acc = 0;
    for (let i = 0; i < n; i++) {
      acc += m[r * n + i] * v[i];
    }
    out[r] = acc;
  }
  return out;
}

const INVERSE_EPSILON = 1e-12;

function invertRowMajor3(values: ArrayLike<number>): THREE.Matrix3 | null {
  if (values.length !== 9 || Array.from(values).some((value) => !Number.isFinite(value))) {
    return null;
  }
  const matrix = new THREE.Matrix3().set(
    values[0],
    values[1],
    values[2],
    values[3],
    values[4],
    values[5],
    values[6],
    values[7],
    values[8],
  );
  const determinant = matrix.determinant();
  return Number.isFinite(determinant) && Math.abs(determinant) > INVERSE_EPSILON
    ? matrix.invert()
    : null;
}

function invertRowMajor4(values: ArrayLike<number>): THREE.Matrix4 | null {
  if (values.length !== 16 || Array.from(values).some((value) => !Number.isFinite(value))) {
    return null;
  }
  const matrix = new THREE.Matrix4().set(
    values[0],
    values[1],
    values[2],
    values[3],
    values[4],
    values[5],
    values[6],
    values[7],
    values[8],
    values[9],
    values[10],
    values[11],
    values[12],
    values[13],
    values[14],
    values[15],
  );
  const determinant = matrix.determinant();
  return Number.isFinite(determinant) && Math.abs(determinant) > INVERSE_EPSILON
    ? matrix.invert()
    : null;
}

/** 把世界/lidar 系点投影到相机像素。接受 THREE.Vector3[] 或 [x,y,z][]。 */
export function projectPoints(
  points: ReadonlyArray<THREE.Vector3 | readonly [number, number, number]>,
  calib: SensorCalibration,
): ProjectionResult {
  const { extrinsic, intrinsic, rect } = calib;
  const pixels: [number, number][] = [];
  const visible: boolean[] = [];
  const depths: number[] = [];

  for (const p of points) {
    // 兼容 THREE.Vector3 与 [x,y,z] 元组。
    const x = p instanceof THREE.Vector3 ? p.x : p[0];
    const y = p instanceof THREE.Vector3 ? p.y : p[1];
    const z = p instanceof THREE.Vector3 ? p.z : p[2];

    // 1. 齐次化并应用 extrinsic (4x4 · [x,y,z,1])。
    let cam = matVec(extrinsic, [x, y, z, 1], 4);

    // 2. 可选 rect 矫正 (4x4 · 4-vec)。
    if (rect) {
      cam = matVec(rect, cam, 4);
    }

    // 3. 取相机系 xyz (丢弃齐次 w; extrinsic/rect 末行 [0,0,0,1] ⇒ w=1)。
    const xyz = [cam[0], cam[1], cam[2]];

    // 4. 内参投影 (3x3 · xyz) → [u, v, w]。
    const uvw = matVec(intrinsic, xyz, 3);
    const w = uvw[2];

    // 5. 透视除法。w==0 时像素无意义, 置 NaN。
    if (w === 0) {
      pixels.push([NaN, NaN]);
    } else {
      pixels.push([uvw[0] / w, uvw[1] / w]);
    }

    // 6. 可见性: 相机前方 (w>0)。behind-camera 仍计算像素, 由调用方据 visible 取舍。
    visible.push(w > 0);
    depths.push(w);
  }

  return { pixels, visible, depths };
}

/**
 * 把原图像素按固定投影深度反投影回世界/lidar 坐标。
 *
 * `depth` 与 `projectPoints().depths` 同义，即内参投影后的齐次 w。矩阵输入仍按
 * SensorCalibration 的 row-major 合同读取；不可逆矩阵、非有限值和非正深度返回 null。
 */
export function unprojectPixelAtDepth(
  pixel: readonly [number, number],
  depth: number,
  calibration: SensorCalibration,
): [number, number, number] | null {
  const [u, v] = pixel;
  if (!Number.isFinite(u) || !Number.isFinite(v) || !Number.isFinite(depth) || depth <= 0) {
    return null;
  }

  const inverseIntrinsic = invertRowMajor3(calibration.intrinsic);
  const inverseExtrinsic = invertRowMajor4(calibration.extrinsic);
  if (!inverseIntrinsic || !inverseExtrinsic) return null;

  const camera = new THREE.Vector3(u * depth, v * depth, depth).applyMatrix3(inverseIntrinsic);
  if (![camera.x, camera.y, camera.z].every(Number.isFinite)) return null;

  const point = new THREE.Vector4(camera.x, camera.y, camera.z, 1);
  if (calibration.rect) {
    const inverseRect = invertRowMajor4(calibration.rect);
    if (!inverseRect) return null;
    point.applyMatrix4(inverseRect);
  }
  point.applyMatrix4(inverseExtrinsic);

  if (![point.x, point.y, point.z, point.w].every(Number.isFinite)) return null;
  if (Math.abs(point.w) <= INVERSE_EPSILON) return null;
  return [point.x / point.w, point.y / point.w, point.z / point.w];
}

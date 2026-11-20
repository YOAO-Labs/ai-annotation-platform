/**
 * 数据集 lidar 轴向约定归一化纯几何 (与 WebGL / renderer 无关, jsdom 可单测)。
 *
 * 平台内部一律按 ISO 8855 (=ROS REP-103) 处理: **+X 前 / +Y 左 / +Z 上**, 右手系。
 * 数据集若来自 KITTI / Apollo / SUSTechPOINTS demo 等非 ISO 系, 在加载侧做一次旋转
 * 归一化, 把点云 positions 和相机 extrinsic 都转到 ISO 系下, 上层几何代码 (cameraAnchor /
 * autofit / projection / triview …) 完全不感知 convention 存在。
 *
 * 约定矩阵 R_norm 的物理含义: 「src 坐标 → ISO 坐标」 的 3x3 旋转矩阵。第 i 列 = src 第 i
 * 个基向量在 ISO 系下的坐标。对于点云 `p_iso = R_norm · p_src`; 对于外参
 * `E_iso = E_src · diag(R_normᵀ, 1)` (推导见 ADR-0034 Decision §3)。
 *
 * 详细枚举语义 / 数学契约 / 反向映射 / 嗅探见 ADR-0034 与 plan
 * `docs/plans/archive/2026-06-05-v0.13.11-lidar-axis-convention.md`。
 */
import * as THREE from "three";

import type { Psr } from "./triview";

/**
 * 支持的 lidar 轴向约定。`iso_8855` / `ros_rep103` / `raw` 都是 identity (raw 语义 = 不为
 * 该数据集承诺 ISO, 但行为上不旋转, 兼容历史数据)。其它 4 种是真实非 ISO 系。
 */
export type LidarAxisConvention =
  | "iso_8855"
  | "ros_rep103"
  | "kitti_camera"
  | "opencv_camera"
  | "apollo"
  | "y_forward"
  | "sustechpoints_demo"
  | "raw";

export const LIDAR_AXIS_CONVENTIONS: LidarAxisConvention[] = [
  "iso_8855",
  "ros_rep103",
  "kitti_camera",
  "opencv_camera",
  "apollo",
  "y_forward",
  "sustechpoints_demo",
  "raw",
];

const SNIFF_CONVENTIONS: LidarAxisConvention[] = LIDAR_AXIS_CONVENTIONS.filter((c) => c !== "raw");

/** 3x3 行主序矩阵 (9 个数, m[r*3+c] = 第 r 行第 c 列)。 */
export type Mat3 = readonly [
  number,
  number,
  number,
  number,
  number,
  number,
  number,
  number,
  number,
];

/**
 * 每个 convention 的 R_norm 列向量 (= src 各基轴在 ISO 系下的坐标)。三列拼成矩阵后即是
 * src → ISO 旋转矩阵, 全部右手 (det = +1) 且正交。
 *
 * - kitti_camera / opencv_camera: +X 车右 / +Y 车下 / +Z 车前
 *   → src+X = ISO-Y, src+Y = ISO-Z, src+Z = ISO+X
 * - apollo / y_forward: +X 车右 / +Y 车前 / +Z 天
 *   → src+X = ISO-Y, src+Y = ISO+X, src+Z = ISO+Z
 * - sustechpoints_demo: +X 车左 / +Y 车后 / +Z 天 (实测 third-party/SUSTechPOINTS 示例帧)
 *   → src+X = ISO+Y, src+Y = ISO-X, src+Z = ISO+Z
 */
const SRC_AXES_IN_ISO: Record<
  LidarAxisConvention,
  readonly [
    readonly [number, number, number],
    readonly [number, number, number],
    readonly [number, number, number],
  ]
> = {
  iso_8855: [
    [1, 0, 0],
    [0, 1, 0],
    [0, 0, 1],
  ],
  ros_rep103: [
    [1, 0, 0],
    [0, 1, 0],
    [0, 0, 1],
  ],
  raw: [
    [1, 0, 0],
    [0, 1, 0],
    [0, 0, 1],
  ],
  kitti_camera: [
    [0, -1, 0],
    [0, 0, -1],
    [1, 0, 0],
  ],
  opencv_camera: [
    [0, -1, 0],
    [0, 0, -1],
    [1, 0, 0],
  ],
  apollo: [
    [0, -1, 0],
    [1, 0, 0],
    [0, 0, 1],
  ],
  y_forward: [
    [0, -1, 0],
    [1, 0, 0],
    [0, 0, 1],
  ],
  sustechpoints_demo: [
    [0, 1, 0],
    [-1, 0, 0],
    [0, 0, 1],
  ],
};

/**
 * 取该约定的 R_norm (src → ISO) 3x3 行主序。
 * 列 c 为 src 第 c 基轴在 ISO 系下的坐标; m[r*3+c] = 该列向量的第 r 分量。
 */
export function rotationMatrixFor(c: LidarAxisConvention): Mat3 {
  const [cx, cy, cz] = SRC_AXES_IN_ISO[c];
  return [cx[0], cy[0], cz[0], cx[1], cy[1], cz[1], cx[2], cy[2], cz[2]];
}

/**
 * 就地左乘 R_norm 到点云 positions (Float32Array, 长度 = 3·N)。
 * `iso_8855` / `ros_rep103` / `raw` 是 identity, 但仍执行 (常数开销, 不分支判断, 写法统一)。
 */
export function applyConventionToPositions(
  positions: Float32Array,
  convention: LidarAxisConvention,
): void {
  const m = rotationMatrixFor(convention);
  // 缓存为局部变量, 内层循环避免反复索引。
  const m00 = m[0],
    m01 = m[1],
    m02 = m[2];
  const m10 = m[3],
    m11 = m[4],
    m12 = m[5];
  const m20 = m[6],
    m21 = m[7],
    m22 = m[8];
  const n = positions.length;
  for (let i = 0; i < n; i += 3) {
    const x = positions[i];
    const y = positions[i + 1];
    const z = positions[i + 2];
    positions[i] = m00 * x + m01 * y + m02 * z;
    positions[i + 1] = m10 * x + m11 * y + m12 * z;
    positions[i + 2] = m20 * x + m21 * y + m22 * z;
  }
}

/**
 * 把 src 系下的 lidar → camera 4x4 行主序 extrinsic 转到 ISO 系。
 *
 * 数学契约 (ADR-0034 Decision §3): `E_iso = E_src · diag(R_normᵀ, 1)`。
 *   - 旋转块 (前三列前三行) = E_src.rot · R_normᵀ
 *   - 平移列 (前三行第四列) 不变 (extrinsic 平移是「相机系下表达的 lidar 原点」,
 *     该量与 lidar 系基无关)
 *   - 末行 [0, 0, 0, 1] 不变
 *
 * 返回新数组, 不修改入参。
 */
export function applyConventionToExtrinsic(
  extrinsic: readonly number[],
  convention: LidarAxisConvention,
): number[] {
  const m = rotationMatrixFor(convention);
  // R_normᵀ: 行列互换。
  const t00 = m[0],
    t01 = m[3],
    t02 = m[6];
  const t10 = m[1],
    t11 = m[4],
    t12 = m[7];
  const t20 = m[2],
    t21 = m[5],
    t22 = m[8];
  // 取 E_src 的旋转块 (前 3 行 × 前 3 列) 和平移列。
  const e00 = extrinsic[0],
    e01 = extrinsic[1],
    e02 = extrinsic[2],
    e03 = extrinsic[3];
  const e10 = extrinsic[4],
    e11 = extrinsic[5],
    e12 = extrinsic[6],
    e13 = extrinsic[7];
  const e20 = extrinsic[8],
    e21 = extrinsic[9],
    e22 = extrinsic[10],
    e23 = extrinsic[11];
  // 旋转块 = E_src.rot · R_normᵀ。
  const r00 = e00 * t00 + e01 * t10 + e02 * t20;
  const r01 = e00 * t01 + e01 * t11 + e02 * t21;
  const r02 = e00 * t02 + e01 * t12 + e02 * t22;
  const r10 = e10 * t00 + e11 * t10 + e12 * t20;
  const r11 = e10 * t01 + e11 * t11 + e12 * t21;
  const r12 = e10 * t02 + e11 * t12 + e12 * t22;
  const r20 = e20 * t00 + e21 * t10 + e22 * t20;
  const r21 = e20 * t01 + e21 * t11 + e22 * t21;
  const r22 = e20 * t02 + e21 * t12 + e22 * t22;
  return [
    r00,
    r01,
    r02,
    e03,
    r10,
    r11,
    r12,
    e13,
    r20,
    r21,
    r22,
    e23,
    extrinsic[12] ?? 0,
    extrinsic[13] ?? 0,
    extrinsic[14] ?? 0,
    extrinsic[15] ?? 1,
  ];
}

/** 行主序 Mat3 → three.js Matrix3。THREE.Matrix3.set 接收行主序参数, 内部存列主序。 */
function mat3ToThree(m: Mat3): THREE.Matrix3 {
  const out = new THREE.Matrix3();
  out.set(m[0], m[1], m[2], m[3], m[4], m[5], m[6], m[7], m[8]);
  return out;
}

/**
 * 把 ISO 系下的 PSR 反向映射回源系 (导出 / 与历史数据互操作时用)。
 *
 * 实现:
 *   - center_src = R_normᵀ · center_iso
 *   - 朝向矩阵 R_box (由 rotation XYZ 欧拉构造) 在源系下表达 =
 *     R_normᵀ · R_box · R_norm, 再分解回 XYZ 欧拉
 *   - size 不变 (size 是 box-local 全边长, 与坐标系基无关)
 *
 * 对 identity convention 是恒等映射, 但仍走完整路径 (类型一致 / 不分支)。
 */
export function unapplyConventionToPsr(psr: Psr, convention: LidarAxisConvention): Psr {
  const m = rotationMatrixFor(convention);
  const R = mat3ToThree(m);
  const Rt = R.clone().transpose();
  // center_src = R_normᵀ · center_iso。
  const c = new THREE.Vector3(psr.center[0], psr.center[1], psr.center[2]).applyMatrix3(Rt);
  // R_box (ISO 系下的框朝向 4x4 旋转): 由 XYZ 欧拉构造。
  const eulerIso = new THREE.Euler(psr.rotation[0], psr.rotation[1], psr.rotation[2], "XYZ");
  const Rbox = new THREE.Matrix4().makeRotationFromEuler(eulerIso);
  // R_norm / R_normᵀ 升到 4x4 才能链乘。
  const R4 = new THREE.Matrix4().setFromMatrix3(R);
  const Rt4 = new THREE.Matrix4().setFromMatrix3(Rt);
  // R_box_src = R_normᵀ · R_box · R_norm。
  const RboxSrc = Rt4.clone().multiply(Rbox).multiply(R4);
  const eulerSrc = new THREE.Euler().setFromRotationMatrix(RboxSrc, "XYZ");
  return {
    center: [c.x, c.y, c.z],
    size: [psr.size[0], psr.size[1], psr.size[2]],
    rotation: [eulerSrc.x, eulerSrc.y, eulerSrc.z],
  };
}

/**
 * 把源系 PSR 映射到平台 ISO 系。用于 convention mismatch 重投影:
 * `psr_new = R_new · R_oldᵀ · psr_old` 可拆成先 unapply(old), 再 apply(new)。
 */
export function applyConventionToPsr(psr: Psr, convention: LidarAxisConvention): Psr {
  const m = rotationMatrixFor(convention);
  const R = mat3ToThree(m);
  const Rt = R.clone().transpose();
  const c = new THREE.Vector3(psr.center[0], psr.center[1], psr.center[2]).applyMatrix3(R);
  const eulerSrc = new THREE.Euler(psr.rotation[0], psr.rotation[1], psr.rotation[2], "XYZ");
  const Rbox = new THREE.Matrix4().makeRotationFromEuler(eulerSrc);
  const R4 = new THREE.Matrix4().setFromMatrix3(R);
  const Rt4 = new THREE.Matrix4().setFromMatrix3(Rt);
  const RboxIso = R4.clone().multiply(Rbox).multiply(Rt4);
  const eulerIso = new THREE.Euler().setFromRotationMatrix(RboxIso, "XYZ");
  return {
    center: [c.x, c.y, c.z],
    size: [psr.size[0], psr.size[1], psr.size[2]],
    rotation: [eulerIso.x, eulerIso.y, eulerIso.z],
  };
}

export interface AxisSniffCandidate {
  convention: LidarAxisConvention;
  score: number;
}

export interface AxisSniffResult {
  best: LidarAxisConvention;
  score: number;
  candidates: AxisSniffCandidate[];
}

/**
 * 从 front camera 外参 row 2 推断源系 axis convention。
 * fz 可选以兼容原 fx/fy 调用; 有完整外参时应传 fz, 否则 KITTI/OpenCV (+Z 前) 不可区分。
 */
export function sniffConventionFromForward(fx: number, fy: number, fz = 0): AxisSniffResult | null {
  const norm = Math.hypot(fx, fy, fz);
  if (norm < 1e-9) return null;
  const ux = fx / norm;
  const uy = fy / norm;
  const uz = fz / norm;
  const candidates = SNIFF_CONVENTIONS.map((convention) => {
    const m = rotationMatrixFor(convention);
    const ex = m[0];
    const ey = m[1];
    const ez = m[2];
    const expectedNorm = Math.hypot(ex, ey, ez);
    const score =
      expectedNorm < 1e-9
        ? -1
        : ux * (ex / expectedNorm) + uy * (ey / expectedNorm) + uz * (ez / expectedNorm);
    return { convention, score };
  }).sort((a, b) => b.score - a.score);
  const best = candidates[0];
  return {
    best: best.convention,
    score: best.score,
    candidates,
  };
}

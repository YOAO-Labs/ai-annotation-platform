import * as THREE from "three";

import type { Psr } from "./triview";

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

export function rotationMatrixFor(c: LidarAxisConvention): Mat3 {
  const [cx, cy, cz] = SRC_AXES_IN_ISO[c];
  return [cx[0], cy[0], cz[0], cx[1], cy[1], cz[1], cx[2], cy[2], cz[2]];
}

export function applyConventionToPositions(
  positions: Float32Array,
  convention: LidarAxisConvention,
): void {
  const m = rotationMatrixFor(convention);
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

export function applyConventionToExtrinsic(
  extrinsic: readonly number[],
  convention: LidarAxisConvention,
): number[] {
  const m = rotationMatrixFor(convention);
  const t00 = m[0],
    t01 = m[3],
    t02 = m[6];
  const t10 = m[1],
    t11 = m[4],
    t12 = m[7];
  const t20 = m[2],
    t21 = m[5],
    t22 = m[8];
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

function mat3ToThree(m: Mat3): THREE.Matrix3 {
  const out = new THREE.Matrix3();
  out.set(m[0], m[1], m[2], m[3], m[4], m[5], m[6], m[7], m[8]);
  return out;
}

export function unapplyConventionToPsr(psr: Psr, convention: LidarAxisConvention): Psr {
  const m = rotationMatrixFor(convention);
  const R = mat3ToThree(m);
  const Rt = R.clone().transpose();
  const c = new THREE.Vector3(psr.center[0], psr.center[1], psr.center[2]).applyMatrix3(Rt);
  const eulerIso = new THREE.Euler(psr.rotation[0], psr.rotation[1], psr.rotation[2], "XYZ");
  const Rbox = new THREE.Matrix4().makeRotationFromEuler(eulerIso);
  const R4 = new THREE.Matrix4().setFromMatrix3(R);
  const Rt4 = new THREE.Matrix4().setFromMatrix3(Rt);
  const RboxSrc = Rt4.clone().multiply(Rbox).multiply(R4);
  const eulerSrc = new THREE.Euler().setFromRotationMatrix(RboxSrc, "XYZ");
  return {
    center: [c.x, c.y, c.z],
    size: [psr.size[0], psr.size[1], psr.size[2]],
    rotation: [eulerSrc.x, eulerSrc.y, eulerSrc.z],
  };
}

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

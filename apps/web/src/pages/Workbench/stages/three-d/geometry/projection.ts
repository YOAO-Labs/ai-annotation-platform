import type { SensorCalibration } from "@/types";
import * as THREE from "three";

export interface ProjectionResult {
  pixels: [number, number][];
  visible: boolean[];
  depths: number[];
}

export const BOX_EDGES: ReadonlyArray<readonly [number, number]> = [
  [0, 1],
  [1, 2],
  [2, 3],
  [3, 0],
  [4, 5],
  [5, 6],
  [6, 7],
  [7, 4],
  [0, 4],
  [1, 5],
  [2, 6],
  [3, 7],
];

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

export function projectPoints(
  points: ReadonlyArray<THREE.Vector3 | readonly [number, number, number]>,
  calib: SensorCalibration,
): ProjectionResult {
  const { extrinsic, intrinsic, rect } = calib;
  const pixels: [number, number][] = [];
  const visible: boolean[] = [];
  const depths: number[] = [];

  for (const p of points) {
    const x = p instanceof THREE.Vector3 ? p.x : p[0];
    const y = p instanceof THREE.Vector3 ? p.y : p[1];
    const z = p instanceof THREE.Vector3 ? p.z : p[2];

    let cam = matVec(extrinsic, [x, y, z, 1], 4);

    if (rect) {
      cam = matVec(rect, cam, 4);
    }

    const xyz = [cam[0], cam[1], cam[2]];

    const uvw = matVec(intrinsic, xyz, 3);
    const w = uvw[2];

    if (w === 0) {
      pixels.push([NaN, NaN]);
    } else {
      pixels.push([uvw[0] / w, uvw[1] / w]);
    }

    visible.push(w > 0);
    depths.push(w);
  }

  return { pixels, visible, depths };
}

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

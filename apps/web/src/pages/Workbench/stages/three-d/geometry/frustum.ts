import * as THREE from "three";

import type { SensorCalibration } from "@/types";

import { fitBottom, fitYaw, psrFromPoints } from "./autofit";
import { projectPoints } from "./projection";
import type { Psr } from "./triview";

type Vec3 = [number, number, number];

const MIN_CLUSTER_POINTS = 3;

export interface SeedRect {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

export interface RectSelection {
  indices: number[];
  depths: number[];
}

export function normalizeRect(ax: number, ay: number, bx: number, by: number): SeedRect {
  return {
    x0: Math.min(ax, bx),
    y0: Math.min(ay, by),
    x1: Math.max(ax, bx),
    y1: Math.max(ay, by),
  };
}

export function selectPointsInRect(
  positions: Float32Array,
  rect: SeedRect,
  calib: SensorCalibration,
): RectSelection {
  const n = Math.floor(positions.length / 3);
  const tuples: Vec3[] = new Array(n);
  for (let i = 0; i < n; i++) {
    tuples[i] = [positions[i * 3], positions[i * 3 + 1], positions[i * 3 + 2]];
  }
  const { pixels, visible, depths } = projectPoints(tuples, calib);
  const indices: number[] = [];
  const outDepths: number[] = [];
  for (let i = 0; i < n; i++) {
    if (!visible[i]) continue;
    const [u, v] = pixels[i];
    if (u >= rect.x0 && u <= rect.x1 && v >= rect.y0 && v <= rect.y1) {
      indices.push(i);
      outDepths.push(depths[i]);
    }
  }
  return { indices, depths: outDepths };
}

function selectForegroundCluster({ indices, depths }: RectSelection): number[] {
  if (indices.length < MIN_CLUSTER_POINTS) return [];
  const byDepth = indices.map((_, index) => index).sort((a, b) => depths[a] - depths[b]);
  let start = 0;
  for (let end = 1; end <= byDepth.length; end++) {
    const near = depths[byDepth[end - 1]];
    const maxGap = Math.min(1.5, Math.max(0.75, near * 0.03));
    if (end < byDepth.length && depths[byDepth[end]] - near <= maxGap) continue;
    if (end - start >= MIN_CLUSTER_POINTS) {
      const cluster = new Array<number>(end - start);
      for (let i = start; i < end; i++) cluster[i - start] = indices[byDepth[i]];
      return cluster;
    }
    start = end;
  }
  return [];
}

export function fitCameraSeedBox(
  positions: Float32Array,
  rect: SeedRect,
  calibration: SensorCalibration,
): Psr | null {
  const indices = selectForegroundCluster(selectPointsInRect(positions, rect, calibration));
  if (indices.length === 0) return null;
  const points = gatherPoints(positions, indices);
  return fitBottom(points, fitYaw(points, psrFromPoints(points)));
}

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
  const direction = dCam.clone().transformDirection(invM);
  return {
    origin: [origin.x, origin.y, origin.z],
    direction: [direction.x, direction.y, direction.z],
  };
}

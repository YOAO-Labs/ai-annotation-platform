import * as THREE from "three";
import { PCDLoader } from "three/examples/jsm/loaders/PCDLoader.js";

import { applyConventionToPositions, type LidarAxisConvention } from "./axisConvention";
import { estimateGroundZ } from "./ground";

export interface DecodedPointCloudFrame {
  positions: Float32Array;
  heightColors: Float32Array;
  totalPoints: number;
  renderedPoints: number;
  decimateStride: number;
  viewCenter: [number, number, number];
  viewRadius: number;
  groundZ: number;
}

function createHeightColors(positions: Float32Array): Float32Array {
  const count = positions.length / 3;
  let zMin = Infinity;
  let zMax = -Infinity;
  for (let i = 0; i < count; i += 1) {
    const z = positions[i * 3 + 2];
    if (z < zMin) zMin = z;
    if (z > zMax) zMax = z;
  }
  const span = zMax - zMin || 1;
  const colors = new Float32Array(count * 3);
  const color = new THREE.Color();
  for (let i = 0; i < count; i += 1) {
    const t = (positions[i * 3 + 2] - zMin) / span;
    color.setHSL(0.62 - 0.62 * t, 0.85, 0.45 + 0.15 * t);
    colors[i * 3] = color.r;
    colors[i * 3 + 1] = color.g;
    colors[i * 3 + 2] = color.b;
  }
  return colors;
}

function robustFrame(positions: Float32Array): {
  center: [number, number, number];
  radius: number;
} {
  const count = positions.length / 3;
  if (count === 0) return { center: [0, 0, 0], radius: 10 };
  let sx = 0;
  let sy = 0;
  let sz = 0;
  let sxx = 0;
  let syy = 0;
  let szz = 0;
  for (let i = 0; i < count; i += 1) {
    const x = positions[i * 3];
    const y = positions[i * 3 + 1];
    const z = positions[i * 3 + 2];
    sx += x;
    sy += y;
    sz += z;
    sxx += x * x;
    syy += y * y;
    szz += z * z;
  }
  const mx = sx / count;
  const my = sy / count;
  const mz = sz / count;
  const sd = (sum2: number, mean: number) => Math.sqrt(Math.max(sum2 / count - mean * mean, 0));
  return {
    center: [mx, my, mz],
    radius: Math.max(2.5 * Math.max(sd(sxx, mx), sd(syy, my), sd(szz, mz)), 5),
  };
}

export function decodePointCloudFrame(
  buffer: ArrayBuffer,
  convention: LidarAxisConvention,
  decimateThreshold: number,
): DecodedPointCloudFrame {
  const loaded = new PCDLoader().parse(buffer);
  const sourceGeometry = loaded.geometry;
  const sourcePosition = sourceGeometry.getAttribute("position") as THREE.BufferAttribute;
  const totalPoints = sourcePosition.count;
  const stride = totalPoints > decimateThreshold ? Math.ceil(totalPoints / decimateThreshold) : 1;
  const renderedPoints = stride > 1 ? Math.floor(totalPoints / stride) : totalPoints;
  const positions = new Float32Array(renderedPoints * 3);
  for (let i = 0, j = 0; i < totalPoints && j < renderedPoints; i += stride, j += 1) {
    positions[j * 3] = sourcePosition.getX(i);
    positions[j * 3 + 1] = sourcePosition.getY(i);
    positions[j * 3 + 2] = sourcePosition.getZ(i);
  }
  sourceGeometry.dispose();
  applyConventionToPositions(positions, convention);
  const frame = robustFrame(positions);
  return {
    positions,
    heightColors: createHeightColors(positions),
    totalPoints,
    renderedPoints,
    decimateStride: stride,
    viewCenter: frame.center,
    viewRadius: frame.radius,
    groundZ: estimateGroundZ(positions, renderedPoints),
  };
}

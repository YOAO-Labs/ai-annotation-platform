import * as THREE from "three";

import type { Psr } from "./triview";
import { MIN_SIZE } from "./triview";

type Vec3 = readonly [number, number, number];

const FIT_YAW_MIN_POINTS = 20;

function rotationQuaternions(rotation: Vec3): {
  q: THREE.Quaternion;
  qInv: THREE.Quaternion;
} {
  const q = new THREE.Quaternion().setFromEuler(
    new THREE.Euler(rotation[0], rotation[1], rotation[2], "XYZ"),
  );
  return { q, qInv: q.clone().invert() };
}

function collectInsidePoints(
  positions: Float32Array,
  psr: Psr,
): {
  count: number;
  worldX: Float32Array;
  worldY: Float32Array;
  worldZ: Float32Array;
  localX: Float32Array;
  localY: Float32Array;
  localZ: Float32Array;
} {
  const { center, size } = psr;
  const { qInv } = rotationQuaternions(psr.rotation);
  const hx = size[0] / 2;
  const hy = size[1] / 2;
  const hz = size[2] / 2;
  const n = Math.floor(positions.length / 3);
  const wx = new Float32Array(n);
  const wy = new Float32Array(n);
  const wz = new Float32Array(n);
  const lx = new Float32Array(n);
  const ly = new Float32Array(n);
  const lz = new Float32Array(n);
  const tmp = new THREE.Vector3();
  let count = 0;
  for (let i = 0; i < n; i++) {
    const px = positions[i * 3];
    const py = positions[i * 3 + 1];
    const pz = positions[i * 3 + 2];
    tmp.set(px - center[0], py - center[1], pz - center[2]).applyQuaternion(qInv);
    if (Math.abs(tmp.x) <= hx && Math.abs(tmp.y) <= hy && Math.abs(tmp.z) <= hz) {
      wx[count] = px;
      wy[count] = py;
      wz[count] = pz;
      lx[count] = tmp.x;
      ly[count] = tmp.y;
      lz[count] = tmp.z;
      count++;
    }
  }
  return {
    count,
    worldX: wx.subarray(0, count),
    worldY: wy.subarray(0, count),
    worldZ: wz.subarray(0, count),
    localX: lx.subarray(0, count),
    localY: ly.subarray(0, count),
    localZ: lz.subarray(0, count),
  };
}

function clampSize(s: Vec3): Vec3 {
  return [Math.max(MIN_SIZE, s[0]), Math.max(MIN_SIZE, s[1]), Math.max(MIN_SIZE, s[2])];
}

export function fitSize(positions: Float32Array, psr: Psr, padding: number = 0.05): Psr {
  const inside = collectInsidePoints(positions, psr);
  if (inside.count < 3) {
    return { center: psr.center, size: psr.size, rotation: psr.rotation };
  }
  let minX = inside.localX[0];
  let maxX = minX;
  let minY = inside.localY[0];
  let maxY = minY;
  let minZ = inside.localZ[0];
  let maxZ = minZ;
  for (let i = 1; i < inside.count; i++) {
    const x = inside.localX[i];
    const y = inside.localY[i];
    const z = inside.localZ[i];
    if (x < minX) minX = x;
    else if (x > maxX) maxX = x;
    if (y < minY) minY = y;
    else if (y > maxY) maxY = y;
    if (z < minZ) minZ = z;
    else if (z > maxZ) maxZ = z;
  }
  const cxLocal = (minX + maxX) / 2;
  const cyLocal = (minY + maxY) / 2;
  const czLocal = (minZ + maxZ) / 2;
  const { q } = rotationQuaternions(psr.rotation);
  const offset = new THREE.Vector3(cxLocal, cyLocal, czLocal).applyQuaternion(q);
  const newCenter: Vec3 = [
    psr.center[0] + offset.x,
    psr.center[1] + offset.y,
    psr.center[2] + offset.z,
  ];
  const newSize = clampSize([
    maxX - minX + 2 * padding,
    maxY - minY + 2 * padding,
    maxZ - minZ + 2 * padding,
  ]);
  return { center: newCenter, size: newSize, rotation: psr.rotation };
}

export function fitBottom(positions: Float32Array, psr: Psr): Psr {
  const inside = collectInsidePoints(positions, psr);
  if (inside.count === 0) {
    return { center: psr.center, size: psr.size, rotation: psr.rotation };
  }
  let zMin = inside.worldZ[0];
  for (let i = 1; i < inside.count; i++) {
    if (inside.worldZ[i] < zMin) zMin = inside.worldZ[i];
  }
  const newCz = zMin + psr.size[2] / 2;
  return {
    center: [psr.center[0], psr.center[1], newCz],
    size: psr.size,
    rotation: psr.rotation,
  };
}

export function fitYaw(positions: Float32Array, psr: Psr): Psr {
  const inside = collectInsidePoints(positions, psr);
  if (inside.count < FIT_YAW_MIN_POINTS) {
    return { center: psr.center, size: psr.size, rotation: psr.rotation };
  }
  let sumX = 0;
  let sumY = 0;
  for (let i = 0; i < inside.count; i++) {
    sumX += inside.worldX[i];
    sumY += inside.worldY[i];
  }
  const meanX = sumX / inside.count;
  const meanY = sumY / inside.count;
  let a = 0;
  let b = 0;
  let d = 0;
  for (let i = 0; i < inside.count; i++) {
    const dx = inside.worldX[i] - meanX;
    const dy = inside.worldY[i] - meanY;
    a += dx * dx;
    b += dx * dy;
    d += dy * dy;
  }
  const tr = a + d;
  const det = a * d - b * b;
  const disc = Math.max(0, (tr / 2) * (tr / 2) - det);
  const lambda1 = tr / 2 + Math.sqrt(disc);
  let vx: number;
  let vy: number;
  if (Math.abs(b) > 1e-12) {
    vx = b;
    vy = lambda1 - a;
  } else {
    if (a >= d) {
      vx = 1;
      vy = 0;
    } else {
      vx = 0;
      vy = 1;
    }
  }
  const newYaw = Math.atan2(vy, vx);
  return {
    center: psr.center,
    size: psr.size,
    rotation: [psr.rotation[0], psr.rotation[1], newYaw],
  };
}

export function psrFromPoints(points: Float32Array, padding: number = 0.05): Psr {
  let minX = points[0];
  let maxX = minX;
  let minY = points[1];
  let maxY = minY;
  let minZ = points[2];
  let maxZ = minZ;
  const n = Math.floor(points.length / 3);
  for (let i = 1; i < n; i++) {
    const x = points[i * 3];
    const y = points[i * 3 + 1];
    const z = points[i * 3 + 2];
    if (x < minX) minX = x;
    else if (x > maxX) maxX = x;
    if (y < minY) minY = y;
    else if (y > maxY) maxY = y;
    if (z < minZ) minZ = z;
    else if (z > maxZ) maxZ = z;
  }
  const size = clampSize([
    maxX - minX + 2 * padding,
    maxY - minY + 2 * padding,
    maxZ - minZ + 2 * padding,
  ]);
  return {
    center: [(minX + maxX) / 2, (minY + maxY) / 2, (minZ + maxZ) / 2],
    size,
    rotation: [0, 0, 0],
  };
}

export function fitSizeAndBottom(positions: Float32Array, psr: Psr, padding: number = 0.05): Psr {
  return fitBottom(positions, fitSize(positions, psr, padding));
}

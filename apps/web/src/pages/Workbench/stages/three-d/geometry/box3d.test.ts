import { describe, it, expect } from "vitest";
import * as THREE from "three";
import {
  boxToMatrix4,
  psrToCorners,
  boxAxisWorldDir,
  worldToBox,
  boxLocalClipPlanes,
} from "./box3d";

function sortedTuples(pts: THREE.Vector3[]): number[][] {
  return pts.map((p) => [p.x, p.y, p.z]).sort((a, b) => a[0] - b[0] || a[1] - b[1] || a[2] - b[2]);
}

function expectCornersClose(got: number[][], want: number[][]) {
  expect(got.length).toBe(want.length);
  got.forEach((g, i) => {
    expect(g[0]).toBeCloseTo(want[i][0]);
    expect(g[1]).toBeCloseTo(want[i][1]);
    expect(g[2]).toBeCloseTo(want[i][2]);
  });
}

describe("psrToCorners", () => {
  it("axis-aligned box: 8 corners are the (±l/2, ±w/2, ±h/2) combinations", () => {
    const corners = psrToCorners([0, 0, 0], [2, 4, 6], [0, 0, 0]);
    const want = sortedTuples(
      [
        [-1, -2, -3],
        [1, -2, -3],
        [1, 2, -3],
        [-1, 2, -3],
        [-1, -2, 3],
        [1, -2, 3],
        [1, 2, 3],
        [-1, 2, 3],
      ].map((c) => new THREE.Vector3(c[0], c[1], c[2])),
    );
    expectCornersClose(sortedTuples(corners), want);
  });

  it("yaw 90° about Z swaps x/y half-extents", () => {
    const corners = psrToCorners([0, 0, 0], [2, 4, 6], [0, 0, Math.PI / 2]);

    const xs = corners.map((c) => c.x);
    const ys = corners.map((c) => c.y);
    const zs = corners.map((c) => c.z);
    expect(Math.max(...xs)).toBeCloseTo(2);
    expect(Math.min(...xs)).toBeCloseTo(-2);
    expect(Math.max(...ys)).toBeCloseTo(1);
    expect(Math.min(...ys)).toBeCloseTo(-1);
    expect(Math.max(...zs)).toBeCloseTo(3);
    expect(Math.min(...zs)).toBeCloseTo(-3);

    expect(corners[0].x).toBeCloseTo(2);
    expect(corners[0].y).toBeCloseTo(-1);
    expect(corners[0].z).toBeCloseTo(-3);
  });

  it("center offset translates all corners", () => {
    const center: [number, number, number] = [10, 20, 30];
    const base = psrToCorners([0, 0, 0], [2, 4, 6], [0, 0, 0]);
    const shifted = psrToCorners(center, [2, 4, 6], [0, 0, 0]);
    shifted.forEach((p, i) => {
      expect(p.x).toBeCloseTo(base[i].x + center[0]);
      expect(p.y).toBeCloseTo(base[i].y + center[1]);
      expect(p.z).toBeCloseTo(base[i].z + center[2]);
    });
  });
});

describe("boxToMatrix4 / psrToCorners consistency", () => {
  it("transforming unit-cube corners by boxToMatrix4 equals psrToCorners", () => {
    const center: [number, number, number] = [3, -5, 7];
    const size: [number, number, number] = [2, 4, 6];
    const rotation: [number, number, number] = [0.1, -0.4, 0.9];

    const m = boxToMatrix4(center, size, rotation);
    const unitCorners: [number, number, number][] = [
      [-0.5, -0.5, -0.5],
      [0.5, -0.5, -0.5],
      [0.5, 0.5, -0.5],
      [-0.5, 0.5, -0.5],
      [-0.5, -0.5, 0.5],
      [0.5, -0.5, 0.5],
      [0.5, 0.5, 0.5],
      [-0.5, 0.5, 0.5],
    ];
    const viaMatrix = unitCorners.map((c) => new THREE.Vector3(c[0], c[1], c[2]).applyMatrix4(m));
    const viaPsr = psrToCorners(center, size, rotation);

    viaMatrix.forEach((p, i) => {
      expect(p.x).toBeCloseTo(viaPsr[i].x);
      expect(p.y).toBeCloseTo(viaPsr[i].y);
      expect(p.z).toBeCloseTo(viaPsr[i].z);
    });
  });
});

describe("boxAxisWorldDir", () => {
  it("无旋转: local 轴 = 世界轴", () => {
    const x = boxAxisWorldDir([0, 0, 0], 0);
    expect(x.x).toBeCloseTo(1);
    expect(x.y).toBeCloseTo(0);
    expect(x.z).toBeCloseTo(0);
  });

  it("yaw 90° about Z: local +X → 世界 +Y, local +Z 不变", () => {
    const x = boxAxisWorldDir([0, 0, Math.PI / 2], 0);
    expect(x.x).toBeCloseTo(0);
    expect(x.y).toBeCloseTo(1);
    expect(x.z).toBeCloseTo(0);
    const z = boxAxisWorldDir([0, 0, Math.PI / 2], 2);
    expect(z.z).toBeCloseTo(1);
  });
});

describe("worldToBox", () => {
  const center: [number, number, number] = [3, -5, 7];
  const size: [number, number, number] = [2, 4, 6];
  const rotation: [number, number, number] = [0.1, -0.4, 0.9];

  it("是 boxToMatrix4 的逆: 世界角点 → 单位立方体角点 (±0.5)", () => {
    const inv = worldToBox(center, size, rotation);
    const corners = psrToCorners(center, size, rotation);
    corners.forEach((p) => {
      const local = p.clone().applyMatrix4(inv);
      expect(Math.abs(local.x)).toBeCloseTo(0.5);
      expect(Math.abs(local.y)).toBeCloseTo(0.5);
      expect(Math.abs(local.z)).toBeCloseTo(0.5);
    });
  });

  it("框心 → local 原点; 框外点 → 某分量 |·|>0.5", () => {
    const inv = worldToBox(center, size, rotation);
    const c = new THREE.Vector3(...center).applyMatrix4(inv);
    expect(c.length()).toBeCloseTo(0);
    const far = new THREE.Vector3(...center)
      .addScaledVector(boxAxisWorldDir(rotation, 0), size[0] * 2)
      .applyMatrix4(inv);
    expect(far.x).toBeCloseTo(2);
  });
});

describe("boxLocalClipPlanes", () => {
  const center: [number, number, number] = [1, 2, 3];
  const size: [number, number, number] = [4, 2, 6];
  const rotation: [number, number, number] = [0, 0, Math.PI / 6];

  it("框内点对全部 6 面 distanceToPoint ≥ 0; 框外点至少一面 < 0", () => {
    const planes = boxLocalClipPlanes(center, size, rotation);
    const c = new THREE.Vector3(...center);
    expect(planes.every((p) => p.distanceToPoint(c) >= -1e-9)).toBe(true);

    const outside = c.clone().addScaledVector(boxAxisWorldDir(rotation, 0), size[0]);
    expect(planes.some((p) => p.distanceToPoint(outside) < 0)).toBe(true);
  });

  it("margin 放宽: 刚出 +X 面的点在 margin 内重新被保留", () => {
    const c = new THREE.Vector3(...center);
    const pt = c.clone().addScaledVector(boxAxisWorldDir(rotation, 0), 2.3);
    const tight = boxLocalClipPlanes(center, size, rotation, 0);
    const loose = boxLocalClipPlanes(center, size, rotation, 0.5);
    expect(tight.some((p) => p.distanceToPoint(pt) < 0)).toBe(true);
    expect(loose.every((p) => p.distanceToPoint(pt) >= -1e-9)).toBe(true);
  });
});

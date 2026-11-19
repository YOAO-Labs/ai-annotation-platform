// §Phase1 frustum 视锥选点纯函数单测。
// SIMPLE_CALIB: identity 外参 + fx=fy=1000/cx=cy=500 → lidar 系即相机系,像素/深度可手算。
import { describe, it, expect } from "vitest";
import type { SensorCalibration } from "@/types";

import {
  centralRay,
  depthGate,
  gatherPoints,
  normalizeRect,
  selectPointsInRect,
  type RectSelection,
} from "./frustum";

const SIMPLE_CALIB: SensorCalibration = {
  extrinsic: [
    1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1,
  ] as unknown as SensorCalibration["extrinsic"],
  intrinsic: [1000, 0, 500, 0, 1000, 500, 0, 0, 1] as unknown as SensorCalibration["intrinsic"],
};

// identity 外参下:lidar (x,y,z) → 像素 (1000x/z+500, 1000y/z+500), 深度 w=z。
function f32(...xyz: number[]): Float32Array {
  return new Float32Array(xyz);
}

describe("normalizeRect", () => {
  it("任意两角点规整为 x0≤x1 / y0≤y1", () => {
    expect(normalizeRect(650, 660, 450, 440)).toEqual({ x0: 450, y0: 440, x1: 650, y1: 660 });
  });
});

describe("selectPointsInRect", () => {
  it("保留投影落在矩形内的相机前方点,排除框外/相机后方点", () => {
    // idx0 (0,0,10)→(500,500) d10;idx1 (1,1,10)→(600,600) d10;idx2 (0,0,5)→(500,500) d5;
    // idx3 (0,0,-5) 相机后方;idx4 (5,0,10)→(1000,500) 框外。
    const positions = f32(0, 0, 10, 1, 1, 10, 0, 0, 5, 0, 0, -5, 5, 0, 10);
    const sel = selectPointsInRect(positions, { x0: 450, y0: 450, x1: 650, y1: 650 }, SIMPLE_CALIB);
    expect(sel.indices).toEqual([0, 1, 2]);
    expect(sel.depths).toEqual([10, 10, 5]);
  });

  it("矩形内无投影点 → 空选区", () => {
    const positions = f32(5, 0, 10); // → (1000,500) 框外
    const sel = selectPointsInRect(positions, { x0: 0, y0: 0, x1: 100, y1: 100 }, SIMPLE_CALIB);
    expect(sel.indices).toEqual([]);
    expect(sel.depths).toEqual([]);
  });
});

describe("depthGate", () => {
  it("取最近簇:保留 [d_min, d_min+band],排除更远背景", () => {
    const sel: RectSelection = { indices: [10, 11, 12, 13], depths: [5, 6, 5.5, 30] };
    expect(depthGate(sel, { bandMeters: 8 })).toEqual([10, 11, 12]); // 30 被排除
  });

  it("band 足够大时全保留", () => {
    const sel: RectSelection = { indices: [0, 1], depths: [5, 30] };
    expect(depthGate(sel, { bandMeters: 50 })).toEqual([0, 1]);
  });

  it("空选区 → 空", () => {
    expect(depthGate({ indices: [], depths: [] })).toEqual([]);
  });
});

describe("gatherPoints", () => {
  it("按索引收成 Float32Array(len=3K)", () => {
    const positions = f32(0, 0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9);
    const out = gatherPoints(positions, [1, 3]);
    expect(Array.from(out)).toEqual([1, 2, 3, 7, 8, 9]);
  });
});

describe("centralRay", () => {
  it("矩形中心在主点 → 沿相机光轴 +Z 的单位射线,光心在原点", () => {
    const ray = centralRay({ x0: 450, y0: 450, x1: 550, y1: 550 }, SIMPLE_CALIB);
    expect(ray.origin[0]).toBeCloseTo(0, 6);
    expect(ray.origin[1]).toBeCloseTo(0, 6);
    expect(ray.origin[2]).toBeCloseTo(0, 6);
    expect(ray.direction[0]).toBeCloseTo(0, 6);
    expect(ray.direction[1]).toBeCloseTo(0, 6);
    expect(ray.direction[2]).toBeCloseTo(1, 6);
  });

  it("中心偏右(像素 u>cx) → 射线方向 +X 分量,且为单位向量", () => {
    const ray = centralRay({ x0: 550, y0: 450, x1: 650, y1: 550 }, SIMPLE_CALIB); // 中心 (600,500)
    expect(ray.direction[0]).toBeGreaterThan(0);
    expect(ray.direction[2]).toBeGreaterThan(0);
    const len = Math.hypot(...ray.direction);
    expect(len).toBeCloseTo(1, 6);
  });
});

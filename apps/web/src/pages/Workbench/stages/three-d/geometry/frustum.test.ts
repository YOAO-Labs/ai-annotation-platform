import { describe, it, expect } from "vitest";
import type { SensorCalibration } from "@/types";

import {
  centralRay,
  fitCameraSeedBox,
  gatherPoints,
  normalizeRect,
  selectPointsInRect,
} from "./frustum";

const SIMPLE_CALIB: SensorCalibration = {
  extrinsic: [
    1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1,
  ] as unknown as SensorCalibration["extrinsic"],
  intrinsic: [1000, 0, 500, 0, 1000, 500, 0, 0, 1] as unknown as SensorCalibration["intrinsic"],
};

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
    const positions = f32(0, 0, 10, 1, 1, 10, 0, 0, 5, 0, 0, -5, 5, 0, 10);
    const sel = selectPointsInRect(positions, { x0: 450, y0: 450, x1: 650, y1: 650 }, SIMPLE_CALIB);
    expect(sel.indices).toEqual([0, 1, 2]);
    expect(sel.depths).toEqual([10, 10, 5]);
  });

  it("矩形内无投影点 → 空选区", () => {
    const positions = f32(5, 0, 10);
    const sel = selectPointsInRect(positions, { x0: 0, y0: 0, x1: 100, y1: 100 }, SIMPLE_CALIB);
    expect(sel.indices).toEqual([]);
    expect(sel.depths).toEqual([]);
  });
});

describe("fitCameraSeedBox", () => {
  const calibration: SensorCalibration = {
    extrinsic: [0, -1, 0, 0, 0, 0, -1, 1.5, 1, 0, 0, -1, 0, 0, 0, 1],
    intrinsic: [768, 0, 640, 0, 720, 360, 0, 0, 1],
  };
  const rect = { x0: 576, y0: 295, x1: 704, y1: 455 };

  it("从带旋转和平移的相机标定拟合目标，保留框边回波并排除近点和较密的背景", () => {
    const positions = [3, 0, 1.5, 13, 1.015625, 1.5, 0, 0, 1.5, 13, 1, 0.5, 13, -1, 0.5];
    for (let i = 0; i <= 6; i++) {
      for (const y of [-1, -0.5, 0.5, 1]) {
        for (const z of [0.5, 2]) positions.push(16 - i * 0.5, y, z);
      }
      for (let y = -1.25; y <= 1.25; y += 0.25) {
        positions.push(19 + i * 0.25, y, 0, 19 + i * 0.25, y, 2.5);
      }
    }
    const points = f32(...positions);
    const selected = selectPointsInRect(points, rect, calibration);
    expect(selected.indices.slice(0, 2)).toEqual([0, 3]);
    expect(selected.depths.slice(0, 2)).toEqual([2, 12]);

    const box = fitCameraSeedBox(points, rect, calibration);
    expect(box?.center).toEqual([14.5, 0, 1.3]);
    expect(box?.size).toEqual([3.1, 2.1, 1.6]);
    expect(box?.rotation).toEqual([0, 0, 0]);
  });

  it("仅在相机框内没有足够连续的回波时交给中央射线降级", () => {
    expect(fitCameraSeedBox(f32(0, 0, 1.5), rect, calibration)).toBeNull();
    const scattered = [3, 0, 1.5, 5, 0, 1.5, 13, 0, 0.5, 13.5, 0, 2, 19, 0, 1.5];
    expect(fitCameraSeedBox(f32(...scattered), rect, calibration)).toBeNull();
    expect(fitCameraSeedBox(f32(...scattered, 14, 0.5, 1), rect, calibration)).not.toBeNull();
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
    const ray = centralRay({ x0: 550, y0: 450, x1: 650, y1: 550 }, SIMPLE_CALIB);
    expect(ray.direction[0]).toBeGreaterThan(0);
    expect(ray.direction[2]).toBeGreaterThan(0);
    const len = Math.hypot(...ray.direction);
    expect(len).toBeCloseTo(1, 6);
  });
});

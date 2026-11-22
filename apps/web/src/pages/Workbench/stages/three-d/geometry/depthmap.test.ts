import { describe, it, expect } from "vitest";
import type { SensorCalibration } from "@/types";
import { buildDepthRaster, buildGpuDepthRaster, sampleDepth } from "./depthmap";

const IDENTITY4 = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1];
const CALIB: SensorCalibration = {
  extrinsic: IDENTITY4 as unknown as SensorCalibration["extrinsic"],
  intrinsic: [1, 0, 0, 0, 1, 0, 0, 0, 1] as unknown as SensorCalibration["intrinsic"],
};

describe("buildDepthRaster + sampleDepth", () => {
  it("GPU depth-only 栅格与完整栅格深度一致，空格可直接上传", () => {
    const positions = new Float32Array([2, 2, 4, 4, 4, 8]);
    const full = buildDepthRaster(positions, CALIB, 4, 4, 1);
    const gpu = buildGpuDepthRaster(positions, CALIB, 4, 4, 1);

    expect(gpu.cols).toBe(full.cols);
    expect(gpu.rows).toBe(full.rows);
    for (let index = 0; index < full.depth.length; index += 1) {
      if (Number.isFinite(full.depth[index])) expect(gpu.depth[index]).toBe(full.depth[index]);
      else expect(gpu.depth[index]).toBeGreaterThan(1e10);
    }
    expect(Array.from(gpu.depth).every(Number.isFinite)).toBe(true);
  });

  it("最近点深度落到对应格,可被采样到", () => {
    const r = buildDepthRaster(new Float32Array([2, 2, 4]), CALIB, 4, 4, 1);
    const hit = sampleDepth(r, 0.5, 0.5);
    expect(hit).not.toBeNull();
    expect(hit!.depth).toBeCloseTo(4, 5);
    expect(hit!.point[0]).toBeCloseTo(2, 5);
  });

  it("同格多点取最近(depth 最小)者", () => {
    const r = buildDepthRaster(new Float32Array([2, 2, 4, 4, 4, 8]), CALIB, 4, 4, 1);
    const hit = sampleDepth(r, 0.5, 0.5);
    expect(hit!.depth).toBeCloseTo(4, 5);
  });

  it("相机后方 (depth<=0) 的点不入栅格", () => {
    const r = buildDepthRaster(new Float32Array([2, 2, -4]), CALIB, 4, 4, 1);
    expect(isFinite(r.minDepth)).toBe(false);
    expect(sampleDepth(r, 0.5, 0.5)).toBeNull();
  });

  it("出框的点不入栅格", () => {
    const r = buildDepthRaster(new Float32Array([20, 20, 4]), CALIB, 4, 4, 1);
    expect(sampleDepth(r, 0.5, 0.5)).toBeNull();
  });

  it("空区(邻域无点)返回 null", () => {
    const r = buildDepthRaster(new Float32Array([0.5, 0.5, 1]), CALIB, 4, 4, 1);
    expect(sampleDepth(r, 3.5, 3.5)).toBeNull();
  });

  it("空格但 1 环内有点 → 取邻格", () => {
    const r = buildDepthRaster(new Float32Array([0.2, 0.2, 1]), CALIB, 4, 4, 1);
    const hit = sampleDepth(r, 1.5, 1.5);
    expect(hit).not.toBeNull();
    expect(hit!.depth).toBeCloseTo(1, 5);
  });
});

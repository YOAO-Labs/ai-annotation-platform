import { describe, it, expect } from "vitest";

import { estimateGroundZ } from "./ground";

function pcd(zs: number[]): Float32Array {
  const a = new Float32Array(zs.length * 3);
  zs.forEach((z, i) => {
    a[3 * i + 2] = z;
  });
  return a;
}

describe("estimateGroundZ", () => {
  it("空点集返回 0", () => {
    expect(estimateGroundZ(new Float32Array(0), 0)).toBe(0);
  });

  it("同一高度的点集返回该高度", () => {
    const zs = Array.from({ length: 1000 }, () => -2.5);
    expect(estimateGroundZ(pcd(zs), zs.length)).toBeCloseTo(-2.5, 5);
  });

  it("均匀分布 → 中位数 ≈ 范围中点(直方图 bin 中心精度)", () => {
    const zs: number[] = [];
    for (let i = 0; i < 1000; i++) zs.push(-5 + (10 * i) / 999);
    const out = estimateGroundZ(pcd(zs), zs.length);
    expect(out).toBeCloseTo(0, 1);
  });

  it("低端噪声不下拉(关键回归):1 个极低离群点 + 大量集中高度,结果仍贴近主体", () => {
    const zs: number[] = [-50];
    for (let i = 0; i < 999; i++) zs.push(-3);
    const out = estimateGroundZ(pcd(zs), zs.length);
    expect(out).toBeCloseTo(-3, 0);
    expect(out).toBeGreaterThan(-4);
  });

  it("地物 + 真地面双峰(车顶 lidar 典型):下半地面 + 上半建筑,中位数靠下", () => {
    const zs: number[] = [];
    for (let i = 0; i < 700; i++) zs.push(-4 + 0.5 * (i / 699));
    for (let i = 0; i < 300; i++) zs.push(0 + 3 * (i / 299));
    const out = estimateGroundZ(pcd(zs), zs.length);
    expect(out).toBeGreaterThan(-4);
    expect(out).toBeLessThan(-3);
  });

  it("无噪声单值范围(zMin == zMax)走 span <= 0 兜底", () => {
    const zs = [1.5, 1.5, 1.5];
    expect(estimateGroundZ(pcd(zs), zs.length)).toBeCloseTo(1.5, 5);
  });
});

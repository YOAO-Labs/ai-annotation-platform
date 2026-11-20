import { describe, it, expect } from "vitest";

import type { Psr } from "./triview";
import { MIN_SIZE } from "./triview";
import { fitSize, fitBottom, fitYaw, fitSizeAndBottom, psrFromPoints } from "./autofit";

/* ──────────────────────────────────────────────────────────────────────
 * autofit 纯几何单测。合成数据 + 手算预期 + toBeCloseTo 浮点比较。
 * inside 判定基于 box-local 反变换 (q⁻¹ · (p - center)), 与实现同口径。
 * ────────────────────────────────────────────────────────────────────── */

/** 在 [xMin,xMax]×[yMin,yMax]×[zMin,zMax] 框内均匀采 nx×ny×nz 网格点 (world 系)。 */
function gridPoints(
  xMin: number,
  xMax: number,
  yMin: number,
  yMax: number,
  zMin: number,
  zMax: number,
  nx: number,
  ny: number,
  nz: number,
): Float32Array {
  const total = nx * ny * nz;
  const arr = new Float32Array(total * 3);
  let k = 0;
  for (let ix = 0; ix < nx; ix++) {
    const x = nx === 1 ? (xMin + xMax) / 2 : xMin + ((xMax - xMin) * ix) / (nx - 1);
    for (let iy = 0; iy < ny; iy++) {
      const y = ny === 1 ? (yMin + yMax) / 2 : yMin + ((yMax - yMin) * iy) / (ny - 1);
      for (let iz = 0; iz < nz; iz++) {
        const z = nz === 1 ? (zMin + zMax) / 2 : zMin + ((zMax - zMin) * iz) / (nz - 1);
        arr[k++] = x;
        arr[k++] = y;
        arr[k++] = z;
      }
    }
  }
  return arr;
}

/** 绕 Z 轴把 (x,y) 转角 θ → (x cosθ - y sinθ, x sinθ + y cosθ); z 不变。 */
function rotateZ(points: Float32Array, theta: number): Float32Array {
  const c = Math.cos(theta);
  const s = Math.sin(theta);
  const out = new Float32Array(points.length);
  const n = points.length / 3;
  for (let i = 0; i < n; i++) {
    const x = points[i * 3];
    const y = points[i * 3 + 1];
    const z = points[i * 3 + 2];
    out[i * 3] = x * c - y * s;
    out[i * 3 + 1] = x * s + y * c;
    out[i * 3 + 2] = z;
  }
  return out;
}

describe("fitSize", () => {
  it("框比点云大: 沿轴对齐点云 → 收紧到 AABB + 2×padding", () => {
    // 点云占 [-1,1] × [-0.5,0.5] × [-0.3,0.3]; 框 size=[4,3,2] 中心(0,0,0)
    const pts = gridPoints(-1, 1, -0.5, 0.5, -0.3, 0.3, 5, 4, 3);
    const psr: Psr = {
      center: [0, 0, 0],
      size: [4, 3, 2],
      rotation: [0, 0, 0],
    };
    const out = fitSize(pts, psr, 0.05);
    expect(out.size[0]).toBeCloseTo(2 + 0.1, 5); // 2×1 + 2×0.05
    expect(out.size[1]).toBeCloseTo(1 + 0.1, 5);
    expect(out.size[2]).toBeCloseTo(0.6 + 0.1, 5);
    // 点云对称于 (0,0,0) → AABB 中心 = (0,0,0) → center 不动。
    expect(out.center[0]).toBeCloseTo(0);
    expect(out.center[1]).toBeCloseTo(0);
    expect(out.center[2]).toBeCloseTo(0);
    // 旋转不变。
    expect(out.rotation[0]).toBe(0);
    expect(out.rotation[1]).toBe(0);
    expect(out.rotation[2]).toBe(0);
  });

  it("inside count < 3 → 返回原 psr(早退,覆盖 1-2 点边界)", () => {
    // box size [1,1,1] 中心 [0,0,0],仅放 2 个 inside 点。
    const pts = new Float32Array([0.1, 0.1, 0.1, -0.1, -0.1, -0.1]);
    const psr: Psr = { center: [0, 0, 0], size: [1, 1, 1], rotation: [0, 0, 0] };
    const out = fitSize(pts, psr);
    expect(out.size[0]).toBe(1);
    expect(out.size[1]).toBe(1);
    expect(out.size[2]).toBe(1);
    expect(out.center).toEqual([0, 0, 0]);
  });

  it("极小点云 + padding 收到 < MIN_SIZE → clamp 到 MIN_SIZE(覆盖 clampSize 分支)", () => {
    // 点云 z 跨度 ~0.001m, 远小于 MIN_SIZE=0.05;padding=0 防止被 padding 撑过 MIN_SIZE。
    const pts = gridPoints(-0.0005, 0.0005, -0.0005, 0.0005, -0.0005, 0.0005, 3, 3, 3);
    const psr: Psr = { center: [0, 0, 0], size: [1, 1, 1], rotation: [0, 0, 0] };
    const out = fitSize(pts, psr, 0);
    expect(out.size[0]).toBeCloseTo(0.05, 5);
    expect(out.size[1]).toBeCloseTo(0.05, 5);
    expect(out.size[2]).toBeCloseTo(0.05, 5);
  });

  it("**不对称 inside → 中心漂移到 AABB 中心**(钉死 fitSize contract)", () => {
    // 框 center=[0,0,0] size=[10,10,2]; inside 点全部在 box-local x∈[0,1](右半):
    //   点云沿 X 从 0→1 均匀,Y/Z 居中。AABB_local 中心 = (0.5, 0, 0)。
    // 期望:新 center.x = 0 + 0.5 = 0.5(向右漂),新 size.x = 1 - 0 + 0.1 = 1.1。
    // 这一例显式证明 fitSize **不是「保中心」**,它把 center 平移到 inside AABB 中心
    // 以让收紧后的 box 恰好包住点云;若未来按"保中心"理解去重构会被本测试卡住。
    const pts = gridPoints(0, 1, -0.2, 0.2, -0.5, 0.5, 11, 5, 5);
    const psr: Psr = {
      center: [0, 0, 0],
      size: [10, 10, 2],
      rotation: [0, 0, 0],
    };
    const out = fitSize(pts, psr, 0.05);
    expect(out.center[0]).toBeCloseTo(0.5, 5);
    expect(out.center[1]).toBeCloseTo(0, 5);
    expect(out.center[2]).toBeCloseTo(0, 5);
    expect(out.size[0]).toBeCloseTo(1.0 + 0.1, 5);
  });

  it("框比点云小 → 扩大到 inside 子集的 AABB + 2×padding (出框点被裁掉)", () => {
    // 点云 x ∈ [-2,2] 取 9 点 (步长 0.5: -2,-1.5,...,2), y 取 5 点 (步长 0.25: -0.5,...,0.5), z 取 3 点。
    // 框 size=[1,1,1] 中心(0,0,0) → 仅看 |x|,|y|,|z|≤0.5 子集。
    // x: 落入 -0.5/0/0.5 → 跨度 1.0; y: 全部 [-0.5,0.5] 全在框内 → 跨度 1.0; z: 全在框内 → 跨度 0.6。
    const pts = gridPoints(-2, 2, -0.5, 0.5, -0.3, 0.3, 9, 5, 3);
    const psr: Psr = {
      center: [0, 0, 0],
      size: [1, 1, 1],
      rotation: [0, 0, 0],
    };
    const out = fitSize(pts, psr, 0.05);
    expect(out.size[0]).toBeCloseTo(1 + 0.1, 5);
    expect(out.size[1]).toBeCloseTo(1 + 0.1, 5);
    expect(out.size[2]).toBeCloseTo(0.6 + 0.1, 5);
  });

  it("yaw=30° box + 同步旋转 30° 点云: local 系内仍轴对齐 → size = 点云原跨度 + 2×padding", () => {
    const theta = Math.PI / 6;
    // 原点云占 [-1,1] × [-0.5,0.5] × [-0.3,0.3] (在 box-local 系应该轴对齐)。
    // 绕 Z 转 30° 得到 world 系点云。
    const base = gridPoints(-1, 1, -0.5, 0.5, -0.3, 0.3, 5, 4, 3);
    const pts = rotateZ(base, theta);
    const psr: Psr = {
      center: [0, 0, 0],
      size: [10, 10, 10], // 故意放大, 确保所有点都在框内
      rotation: [0, 0, theta],
    };
    const out = fitSize(pts, psr, 0.05);
    // 在 box-local 系内, 点云跨度恢复为原值 → size = (2, 1, 0.6) + 0.1。
    expect(out.size[0]).toBeCloseTo(2 + 0.1, 5);
    expect(out.size[1]).toBeCloseTo(1 + 0.1, 5);
    expect(out.size[2]).toBeCloseTo(0.6 + 0.1, 5);
    // 点云中心 (0,0,0) → center 不动。
    expect(out.center[0]).toBeCloseTo(0);
    expect(out.center[1]).toBeCloseTo(0);
    expect(out.center[2]).toBeCloseTo(0);
    // yaw 保持 30°。
    expect(out.rotation[2]).toBeCloseTo(theta);
  });

  it("空 positions / 框内无点 → 返回原 psr (各分量逐字段)", () => {
    const psr: Psr = {
      center: [1, 2, 3],
      size: [4, 5, 6],
      rotation: [0.1, 0.2, 0.3],
    };
    // 完全空。
    const out1 = fitSize(new Float32Array(0), psr);
    expect(out1.center).toEqual([1, 2, 3]);
    expect(out1.size).toEqual([4, 5, 6]);
    expect(out1.rotation).toEqual([0.1, 0.2, 0.3]);
    // 全部远离框 (在外面)。
    const farPts = new Float32Array([100, 100, 100, 200, 200, 200]);
    const out2 = fitSize(farPts, psr);
    expect(out2.center).toEqual([1, 2, 3]);
    expect(out2.size).toEqual([4, 5, 6]);
    expect(out2.rotation).toEqual([0.1, 0.2, 0.3]);
  });
});

describe("fitBottom", () => {
  it("点云最低 z=0, 新 cz = z_min(inside) + sz/2 (让 box 下沿 = 点云最低)", () => {
    // 注: 计划文本 "box cz=5 sz=2 → 新 cz=1" 隐含 box 需 inside z=0, 但 cz=5 sz=2 → world z ∈ [4,6]
    // 永远见不到 z=0, 直接套数会不自洽。取 box 包住点云的合理构造, 等价验证 cz = z_min + sz/2 公式。
    const pts = gridPoints(-1, 1, -1, 1, 0, 2, 3, 3, 3);
    const psr: Psr = {
      center: [0, 0, 5],
      size: [10, 10, 12], // world z ∈ [-1, 11] 包住点云 [0, 2]
      rotation: [0, 0, 0],
    };
    const out = fitBottom(pts, psr);
    // inside 点 z_min = 0 (网格采到的最低 z); 新 cz = 0 + 12/2 = 6。
    expect(out.center[2]).toBeCloseTo(0 + psr.size[2] / 2, 5);
  });

  it("不改 cx/cy/size/rotation (逐字段断言)", () => {
    const psr: Psr = {
      center: [7, 8, 5],
      size: [10, 10, 12],
      rotation: [0.1, 0.2, 0.3],
    };
    // rotation 非零 → 点云需移到框中心 (7,8,*) 附近以保证 inside 非空。
    const pts = gridPoints(6, 8, 7, 9, 0, 2, 3, 3, 3);
    const out = fitBottom(pts, psr);
    expect(out.center[0]).toBe(7);
    expect(out.center[1]).toBe(8);
    expect(out.size[0]).toBe(10);
    expect(out.size[1]).toBe(10);
    expect(out.size[2]).toBe(12);
    expect(out.rotation[0]).toBe(0.1);
    expect(out.rotation[1]).toBe(0.2);
    expect(out.rotation[2]).toBe(0.3);
  });

  it("框内空 → 返回原 psr", () => {
    const psr: Psr = {
      center: [0, 0, 0],
      size: [1, 1, 1],
      rotation: [0, 0, 0],
    };
    const farPts = new Float32Array([100, 100, 100]);
    const out = fitBottom(farPts, psr);
    expect(out.center).toEqual([0, 0, 0]);
    expect(out.size).toEqual([1, 1, 1]);
    expect(out.rotation).toEqual([0, 0, 0]);
  });
});

describe("fitYaw", () => {
  it("沿 X 拉长的点云, box yaw=0 → 新 yaw ≈ 0 (已对齐)", () => {
    // 25 点沿 X 拉长 (x ∈ [-3,3]), y ∈ [-0.5,0.5]
    const pts = gridPoints(-3, 3, -0.5, 0.5, 0, 0, 25, 3, 1);
    const psr: Psr = {
      center: [0, 0, 0],
      size: [10, 10, 1],
      rotation: [0, 0, 0],
    };
    const out = fitYaw(pts, psr);
    // 主轴沿 X → atan2(0,1) = 0。允许浮点轻微抖动。
    expect(Math.abs(out.rotation[2])).toBeLessThan(1e-3);
  });

  it("沿 X 拉长的点云, box 初始 yaw=π/4 → 新 yaw ≈ 0 (转回主轴)", () => {
    const theta0 = Math.PI / 4;
    const pts = gridPoints(-3, 3, -0.5, 0.5, 0, 0, 25, 3, 1);
    const psr: Psr = {
      center: [0, 0, 0],
      size: [10, 10, 1],
      rotation: [0, 0, theta0],
    };
    const out = fitYaw(pts, psr);
    // 注意 inside 判定基于 box-local; 但 size=10 远大于点云 → 点云全 inside。
    // PCA 在 world 系跑, 主轴沿 X → 新 yaw ≈ 0。
    expect(Math.abs(out.rotation[2])).toBeLessThan(1e-3);
    // 其他轴不动。
    expect(out.rotation[0]).toBe(0);
    expect(out.rotation[1]).toBe(0);
  });

  it("沿 Y 拉长(b≈0, a<d)→ 退化分支 vy=1 → 新 yaw ≈ π/2", () => {
    // 完全沿 Y 拉长的对齐点云: x ∈ {0}, y ∈ [-3, 3] 步长 0.1 → 协方差 b == 0, a < d。
    // 走 fitYaw 闭式 PCA 退化分支 (vx=0, vy=1) → atan2(1, 0) = π/2。
    const xs: number[] = [];
    const ys: number[] = [];
    for (let y = -3; y <= 3.001; y += 0.1) {
      xs.push(0);
      ys.push(y);
    }
    const pts = new Float32Array(xs.length * 3);
    xs.forEach((x, i) => {
      pts[3 * i] = x;
      pts[3 * i + 1] = ys[i];
      pts[3 * i + 2] = 0;
    });
    const psr: Psr = { center: [0, 0, 0], size: [10, 10, 10], rotation: [0, 0, 0] };
    const out = fitYaw(pts, psr);
    expect(out.rotation[2]).toBeCloseTo(Math.PI / 2, 3);
  });

  it("点数 < 20 → 返回原 psr (rotation 完全相等)", () => {
    // 仅 8 点
    const pts = gridPoints(-1, 1, -0.5, 0.5, 0, 0, 4, 2, 1);
    const psr: Psr = {
      center: [0, 0, 0],
      size: [10, 10, 10],
      rotation: [0.1, 0.2, 0.3],
    };
    const out = fitYaw(pts, psr);
    expect(out.rotation[0]).toBe(0.1);
    expect(out.rotation[1]).toBe(0.2);
    expect(out.rotation[2]).toBe(0.3);
    expect(out.center).toEqual([0, 0, 0]);
    expect(out.size).toEqual([10, 10, 10]);
  });
});

describe("fitSizeAndBottom", () => {
  it("等价于先 fitSize 再 fitBottom (链式调用)", () => {
    const pts = gridPoints(-1, 1, -0.5, 0.5, 0, 0.6, 4, 3, 3);
    const psr: Psr = {
      center: [0, 0, 1],
      size: [5, 5, 5], // 框宽松包住点云
      rotation: [0, 0, 0],
    };
    const combined = fitSizeAndBottom(pts, psr, 0.05);
    const stepwise = fitBottom(pts, fitSize(pts, psr, 0.05));
    expect(combined.center[0]).toBeCloseTo(stepwise.center[0]);
    expect(combined.center[1]).toBeCloseTo(stepwise.center[1]);
    expect(combined.center[2]).toBeCloseTo(stepwise.center[2]);
    expect(combined.size[0]).toBeCloseTo(stepwise.size[0]);
    expect(combined.size[1]).toBeCloseTo(stepwise.size[1]);
    expect(combined.size[2]).toBeCloseTo(stepwise.size[2]);
    expect(combined.rotation[0]).toBeCloseTo(stepwise.rotation[0]);
    expect(combined.rotation[1]).toBeCloseTo(stepwise.rotation[1]);
    expect(combined.rotation[2]).toBeCloseTo(stepwise.rotation[2]);
  });
});

describe("psrFromPoints · 选中点 AABB → 轴对齐 PSR (框选选点)", () => {
  it("center = AABB 中心; size = 跨度 + 2×padding; rotation = 0", () => {
    // 点云 XY∈[9,11]×[19.5,20.5] z∈[1.0,2.6] (跨度 2,1,1.6), 中心 (10,20,1.8)。
    const pts = gridPoints(9, 11, 19.5, 20.5, 1.0, 2.6, 4, 3, 5);
    const out = psrFromPoints(pts, 0.05);
    expect(out.center[0]).toBeCloseTo(10);
    expect(out.center[1]).toBeCloseTo(20);
    expect(out.center[2]).toBeCloseTo(1.8); // (1.0+2.6)/2
    expect(out.size[0]).toBeCloseTo(2 + 0.1, 5); // 跨度 2 + 2×0.05
    expect(out.size[1]).toBeCloseTo(1 + 0.1, 5);
    expect(out.size[2]).toBeCloseTo(1.6 + 0.1, 5);
    expect(out.rotation).toEqual([0, 0, 0]);
  });

  it("单点 → 退化为 MIN_SIZE 立方体, 中心即该点", () => {
    const out = psrFromPoints(new Float32Array([3, -4, 5]), 0);
    expect(out.center).toEqual([3, -4, 5]);
    expect(out.size[0]).toBeCloseTo(MIN_SIZE);
    expect(out.size[1]).toBeCloseTo(MIN_SIZE);
    expect(out.size[2]).toBeCloseTo(MIN_SIZE);
  });

  it("某轴近共面 (跨度≈0) + padding=0 → 该轴 size clamp MIN_SIZE", () => {
    const pts = gridPoints(-1, 1, -1, 1, 0, 0, 4, 4, 1); // z 全 0
    const out = psrFromPoints(pts, 0);
    expect(out.size[0]).toBeCloseTo(2, 5);
    expect(out.size[2]).toBeCloseTo(MIN_SIZE, 5);
  });
});

describe("fitSize: MIN_SIZE 下限", () => {
  it("近共面的点云 (z 跨度 ≈ 0) 收紧后 sz 不低于 MIN_SIZE", () => {
    // z 全部为 0 → AABB extent z=0 + padding 0.1 = 0.1 > MIN_SIZE=0.05 → sz=0.1。
    // 进一步用更小 padding=0 验证 MIN_SIZE 下限触发。
    const pts = gridPoints(-1, 1, -1, 1, 0, 0, 5, 5, 1);
    const psr: Psr = {
      center: [0, 0, 0],
      size: [3, 3, 3],
      rotation: [0, 0, 0],
    };
    const out = fitSize(pts, psr, 0);
    expect(out.size[2]).toBeGreaterThanOrEqual(MIN_SIZE);
    expect(out.size[2]).toBeCloseTo(MIN_SIZE, 5);
  });
});

import { describe, expect, it } from "vitest";
import { runReferenceKalman, type ReferenceKalmanKeyframe } from "./videoReferenceKalman";

function linearExtrapolateCx(keyframes: ReferenceKalmanKeyframe[], targetFrame: number): number {
  const k2 = keyframes[keyframes.length - 1];
  const k1 = keyframes[keyframes.length - 2];
  const span = Math.max(1, k2.frame - k1.frame);
  const dt = targetFrame - k2.frame;
  return k2.cx + ((k2.cx - k1.cx) / span) * dt;
}

function makeNoise(seed: number) {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return (s / 0xffffffff - 0.5) * 2;
  };
}

describe("runReferenceKalman", () => {
  it("线性运动 + 噪声:卡尔曼平均误差比两点外推更小(平滑降方差)", () => {
    const target = 24;
    const truth = 0.1 + 0.01 * target;
    let kfErr = 0;
    let linearErr = 0;
    const trials = 80;
    for (let seed = 1; seed <= trials; seed++) {
      const noise = makeNoise(seed * 2654435761);
      const keyframes: ReferenceKalmanKeyframe[] = [];
      for (let f = 0; f < 20; f++) {
        keyframes.push({ frame: f, cx: 0.1 + 0.01 * f + noise() * 0.006, cy: 0.5, w: 0.2, h: 0.2 });
      }
      kfErr += Math.abs(runReferenceKalman(keyframes, target, "stable").cx - truth);
      linearErr += Math.abs(linearExtrapolateCx(keyframes, target) - truth);
    }
    expect(kfErr / trials).toBeLessThan(linearErr / trials);
  });

  it("恒速序列:卡尔曼预测收敛到真实位置", () => {
    const keyframes: ReferenceKalmanKeyframe[] = [];
    for (let f = 0; f <= 10; f++) {
      keyframes.push({ frame: f, cx: 0.2 + 0.02 * f, cy: 0.4 - 0.01 * f, w: 0.15, h: 0.15 });
    }
    const kf = runReferenceKalman(keyframes, 15, "stable");
    expect(kf.cx).toBeCloseTo(0.5, 2);
    expect(kf.cy).toBeCloseTo(0.25, 2);
    expect(kf.w).toBeCloseTo(0.15, 2);
  });

  it("阶跃突变:灵敏档比平稳档更紧跟最新关键帧", () => {
    const keyframes: ReferenceKalmanKeyframe[] = [
      { frame: 0, cx: 0.3, cy: 0.5, w: 0.2, h: 0.2 },
      { frame: 1, cx: 0.3, cy: 0.5, w: 0.2, h: 0.2 },
      { frame: 2, cx: 0.3, cy: 0.5, w: 0.2, h: 0.2 },
      { frame: 3, cx: 0.5, cy: 0.5, w: 0.2, h: 0.2 },
    ];
    const stable = runReferenceKalman(keyframes, 4, "stable");
    const agile = runReferenceKalman(keyframes, 4, "agile");
    expect(agile.cx).toBeGreaterThan(stable.cx);
  });

  it("不确定度 σ:外推越远越大,且恒正(用于误差椭圆)", () => {
    const keyframes: ReferenceKalmanKeyframe[] = [];
    for (let f = 0; f <= 6; f++) {
      keyframes.push({ frame: f, cx: 0.2 + 0.02 * f, cy: 0.4, w: 0.15, h: 0.15 });
    }
    const near = runReferenceKalman(keyframes, 7, "stable");
    const far = runReferenceKalman(keyframes, 30, "stable");
    expect(near.sx).toBeGreaterThan(0);
    expect(near.sy).toBeGreaterThan(0);
    expect(far.sx).toBeGreaterThan(near.sx);
  });

  it("输出可被调用侧 clamp:不强制自身归一(预测可越界,由 clampGeom 收口)", () => {
    const keyframes: ReferenceKalmanKeyframe[] = [
      { frame: 0, cx: 0.8, cy: 0.5, w: 0.2, h: 0.2 },
      { frame: 1, cx: 0.9, cy: 0.5, w: 0.2, h: 0.2 },
    ];
    const kf = runReferenceKalman(keyframes, 10, "agile");
    expect(kf.cx).toBeGreaterThan(1);
  });
});

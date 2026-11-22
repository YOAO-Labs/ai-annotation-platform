import { describe, it, expect } from "vitest";
import * as THREE from "three";

import { boxAxisWorldDir } from "./box3d";
import {
  VIEW_AXES,
  MIN_SIZE,
  FRAME_MARGIN,
  TRI_ZOOM_MAX,
  TRI_ZOOM_MIN,
  clampTriZoom,
  frameOrtho,
  stepTriZoom,
  dragEdge,
  dragCorner,
  dragHandle,
  dragRotation,
  type Psr,
} from "./triview";

const base: Psr = {
  center: [0, 0, 0],
  size: [4, 2, 1.5],
  rotation: [0, 0, 0],
};

describe("VIEW_AXES", () => {
  it("每视图 u/v/normal 是 {0,1,2} 的一个排列 (不重叠)", () => {
    for (const view of ["top", "side", "front"] as const) {
      const { u, v, normal } = VIEW_AXES[view];
      expect(new Set([u, v, normal]).size).toBe(3);
    }
  });
});

describe("frameOrtho · 正交取景半宽/半高", () => {
  it("保持视口 aspect, 且必框住 box+margin (较紧一维贴边)", () => {
    const { halfW, halfH } = frameOrtho(base.size, "top", 2);
    expect(halfW / halfH).toBeCloseTo(2);
    expect(halfW).toBeGreaterThanOrEqual(base.size[0] / 2 + FRAME_MARGIN - 1e-9);
    expect(halfH).toBeGreaterThanOrEqual(base.size[1] / 2 + FRAME_MARGIN - 1e-9);
    expect(halfH).toBeCloseTo(1.6);
    expect(halfW).toBeCloseTo(3.2);
  });

  it("框比视口更'宽'时按宽贴边 (横轴顶满, 纵轴按 aspect 撑开)", () => {
    const { halfW, halfH } = frameOrtho(base.size, "top", 1);
    expect(halfW).toBeCloseTo(2.6);
    expect(halfH).toBeCloseTo(2.6);
  });

  it("zoom 同步缩放两个正交半轴，默认参数保持既有行为", () => {
    const baseFrame = frameOrtho(base.size, "top", 2);
    const zoomed = frameOrtho(base.size, "top", 2, FRAME_MARGIN, 2);
    expect(zoomed.halfW).toBeCloseTo(baseFrame.halfW / 2);
    expect(zoomed.halfH).toBeCloseTo(baseFrame.halfH / 2);
  });
});

describe("三视图离散缩放", () => {
  it("每档乘或除 1.12，往返恢复原值", () => {
    const zoomed = stepTriZoom(1, 1);
    expect(zoomed).toBeCloseTo(1.12);
    expect(stepTriZoom(zoomed, -1)).toBeCloseTo(1);
  });

  it("限制在 0.50x 至 8.00x，并把非法值恢复为 1.00x", () => {
    expect(stepTriZoom(TRI_ZOOM_MAX, 1)).toBe(TRI_ZOOM_MAX);
    expect(stepTriZoom(TRI_ZOOM_MIN, -1)).toBe(TRI_ZOOM_MIN);
    expect(clampTriZoom(Number.NaN)).toBe(1);
  });
});

describe("dragEdge · 半/全边长口径", () => {
  it("Top 拖 +X 边 +0.5m → size[0]=4.5 (全边长, 非 SUSTech 半边长的 5.0)", () => {
    const r = dragEdge(base, 0, 1, 0.5);
    expect(r.size[0]).toBeCloseTo(4.5);
    expect(r.size[1]).toBeCloseTo(2);
    expect(r.size[2]).toBeCloseTo(1.5);
    expect(r.center[0]).toBeCloseTo(0.25);
    expect(r.center[1]).toBeCloseTo(0);
    expect(r.center[2]).toBeCloseTo(0);
    expect(r.rotation).toEqual([0, 0, 0]);
  });

  it("拖 -X 边 +0.5m (往里推) → size 减 0.5, 中心朝 +X 移 0.25 (对侧 +X 固定)", () => {
    const r = dragEdge(base, 0, -1, 0.5);
    expect(r.size[0]).toBeCloseTo(3.5);
    expect(r.center[0]).toBeCloseTo(0.25);
  });

  it("clamp: 把 X 边往里推超过本身尺寸 → 收敛到 MIN_SIZE", () => {
    const r = dragEdge(base, 0, 1, -100);
    expect(r.size[0]).toBeCloseTo(MIN_SIZE);
  });

  it("旋转后中心沿世界轴移动: yaw 90° 时 local +X 世界方向 = +Y", () => {
    const yawed: Psr = { ...base, rotation: [0, 0, Math.PI / 2] };
    const r = dragEdge(yawed, 0, 1, 0.5);
    expect(r.center[0]).toBeCloseTo(0);
    expect(r.center[1]).toBeCloseTo(0.25);
  });
});

describe("dragCorner", () => {
  it("Top 拖 (+X,+Y) 角 各 +0.4m → 两维尺寸各 +0.4, 中心各移半程", () => {
    const r = dragCorner(base, 0, 1, 0.4, 1, 1, 0.4);
    expect(r.size[0]).toBeCloseTo(4.4);
    expect(r.size[1]).toBeCloseTo(2.4);
    expect(r.size[2]).toBeCloseTo(1.5);
    expect(r.center[0]).toBeCloseTo(0.2);
    expect(r.center[1]).toBeCloseTo(0.2);
  });
});

describe("dragHandle · 屏幕 handle → 边/角", () => {
  it("e (右边) = dragEdge(u,+1): Top dU=0.5 → size[0]+0.5, 中心移半程", () => {
    const r = dragHandle(base, "top", "e", 0.5, 0);
    expect(r.size[0]).toBeCloseTo(4.5);
    expect(r.size[1]).toBeCloseTo(2);
    expect(r.center[0]).toBeCloseTo(0.25);
  });

  it("w (左边): Top 往左拖 dU=-0.5 → +X 固定, size[0]+0.5, 中心朝 -X 移", () => {
    const r = dragHandle(base, "top", "w", -0.5, 0);
    expect(r.size[0]).toBeCloseTo(4.5);
    expect(r.center[0]).toBeCloseTo(-0.25);
  });

  it("n (上边) = dragEdge(v,+1): Top dV=0.3 → size[1]+0.3 (width)", () => {
    const r = dragHandle(base, "top", "n", 0, 0.3);
    expect(r.size[1]).toBeCloseTo(2.3);
    expect(r.size[0]).toBeCloseTo(4);
    expect(r.center[1]).toBeCloseTo(0.15);
  });

  it("sw 角: Top dU=-0.3 dV=-0.3 → size 两维各 +0.3, 中心朝 -X/-Y 各移 0.15", () => {
    const r = dragHandle(base, "top", "sw", -0.3, -0.3);
    expect(r.size[0]).toBeCloseTo(4.3);
    expect(r.size[1]).toBeCloseTo(2.3);
    expect(r.center[0]).toBeCloseTo(-0.15);
    expect(r.center[1]).toBeCloseTo(-0.15);
  });

  it("Front 视图 e = 改 width(v=Y? u=Y): u 轴=Y → size[1] 变", () => {
    const r = dragHandle(base, "front", "e", 0.4, 0);
    expect(r.size[1]).toBeCloseTo(2.4);
    expect(r.size[0]).toBeCloseTo(4);
    expect(r.size[2]).toBeCloseTo(1.5);
  });
});

describe("dragRotation · 三轴 yaw/pitch/roll", () => {
  it("Top 纯 yaw: rotation[2] 增量, 其余轴不动", () => {
    const start: Psr = { ...base, rotation: [0, 0, 0.3] };
    const r = dragRotation(start, "top", 0.2);
    expect(r.rotation[0]).toBeCloseTo(0);
    expect(r.rotation[1]).toBeCloseTo(0);
    expect(r.rotation[2]).toBeCloseTo(0.5);
    expect(r.size).toEqual(base.size);
    expect(r.center).toEqual(base.center);
  });

  it.each([
    ["top", 2],
    ["side", 1],
    ["front", 0],
  ] as const)("round-trip: %s 转 dθ 再转 -dθ 复原 (含非零起始姿态)", (view, _axis) => {
    const start: Psr = { ...base, rotation: [0.15, -0.25, 0.35] };
    const there = dragRotation(start, view, 0.3);
    const back = dragRotation(there, view, -0.3);
    const q0 = new THREE.Quaternion().setFromEuler(new THREE.Euler(...start.rotation, "XYZ"));
    const q1 = new THREE.Quaternion().setFromEuler(new THREE.Euler(...back.rotation, "XYZ"));
    expect(Math.abs(q0.dot(q1))).toBeCloseTo(1);
  });

  it("非零 pitch 起调 yaw 不串轴: 绕 local Z 转 → Z 轴方向不变, X 轴方向转过 dθ", () => {
    const start: Psr = { ...base, rotation: [0, 0.4, 0] };
    const dTheta = 0.25;
    const r = dragRotation(start, "top", dTheta);

    const zBefore = boxAxisWorldDir(start.rotation, 2);
    const zAfter = boxAxisWorldDir(r.rotation, 2);
    expect(zAfter.angleTo(zBefore)).toBeCloseTo(0);

    const xBefore = boxAxisWorldDir(start.rotation, 0);
    const xAfter = boxAxisWorldDir(r.rotation, 0);
    expect(xAfter.angleTo(xBefore)).toBeCloseTo(dTheta);
  });
});

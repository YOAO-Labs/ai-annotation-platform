import { describe, it, expect } from "vitest";
import { applyResize, applyRotatedResize } from "./ResizeHandles";
import type { Annotation, RotatedBboxGeometry } from "@/types";

const base: Annotation = {
  id: "x",
  cls: "car",
  conf: 1,
  source: "manual",
  x: 0.4,
  y: 0.4,
  w: 0.2,
  h: 0.1,
};

describe("applyResize · 修饰键和尺寸限制", () => {
  it("baseline: SE 角拖动 +0.1,+0.05 扩展", () => {
    const r = applyResize(base, { x: 0, y: 0 }, { x: 0.1, y: 0.05 }, "se");
    expect(r.x).toBeCloseTo(0.4);
    expect(r.y).toBeCloseTo(0.4);
    expect(r.w).toBeCloseTo(0.3, 5);
    expect(r.h).toBeCloseTo(0.15, 5);
  });

  it("shiftKey: projects corner drag onto the locked aspect-ratio diagonal", () => {
    const r = applyResize(base, { x: 0, y: 0 }, { x: 0.1, y: 0 }, "se", { shiftKey: true });
    expect(r.w / r.h).toBeCloseTo(2, 5);
    expect(r.w).toBeCloseTo(0.28, 5);
    expect(r.h).toBeCloseTo(0.14, 5);
  });

  it("shiftKey: avoids switching between horizontal and vertical master axes near tiny jitter", () => {
    const lower = applyResize(base, { x: 0, y: 0 }, { x: 0.02, y: 0.009 }, "se", {
      shiftKey: true,
    });
    const upper = applyResize(base, { x: 0, y: 0 }, { x: 0.02, y: 0.011 }, "se", {
      shiftKey: true,
    });

    expect(upper.w - lower.w).toBeCloseTo(0.0008, 5);
    expect(upper.h - lower.h).toBeCloseTo(0.0004, 5);
  });

  it("altKey SE 角: 中心扩展，等价两边都 +dx", () => {
    const r = applyResize(base, { x: 0, y: 0 }, { x: 0.1, y: 0.025 }, "se", { altKey: true });
    const cx = r.x + r.w / 2;
    const cy = r.y + r.h / 2;
    expect(cx).toBeCloseTo(0.5, 2);
    expect(cy).toBeCloseTo(0.45, 2);
    expect(r.w).toBeCloseTo(0.4, 2);
    expect(r.h).toBeCloseTo(0.15, 2);
  });

  it("altKey N 边: 仅纵向中心扩展，宽度不变", () => {
    const r = applyResize(base, { x: 0, y: 0 }, { x: 0, y: -0.025 }, "n", { altKey: true });
    const cy = r.y + r.h / 2;
    expect(cy).toBeCloseTo(0.45, 2);
    expect(r.w).toBeCloseTo(0.2, 2);
    expect(r.h).toBeCloseTo(0.15, 2);
  });

  it("shift+alt SE 角: 锁纵横比 + 中心扩展", () => {
    const r = applyResize(base, { x: 0, y: 0 }, { x: 0.1, y: 0 }, "se", {
      shiftKey: true,
      altKey: true,
    });
    expect(r.w / r.h).toBeCloseTo(2, 1);
    const cx = r.x + r.w / 2;
    expect(cx).toBeCloseTo(0.5, 2);
  });

  it("alt 超出 [0,1] 时被 clamp", () => {
    const edge: Annotation = { ...base, x: 0.85, w: 0.1 };
    const r = applyResize(edge, { x: 0, y: 0 }, { x: 0.1, y: 0 }, "e", { altKey: true });
    expect(r.x).toBeGreaterThanOrEqual(0);
    expect(r.x + r.w).toBeLessThanOrEqual(1.0001);
  });
});

const rotatedBase: RotatedBboxGeometry = {
  type: "rotated_bbox",
  cx: 0.5,
  cy: 0.5,
  w: 0.2,
  h: 0.1,
  angle: 0,
};

describe("applyRotatedResize", () => {
  it("angle 0: SE handle resizes like an axis-aligned bbox", () => {
    const r = applyRotatedResize(rotatedBase, { x: 0, y: 0 }, { x: 0.1, y: 0.1 }, "se", {
      w: 1000,
      h: 500,
    });

    expect(r.cx).toBeCloseTo(0.55);
    expect(r.cy).toBeCloseTo(0.55);
    expect(r.w).toBeCloseTo(0.3);
    expect(r.h).toBeCloseTo(0.2);
    expect(r.angle).toBe(rotatedBase.angle);
  });

  it("projects pointer movement onto the rotated local axes", () => {
    const r = applyRotatedResize(
      { ...rotatedBase, h: 0.2, angle: 90 },
      { x: 0, y: 0 },
      { x: 0, y: 0.1 },
      "e",
      { w: 1000, h: 1000 },
    );

    expect(r.cx).toBeCloseTo(0.5);
    expect(r.cy).toBeCloseTo(0.55);
    expect(r.w).toBeCloseTo(0.3);
    expect(r.h).toBeCloseTo(0.2);
  });

  it("altKey keeps the rotated box centered while resizing", () => {
    const r = applyRotatedResize(
      rotatedBase,
      { x: 0, y: 0 },
      { x: 0.05, y: 0.05 },
      "se",
      { w: 1000, h: 1000 },
      { altKey: true },
    );

    expect(r.cx).toBeCloseTo(rotatedBase.cx);
    expect(r.cy).toBeCloseTo(rotatedBase.cy);
    expect(r.w).toBeCloseTo(0.3);
    expect(r.h).toBeCloseTo(0.2);
  });
});

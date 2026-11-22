import { describe, expect, it } from "vitest";
import { fitToCanvas } from "./fit";
import { SCALE_RANGE, clampScale, fitAwareScaleRange, zoomAtPoint } from "./zoom";
import { dashToWorld, screenToWorld } from "./scaleCancel";

describe("fitToCanvas", () => {
  it("内容比视口宽 → 取宽度比,水平贴边、垂直居中", () => {
    const vp = fitToCanvas(800, 600, 1000, 500);
    expect(vp).not.toBeNull();
    expect(vp!.scale).toBeCloseTo(0.8);
    expect(vp!.tx).toBeCloseTo(0);
    expect(vp!.ty).toBeCloseTo(100);
  });

  it("内容比视口高 → 取高度比,垂直贴边、水平居中", () => {
    const vp = fitToCanvas(800, 600, 400, 1200);
    expect(vp!.scale).toBeCloseTo(0.5);
    expect(vp!.tx).toBeCloseTo(300);
    expect(vp!.ty).toBeCloseTo(0);
  });

  it("任一尺寸为 0(未就绪)→ 返回 null,调用方保留当前 vp", () => {
    expect(fitToCanvas(0, 600, 1000, 500)).toBeNull();
    expect(fitToCanvas(800, 0, 1000, 500)).toBeNull();
    expect(fitToCanvas(800, 600, 0, 500)).toBeNull();
    expect(fitToCanvas(800, 600, 1000, 0)).toBeNull();
  });
});

describe("clampScale", () => {
  it("夹到默认 [0.2, 8] 范围", () => {
    expect(clampScale(0.05)).toBe(SCALE_RANGE.min);
    expect(clampScale(20)).toBe(SCALE_RANGE.max);
    expect(clampScale(1.5)).toBe(1.5);
  });

  it("支持自定义范围", () => {
    expect(clampScale(5, { min: 1, max: 4 })).toBe(4);
    expect(clampScale(0.5, { min: 1, max: 4 })).toBe(1);
  });

  it("大图下限放宽到当前视口的适应比例", () => {
    const range = fitAwareScaleRange(1200, 800, 21_600, 10_800);
    expect(range.min).toBeCloseTo(1200 / 21_600);
    expect(range.max).toBe(SCALE_RANGE.max);
  });

  it("普通图片仍保持默认 20% 下限", () => {
    expect(fitAwareScaleRange(1200, 800, 1200, 800)).toEqual(SCALE_RANGE);
  });
});

describe("zoomAtPoint", () => {
  it("围绕光标定点放大:该屏幕点的世界坐标不漂移", () => {
    const vp = { scale: 1, tx: 0, ty: 0 };
    const cx = 200;
    const cy = 150;
    const worldBefore = { x: (cx - vp.tx) / vp.scale, y: (cy - vp.ty) / vp.scale };
    const next = zoomAtPoint(vp, cx, cy, 2);
    expect(next.scale).toBe(2);
    const screenX = worldBefore.x * next.scale + next.tx;
    const screenY = worldBefore.y * next.scale + next.ty;
    expect(screenX).toBeCloseTo(cx);
    expect(screenY).toBeCloseTo(cy);
  });

  it("目标 scale 超上限 → clamp,且仍围绕定点", () => {
    const vp = { scale: 4, tx: 10, ty: 20 };
    const next = zoomAtPoint(vp, 100, 100, 100);
    expect(next.scale).toBe(SCALE_RANGE.max);
  });

  it("clamp 后 scale 未变 → 原样返回同一引用(不抖动)", () => {
    const vp = { scale: SCALE_RANGE.max, tx: 10, ty: 20 };
    expect(zoomAtPoint(vp, 100, 100, 999)).toBe(vp);
  });

  it("自定义大图下限时可从 20% 以下继续缩小", () => {
    const range = fitAwareScaleRange(1200, 800, 21_600, 10_800);
    const next = zoomAtPoint({ scale: 0.1, tx: 0, ty: 0 }, 600, 400, 0.08, range);
    expect(next.scale).toBeCloseTo(0.08);
  });
});

describe("screenToWorld / dashToWorld", () => {
  it("屏幕像素量除以 scale 换算成世界量", () => {
    expect(screenToWorld(2, 1)).toBe(2);
    expect(screenToWorld(2, 4)).toBe(0.5);
  });

  it("dash 数组逐项抵消", () => {
    expect(dashToWorld([4, 3], 2)).toEqual([2, 1.5]);
  });
});

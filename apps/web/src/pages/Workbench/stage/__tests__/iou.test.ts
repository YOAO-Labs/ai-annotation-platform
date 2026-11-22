import { describe, it, expect } from "vitest";
import { iou, iouShape } from "../iou";

describe("iou (axis-aligned)", () => {
  it("完全重叠 → 1", () => {
    const g = { x: 0, y: 0, w: 1, h: 1 };
    expect(iou(g, g)).toBe(1);
  });

  it("不相交 → 0", () => {
    expect(iou({ x: 0, y: 0, w: 1, h: 1 }, { x: 2, y: 2, w: 1, h: 1 })).toBe(0);
  });

  it("仅边接触 → 0（iw <= 0）", () => {
    expect(iou({ x: 0, y: 0, w: 1, h: 1 }, { x: 1, y: 0, w: 1, h: 1 })).toBe(0);
  });

  it("一半重叠 → 1/3", () => {
    const v = iou({ x: 0, y: 0, w: 2, h: 1 }, { x: 1, y: 0, w: 2, h: 1 });
    expect(v).toBeCloseTo(1 / 3, 6);
  });

  it("退化 (w=0) → 0", () => {
    expect(iou({ x: 0, y: 0, w: 0, h: 1 }, { x: 0, y: 0, w: 1, h: 1 })).toBe(0);
  });
});

describe("iouShape", () => {
  it("两个 bbox 不带 polygon → 走 axis-aligned", () => {
    const a = { x: 0, y: 0, w: 1, h: 1 };
    const b = { x: 0, y: 0, w: 1, h: 1 };
    expect(iouShape(a, b)).toBe(1);
  });

  it("polygon vs polygon 完全重叠", () => {
    const sq: [number, number][] = [
      [0, 0],
      [1, 0],
      [1, 1],
      [0, 1],
    ];
    const a = { x: 0, y: 0, w: 1, h: 1, polygon: sq };
    const b = { x: 0, y: 0, w: 1, h: 1, polygon: sq };
    expect(iouShape(a, b)).toBeCloseTo(1, 6);
  });

  it("polygon 与 bbox 完全错开 → 0", () => {
    const tri: [number, number][] = [
      [2, 2],
      [3, 2],
      [2.5, 3],
    ];
    const a = { x: 2, y: 2, w: 1, h: 1, polygon: tri };
    const b = { x: 0, y: 0, w: 1, h: 1 };
    expect(iouShape(a, b)).toBe(0);
  });

  it("退化 polygon (2 点) → 走 bbox 回落", () => {
    const a = {
      x: 0,
      y: 0,
      w: 1,
      h: 1,
      polygon: [
        [0, 0],
        [1, 0],
      ] as [number, number][],
    };
    const b = { x: 0, y: 0, w: 1, h: 1 };
    expect(iouShape(a, b)).toBe(1);
  });
});

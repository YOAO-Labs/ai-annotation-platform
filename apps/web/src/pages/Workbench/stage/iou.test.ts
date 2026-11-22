import { describe, expect, it } from "vitest";
import { iou, iouShape } from "./iou";

describe("iou (bbox)", () => {
  it("identical boxes → 1", () => {
    expect(iou({ x: 0.1, y: 0.1, w: 0.2, h: 0.2 }, { x: 0.1, y: 0.1, w: 0.2, h: 0.2 })).toBeCloseTo(
      1,
      6,
    );
  });

  it("disjoint → 0", () => {
    expect(iou({ x: 0, y: 0, w: 0.1, h: 0.1 }, { x: 0.5, y: 0.5, w: 0.1, h: 0.1 })).toBe(0);
  });

  it("touching but not overlapping → 0", () => {
    expect(iou({ x: 0, y: 0, w: 0.1, h: 0.1 }, { x: 0.1, y: 0, w: 0.1, h: 0.1 })).toBe(0);
  });

  it("half overlap horizontally → 1/3", () => {
    const v = iou({ x: 0, y: 0, w: 0.2, h: 0.1 }, { x: 0.1, y: 0, w: 0.2, h: 0.1 });
    expect(v).toBeCloseTo(0.01 / 0.03, 6);
  });

  it("contained box (b inside a) → b.area / a.area", () => {
    const v = iou({ x: 0, y: 0, w: 0.4, h: 0.4 }, { x: 0.1, y: 0.1, w: 0.1, h: 0.1 });
    expect(v).toBeCloseTo(0.01 / 0.16, 6);
  });

  it("zero area defensive → 0", () => {
    expect(iou({ x: 0, y: 0, w: 0, h: 0 }, { x: 0, y: 0, w: 0, h: 0 })).toBe(0);
  });
});

describe("iouShape (polygon)", () => {
  const square = (x: number, y: number, w: number, h: number): [number, number][] => [
    [x, y],
    [x + w, y],
    [x + w, y + h],
    [x, y + h],
  ];

  it("identical polygons → 1", () => {
    const poly: [number, number][] = square(0.1, 0.1, 0.2, 0.2);
    const a = { x: 0.1, y: 0.1, w: 0.2, h: 0.2, polygon: poly };
    const b = { x: 0.1, y: 0.1, w: 0.2, h: 0.2, polygon: poly };
    expect(iouShape(a, b)).toBeCloseTo(1, 6);
  });

  it("disjoint polygons → 0", () => {
    const a = { x: 0, y: 0, w: 0.1, h: 0.1, polygon: square(0, 0, 0.1, 0.1) };
    const b = { x: 0.5, y: 0.5, w: 0.1, h: 0.1, polygon: square(0.5, 0.5, 0.1, 0.1) };
    expect(iouShape(a, b)).toBe(0);
  });

  it("half overlap polygons match bbox iou for axis-aligned", () => {
    const a = { x: 0, y: 0, w: 0.2, h: 0.1, polygon: square(0, 0, 0.2, 0.1) };
    const b = { x: 0.1, y: 0, w: 0.2, h: 0.1, polygon: square(0.1, 0, 0.2, 0.1) };
    expect(iouShape(a, b)).toBeCloseTo(0.01 / 0.03, 5);
  });

  it("polygon vs bbox: a triangle covering half a unit square → 0.5", () => {
    const tri: [number, number][] = [
      [0, 0],
      [1, 0],
      [0, 1],
    ];
    const triShape = { x: 0, y: 0, w: 1, h: 1, polygon: tri };
    const bbox = { x: 0, y: 0, w: 1, h: 1 };
    expect(iouShape(triShape, bbox)).toBeCloseTo(0.5, 5);
  });
});

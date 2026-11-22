import { describe, it, expect } from "vitest";
import type { Annotation } from "@/types";
import { translateGeometry, translatePoints, clamp01 } from "./geometryTranslate";

function anno(geometry: Annotation["geometry"]): Annotation {
  return { id: "a", geometry, x: 0, y: 0, w: 0, h: 0 } as Annotation;
}

describe("clamp01 / translatePoints", () => {
  it("clamp01 夹到 [0,1]", () => {
    expect(clamp01(-0.2)).toBe(0);
    expect(clamp01(1.5)).toBe(1);
    expect(clamp01(0.3)).toBe(0.3);
  });
  it("translatePoints 求公共上下界后统一平移, 贴边时保持形状不被拉扁", () => {
    const out = translatePoints(
      [
        [0.1, 0.1],
        [0.9, 0.9],
      ],
      0.2,
      -0.05,
    );
    expect(out[0][0]).toBeCloseTo(0.2, 10);
    expect(out[0][1]).toBeCloseTo(0.05, 10);
    expect(out[1][0]).toBeCloseTo(1.0, 10);
    expect(out[1][1]).toBeCloseTo(0.85, 10);
    expect(out[1][0] - out[0][0]).toBeCloseTo(0.8, 10);
  });
});

describe("translateGeometry", () => {
  it("bbox 平移, x/y clamp 到不越界 (保 w/h)", () => {
    const { geometry } = translateGeometry(
      anno({ type: "bbox", x: 0.2, y: 0.2, w: 0.3, h: 0.3 }),
      0.1,
      -0.1,
    );
    expect(geometry).toEqual({ type: "bbox", x: 0.30000000000000004, y: 0.1, w: 0.3, h: 0.3 });
  });

  it("bbox 越右边界 → x clamp 到 1-w", () => {
    const { geometry } = translateGeometry(
      anno({ type: "bbox", x: 0.8, y: 0.1, w: 0.3, h: 0.3 }),
      0.5,
      0,
    );
    expect((geometry as { x: number }).x).toBeCloseTo(0.7);
  });

  it("polygon 逐顶点平移", () => {
    const { geometry } = translateGeometry(
      anno({
        type: "polygon",
        points: [
          [0.1, 0.1],
          [0.2, 0.2],
          [0.3, 0.1],
        ],
      }),
      0.1,
      0.1,
    );
    expect((geometry as { points: [number, number][] }).points).toEqual([
      [0.2, 0.2],
      [0.30000000000000004, 0.30000000000000004],
      [0.4, 0.2],
    ]);
  });

  it("polygon 贴右边界: 顶点被公共上下界截, 形状不被压扁 (Alt 拖动联动正确)", () => {
    const { geometry } = translateGeometry(
      anno({
        type: "polygon",
        points: [
          [0.1, 0.2],
          [0.5, 0.4],
          [0.9, 0.2],
        ],
      }),
      0.5,
      0,
    );
    const pts = (geometry as { points: [number, number][] }).points;
    expect(pts.map(([x]) => Number(x.toFixed(6)))).toEqual([0.2, 0.6, 1.0]);
    expect(pts.map(([, y]) => y)).toEqual([0.2, 0.4, 0.2]);
    expect(pts[2][0] - pts[0][0]).toBeCloseTo(0.8, 10);
  });

  it("multi_polygon 跨 polygon 共享 shift: 两块相对位置不漂移", () => {
    const { geometry } = translateGeometry(
      anno({
        type: "multi_polygon",
        polygons: [
          {
            type: "polygon",
            points: [
              [0.1, 0.2],
              [0.3, 0.2],
              [0.2, 0.4],
            ],
          },
          {
            type: "polygon",
            points: [
              [0.6, 0.2],
              [0.8, 0.2],
              [0.7, 0.4],
            ],
          },
        ],
      }),
      0.5,
      0,
    );
    const polys = (geometry as { polygons: { points: [number, number][] }[] }).polygons;
    const flat = polys.flatMap((p) => p.points);
    const expected: [number, number][] = [
      [0.3, 0.2],
      [0.5, 0.2],
      [0.4, 0.4],
      [0.8, 0.2],
      [1.0, 0.2],
      [0.9, 0.4],
    ];
    flat.forEach(([x, y], i) => {
      expect(x).toBeCloseTo(expected[i][0], 10);
      expect(y).toBeCloseTo(expected[i][1], 10);
    });
    expect(polys[1].points[0][0] - polys[0].points[1][0]).toBeCloseTo(0.3, 10);
  });

  it("rotated_bbox 平移中心 cx/cy, 保 angle/w/h", () => {
    const { geometry } = translateGeometry(
      anno({ type: "rotated_bbox", cx: 0.4, cy: 0.4, w: 0.2, h: 0.1, angle: 30 }),
      0.1,
      0.1,
    );
    expect(geometry).toMatchObject({ cx: 0.5, cy: 0.5, angle: 30, w: 0.2, h: 0.1 });
  });

  it("raster_mask 拒绝降级为 bbox", () => {
    const raster = anno({
      type: "raster_mask",
      mask: {
        encoding: "coco_rle_ref",
        size: [10, 20],
        object_key: "raster-masks/sha256/aa/bb/digest.json",
        sha256: "a".repeat(64),
        runs: 4,
        bytes: 32,
      },
    });

    expect(() => translateGeometry(raster, 0.1, 0.1)).toThrow(
      "raster_mask does not support geometric translation",
    );
  });
});

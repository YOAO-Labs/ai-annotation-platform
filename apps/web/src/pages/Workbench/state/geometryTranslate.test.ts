// geometryTranslate 平移守护 (粘贴偏移 + 父子 Alt 拖动联动共用同一套)。
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
    // 点集 x∈[0.1,0.9] (宽 0.8), 要求平移 +0.2: 右侧界 = 1 - 0.9 = 0.1, dx clamp 到 0.1
    // → 平移后 x∈[0.2, 1.0] (宽仍 0.8, 未被拉伸)。y 未越界 → dy = -0.05 直接施加。
    // (浮点尾巴: 0.1 + 0.1 = 0.19999999999999998, 保持形状这条不变量本身仍成立)
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
    // 关键不变量: 宽度 (max - min) 平移前后一致 → 形状不被拉扁。
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
    expect((geometry as { x: number }).x).toBeCloseTo(0.7); // 1 - 0.3
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
    // 三角形 x∈[0.1, 0.9] (宽 0.8), 请求平移 +0.5: 右侧界 = 1 - 0.9 = 0.1, dx clamp 0.1
    // → 结果 x∈[0.2, 1.0] (宽仍 0.8)。原逐点 clamp 会得到 [[0.6,...], [0.7,...], [1.0,...]]
    // 宽度被压到 0.4, 形状拉扁。此测试守护「保持形状」这条不变量。
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
    // y 不越界: 直接 +0
    expect(pts.map(([, y]) => y)).toEqual([0.2, 0.4, 0.2]);
    // 宽度保持 = 0.8
    expect(pts[2][0] - pts[0][0]).toBeCloseTo(0.8, 10);
  });

  it("multi_polygon 跨 polygon 共享 shift: 两块相对位置不漂移", () => {
    // 两个 polygon: A 在左 (x∈[0.1,0.3]), B 在右 (x∈[0.6,0.8]), 请求 +0.5
    // 合并 bbox x∈[0.1,0.8], 右侧界 = 1 - 0.8 = 0.2 < 0.5 → dx = 0.2
    // 两个 polygon 各自被同一 0.2 平移: A→[0.3,0.5], B→[0.8,1.0]。相对位置保持 (原 gap 0.3, 现仍 0.3)。
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
    // 各顶点被同一 dx=0.2 平移 (allow tiny float tail)。
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
    // 关键不变量: A 与 B 之间相对间距不漂移 (原 0.3, 现仍 0.3)。
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

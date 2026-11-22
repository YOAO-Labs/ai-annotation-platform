import { describe, expect, it } from "vitest";
import type { Geometry } from "@/types";
import { geometryMetrics, shoelaceArea, polylinePerimeterPx } from "./geometryMetrics";

function byLabel(metrics: { label: string; value: string; hint?: string }[], label: string) {
  const m = metrics.find((x) => x.label === label);
  if (!m)
    throw new Error(`metric ${label} not found in [${metrics.map((x) => x.label).join(", ")}]`);
  return m;
}

describe("shoelaceArea / polylinePerimeterPx", () => {
  it("半幅正方形面积 = 0.25", () => {
    expect(
      shoelaceArea([
        [0, 0],
        [0.5, 0],
        [0.5, 0.5],
        [0, 0.5],
      ]),
    ).toBeCloseTo(0.25, 6);
  });
  it("退化(<3 点)面积为 0", () => {
    expect(
      shoelaceArea([
        [0, 0],
        [1, 1],
      ]),
    ).toBe(0);
  });
  it("闭合周长计入闭合边", () => {
    const p = polylinePerimeterPx(
      [
        [0, 0],
        [0.5, 0],
        [0.5, 0.5],
        [0, 0.5],
      ],
      1000,
      800,
      true,
    );
    expect(p).toBeCloseTo(1800, 3);
  });
  it("折线周长不计闭合边", () => {
    const p = polylinePerimeterPx(
      [
        [0, 0],
        [0.5, 0],
        [0.5, 0.5],
      ],
      1000,
      800,
      false,
    );
    expect(p).toBeCloseTo(900, 3);
  });
});

describe("geometryMetrics · bbox", () => {
  const g: Geometry = { type: "bbox", x: 0.1, y: 0.1, w: 0.25, h: 0.2 };

  it("有像素维度:尺寸 / 占图 / 位置 / 宽高比", () => {
    const m = geometryMetrics(g, 1920, 1080);
    expect(byLabel(m, "尺寸").value).toBe("480×216 px");
    expect(byLabel(m, "占图").value).toBe("5.0%");
    expect(byLabel(m, "位置").value).toBe("192, 108");
    expect(byLabel(m, "宽高比").value).toBe("2.22 : 1");
  });

  it("无像素维度:降级为相对值,省略宽高比", () => {
    const m = geometryMetrics(g, null, null);
    expect(byLabel(m, "尺寸").value).toBe("25.0%×20.0%");
    expect(byLabel(m, "占图").value).toBe("5.0%");
    expect(m.find((x) => x.label === "宽高比")).toBeUndefined();
  });

  it("竖向框宽高比写作 1 : N", () => {
    const tall: Geometry = { type: "bbox", x: 0, y: 0, w: 0.1, h: 0.4 };
    const m = geometryMetrics(tall, 1000, 1000);
    expect(byLabel(m, "宽高比").value).toBe("1 : 4.00");
  });
});

describe("geometryMetrics · polygon", () => {
  it("顶点 / 占图 / 面积 px² / 周长", () => {
    const g: Geometry = {
      type: "polygon",
      points: [
        [0, 0],
        [0.5, 0],
        [0.5, 0.5],
        [0, 0.5],
      ],
    };
    const m = geometryMetrics(g, 1000, 800);
    expect(byLabel(m, "顶点").value).toBe("4");
    expect(byLabel(m, "占图").value).toBe("25.0%");
    expect(byLabel(m, "面积").value).toBe("≈ 200,000 px²");
    expect(byLabel(m, "周长").value).toBe("≈ 1,800 px");
  });

  it("内环面积从净面积中扣除", () => {
    const g: Geometry = {
      type: "polygon",
      points: [
        [0, 0],
        [0.5, 0],
        [0.5, 0.5],
        [0, 0.5],
      ],
      holes: [
        [
          [0.1, 0.1],
          [0.3, 0.1],
          [0.3, 0.3],
          [0.1, 0.3],
        ],
      ],
    };
    const m = geometryMetrics(g, 1000, 1000);
    expect(byLabel(m, "占图").value).toBe("21.0%");
    expect(byLabel(m, "顶点").value).toBe("8");
    expect(byLabel(m, "顶点").hint).toBe("外环 4 + 1 内环");
    expect(byLabel(m, "周长").value).toBe("≈ 2,800 px");
  });
});

describe("geometryMetrics · 其它几何", () => {
  it("multi_polygon 环 / 顶点 + 总面积", () => {
    const g: Geometry = {
      type: "multi_polygon",
      polygons: [
        {
          type: "polygon",
          points: [
            [0, 0],
            [0.5, 0],
            [0.5, 0.5],
            [0, 0.5],
          ],
          holes: [
            [
              [0.1, 0.1],
              [0.2, 0.1],
              [0.2, 0.2],
              [0.1, 0.2],
            ],
          ],
        },
        {
          type: "polygon",
          points: [
            [0.5, 0.5],
            [1, 0.5],
            [1, 1],
            [0.5, 1],
          ],
        },
      ],
    };
    const m = geometryMetrics(g, 1000, 1000);
    expect(byLabel(m, "环 / 顶点").value).toBe("3 / 12");
    expect(byLabel(m, "占图").value).toBe("49.0%");
  });

  it("polyline 点数 + 总长", () => {
    const g: Geometry = {
      type: "polyline",
      points: [
        [0, 0],
        [0.5, 0],
        [0.5, 0.5],
      ],
    };
    const m = geometryMetrics(g, 1000, 800);
    expect(byLabel(m, "点数").value).toBe("3");
    expect(byLabel(m, "总长").value).toBe("≈ 900 px");
  });

  it("keypoint 可见 / 总数 + 遮挡提示", () => {
    const g: Geometry = {
      type: "keypoint",
      points: [
        { x: 0, y: 0, v: 2 },
        { x: 0, y: 0, v: 2 },
        { x: 0, y: 0, v: 1 },
        { x: 0, y: 0, v: 0 },
      ],
    };
    const m = geometryMetrics(g, 1000, 1000);
    expect(byLabel(m, "可见").value).toBe("2 / 4");
    expect(byLabel(m, "可见").hint).toBe("1 遮挡 · 1 跳过");
  });

  it("rotated_bbox 旋转角 + 占图", () => {
    const g: Geometry = { type: "rotated_bbox", cx: 0.5, cy: 0.5, w: 0.2, h: 0.1, angle: 37 };
    const m = geometryMetrics(g, 1000, 1000);
    expect(byLabel(m, "尺寸").value).toBe("200×100 px");
    expect(byLabel(m, "占图").value).toBe("2.0%");
    expect(byLabel(m, "旋转角").value).toBe("37°");
  });

  it("video_bbox 复用 bbox 指标", () => {
    const g: Geometry = { type: "video_bbox", frame_index: 12, x: 0.1, y: 0.1, w: 0.25, h: 0.2 };
    const m = geometryMetrics(g, 1920, 1080);
    expect(byLabel(m, "尺寸").value).toBe("480×216 px");
  });

  it("Raster / Video Mask 显示画布、RLE 编码段与存储大小", () => {
    const mask = {
      encoding: "coco_rle_ref" as const,
      size: [1080, 1920] as [number, number],
      object_key: "mask/test",
      sha256: "a".repeat(64),
      runs: 12_345,
      bytes: 2_560,
    };
    for (const geometry of [
      { type: "raster_mask" as const, mask },
      { type: "video_mask" as const, frame_index: 12, mask },
    ]) {
      const metrics = geometryMetrics(geometry, 1920, 1080);
      expect(byLabel(metrics, "画布").value).toBe("1,920×1,080 px");
      expect(byLabel(metrics, "编码段").value).toBe("12,345");
      expect(byLabel(metrics, "存储")).toEqual({
        label: "存储",
        value: "2.5 KB",
        hint: "COCO RLE",
      });
    }
  });

  it("video_track_bbox / 3D 几何返回空数组", () => {
    const track: Geometry = { type: "video_track_bbox", track_id: "t1", keyframes: [] };
    expect(geometryMetrics(track, 1000, 1000)).toEqual([]);
  });
});

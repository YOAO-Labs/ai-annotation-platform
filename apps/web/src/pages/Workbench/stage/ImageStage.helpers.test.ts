import { describe, it, expect } from "vitest";
import {
  imageBoxFromDrag,
  isNormalizedImagePoint,
  normalizeImageCoordinate,
  resolveSnapMatch,
  shouldRenderImageAnnotationShape,
  siblingHighlightChildren,
} from "./ImageStage.helpers";
import type { Pt } from "./polygonGeom";

describe("imageBoxFromDrag", () => {
  it.each([
    [0.7, 0.6],
    [0.3, 0.6],
    [0.3, 0.4],
    [0.7, 0.4],
  ])("keeps the down point at the center for endpoint %s,%s", (cx, cy) => {
    const box = imageBoxFromDrag({ sx: 0.5, sy: 0.5, cx, cy, fromCenter: true });
    expect(box.x + box.w / 2).toBeCloseTo(0.5);
    expect(box.y + box.h / 2).toBeCloseTo(0.5);
    expect(box.w).toBeCloseTo(0.4);
    expect(box.h).toBeCloseTo(0.2);
  });

  it.each([
    [0.05, 0.5],
    [0.95, 0.5],
    [0.5, 0.05],
    [0.5, 0.95],
  ])("limits both sides symmetrically near image boundary %s,%s", (sx, sy) => {
    for (const [cx, cy] of [
      [-1, -1],
      [2, 2],
    ]) {
      const box = imageBoxFromDrag({ sx, sy, cx, cy, fromCenter: true });
      expect(box.x + box.w / 2).toBeCloseTo(sx);
      expect(box.y + box.h / 2).toBeCloseTo(sy);
      expect(box.x).toBeGreaterThanOrEqual(0);
      expect(box.y).toBeGreaterThanOrEqual(0);
      expect(box.x + box.w).toBeLessThanOrEqual(1);
      expect(box.y + box.h).toBeLessThanOrEqual(1);
    }
  });

  it("retains ordinary corner geometry and zero movement", () => {
    const corner = imageBoxFromDrag({ sx: 0.7, sy: 0.6, cx: 0.2, cy: 0.1 });
    expect(corner).toMatchObject({ x: 0.2, y: 0.1, h: 0.5 });
    expect(corner.w).toBeCloseTo(0.5);
    expect(imageBoxFromDrag({ sx: 0.4, sy: 0.3, cx: 0.4, cy: 0.3, fromCenter: true })).toEqual({
      x: 0.4,
      y: 0.3,
      w: 0,
      h: 0,
    });
  });
});

describe("siblingHighlightChildren", () => {
  const boxes = [
    { id: "p", parent_annotation_id: null },
    { id: "c1", parent_annotation_id: "p" },
    { id: "c2", parent_annotation_id: "p" },
    { id: "other", parent_annotation_id: "q" },
  ];

  it("单选父框 → 返回其直接子框", () => {
    expect(siblingHighlightChildren(boxes, "p", 1).map((b) => b.id)).toEqual(["c1", "c2"]);
  });

  it("单选无子框的框 → 空", () => {
    expect(siblingHighlightChildren(boxes, "other", 1)).toEqual([]);
  });

  it("多选 → 空 (语义模糊不画环)", () => {
    expect(siblingHighlightChildren(boxes, "p", 2)).toEqual([]);
  });

  it("无选 → 空", () => {
    expect(siblingHighlightChildren(boxes, null, 0)).toEqual([]);
  });
});

describe("shouldRenderImageAnnotationShape", () => {
  it("raster_mask 不落入 ImageStage 矢量 shape 分支", () => {
    expect(
      shouldRenderImageAnnotationShape({
        geometry: {
          type: "raster_mask",
          mask: {
            encoding: "coco_rle_ref",
            size: [10, 20],
            object_key: "raster-masks/sha256/aa/bb/digest.json",
            sha256: "a".repeat(64),
            runs: 4,
            bytes: 32,
          },
        },
      }),
    ).toBe(false);
    expect(
      shouldRenderImageAnnotationShape({
        geometry: { type: "bbox", x: 0.1, y: 0.2, w: 0.3, h: 0.4 },
      }),
    ).toBe(true);
  });
});

describe("normalizeImageCoordinate", () => {
  it("逆 viewport 平移/缩放后归一为图坐标", () => {
    const out = normalizeImageCoordinate(
      230,
      0,
      { left: 10, top: 0 },
      { tx: 20, ty: 0, scale: 2 },
      100,
      100,
    );
    expect(out.x).toBeCloseTo(1);
  });

  it("无平移无缩放时即比例坐标", () => {
    const out = normalizeImageCoordinate(
      50,
      25,
      { left: 0, top: 0 },
      { tx: 0, ty: 0, scale: 1 },
      100,
      50,
    );
    expect(out).toEqual({ x: 0.5, y: 0.5 });
  });
});

describe("isNormalizedImagePoint", () => {
  it("仅接受图片边界内的归一化坐标", () => {
    expect(isNormalizedImagePoint({ x: 0, y: 1 })).toBe(true);
    expect(isNormalizedImagePoint({ x: 1.001, y: 0.5 })).toBe(false);
    expect(isNormalizedImagePoint({ x: 0.5, y: -0.001 })).toBe(false);
    expect(isNormalizedImagePoint(null)).toBe(false);
  });
});

const SNAP_T = { imgW: 100, imgH: 100, scale: 1 };
const P = (x: number, y: number): Pt => [x, y];

describe("resolveSnapMatch", () => {
  it("点吸附与线段吸附都命中时取距离更近者", () => {
    const m = resolveSnapMatch(
      P(0.5, 0.5),
      {
        points: [{ point: P(0.51, 0.5) }],
        segments: [{ a: P(0.5, 0.6), b: P(0.6, 0.6) }],
      },
      20,
      SNAP_T,
    );
    expect(m?.kind).toBe("point");
    expect(m?.distancePx).toBeCloseTo(1);
  });

  it("只有线段命中时返回 segment", () => {
    const m = resolveSnapMatch(
      P(0.5, 0.5),
      { points: [], segments: [{ a: P(0.5, 0.6), b: P(0.6, 0.6) }] },
      20,
      SNAP_T,
    );
    expect(m?.kind).toBe("segment");
  });

  it("阈值内无候选时返回 null", () => {
    const m = resolveSnapMatch(
      P(0.5, 0.5),
      { points: [{ point: P(0.9, 0.9) }], segments: [] },
      5,
      SNAP_T,
    );
    expect(m).toBeNull();
  });
});

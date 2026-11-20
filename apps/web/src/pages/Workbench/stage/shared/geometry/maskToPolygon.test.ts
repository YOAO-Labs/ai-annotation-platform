// maskToPolygon 单测：
// - 空 mask → 空
// - 矩形 mask → 顶点紧绕外环（≥3，闭合后去重）
// - 圆形 mask → 顶点数远小于 perimeter（RDP 起效）
// - 多连通 mask → 取最大分量；multipleComponents=true
// - 顶点无重复（去重生效）
//
// (ADR-0052 D5) · 新增 lossy 显式报告测试:
// - 多连通 → lossy=true, droppedComponents=分量数-1, lossyReason 非空
// - 单前景连通但含背景孔洞 → lossy=true, droppedHoles=孔洞数
// - 单连通 → lossy=false
// - 空 mask → lossy=false (无几何可丢)

import { describe, expect, it } from "vitest";
import { MaskBuffer } from "./maskBuffer";
import { maskToPolygon } from "./maskToPolygon";

function makeRect(
  w: number,
  h: number,
  x0: number,
  y0: number,
  x1: number,
  y1: number,
): MaskBuffer {
  const m = new MaskBuffer({ width: w, height: h });
  m.fromPolygon([
    [x0, y0],
    [x1, y0],
    [x1, y1],
    [x0, y1],
  ]);
  return m;
}

function makeDonut(w = 40, h = 40): MaskBuffer {
  const m = new MaskBuffer({ width: w, height: h });
  for (let y = 5; y < h - 5; y++) {
    for (let x = 5; x < w - 5; x++) m.data[y * w + x] = 255;
  }
  for (let y = 14; y < h - 14; y++) {
    for (let x = 14; x < w - 14; x++) m.data[y * w + x] = 0;
  }
  return m;
}

describe("maskToPolygon", () => {
  it("空 mask → 空 points", () => {
    const m = new MaskBuffer({ width: 20, height: 20 });
    const out = maskToPolygon(m);
    expect(out.points).toEqual([]);
    expect(out.multipleComponents).toBe(false);
  });

  it("矩形 mask → polygon 形状贴近", () => {
    const m = makeRect(50, 50, 10, 10, 30, 30);
    const out = maskToPolygon(m, { simplifyEpsilon: 0.5 });
    expect(out.points.length).toBeGreaterThanOrEqual(4);
    // bounding box 应该接近 (10..30, 10..30)
    const xs = out.points.map((p) => p[0]);
    const ys = out.points.map((p) => p[1]);
    expect(Math.min(...xs)).toBeLessThanOrEqual(11);
    expect(Math.max(...xs)).toBeGreaterThanOrEqual(29);
    expect(Math.min(...ys)).toBeLessThanOrEqual(11);
    expect(Math.max(...ys)).toBeGreaterThanOrEqual(29);
    expect(out.multipleComponents).toBe(false);
  });

  it("圆形 mask → 简化后顶点数远小于 perimeter", () => {
    const m = new MaskBuffer({ width: 80, height: 80 });
    m.brush(40, 40, 20);
    const out = maskToPolygon(m, { simplifyEpsilon: 1.5 });
    // 半径 20 圆周 ≈ 125 像素；简化后应 < 60
    expect(out.points.length).toBeGreaterThan(8);
    expect(out.points.length).toBeLessThan(60);
  });

  it("两个分量 → 取面积更大的；multipleComponents=true", () => {
    const m = new MaskBuffer({ width: 60, height: 60 });
    // 大方块 20x20
    m.fromPolygon([
      [5, 5],
      [25, 5],
      [25, 25],
      [5, 25],
    ]);
    // 小方块 5x5
    m.fromPolygon([
      [40, 40],
      [45, 40],
      [45, 45],
      [40, 45],
    ]);
    const out = maskToPolygon(m, { simplifyEpsilon: 0.5 });
    expect(out.multipleComponents).toBe(true);
    // 取的应该是大方块（x 范围在 0..30）
    const xs = out.points.map((p) => p[0]);
    expect(Math.max(...xs)).toBeLessThan(30);
  });

  it("输出顶点无连续重复", () => {
    const m = makeRect(40, 40, 5, 5, 35, 35);
    const out = maskToPolygon(m);
    for (let i = 1; i < out.points.length; i++) {
      const a = out.points[i - 1];
      const b = out.points[i];
      expect(a[0] === b[0] && a[1] === b[1]).toBe(false);
    }
  });

  it("threshold 控制实心判定", () => {
    const m = new MaskBuffer({ width: 20, height: 20 });
    // 单点画半径 3 → alpha=255
    m.brush(10, 10, 3);
    // threshold=200 应找到分量；threshold=300（>255）则没像素 → 空
    expect(maskToPolygon(m, { threshold: 200 }).points.length).toBeGreaterThan(0);
    expect(maskToPolygon(m, { threshold: 300 }).points).toEqual([]);
  });

  it("simplifyEpsilon=0 跳过简化", () => {
    const m = new MaskBuffer({ width: 80, height: 80 });
    m.brush(40, 40, 20);
    const off = maskToPolygon(m, { simplifyEpsilon: 0 });
    const on = maskToPolygon(m, { simplifyEpsilon: 1.5 });
    expect(off.points.length).toBeGreaterThan(on.points.length);
  });
});

// ADR-0052 D5: maskToPolygon 必须显式报告 lossy, 不再静默 pickLargest。
// 调用方 (useImageAnnotationActions.commitMaskAsPolygon) 检查 lossy 后阻断有损提交。
describe("maskToPolygon · lossy 显式报告", () => {
  it("test_mask_to_polygon_marks_lossy_on_multiple_components: 2 个分量 → lossy=true, droppedComponents=1", () => {
    const m = new MaskBuffer({ width: 60, height: 60 });
    // 两个互不相交的方块 → 2 个连通分量。
    m.fromPolygon([
      [5, 5],
      [25, 5],
      [25, 25],
      [5, 25],
    ]);
    m.fromPolygon([
      [35, 35],
      [50, 35],
      [50, 50],
      [35, 50],
    ]);
    const out = maskToPolygon(m, { simplifyEpsilon: 0.5 });
    expect(out.multipleComponents).toBe(true);
    expect(out.lossy).toBe(true);
    expect(out.droppedComponents).toBe(1); // 2 个分量 - 1 个主分量 = 丢弃 1 个。
    expect(out.lossyReason).toBeTruthy();
    expect(out.lossyReason).toContain("多个连通分量");
    // 仍返回最大外环 (不破现有调用方), 但调用方必须先检查 lossy。
    expect(out.points.length).toBeGreaterThanOrEqual(3);
  });

  it("test_mask_to_polygon_lossless_on_single_component: 单分量 → lossy=false, droppedComponents=undefined", () => {
    const m = makeRect(50, 50, 10, 10, 30, 30);
    const out = maskToPolygon(m, { simplifyEpsilon: 0.5 });
    expect(out.multipleComponents).toBe(false);
    expect(out.lossy).toBe(false);
    expect(out.droppedComponents).toBeUndefined();
    expect(out.lossyReason).toBeUndefined();
  });

  it("donut 单前景分量带孔洞 → lossy=true, droppedHoles=1", () => {
    const out = maskToPolygon(makeDonut(), { simplifyEpsilon: 0.5 });

    expect(out.multipleComponents).toBe(false);
    expect(out.droppedComponents).toBeUndefined();
    expect(out.lossy).toBe(true);
    expect(out.droppedHoles).toBe(1);
    expect(out.lossyReason).toContain("孔洞");
    expect(out.points.length).toBeGreaterThanOrEqual(3);
  });

  it("同一前景分量内的两个孔洞分别计数", () => {
    const m = new MaskBuffer({ width: 50, height: 30 });
    m.data.fill(255);
    for (let y = 8; y < 16; y++) {
      for (let x = 8; x < 16; x++) m.data[y * m.width + x] = 0;
      for (let x = 32; x < 40; x++) m.data[y * m.width + x] = 0;
    }

    const out = maskToPolygon(m, { simplifyEpsilon: 0 });
    expect(out.multipleComponents).toBe(false);
    expect(out.lossy).toBe(true);
    expect(out.droppedHoles).toBe(2);
  });

  it("背景通道连到画布边界时不误报孔洞", () => {
    const m = makeDonut();
    // 从内孔向上打通到外部背景，几何成为单连通、无孔的 C 形。
    for (let y = 0; y <= 14; y++) m.data[y * m.width + 20] = 0;

    const out = maskToPolygon(m, { simplifyEpsilon: 0.5 });
    expect(out.multipleComponents).toBe(false);
    expect(out.lossy).toBe(false);
    expect(out.droppedHoles).toBeUndefined();
  });

  it("test_mask_to_polygon_empty_mask: 空 mask → {points:[], lossy:false, multipleComponents:false}", () => {
    const m = new MaskBuffer({ width: 20, height: 20 });
    const out = maskToPolygon(m);
    expect(out.points).toEqual([]);
    expect(out.multipleComponents).toBe(false);
    expect(out.lossy).toBe(false);
    expect(out.droppedComponents).toBeUndefined();
    expect(out.lossyReason).toBeUndefined();
  });

  it("三个分量 → droppedComponents=2 (诊断准确, 不只是布尔)", () => {
    const m = new MaskBuffer({ width: 80, height: 80 });
    m.fromPolygon([
      [5, 5],
      [20, 5],
      [20, 20],
      [5, 20],
    ]);
    m.fromPolygon([
      [30, 30],
      [40, 30],
      [40, 40],
      [30, 40],
    ]);
    m.fromPolygon([
      [55, 55],
      [65, 55],
      [65, 65],
      [55, 65],
    ]);
    const out = maskToPolygon(m, { simplifyEpsilon: 0.5 });
    expect(out.lossy).toBe(true);
    expect(out.droppedComponents).toBe(2);
  });
});

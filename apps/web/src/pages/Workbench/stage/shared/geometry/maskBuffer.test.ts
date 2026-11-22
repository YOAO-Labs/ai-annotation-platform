import { describe, expect, it } from "vitest";
import { MaskBuffer } from "./maskBuffer";

describe("MaskBuffer · 构造与基础", () => {
  it("COCO RLE 与 row-major alpha buffer 无损往返", () => {
    const rle = {
      encoding: "coco_rle" as const,
      size: [2, 3] as [number, number],
      counts: [1, 2, 2, 1],
    };
    const buffer = MaskBuffer.fromRle(rle);
    expect(Array.from(buffer.data)).toEqual([0, 255, 0, 255, 0, 255]);
    expect(buffer.toRle()).toEqual(rle);
  });

  it("拒绝非正整数尺寸", () => {
    expect(() => new MaskBuffer({ width: 0, height: 10 })).toThrow();
    expect(() => new MaskBuffer({ width: 10, height: -1 })).toThrow();
    expect(() => new MaskBuffer({ width: 1.5, height: 10 })).toThrow();
  });

  it("初始全 0", () => {
    const m = new MaskBuffer({ width: 10, height: 10 });
    expect(m.countSet()).toBe(0);
  });
});

describe("MaskBuffer · brush / erase / clear", () => {
  it("brush 在 center 画圆，countSet 与理论面积接近", () => {
    const m = new MaskBuffer({ width: 100, height: 100 });
    m.brush(50, 50, 10);
    const n = m.countSet();
    expect(n).toBeGreaterThan(250);
    expect(n).toBeLessThan(380);
    expect(m.get(50, 50)).toBe(255);
    expect(m.get(80, 80)).toBe(0);
  });

  it("brush 越界部分被裁，仍能落入合法像素", () => {
    const m = new MaskBuffer({ width: 20, height: 20 });
    m.brush(0, 0, 5);
    expect(m.countSet()).toBeGreaterThan(0);
    expect(m.get(0, 0)).toBe(255);
  });

  it("erase 抹掉已画区域", () => {
    const m = new MaskBuffer({ width: 50, height: 50 });
    m.brush(25, 25, 10);
    const before = m.countSet();
    expect(before).toBeGreaterThan(0);
    m.erase(25, 25, 12);
    expect(m.countSet()).toBe(0);
  });

  it("clear 全清零", () => {
    const m = new MaskBuffer({ width: 30, height: 30 });
    m.brush(15, 15, 8);
    expect(m.countSet()).toBeGreaterThan(0);
    m.clear();
    expect(m.countSet()).toBe(0);
  });

  it("方笔刷与方橡皮遵循同一硬边 footprint", () => {
    const m = new MaskBuffer({ width: 5, height: 3 });
    m.brush(0, 1, 1, 255, "square");
    expect(Array.from(m.data, (value) => (value ? 1 : 0))).toEqual([
      1, 1, 0, 0, 0, 1, 1, 0, 0, 0, 1, 1, 0, 0, 0,
    ]);
    m.erase(0, 1, 1, "square");
    expect(m.countSet()).toBe(0);
  });

  it("replaceAlpha 只标记真实变化范围并拒绝灰度", () => {
    const m = new MaskBuffer({ width: 4, height: 3 });
    const replacement = new Uint8Array(12);
    replacement[1 * 4 + 2] = 255;
    replacement[2 * 4 + 3] = 255;
    expect(m.replaceAlpha(replacement)).toEqual({
      changedPixels: 2,
      bounds: { x0: 2, y0: 1, x1: 4, y1: 3 },
    });
    expect(m.consumeDirty()).toEqual({ x0: 2, y0: 1, x1: 4, y1: 3 });
    expect(m.replaceAlpha(replacement)).toEqual({ changedPixels: 0, bounds: null });
    expect(m.consumeDirty()).toBeNull();
    expect(() => m.replaceAlpha(Uint8Array.from({ length: 12 }, () => 128))).toThrow(/binary/);
  });
});

describe("MaskBuffer · fromPolygon", () => {
  it("矩形 polygon 填出矩形 mask", () => {
    const m = new MaskBuffer({ width: 50, height: 50 });
    m.fromPolygon([
      [10, 10],
      [30, 10],
      [30, 30],
      [10, 30],
    ]);
    const n = m.countSet();
    expect(n).toBeGreaterThan(360);
    expect(n).toBeLessThan(450);
    expect(m.get(20, 20)).toBe(255);
    expect(m.get(40, 40)).toBe(0);
  });

  it("三角形 polygon 大致填出三角形面积", () => {
    const m = new MaskBuffer({ width: 100, height: 100 });
    m.fromPolygon([
      [10, 10],
      [50, 10],
      [10, 50],
    ]);
    const n = m.countSet();
    expect(n).toBeGreaterThan(700);
    expect(n).toBeLessThan(900);
  });

  it("顶点 < 3 时静默不画", () => {
    const m = new MaskBuffer({ width: 20, height: 20 });
    m.fromPolygon([
      [1, 1],
      [10, 10],
    ]);
    expect(m.countSet()).toBe(0);
  });

  it("polygon 部分越界仍能填到有效区域", () => {
    const m = new MaskBuffer({ width: 20, height: 20 });
    m.fromPolygon([
      [-10, -10],
      [25, -10],
      [25, 25],
      [-10, 25],
    ]);
    expect(m.countSet()).toBe(20 * 20);
  });

  it("polygon subtract 与 add 使用相同像素中心规则", () => {
    const m = new MaskBuffer({ width: 6, height: 4 });
    m.fromPolygon([
      [1, 1],
      [5, 1],
      [5, 3],
      [1, 3],
    ]);
    m.fromPolygon(
      [
        [2, 0],
        [4, 0],
        [4, 4],
        [2, 4],
      ],
      0,
    );
    expect(Array.from(m.data, (value) => (value ? 1 : 0))).toEqual([
      0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 1, 0, 0, 1, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0,
    ]);
  });
});

describe("MaskBuffer · XOR bit patch", () => {
  it("toggles the same patch in both directions and marks its exact bounds dirty", () => {
    const buffer = new MaskBuffer({ width: 6, height: 4 });
    buffer.consumeDirty();
    const bits = Uint8Array.of(0b0000_1001);
    expect(buffer.applyXorBits(2, 1, 2, 2, bits)).toEqual({
      changedPixels: 2,
      bounds: { x0: 2, y0: 1, x1: 4, y1: 3 },
    });
    expect(buffer.get(2, 1)).toBe(255);
    expect(buffer.get(3, 2)).toBe(255);
    expect(buffer.consumeDirty()).toEqual({ x0: 2, y0: 1, x1: 4, y1: 3 });
    buffer.applyXorBits(2, 1, 2, 2, bits);
    expect(buffer.countSet()).toBe(0);
  });

  it("rejects malformed patch dimensions and byte lengths", () => {
    const buffer = new MaskBuffer({ width: 4, height: 4 });
    expect(() => buffer.applyXorBits(3, 3, 2, 2, Uint8Array.of(1))).toThrow(/bounds/);
    expect(() => buffer.applyXorBits(0, 0, 4, 4, Uint8Array.of(1))).toThrow(/length/);
  });
});

describe("MaskBuffer · dirtyRect", () => {
  it("初始无脏区", () => {
    const m = new MaskBuffer({ width: 30, height: 30 });
    expect(m.consumeDirty()).toBeNull();
  });

  it("brush 后 dirtyRect 覆盖笔刷外接方框", () => {
    const m = new MaskBuffer({ width: 100, height: 100 });
    m.brush(50, 50, 10);
    const rect = m.consumeDirty();
    expect(rect).not.toBeNull();
    expect(rect!.x0).toBeGreaterThanOrEqual(39);
    expect(rect!.x0).toBeLessThanOrEqual(41);
    expect(rect!.x1).toBeGreaterThanOrEqual(60);
    expect(rect!.x1).toBeLessThanOrEqual(62);
    expect(rect!.y0).toBeGreaterThanOrEqual(39);
    expect(rect!.y0).toBeLessThanOrEqual(41);
    expect(rect!.y1).toBeGreaterThanOrEqual(60);
    expect(rect!.y1).toBeLessThanOrEqual(62);
  });

  it("两次 brush 后脏区是 union", () => {
    const m = new MaskBuffer({ width: 200, height: 200 });
    m.brush(20, 20, 5);
    m.brush(180, 180, 5);
    const rect = m.consumeDirty()!;
    expect(rect.x0).toBeLessThanOrEqual(15);
    expect(rect.x1).toBeGreaterThanOrEqual(185);
    expect(rect.y0).toBeLessThanOrEqual(15);
    expect(rect.y1).toBeGreaterThanOrEqual(185);
  });

  it("consumeDirty 后再次 consume 返 null（数据未变）", () => {
    const m = new MaskBuffer({ width: 50, height: 50 });
    m.brush(25, 25, 5);
    const before = m.countSet();
    expect(m.consumeDirty()).not.toBeNull();
    expect(m.consumeDirty()).toBeNull();
    expect(m.countSet()).toBe(before);
  });

  it("clear 后脏区 = 全图", () => {
    const m = new MaskBuffer({ width: 40, height: 30 });
    m.consumeDirty();
    m.clear();
    const rect = m.consumeDirty()!;
    expect(rect).toEqual({ x0: 0, y0: 0, x1: 40, y1: 30 });
  });

  it("fromPolygon 的脏区 = polygon bbox（clamp 到画布）", () => {
    const m = new MaskBuffer({ width: 100, height: 100 });
    m.fromPolygon([
      [10, 20],
      [60, 20],
      [60, 80],
      [10, 80],
    ]);
    const rect = m.consumeDirty()!;
    expect(rect.x0).toBeLessThanOrEqual(10);
    expect(rect.x1).toBeGreaterThanOrEqual(60);
    expect(rect.y0).toBeLessThanOrEqual(20);
    expect(rect.y1).toBeGreaterThanOrEqual(80);
    expect(rect.x0).toBeGreaterThanOrEqual(0);
    expect(rect.y0).toBeGreaterThanOrEqual(0);
  });

  it("brush 越界部分 clamp 进脏区，不越界", () => {
    const m = new MaskBuffer({ width: 20, height: 20 });
    m.brush(0, 0, 10);
    const rect = m.consumeDirty()!;
    expect(rect.x0).toBe(0);
    expect(rect.y0).toBe(0);
    expect(rect.x1).toBeLessThanOrEqual(20);
    expect(rect.y1).toBeLessThanOrEqual(20);
  });

  it("toAlphaImageDataRect 切片字节正确", () => {
    const m = new MaskBuffer({ width: 10, height: 10 });
    m.brush(5, 5, 1);
    const slice = m.toAlphaImageDataRect({ x0: 4, y0: 4, x1: 7, y1: 7 });
    expect(slice.length).toBe(36);
    const centerIdx = (1 * 3 + 1) * 4;
    expect(slice[centerIdx + 3]).toBe(255);
    expect(slice[0 + 3]).toBe(0);
  });

  it("toAlphaImageDataRect 退化区域返空", () => {
    const m = new MaskBuffer({ width: 10, height: 10 });
    const empty = m.toAlphaImageDataRect({ x0: 5, y0: 5, x1: 5, y1: 5 });
    expect(empty.length).toBe(0);
  });

  it("clone 复制脏区", () => {
    const m = new MaskBuffer({ width: 20, height: 20 });
    m.brush(10, 10, 3);
    const c = m.clone();
    expect(c.consumeDirty()).not.toBeNull();
    expect(m.consumeDirty()).not.toBeNull();
  });
});

describe("MaskBuffer · toAlphaImageData / clone", () => {
  it("toAlphaImageData 仅 alpha 通道有 mask 值", () => {
    const m = new MaskBuffer({ width: 4, height: 4 });
    m.brush(2, 2, 1);
    const out = m.toAlphaImageData();
    expect(out.length).toBe(4 * 4 * 4);
    for (let i = 0; i < out.length; i += 4) {
      if (out[i + 3] === 255) {
        expect(out[i]).toBe(0);
        expect(out[i + 1]).toBe(0);
        expect(out[i + 2]).toBe(0);
      }
    }
  });

  it("clone 是独立拷贝", () => {
    const a = new MaskBuffer({ width: 10, height: 10 });
    a.brush(5, 5, 3);
    const before = a.countSet();
    const b = a.clone();
    expect(b.countSet()).toBe(before);
    b.clear();
    expect(b.countSet()).toBe(0);
    expect(a.countSet()).toBe(before);
  });
});

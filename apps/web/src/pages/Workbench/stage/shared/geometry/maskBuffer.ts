// Mask 编辑器 v1 数据层：离屏 alpha 缓冲 + 笔刷 / 橡皮 / clear /
// fromPolygon / toAlphaImageData。
//
// 设计取舍：
// - 数据存 `Uint8Array(W*H)` 单通道 alpha（0/255 二值，无中间灰度）；环境无 OffscreenCanvas
//   依赖（vitest jsdom 也能跑）。真正展示叠加靠 Konva.Image 拉这块 ImageData 即可。
// - 笔刷支持圆 / 方硬边。圆心 + 半径用栅格化双循环画，
//   半径上限 200px 时单笔 ≤ ~125k 像素操作，远低于 1 帧预算。
// - polygon → mask 用扫描线填充（ray casting），与浏览器 canvas2d 的 fill 等价；
//   v1 不引入 d3-contour / scanline 库依赖。

import { decodeCocoRle, encodeCocoRle, type CocoRle } from "./maskRle";
import { rasterizeMaskBrush, rasterizeMaskPolygon } from "./maskRasterization";

export interface MaskBufferOptions {
  width: number;
  height: number;
}

/**
 * 脏区半开矩形 [x0, x1) × [y0, y1)，像素坐标。供渲染层增量 putImageData 用。
 * 典型用法：`const rect = buffer.consumeDirty(); if (rect) ctx.putImageData(...)`。
 */
export interface DirtyRect {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

export type MaskBrushShape = "circle" | "square";

export interface MaskBufferChange {
  changedPixels: number;
  bounds: DirtyRect | null;
}

/**
 * 离屏 alpha mask 缓冲。坐标系 = 归一化前的像素 (0..width, 0..height)。
 *
 * - `data[y*width + x]` 取值 0 或 255。
 * - 笔刷 / 橡皮 / fromPolygon 都改 in-place；调用方按需复制。
 * - 内部维护 `_dirty` 半开矩形，所有写操作 union 进去；渲染层 `consumeDirty()` 取走并清零。
 */
export class MaskBuffer {
  readonly width: number;
  readonly height: number;
  readonly data: Uint8Array;
  private _dirty: DirtyRect | null = null;

  constructor({ width, height }: MaskBufferOptions) {
    if (!Number.isInteger(width) || width <= 0) throw new Error("MaskBuffer: width 必须正整数");
    if (!Number.isInteger(height) || height <= 0) throw new Error("MaskBuffer: height 必须正整数");
    this.width = width;
    this.height = height;
    this.data = new Uint8Array(width * height);
  }

  static fromRle(rle: CocoRle): MaskBuffer {
    const [height, width] = rle.size;
    const buffer = new MaskBuffer({ width, height });
    buffer.data.set(decodeCocoRle(rle));
    buffer.markDirty(0, 0, width, height);
    return buffer;
  }

  toRle(): CocoRle {
    return encodeCocoRle(this.data, this.width, this.height);
  }

  /** 把 [x0, x1) × [y0, y1) 与当前脏区 union；空区间静默忽略。坐标会 clamp 到画布。 */
  private markDirty(x0: number, y0: number, x1: number, y1: number): void {
    const cx0 = Math.max(0, Math.floor(x0));
    const cy0 = Math.max(0, Math.floor(y0));
    const cx1 = Math.min(this.width, Math.ceil(x1));
    const cy1 = Math.min(this.height, Math.ceil(y1));
    if (cx1 <= cx0 || cy1 <= cy0) return;
    if (!this._dirty) {
      this._dirty = { x0: cx0, y0: cy0, x1: cx1, y1: cy1 };
      return;
    }
    const d = this._dirty;
    if (cx0 < d.x0) d.x0 = cx0;
    if (cy0 < d.y0) d.y0 = cy0;
    if (cx1 > d.x1) d.x1 = cx1;
    if (cy1 > d.y1) d.y1 = cy1;
  }

  /** 取走当前脏区并清零；无脏区返 null。渲染层每次重画后调一次。 */
  consumeDirty(): DirtyRect | null {
    const d = this._dirty;
    this._dirty = null;
    return d;
  }

  /** 全清零；脏区 = 全图。 */
  clear(): void {
    this.data.fill(0);
    this.markDirty(0, 0, this.width, this.height);
  }

  /** Replace the full binary alpha plane and mark only actually changed pixels dirty. */
  replaceAlpha(alpha: Uint8Array): MaskBufferChange {
    if (alpha.length !== this.data.length) {
      throw new Error("MaskBuffer: replacement alpha length must match the buffer");
    }
    for (const value of alpha) {
      if (value !== 0 && value !== 255) {
        throw new Error("MaskBuffer: replacement alpha must be binary (0 or 255)");
      }
    }
    let changedPixels = 0;
    let minX = this.width;
    let minY = this.height;
    let maxX = -1;
    let maxY = -1;
    for (let index = 0; index < alpha.length; index += 1) {
      const value = alpha[index];
      if (this.data[index] === value) continue;
      this.data[index] = value;
      changedPixels += 1;
      const x = index % this.width;
      const y = Math.floor(index / this.width);
      minX = Math.min(minX, x);
      minY = Math.min(minY, y);
      maxX = Math.max(maxX, x);
      maxY = Math.max(maxY, y);
    }
    const bounds = changedPixels === 0 ? null : { x0: minX, y0: minY, x1: maxX + 1, y1: maxY + 1 };
    if (bounds) this.markDirty(bounds.x0, bounds.y0, bounds.x1, bounds.y1);
    return { changedPixels, bounds };
  }

  /** Apply a row-major 1-bit XOR patch at the given pixel origin. */
  applyXorBits(
    x0: number,
    y0: number,
    width: number,
    height: number,
    xorBits: Uint8Array,
  ): MaskBufferChange {
    if (
      !Number.isInteger(x0) ||
      !Number.isInteger(y0) ||
      !Number.isInteger(width) ||
      !Number.isInteger(height) ||
      x0 < 0 ||
      y0 < 0 ||
      width <= 0 ||
      height <= 0 ||
      x0 + width > this.width ||
      y0 + height > this.height
    ) {
      throw new Error("MaskBuffer: XOR patch bounds must fit the buffer");
    }
    if (xorBits.byteLength !== Math.ceil((width * height) / 8)) {
      throw new Error("MaskBuffer: XOR patch length does not match its dimensions");
    }
    let changedPixels = 0;
    let minX = this.width;
    let minY = this.height;
    let maxX = -1;
    let maxY = -1;
    for (let y = 0; y < height; y += 1) {
      const targetRow = (y0 + y) * this.width + x0;
      const patchRow = y * width;
      for (let x = 0; x < width; x += 1) {
        const bitIndex = patchRow + x;
        if ((xorBits[bitIndex >> 3] & (1 << (bitIndex & 7))) === 0) continue;
        this.data[targetRow + x] = this.data[targetRow + x] === 0 ? 255 : 0;
        changedPixels += 1;
        minX = Math.min(minX, x0 + x);
        minY = Math.min(minY, y0 + y);
        maxX = Math.max(maxX, x0 + x);
        maxY = Math.max(maxY, y0 + y);
      }
    }
    const bounds = changedPixels === 0 ? null : { x0: minX, y0: minY, x1: maxX + 1, y1: maxY + 1 };
    if (bounds) this.markDirty(bounds.x0, bounds.y0, bounds.x1, bounds.y1);
    return { changedPixels, bounds };
  }

  /** 当前非零像素数（调试 / 测试用，O(N)）。 */
  countSet(): number {
    let n = 0;
    for (let i = 0; i < this.data.length; i++) if (this.data[i]) n++;
    return n;
  }

  /** 单点查询。越界返回 0。 */
  get(x: number, y: number): number {
    if (x < 0 || y < 0 || x >= this.width || y >= this.height) return 0;
    return this.data[Math.floor(y) * this.width + Math.floor(x)];
  }

  /**
   * 硬边笔刷：以 (cx, cy) 为中心、半径 r 画实心圆 / 方形（v=255）或擦除（v=0）。
   * 半径 < 1 时仍至少改中心点；越界部分裁掉。
   */
  brush(
    cx: number,
    cy: number,
    r: number,
    value: 0 | 255 = 255,
    shape: MaskBrushShape = "circle",
  ): void {
    const change = rasterizeMaskBrush(this.data, this.width, this.height, {
      cx,
      cy,
      radius: r,
      value,
      shape,
    });
    if (change.touchedBounds) {
      this.markDirty(
        change.touchedBounds.x0,
        change.touchedBounds.y0,
        change.touchedBounds.x1,
        change.touchedBounds.y1,
      );
    }
  }

  /** 橡皮 = brush(cx, cy, r, 0) 的语义糖。 */
  erase(cx: number, cy: number, r: number, shape: MaskBrushShape = "circle"): void {
    this.brush(cx, cy, r, 0, shape);
  }

  /**
   * polygon 顶点 (image-space) → 填充到 alpha mask。
   *
   * - 用扫描线算法（射线投票判内外）；环不必闭合，会自动连首尾。
   * - 顶点 < 3 时静默不画。
   * - value=255 与已有 Mask 做 add，value=0 做 subtract；调用方要全量替换时先 `clear()`。
   */
  fromPolygon(points: ReadonlyArray<readonly [number, number]>, value: 0 | 255 = 255): void {
    const change = rasterizeMaskPolygon(this.data, this.width, this.height, points, value);
    if (change.touchedBounds) {
      this.markDirty(
        change.touchedBounds.x0,
        change.touchedBounds.y0,
        change.touchedBounds.x1,
        change.touchedBounds.y1,
      );
    }
  }

  /**
   * 输出为 RGBA ImageData 兼容缓冲：A 通道 = alpha mask；R/G/B 在调用方可叠色。
   * 返回 Uint8ClampedArray 长度 = width * height * 4。
   *
   * 仅 alpha 通道有效；调用方画 Konva.Image 时用 putImageData 即可。
   */
  toAlphaImageData(): Uint8ClampedArray {
    const out = new Uint8ClampedArray(this.width * this.height * 4);
    for (let i = 0; i < this.data.length; i++) {
      out[i * 4 + 3] = this.data[i];
    }
    return out;
  }

  /**
   * 切片版 toAlphaImageData：只输出 [x0, x1) × [y0, y1) 区域的 RGBA 缓冲。
   * 配合 `consumeDirty()` 让渲染层做增量 `putImageData`。
   *
   * 长度 = (x1-x0) * (y1-y0) * 4；R/G/B = 0，A = mask alpha。
   * rect 必须已 clamp 到画布（与 `consumeDirty` 返回的脏区一致）；越界静默返空。
   */
  toAlphaImageDataRect(rect: DirtyRect): Uint8ClampedArray {
    const { x0, y0, x1, y1 } = rect;
    const w = x1 - x0;
    const h = y1 - y0;
    if (w <= 0 || h <= 0) return new Uint8ClampedArray(0);
    const out = new Uint8ClampedArray(w * h * 4);
    for (let y = 0; y < h; y++) {
      const srcRow = (y0 + y) * this.width + x0;
      const dstRow = y * w;
      for (let x = 0; x < w; x++) {
        out[(dstRow + x) * 4 + 3] = this.data[srcRow + x];
      }
    }
    return out;
  }

  /** 拷贝。脏区一并复制。 */
  clone(): MaskBuffer {
    const c = new MaskBuffer({ width: this.width, height: this.height });
    c.data.set(this.data);
    if (this._dirty) c._dirty = { ...this._dirty };
    return c;
  }
}

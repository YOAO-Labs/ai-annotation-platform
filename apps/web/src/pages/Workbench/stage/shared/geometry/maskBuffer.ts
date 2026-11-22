import { decodeCocoRle, encodeCocoRle, type CocoRle } from "./maskRle";
import { rasterizeMaskBrush, rasterizeMaskPolygon } from "./maskRasterization";

export interface MaskBufferOptions {
  width: number;
  height: number;
}

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

  consumeDirty(): DirtyRect | null {
    const d = this._dirty;
    this._dirty = null;
    return d;
  }

  clear(): void {
    this.data.fill(0);
    this.markDirty(0, 0, this.width, this.height);
  }

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

  countSet(): number {
    let n = 0;
    for (let i = 0; i < this.data.length; i++) if (this.data[i]) n++;
    return n;
  }

  get(x: number, y: number): number {
    if (x < 0 || y < 0 || x >= this.width || y >= this.height) return 0;
    return this.data[Math.floor(y) * this.width + Math.floor(x)];
  }

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

  erase(cx: number, cy: number, r: number, shape: MaskBrushShape = "circle"): void {
    this.brush(cx, cy, r, 0, shape);
  }

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

  toAlphaImageData(): Uint8ClampedArray {
    const out = new Uint8ClampedArray(this.width * this.height * 4);
    for (let i = 0; i < this.data.length; i++) {
      out[i * 4 + 3] = this.data[i];
    }
    return out;
  }

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

  clone(): MaskBuffer {
    const c = new MaskBuffer({ width: this.width, height: this.height });
    c.data.set(this.data);
    if (this._dirty) c._dirty = { ...this._dirty };
    return c;
  }
}

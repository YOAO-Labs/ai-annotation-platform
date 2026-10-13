import { describe, expect, it } from "vitest";

import fixture from "@/__fixtures__/rasterMaskRle.json";
import {
  cocoRleContainsPixel,
  cocoRleArea,
  cocoRleBounds,
  decodeCocoRle,
  encodeCocoRle,
  prepareCocoRleGzipUpload,
  validateCocoRle,
  MAX_IMAGE_MASK_PIXELS,
} from "./maskRle";

interface ValidCase {
  name: string;
  width: number;
  height: number;
  pixels_row_major: number[];
  counts: number[];
}

interface InvalidCase {
  name: string;
  size: number[];
  counts: unknown[];
}

describe("COCO uncompressed RLE", () => {
  for (const testCase of fixture.valid as ValidCase[]) {
    it(`round-trips ${testCase.name} in column-major order`, () => {
      const rle = encodeCocoRle(testCase.pixels_row_major, testCase.width, testCase.height);
      expect(rle.counts).toEqual(testCase.counts);
      expect(Array.from(decodeCocoRle(rle), (value) => (value ? 1 : 0))).toEqual(
        testCase.pixels_row_major,
      );
    });
  }

  for (const testCase of fixture.invalid as InvalidCase[]) {
    it(`rejects ${testCase.name}`, () => {
      expect(() =>
        validateCocoRle({
          encoding: "coco_rle",
          size: testCase.size,
          counts: testCase.counts,
        }),
      ).toThrow();
    });
  }

  it("counts area as the odd-index run sum, the contract E2E foregroundArea helpers assume", () => {
    const pixels = [1, 1, 0, 0, 1, 0, 0, 0, 1];
    const rle = encodeCocoRle(pixels, 3, 3);
    const oddRunSum = rle.counts.reduce(
      (area: number, count: number, index: number) => area + (index % 2 === 1 ? count : 0),
      0,
    );
    const decodedForeground = Array.from(decodeCocoRle(rle)).filter((value) => value !== 0).length;
    expect(cocoRleArea(rle)).toBe(4);
    expect(cocoRleArea(rle)).toBe(oddRunSum);
    expect(cocoRleArea(rle)).toBe(decodedForeground);
  });

  it("accepts the structural 8K image envelope without decoding it", () => {
    expect(
      validateCocoRle({
        encoding: "coco_rle",
        size: [8192, 8192],
        counts: [MAX_IMAGE_MASK_PIXELS],
      }).size,
    ).toEqual([8192, 8192]);
  });

  it("prepares HTTP gzip with a separate storage preference", async () => {
    let captured: Uint8Array | undefined;
    const prepared = await prepareCocoRleGzipUpload(
      { encoding: "coco_rle", size: [2, 3], counts: [1, 2, 2, 1] },
      {
        minBytes: 1,
        compress: async (raw) => {
          captured = raw;
          return new Uint8Array(Math.ceil(raw.byteLength / 2)).fill(1);
        },
      },
    );
    expect(prepared).not.toBeNull();
    expect(JSON.parse(new TextDecoder().decode(captured))).toMatchObject({
      encoding: "coco_rle",
      storage_encoding: "gzip",
    });
    expect(prepared?.body.type).toBe("application/json");
  });

  it("looks up exact pixels in column-major RLE without decoding a full alpha plane", () => {
    const rle = encodeCocoRle([1, 0, 1, 0, 1, 0], 3, 2);

    expect(cocoRleContainsPixel(rle, 0, 0)).toBe(true);
    expect(cocoRleContainsPixel(rle, 1, 0)).toBe(false);
    expect(cocoRleContainsPixel(rle, 1, 1)).toBe(true);
    expect(cocoRleContainsPixel(rle, 2, 0)).toBe(true);
    expect(cocoRleContainsPixel(rle, -1, 0)).toBe(false);
    expect(cocoRleContainsPixel(rle, 3, 0)).toBe(false);
    expect(cocoRleArea(rle)).toBe(3);
  });

  it("computes exact foreground bounds without waiting for bitmap decoding", () => {
    const rle = encodeCocoRle([0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0], 4, 3);

    expect(cocoRleBounds(rle)).toEqual({
      x: 0.25,
      y: 0,
      w: 0.5,
      h: 2 / 3,
    });
    expect(
      cocoRleBounds({
        encoding: "coco_rle",
        size: [3, 4],
        counts: [12],
      }),
    ).toBeNull();
  });

  it("falls back when gzip would exceed the shared expansion ratio", async () => {
    const prepared = await prepareCocoRleGzipUpload(
      { encoding: "coco_rle", size: [2, 3], counts: [1, 2, 2, 1] },
      { minBytes: 1, compress: async () => new Uint8Array([1]) },
    );
    expect(prepared).toBeNull();
  });

  it("falls back when browser gzip compression fails", async () => {
    const prepared = await prepareCocoRleGzipUpload(
      { encoding: "coco_rle", size: [2, 3], counts: [1, 2, 2, 1] },
      {
        minBytes: 1,
        compress: async () => {
          throw new Error("compression failed");
        },
      },
    );
    expect(prepared).toBeNull();
  });

  it("keeps small payloads on the backward-compatible JSON path", async () => {
    let called = false;
    const prepared = await prepareCocoRleGzipUpload(
      { encoding: "coco_rle", size: [2, 3], counts: [1, 2, 2, 1] },
      {
        compress: async () => {
          called = true;
          return new Uint8Array([1]);
        },
      },
    );
    expect(prepared).toBeNull();
    expect(called).toBe(false);
  });
});

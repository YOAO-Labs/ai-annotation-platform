import { describe, expect, it } from "vitest";

import { decodePointCloudFrame, decodeAuxiliaryPointCloudFrame } from "./pointcloudFrame";

function asciiPcd(points: Array<[number, number, number]>): ArrayBuffer {
  const text = [
    "VERSION .7",
    "FIELDS x y z",
    "SIZE 4 4 4",
    "TYPE F F F",
    "COUNT 1 1 1",
    `WIDTH ${points.length}`,
    "HEIGHT 1",
    "VIEWPOINT 0 0 0 1 0 0 0",
    `POINTS ${points.length}`,
    "DATA ascii",
    ...points.map((point) => point.join(" ")),
  ].join("\n");
  return new TextEncoder().encode(text).buffer;
}

describe("decodePointCloudFrame", () => {
  it("parses, decimates and normalizes the lidar convention outside the renderer", () => {
    const frame = decodePointCloudFrame(
      asciiPcd([
        [1, 2, 0],
        [2, 3, 1],
        [3, 4, 2],
        [4, 5, 3],
      ]),
      "y_forward",
      2,
    );

    expect(frame.totalPoints).toBe(4);
    expect(frame.renderedPoints).toBe(2);
    expect(frame.decimateStride).toBe(2);
    expect(Array.from(frame.positions)).toEqual([2, -1, 0, 4, -3, 2]);
    expect(frame.heightColors).toHaveLength(6);
    expect(frame.viewRadius).toBeGreaterThanOrEqual(5);
    expect(frame.groundZ).toBeGreaterThanOrEqual(0);
  });
});

export function rgbPcd(): ArrayBuffer {
  const header = new TextEncoder().encode(
    "VERSION .7\nFIELDS x y z rgb\nSIZE 4 4 4 4\nTYPE F F F F\nCOUNT 1 1 1 1\nWIDTH 2\nHEIGHT 1\nPOINTS 2\nDATA binary\n",
  );
  const buffer = new ArrayBuffer(header.length + 32);
  new Uint8Array(buffer).set(header);
  const values = new DataView(buffer, header.length);
  [1, 2, 3, 4, 5, 6].forEach((value, index) =>
    values.setFloat32(Math.floor(index / 3) * 16 + (index % 3) * 4, value, true),
  );
  values.setUint32(12, 0xff0000, true);
  values.setUint32(28, 0x0000ff, true);
  return buffer;
}

describe("decodeAuxiliaryPointCloudFrame", () => {
  it("retains source RGB, normalizes exactly once and bounds only auxiliary geometry", () => {
    const buffer = rgbPcd();
    const original = new Uint8Array(buffer).slice();
    const auxiliary = decodeAuxiliaryPointCloudFrame(buffer, "y_forward", 1);
    expect(auxiliary).toEqual({
      positions: new Float32Array([2, -1, 3]),
      colors: new Float32Array([1, 0, 0]),
      totalPoints: 2,
      renderedPoints: 1,
    });
    expect(new Uint8Array(buffer)).toEqual(original);
    const primary = decodePointCloudFrame(buffer, "y_forward", 2);
    expect(primary.totalPoints).toBe(2);
    expect(primary.heightColors).not.toEqual(new Float32Array([1, 0, 0, 0, 0, 1]));
    expect(primary.positions).toEqual(new Float32Array([2, -1, 3, 5, -4, 6]));
  });
  it("rejects missing source colors and unsafe display budgets", () => {
    expect(() => decodeAuxiliaryPointCloudFrame(asciiPcd([[0, 0, 0]]), "iso_8855", 100)).toThrow(
      "缺少 RGB",
    );
    expect(() => decodeAuxiliaryPointCloudFrame(rgbPcd(), "iso_8855", 80_001)).toThrow("预算");
  });
});

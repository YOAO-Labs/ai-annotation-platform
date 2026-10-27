import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createOriginalImageAddress, typedOriginalImageBlob } from "./originalImageDisplay";

function bytes(blob: Blob): Promise<ArrayBuffer> {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as ArrayBuffer);
    reader.readAsArrayBuffer(blob);
  });
}
beforeEach(() =>
  vi.stubGlobal(
    "URL",
    Object.assign(class extends URL {}, {
      createObjectURL: vi.fn(),
      revokeObjectURL: vi.fn(),
    }),
  ),
);
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
describe("original image display bytes", () => {
  it.each([
    [[137, 80, 78, 71, 13, 10, 26, 10], "image/png"],
    [[255, 216, 255, 224], "image/jpeg"],
    ["GIF89a", "image/gif"],
    ["GIF87a", "image/gif"],
    ["RIFFabcdWEBP", "image/webp"],
    ["BM1234", "image/bmp"],
    [[73, 73, 42, 0], "image/tiff"],
    [[77, 77, 0, 42], "image/tiff"],
    [
      '\uFEFF<?xml version="1.0"?><!-- 中文 --><svg xmlns="http://www.w3.org/2000/svg"/>',
      "image/svg+xml",
    ],
  ])("types actual bytes as %s without rewriting them", async (raw, type) => {
    const blob = new Blob([typeof raw === "string" ? raw : new Uint8Array(raw)], {
      type: "application/octet-stream",
    });
    const typed = await typedOriginalImageBlob(blob);
    expect(typed.type).toBe(type);
    expect(await bytes(typed)).toEqual(await bytes(blob));
  });
  it("does not trust a claimed MIME for HTML or empty bytes", async () => {
    for (const raw of ["", "<html><svg/></html>"])
      await expect(
        typedOriginalImageBlob(new Blob([raw], { type: "image/svg+xml" })),
      ).rejects.toThrow("无法识别");
  });
  it("gives SVG an exact opaque address and never creates a same-origin SVG blob", async () => {
    const create = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:forbidden");
    const raw = '<svg xmlns="http://www.w3.org/2000/svg"><text>中文 % 空格</text></svg>';
    const address = await createOriginalImageAddress(new Blob([raw]));
    expect(address.url).toBe(
      `data:image/svg+xml;base64,${btoa(unescape(encodeURIComponent(raw)))}`,
    );
    expect(create).not.toHaveBeenCalled();
    address.release();
  });
  it("releases raster addresses exactly once and rejects cancelled conversion", async () => {
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:raster");
    const revoke = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
    const blob = new Blob(["GIF89a"]);
    const address = await createOriginalImageAddress(blob);
    address.release();
    address.release();
    expect(revoke).toHaveBeenCalledTimes(1);
    expect(revoke).toHaveBeenCalledWith("blob:raster");
    const controller = new AbortController();
    controller.abort();
    await expect(createOriginalImageAddress(blob, controller.signal)).rejects.toHaveProperty(
      "name",
      "AbortError",
    );
  });
});

import { webcrypto } from "node:crypto";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  PointCloudAugmentationAsset,
  PointCloudAugmentationManifest,
} from "@/api/pointCloudAugmentation";
import {
  augmentationContentKey,
  loadAugmentationAsset,
  PointCloudAugmentationCache,
  validateAugmentationAsset,
} from "./pointCloudAugmentationAssets";
import { decodeAuxiliaryPointCloudFrameAsync } from "./geometry/pointCloudComputeSession";

vi.mock("./geometry/pointCloudComputeSession", () => ({
  decodeAuxiliaryPointCloudFrameAsync: vi.fn(),
}));
const decode = vi.mocked(decodeAuxiliaryPointCloudFrameAsync);
const frame = {
  positions: new Float32Array([1, 2, 3]),
  colors: new Float32Array([1, 0, 0]),
  renderedPoints: 1,
  totalPoints: 1,
};
const bytes = new Uint8Array([1, 2, 3, 4]);
let manifest: PointCloudAugmentationManifest;
let asset: PointCloudAugmentationAsset;

beforeEach(async () => {
  vi.stubGlobal("crypto", webcrypto);
  const hash = [...new Uint8Array(await webcrypto.subtle.digest("SHA-256", bytes))]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
  manifest = {
    task_id: "t1",
    enabled: true,
    status: "ready",
    reason: null,
    asset_id: "a1",
    generation: 1,
    profile: "omega-current-mono-512",
    input_digest: "input1",
    coordinate_frame: "primary_pointcloud",
    axis_convention: "iso_8855",
    cameras: [
      { role: "front", name: "前方", point_count: 1, byte_size: bytes.byteLength, sha256: hash },
    ],
  };
  asset = {
    url: "/signed.pcd",
    expires_in: 300,
    format: "pcd",
    point_count: 1,
    sha256: hash,
    generation: 1,
    camera_role: "front",
    coordinate_frame: "primary_pointcloud",
    axis_convention: "iso_8855",
  };
  decode.mockReset().mockResolvedValue(frame);
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe("auxiliary asset identity and bounds", () => {
  it("caches content identity independently of signed URLs and bounds memory and entries", () => {
    const cache = new PointCloudAugmentationCache();
    for (let index = 0; index < 4; index++) cache.put(String(index), frame);
    expect(cache.size).toBe(3);
    expect(cache.get("0")).toBeUndefined();
    expect(cache.bytes).toBe(72);
    cache.clear();
    expect(cache.bytes).toBe(0);
    const key = augmentationContentKey("u1", manifest, manifest.cameras[0], 100);
    expect(augmentationContentKey("u2", manifest, manifest.cameras[0], 100)).not.toBe(key);
    expect(
      augmentationContentKey("u1", { ...manifest, generation: 2 }, manifest.cameras[0], 100),
    ).not.toBe(key);
    expect(augmentationContentKey("u1", manifest, manifest.cameras[0], 50)).not.toBe(key);
  });
  it.each([
    "generation",
    "camera_role",
    "sha256",
    "axis_convention",
    "coordinate_frame",
    "point_count",
  ] as const)("rejects changed %s before decoding", (field) => {
    expect(() =>
      validateAugmentationAsset({ ...asset, [field]: "wrong" }, manifest, manifest.cameras[0]),
    ).toThrow("身份不一致");
  });
  it("refreshes an expired URL once, validates SHA256 and decodes only this selected camera", async () => {
    const issue = vi.fn().mockResolvedValue(asset);
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response(null, { status: 403 }))
      .mockResolvedValueOnce(new Response(bytes));
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();
    await expect(
      loadAugmentationAsset(
        manifest,
        manifest.cameras[0],
        "iso_8855",
        100,
        issue,
        controller.signal,
      ),
    ).resolves.toBe(frame);
    expect(issue).toHaveBeenCalledTimes(2);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(decode).toHaveBeenCalledWith(bytes.buffer, "iso_8855", 100, controller.signal);
  });
  it("bounds expired-URL refresh instead of retrying indefinitely", async () => {
    const issue = vi.fn().mockResolvedValue(asset);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(() => Promise.resolve(new Response(null, { status: 403 }))),
    );
    await expect(
      loadAugmentationAsset(
        manifest,
        manifest.cameras[0],
        "iso_8855",
        100,
        issue,
        new AbortController().signal,
      ),
    ).rejects.toThrow("403");
    expect(issue).toHaveBeenCalledTimes(2);
    expect(decode).not.toHaveBeenCalled();
  });
  it("rejects corrupt, oversize and incompatible-coordinate payloads before decode", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(() => Promise.resolve(new Response(new Uint8Array([4, 3, 2, 1])))),
    );
    const issue = vi.fn().mockResolvedValue(asset);
    await expect(
      loadAugmentationAsset(
        manifest,
        manifest.cameras[0],
        "iso_8855",
        100,
        issue,
        new AbortController().signal,
      ),
    ).rejects.toThrow("完整性");
    await expect(
      loadAugmentationAsset(
        manifest,
        manifest.cameras[0],
        "y_forward",
        100,
        issue,
        new AbortController().signal,
      ),
    ).rejects.toThrow("坐标系");
    await expect(
      loadAugmentationAsset(
        manifest,
        { ...manifest.cameras[0], byte_size: 32 * 1024 * 1024 + 1 },
        "iso_8855",
        100,
        issue,
        new AbortController().signal,
      ),
    ).rejects.toThrow("预算");
    expect(decode).not.toHaveBeenCalled();
  });
});

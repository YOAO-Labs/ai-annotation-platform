import { webcrypto, createHash } from "node:crypto";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "./client";
import { useAuthStore } from "@/stores/authStore";
import {
  assertVideoDerivativeOwner,
  assertVideoSamplesIdentity,
  videoDerivativeKey,
  videoDerivativeOwnerKey,
  verifyVideoChunkBytes,
  retryPreparingVideoMetadata,
  videoMetadataRetryDelay,
} from "./videoDerivatives";

const bytes = new Uint8Array([1, 2, 3, 4]).buffer;
const identity = {
  metadata_generation_id: "10000000-0000-4000-8000-000000000001",
  output_id: "20000000-0000-4000-8000-000000000001",
  sha256: createHash("sha256").update(new Uint8Array(bytes)).digest("hex"),
  byte_size: 4,
};

describe("video derivative content and authority", () => {
  beforeEach(() => {
    vi.stubGlobal("crypto", webcrypto);
    localStorage.setItem("token", "video-token");
    useAuthStore.setState({
      token: "video-token",
      sessionId: "session-1",
      user: { id: "user-1" } as NonNullable<ReturnType<typeof useAuthStore.getState>["user"]>,
    });
  });
  afterEach(() => vi.unstubAllGlobals());

  it("requires immutable output, metadata generation and SHA together", () => {
    expect(videoDerivativeKey(identity)).not.toBeNull();
    expect(videoDerivativeKey({ ...identity, output_id: null })).toBeNull();
    expect(videoDerivativeKey({ ...identity, metadata_generation_id: null })).toBeNull();
    expect(videoDerivativeKey({ ...identity, sha256: "unverified" })).toBeNull();
  });

  it("rejects old packet offsets even when item and chunk coordinates match", () => {
    expect(() => assertVideoSamplesIdentity(identity, identity)).not.toThrow();
    expect(() =>
      assertVideoSamplesIdentity(identity, {
        ...identity,
        output_id: "20000000-0000-4000-8000-000000000002",
      }),
    ).toThrow();
    expect(() =>
      assertVideoSamplesIdentity(identity, { ...identity, sha256: "a".repeat(64) }),
    ).toThrow();
    expect(() =>
      assertVideoSamplesIdentity(identity, {
        ...identity,
        metadata_generation_id: "10000000-0000-4000-8000-000000000002",
      }),
    ).toThrow();
  });

  it("verifies the entire downloaded file independently of its URL and headers", async () => {
    await expect(verifyVideoChunkBytes(bytes, identity)).resolves.toBeUndefined();
    await expect(
      verifyVideoChunkBytes(new Uint8Array([1, 2, 3, 5]).buffer, identity),
    ).rejects.toThrow("校验失败");
    await expect(verifyVideoChunkBytes(bytes.slice(0, 3), identity)).rejects.toThrow("长度");
    await expect(verifyVideoChunkBytes(bytes, { ...identity, byte_size: null })).rejects.toThrow(
      "长度",
    );
  });

  it("isolates renewed credentials without exposing tokens in a cache namespace", () => {
    const first = videoDerivativeOwnerKey();
    expect(first).not.toContain("video-token");
    useAuthStore.getState().setToken("renewed-token");
    const renewed = videoDerivativeOwnerKey();
    expect(renewed).not.toBe(first);
    expect(renewed).not.toContain("renewed-token");
    expect(() => assertVideoDerivativeOwner(first)).toThrow("会话");
    expect(() => assertVideoDerivativeOwner(renewed)).not.toThrow();
    useAuthStore.getState().logout();
    expect(() => assertVideoDerivativeOwner(renewed)).toThrow("会话");
  });

  it("rejects a second tab replacing storage before the store renders", () => {
    const owner = videoDerivativeOwnerKey();
    localStorage.setItem("token", "other-account-token");
    expect(videoDerivativeOwnerKey()).toBeNull();
    expect(() => assertVideoDerivativeOwner(owner)).toThrow("会话");
  });
  it("preserves the bounded metadata-preparing policy and server retry hint", () => {
    const preparing = new ApiError(
      503,
      "preparing",
      { code: "video_metadata_preparing" },
      { "retry-after": "3" },
    );
    expect(retryPreparingVideoMetadata(0, preparing)).toBe(true);
    expect(retryPreparingVideoMetadata(10, preparing)).toBe(false);
    expect(videoMetadataRetryDelay(0, preparing)).toBe(3000);
    expect(retryPreparingVideoMetadata(0, new ApiError(503, "unavailable"))).toBe(false);
    expect(
      retryPreparingVideoMetadata(
        0,
        new ApiError(409, "unverified", { code: "video_source_unverified" }),
      ),
    ).toBe(false);
  });

  it.each(["content_busy", "storage_original_busy", "storage_maintenance_busy"])(
    "retries only three times with short bounded delays for explicit 409 %s",
    (code) => {
      const error = new ApiError(409, "busy", { code }, { "retry-after": "120" });
      expect([0, 1, 2, 3, 10].map((count) => retryPreparingVideoMetadata(count, error))).toEqual([
        true,
        true,
        true,
        false,
        false,
      ]);
      expect([0, 1, 2, 3, 10].map((attempt) => videoMetadataRetryDelay(attempt, error))).toEqual([
        200, 400, 600, 600, 600,
      ]);
    },
  );

  it.each([
    [401, "content_busy"],
    [403, "content_busy"],
    [409, "video_source_unverified"],
    [409, "video_source_unknown"],
    [409, "storage_gate_closed"],
    [409, "video_metadata_changed"],
    [409, "video_metadata_preparing"],
    [409, "unknown_conflict"],
    [503, "content_busy"],
    [503, "storage_original_busy"],
    [503, "storage_maintenance_busy"],
  ] as const)("does not retry status %i code %s", (status, code) => {
    expect(retryPreparingVideoMetadata(0, new ApiError(status, "rejected", { code }))).toBe(false);
  });

  it("does not infer a transient conflict from missing, malformed or untrusted error details", () => {
    for (const error of [
      new ApiError(409, "content_busy"),
      new ApiError(409, "busy", { code: ["content_busy"] }),
      new ApiError(409, "busy", { code: "content_busy_extra" }),
      { status: 409, detailRaw: { code: "content_busy" } },
      new Error("content_busy"),
    ])
      expect(retryPreparingVideoMetadata(0, error)).toBe(false);
  });

  it("keeps ten preparing retries at two seconds by default and bounds valid retry hints", () => {
    const preparing = (hint?: string) =>
      new ApiError(
        503,
        "preparing",
        { code: "video_metadata_preparing" },
        hint ? { "retry-after": hint } : undefined,
      );
    expect(retryPreparingVideoMetadata(9, preparing())).toBe(true);
    expect(retryPreparingVideoMetadata(10, preparing())).toBe(false);
    expect(videoMetadataRetryDelay(0, preparing())).toBe(2_000);
    expect(videoMetadataRetryDelay(9, preparing())).toBe(2_000);
    expect(videoMetadataRetryDelay(0, preparing("invalid"))).toBe(2_000);
    expect(videoMetadataRetryDelay(0, preparing("0.2"))).toBe(1_000);
    expect(videoMetadataRetryDelay(0, preparing("120"))).toBe(10_000);
  });
});

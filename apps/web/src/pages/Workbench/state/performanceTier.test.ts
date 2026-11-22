import { describe, expect, it, vi } from "vitest";

import { ByteLru } from "@/pages/Workbench/stage/videoByteLru";
import { resolveWorkbenchPerformanceTier, WORKBENCH_PERFORMANCE_TIERS } from "./performanceTier";

const MIB = 1024 * 1024;

describe("workbench performance tiers", () => {
  it("keeps standard aligned with the byte-budget video decode tiers", () => {
    expect(WORKBENCH_PERFORMANCE_TIERS.standard).toEqual({
      videoBitmapCache: 48,
      videoDecoderBitmapCacheBytes: 256 * MIB,
      videoChunkByteCacheBytes: 96 * MIB,
      videoDecodePrefetchFrames: 2,
      previewCache: 120,
      prefetchHalfWindow: 3,
      anchorPrefetch: 8,
      pcdDecimate: 500_000,
    });
  });

  it("light / standard / aggressive 的字节预算与预取帧单调递增", () => {
    const { light, standard, aggressive } = WORKBENCH_PERFORMANCE_TIERS;
    expect(light.videoDecoderBitmapCacheBytes).toBe(96 * MIB);
    expect(standard.videoDecoderBitmapCacheBytes).toBe(256 * MIB);
    expect(aggressive.videoDecoderBitmapCacheBytes).toBe(512 * MIB);
    expect(light.videoChunkByteCacheBytes).toBe(32 * MIB);
    expect(standard.videoChunkByteCacheBytes).toBe(96 * MIB);
    expect(aggressive.videoChunkByteCacheBytes).toBe(192 * MIB);
    expect(light.videoDecodePrefetchFrames).toBe(0);
    expect(standard.videoDecodePrefetchFrames).toBe(2);
    expect(aggressive.videoDecodePrefetchFrames).toBe(4);
  });

  it("falls back to standard for empty input", () => {
    expect(resolveWorkbenchPerformanceTier(null)).toBe(WORKBENCH_PERFORMANCE_TIERS.standard);
  });

  it("切换到 light 档会立即收缩已填充的 bitmap 缓存并释放资源", () => {
    const standardBudget = WORKBENCH_PERFORMANCE_TIERS.standard.videoDecoderBitmapCacheBytes;
    const lightBudget = WORKBENCH_PERFORMANCE_TIERS.light.videoDecoderBitmapCacheBytes;
    const lru = new ByteLru<string, number>(standardBudget);
    const dispose = vi.fn();
    const frameBytes = 1920 * 1080 * 4;
    const filled = Math.floor(standardBudget / frameBytes);
    for (let i = 0; i < filled; i++) {
      lru.set(`f${i}`, { value: i, bytes: frameBytes, dispose });
    }
    expect(lru.bytes).toBe(filled * frameBytes);

    const evicted = lru.setBudget(lightBudget);
    expect(evicted).toBeGreaterThan(0);
    expect(lru.bytes).toBeLessThanOrEqual(lightBudget);
    expect(dispose).toHaveBeenCalledTimes(evicted);
  });
});

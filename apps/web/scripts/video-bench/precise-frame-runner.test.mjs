import { describe, expect, it } from "vitest";
import { preciseFrameMath } from "./precise-frame-runner.mjs";

const budgets = {
  pipelineBlockingP95Ms: 16,
  continuousPlaybackRafFpsMin: 45,
  interactionRafRatioMin: 0.9,
};

function completedRow(overrides = {}) {
  return {
    summary: {
      status: "completed",
      pipelineBlockingP95Ms: 8,
      attributedLongTaskGte50Ms: 0,
      interactionRafRatio: 1,
    },
    playback: { rafFps: 60 },
    ...overrides,
  };
}

const playback = {
  requestedSeconds: 1,
  observedSeconds: 1,
  mediaTimeDeltaSeconds: 1,
  mediaEnded: false,
  preciseRequests: 0,
  rafFps: 60,
};

describe("macOS WebCodecs decoder evidence", () => {
  const host = { platform: "darwin", arch: "arm64" };

  it("允许空静态 profile，由实际 WebCodecs VideoToolbox player 完成资格", () => {
    expect(
      preciseFrameMath.classifyVideoDecoderEvidence(
        [
          {
            loadUrls: ["WebCodecs::VideoDecoder"],
            decoderName: "VideoToolboxVideoDecoder",
            platformDecoder: true,
          },
        ],
        host,
      ),
    ).toMatchObject({
      status: "verified",
      matchedPlayerCount: 1,
      decoderName: "VideoToolboxVideoDecoder",
      platformDecoder: true,
    });
  });

  it("隐藏原生 video 的 VideoToolbox 条目不能冒充 WebCodecs", () => {
    expect(
      preciseFrameMath.classifyVideoDecoderEvidence(
        [
          {
            loadUrls: ["http://localhost/video.mp4"],
            decoderName: "VideoToolboxVideoDecoder",
            platformDecoder: true,
          },
        ],
        host,
      ),
    ).toMatchObject({ status: "unverified", matchedPlayerCount: 0 });
  });

  it("忽略未初始化的 kLoad-only player，但完整证据仍必须至少一条", () => {
    expect(
      preciseFrameMath.classifyVideoDecoderEvidence(
        [
          { loadUrls: ["WebCodecs::VideoDecoder"], decoderName: null, platformDecoder: null },
          {
            loadUrls: ["WebCodecs::VideoDecoder"],
            decoderName: "VideoToolboxVideoDecoder",
            platformDecoder: true,
          },
        ],
        host,
      ),
    ).toMatchObject({ status: "verified", matchedPlayerCount: 2, initializedPlayerCount: 1 });
    expect(
      preciseFrameMath.classifyVideoDecoderEvidence(
        [{ loadUrls: ["WebCodecs::VideoDecoder"], decoderName: null, platformDecoder: null }],
        host,
      ).status,
    ).toBe("unverified");
  });

  it.each([
    { decoderName: "FFmpegVideoDecoder", platformDecoder: false },
    { decoderName: "VideoToolboxVideoDecoder", platformDecoder: false },
    {
      decoderNames: ["FFmpegVideoDecoder", "VideoToolboxVideoDecoder"],
      platformDecoderValues: [false, true],
    },
  ])("decoder 属性不完整或非平台 VideoToolbox 时 fail closed", (properties) => {
    expect(
      preciseFrameMath.classifyVideoDecoderEvidence(
        [{ loadUrls: ["WebCodecs::VideoDecoder"], ...properties }],
        host,
      ).status,
    ).toBe("unverified");
  });

  it("CDP session 关闭后丢弃已采集证据", async () => {
    const listeners = new Map();
    const session = {
      on: (event, listener) => listeners.set(event, listener),
      send: async () => undefined,
      detach: async () => undefined,
    };
    const probe = await preciseFrameMath.installVideoDecoderEvidenceProbe(
      {
        context: () => ({ newCDPSession: async () => session }),
      },
      host,
    );
    listeners.get("Media.playerEventsAdded")?.({
      playerId: "webcodecs",
      events: [{ value: JSON.stringify({ event: "kLoad", url: "WebCodecs::VideoDecoder" }) }],
    });
    listeners.get("Media.playerPropertiesChanged")?.({
      playerId: "webcodecs",
      properties: [
        { name: "kVideoDecoderName", value: "VideoToolboxVideoDecoder" },
        { name: "kIsPlatformVideoDecoder", value: "true" },
      ],
    });
    expect(probe.snapshot().status).toBe("verified");
    listeners.get("close")?.();
    expect(probe.snapshot()).toMatchObject({ status: "unverified", platformDecoder: true });
  });
});

describe("precise-frame benchmark decision", () => {
  it("矩阵不完整时保持 inconclusive，不输出 false", () => {
    expect(
      preciseFrameMath.workerDecision(
        [{ summary: { status: "inconclusive" }, playback: null }],
        budgets,
      ),
    ).toMatchObject({ status: "inconclusive", triggered: null });
  });

  it("真实 blocking / long task / rAF 任一越门即触发", () => {
    const rows = [
      completedRow({
        summary: {
          status: "completed",
          pipelineBlockingP95Ms: 17,
          attributedLongTaskGte50Ms: 0,
          interactionRafRatio: 1,
        },
      }),
    ];
    expect(preciseFrameMath.workerDecision(rows, budgets)).toMatchObject({
      status: "triggered",
      triggered: true,
    });
  });

  it("完整矩阵全部满足门限才得到 not-triggered", () => {
    expect(
      preciseFrameMath.workerDecision([completedRow(), completedRow(), completedRow()], budgets),
    ).toMatchObject({ status: "not-triggered", triggered: false });
  });

  it("p95 使用向上取整的最近秩", () => {
    expect(preciseFrameMath.percentile([1, 2, 3, 100], 0.95)).toBe(100);
    expect(preciseFrameMath.percentile([], 0.95)).toBeNull();
  });

  it("把绝对帧号映射到时间轴的 0..10000 百分比坐标", () => {
    expect(preciseFrameMath.sliderValueForFrame(0, 1949)).toBe(0);
    expect(preciseFrameMath.sliderValueForFrame(59, 1949)).toBe(303);
    expect(preciseFrameMath.sliderValueForFrame(1949, 1949)).toBe(10_000);
    expect(() => preciseFrameMath.sliderValueForFrame(1950, 1949)).toThrow("outside 0..1949");
  });

  it("same-chunk 目标是可复现的均匀洗牌，不退化为逐帧倒序", () => {
    const frames = Array.from({ length: 60 }, (_, index) => index);
    const first = preciseFrameMath.seededSampleTargets(frames, 30);
    const second = preciseFrameMath.seededSampleTargets(frames, 30);
    expect(first).toEqual(second);
    expect(new Set(first).size).toBe(30);
    expect(first).not.toEqual(Array.from({ length: 30 }, (_, index) => (60 - index) % 60));
  });

  it("最后一个样本 fallback 也必须让矩阵保持 inconclusive", () => {
    const diagnostics = (gopStartDecodeIndex) => ({
      gopStartDecodeIndex,
      counters: { activeDecoders: 1, liveVideoFrames: 0 },
      cache: {
        bitmapBytes: 32,
        bitmapBudgetBytes: 64,
        chunkBytes: 16,
        chunkBudgetBytes: 64,
      },
    });
    const observation = (scenario, gopStartDecodeIndex = 0) => ({
      scenario,
      latencyMs: 5,
      source: "webcodecs",
      state: "ready",
      pipelineBlockingMs: 1,
      longTasks: [],
      diagnostics: diagnostics(gopStartDecodeIndex),
    });
    const observations = [
      observation("warm-same-gop-seek"),
      observation("warm-same-chunk-random-seek"),
      observation("cross-gop-roundtrip", 0),
      observation("cross-gop-roundtrip", 30),
      observation("frame-step-stability", 30),
    ];
    observations.at(-1).source = "native-bitmap";
    const resourceSnapshot = {
      bitmapBytes: 32,
      bitmapBudgetBytes: 64,
      chunkBytes: 16,
      chunkBudgetBytes: 64,
      activeDecoders: 1,
      liveVideoFrames: 0,
    };
    const summary = preciseFrameMath.summarizeRow(
      {
        capability: "ready",
        observations,
        playback,
        nativeBaselinePlayback: playback,
        interactionRaf: { rafFps: 60 },
        nativeBaselineInteractionRaf: { rafFps: 60 },
        flagOffPreciseRequests: 0,
        rapidScrub: { staleFrameActivations: 0 },
        memoryPlateauStart: resourceSnapshot,
        memoryAfter: resourceSnapshot,
      },
      {
        samples: {
          warmSameGop: 1,
          warmSameChunkRandom: 1,
          crossGopRoundtrip: 2,
          stabilityOperations: 1,
          continuousPlaybackSeconds: 1,
        },
        budgets: {
          flagOffPreciseRequests: 0,
          staleFrameActivations: 0,
          activeDecodersMax: 1,
          liveVideoFramesAfterOps: 0,
          budgetEvictionMemoryGrowth: 0,
          continuousPlaybackRafFpsMin: 45,
        },
      },
    );
    expect(summary).toMatchObject({
      status: "inconclusive",
      fallbackCount: 1,
    });
  });

  it("warm seek 超出退出门时不能把矩阵标成 completed", () => {
    const diagnostics = {
      gopStartDecodeIndex: 0,
      counters: { activeDecoders: 1, liveVideoFrames: 0 },
      cache: {
        bitmapBytes: 32,
        bitmapBudgetBytes: 64,
        chunkBytes: 16,
        chunkBudgetBytes: 64,
      },
    };
    const observation = (scenario, latencyMs, gopStartDecodeIndex = 0) => ({
      scenario,
      latencyMs,
      source: "webcodecs",
      state: "ready",
      pipelineBlockingMs: 1,
      longTasks: [],
      diagnostics: { ...diagnostics, gopStartDecodeIndex },
    });
    const resourceSnapshot = {
      bitmapBytes: 32,
      bitmapBudgetBytes: 64,
      chunkBytes: 16,
      chunkBudgetBytes: 64,
      activeDecoders: 1,
      liveVideoFrames: 0,
    };
    const summary = preciseFrameMath.summarizeRow(
      {
        capability: "ready",
        observations: [
          observation("warm-same-gop-seek", 81),
          observation("warm-same-chunk-random-seek", 5),
          observation("cross-gop-roundtrip", 5, 0),
          observation("cross-gop-roundtrip", 5, 30),
          observation("frame-step-stability", 5),
        ],
        playback,
        nativeBaselinePlayback: playback,
        interactionRaf: { rafFps: 60 },
        nativeBaselineInteractionRaf: { rafFps: 60 },
        flagOffPreciseRequests: 0,
        rapidScrub: { staleFrameActivations: 0 },
        memoryPlateauStart: resourceSnapshot,
        memoryAfter: resourceSnapshot,
      },
      {
        samples: {
          warmSameGop: 1,
          warmSameChunkRandom: 1,
          crossGopRoundtrip: 2,
          stabilityOperations: 1,
          continuousPlaybackSeconds: 1,
        },
        budgets: {
          warmSameGopSeekP95Ms: 80,
          warmSameChunkRandomSeekP95Ms: 200,
          flagOffPreciseRequests: 0,
          staleFrameActivations: 0,
          activeDecodersMax: 1,
          liveVideoFramesAfterOps: 0,
          budgetEvictionMemoryGrowth: 0,
          continuousPlaybackRafFpsMin: 45,
        },
      },
    );

    expect(summary).toMatchObject({
      status: "inconclusive",
      latencyWithinBudget: false,
      warmSameGopSeekP95Ms: 81,
    });
  });
});

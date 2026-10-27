// useVideoPreciseFrame pipeline 编排:manifest v2 → chunk 轮询 → samples → bytes → decode。
// query 层 mock tasksApi/videoApi/fetch;decode 层用真实 useVideoChunkDecoder + stub WebCodecs。
import { createElement } from "react";
import { createHash, webcrypto } from "node:crypto";
import { useAuthStore } from "@/stores/authStore";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { tasksApi } from "@/api/tasks";
import { videoApi } from "@/api/videos";
import { ApiError } from "@/api/client";
import type { VideoChunkOut, VideoChunkSamplesResponse, VideoManifestV2Response } from "@/types";
import { WEBCODECS_FLAG_STORAGE_KEY } from "./useVideoChunkDecoder";
import { retryAfterSecondsToMs, useVideoPreciseFrame } from "./useVideoPreciseFrame";

vi.mock("@/api/tasks", () => ({
  tasksApi: { getVideoManifestV2: vi.fn() },
}));
vi.mock("@/api/videos", () => ({
  videoApi: { getChunk: vi.fn(), getChunkSamples: vi.fn() },
}));

const getManifestV2 = vi.mocked(tasksApi.getVideoManifestV2);
const getChunk = vi.mocked(videoApi.getChunk);
const getSamples = vi.mocked(videoApi.getChunkSamples);

// ── WebCodecs 替身(真实走 useVideoChunkDecoder.decodePlan → 持久 GOP session)────────

function fakeFrame(timestamp: number) {
  const close = vi.fn();
  return {
    frame: { timestamp, displayWidth: 320, displayHeight: 240, close } as unknown as VideoFrame,
    close,
  };
}
function fakeBitmap() {
  return { width: 320, height: 240, close: vi.fn() } as unknown as ImageBitmap;
}

class FakeDecoder {
  readonly output: (f: VideoFrame) => void;
  readonly error: (e: DOMException) => void;
  configure = vi.fn();
  decode = vi.fn();
  flush = vi.fn().mockResolvedValue(undefined);
  reset = vi.fn();
  close = vi.fn();
  constructor(init: { output: (f: VideoFrame) => void; error: (e: DOMException) => void }) {
    this.output = init.output;
    this.error = init.error;
  }
  emit(frame: VideoFrame) {
    this.output(frame);
  }
}

interface DecoderCtrl {
  decodeImpl: ((d: FakeDecoder, chunk: FakeEncodedVideoChunk) => void) | null;
  decodeError: DOMException | null;
  supported: boolean;
}
let decoderCtrl: DecoderCtrl;
let decoders: FakeDecoder[];

class FakeEncodedVideoChunk {
  readonly type: "key" | "delta";
  readonly timestamp: number;
  readonly duration: number | undefined;
  readonly byteLength: number;
  constructor(init: {
    type: "key" | "delta";
    timestamp: number;
    duration?: number;
    data: Uint8Array;
  }) {
    this.type = init.type;
    this.timestamp = init.timestamp;
    this.duration = init.duration;
    this.byteLength = init.data.byteLength;
  }
  copyTo(): void {}
}

/** 默认 decode:每个 access unit 产生同 timestamp output。 */
function defaultDecodeEmitChunk() {
  decoderCtrl.decodeImpl = (d, chunk) => {
    d.emit(fakeFrame(chunk.timestamp).frame);
  };
}

function installWebCodecs() {
  decoderCtrl = { decodeImpl: null, decodeError: null, supported: true };
  decoders = [];
  vi.stubGlobal("EncodedVideoChunk", FakeEncodedVideoChunk);
  vi.stubGlobal("createImageBitmap", () => Promise.resolve(fakeBitmap()));
  class FakeVideoDecoder extends FakeDecoder {
    static isConfigSupported = vi.fn(async () => ({
      supported: decoderCtrl.supported,
      config: undefined,
    }));
    constructor(init: { output: (f: VideoFrame) => void; error: (e: DOMException) => void }) {
      super(init);
      decoders.push(this);
      this.decode.mockImplementation((chunk: FakeEncodedVideoChunk) => {
        if (decoderCtrl.decodeError) init.error(decoderCtrl.decodeError);
        decoderCtrl.decodeImpl?.(this, chunk);
      });
    }
  }
  vi.stubGlobal("VideoDecoder", FakeVideoDecoder);
  defaultDecodeEmitChunk();
}

const METADATA_GENERATION = "10000000-0000-4000-8000-000000000001";
function identity(chunkId = 0, size = 64) {
  return {
    metadata_generation_id: METADATA_GENERATION,
    output_id: `20000000-0000-4000-8000-${String(chunkId + 1).padStart(12, "0")}`,
    sha256: createHash("sha256")
      .update(new Uint8Array(makeBytes(size)))
      .digest("hex"),
  };
}

// ── fixtures ──────────────────────────────────────────────────────────────────

function manifest(chunkSize = 10): VideoManifestV2Response {
  return {
    metadata_generation_id: METADATA_GENERATION,
    task_id: "t1",
    dataset_item_id: "ds-1",
    video_url: "http://x/v.mp4",
    poster_url: null,
    fps: 30,
    frame_count: 100,
    duration_ms: 3300,
    chunks_manifest_url: "http://x/chunks",
    frame_timetable_url: "http://x/tt",
    frame_service_base: "http://x",
    chunk_size_frames: chunkSize,
    segments: [],
    frame_cache_formats: ["webp", "jpeg"],
    expires_in: 3600,
  };
}
function readyChunk(url = "http://storage/chunk.mp4", chunkId = 0, size = 64): VideoChunkOut {
  return {
    ...identity(chunkId, size),
    chunk_id: chunkId,
    start_frame: chunkId * 10,
    end_frame: chunkId * 10 + 9,
    status: "ready",
    url,
    byte_size: size,
    generation_mode: null,
    diagnostics: null,
    retry_after: null,
    error: null,
  };
}
function pendingChunk(retryAfter = 1): VideoChunkOut {
  return { ...readyChunk(), status: "pending", url: null, retry_after: retryAfter };
}
function failedChunk(): VideoChunkOut {
  return { ...readyChunk(), status: "failed", url: null, error: "transcode failed" };
}
function samples(frame = 5, ptsMs = 166, chunkId = 0): VideoChunkSamplesResponse {
  return {
    ...identity(chunkId),
    dataset_item_id: "ds-1",
    chunk_id: chunkId,
    codec_string: "avc1.4d001e",
    description: btoa("config"),
    width: 320,
    height: 240,
    samples: [
      {
        frame_index: frame,
        pts_ms: ptsMs,
        duration_ms: 33,
        is_keyframe: true,
        size_bytes: 10,
        offset_in_chunk: 0,
      },
    ],
  };
}

/** 多帧单 GOP samples:frame 0 为 key,其余 delta;decode order == frame order。 */
function multiSamples(frameCount = 10): VideoChunkSamplesResponse {
  const arr = Array.from({ length: frameCount }, (_, f) => ({
    frame_index: f,
    pts_ms: f * 33,
    duration_ms: 33,
    is_keyframe: f === 0,
    size_bytes: 10,
    offset_in_chunk: f * 10,
  }));
  return {
    ...identity(0, 116),
    dataset_item_id: "ds-1",
    chunk_id: 0,
    codec_string: "avc1.4d001e",
    description: btoa("config"),
    width: 320,
    height: 240,
    samples: arr,
  };
}

function makeBytes(n = 64): ArrayBuffer {
  const buf = new ArrayBuffer(n);
  const v = new Uint8Array(buf);
  for (let i = 0; i < n; i++) v[i] = i & 0xff;
  return buf;
}

function apiError(status: number, detail = "err") {
  const e = new Error(detail);
  (e as { status?: number }).status = status;
  return e;
}

let queryClient: QueryClient;
function Wrapper({ children }: { children: ReactNode }) {
  return createElement(QueryClientProvider, { client: queryClient }, children);
}

type PreciseProps = Omit<Parameters<typeof useVideoPreciseFrame>[0], "metadataGenerationId"> & {
  metadataGenerationId?: string | null;
};
function renderPrecise(initialProps: PreciseProps) {
  return renderHook(
    (props: PreciseProps) =>
      useVideoPreciseFrame({ metadataGenerationId: METADATA_GENERATION, ...props }),
    { initialProps, wrapper: Wrapper },
  );
}

describe("useVideoPreciseFrame", () => {
  beforeEach(() => {
    vi.useRealTimers();
    queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false, gcTime: 0, staleTime: 0, refetchOnWindowFocus: false },
      },
    });
    getManifestV2.mockReset();
    getChunk.mockReset();
    getSamples.mockReset();
    vi.spyOn(global, "fetch").mockReset();
    localStorage.setItem(WEBCODECS_FLAG_STORAGE_KEY, "1");
    installWebCodecs();
    vi.stubGlobal("crypto", webcrypto);
    localStorage.setItem("token", "video-test-token");
    useAuthStore.setState({
      token: "video-test-token",
      sessionId: "video-test-session",
      user: { id: "user-1" } as NonNullable<ReturnType<typeof useAuthStore.getState>["user"]>,
    });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    localStorage.removeItem(WEBCODECS_FLAG_STORAGE_KEY);
    queryClient.clear();
  });

  it("将后端 retry_after 秒数转换为毫秒并限制轮询区间", () => {
    expect(retryAfterSecondsToMs(3)).toBe(3000);
    expect(retryAfterSecondsToMs(0.25)).toBe(1000);
    expect(retryAfterSecondsToMs(30)).toBe(10_000);
  });

  it.each(["chunk", "samples"] as const)(
    "%s retries three explicit short locks then reaches ready without waiting for polling",
    async (stage) => {
      getManifestV2.mockResolvedValue(manifest());
      getChunk.mockResolvedValue(readyChunk());
      getSamples.mockResolvedValue(samples());
      const request = stage === "chunk" ? getChunk : getSamples;
      for (const code of ["content_busy", "storage_original_busy", "storage_maintenance_busy"])
        request.mockRejectedValueOnce(new ApiError(409, "busy", { code }));
      vi.mocked(global.fetch).mockResolvedValue({
        ok: true,
        status: 200,
        arrayBuffer: async () => makeBytes(),
      } as Response);
      const { result } = renderPrecise({
        taskId: "t1",
        frameIndex: 5,
        enabled: true,
        bitmapBudgetBytes: 4_000_000,
        chunkBudgetBytes: 8_000_000,
        prefetchFrames: 0,
      });
      await waitFor(() => expect(request).toHaveBeenCalledTimes(1));
      expect(result.current.fallbackReason).toBeNull();
      await waitFor(() => expect(result.current.sourceState).toBe("ready"), { timeout: 3000 });
      expect(request).toHaveBeenCalledTimes(4);
      expect(result.current.bitmap?.frameIndex).toBe(5);
      expect(result.current.fallbackReason).toBeNull();
      expect(stage === "chunk" ? getSamples : getChunk).toHaveBeenCalledTimes(1);
    },
  );

  it.each(
    (["chunk", "samples"] as const).flatMap((stage) => [
      { stage, status: 409, code: "video_metadata_changed" },
      { stage, status: 409, code: "video_output_changed" },
      { stage, status: 403, code: "content_busy" },
      { stage, status: 404, code: "content_busy" },
      { stage, status: 500, code: "content_busy" },
      { stage, status: 503, code: "video_metadata_preparing" },
    ]),
  )(
    "$stage immediately rejects $status $code without retrying",
    async ({ stage, status, code }) => {
      getManifestV2.mockResolvedValue(manifest());
      getChunk.mockResolvedValue(readyChunk());
      getSamples.mockResolvedValue(samples());
      const request = stage === "chunk" ? getChunk : getSamples;
      request.mockRejectedValue(new ApiError(status, "rejected", { code }));
      vi.mocked(global.fetch).mockResolvedValue({
        ok: true,
        status: 200,
        arrayBuffer: async () => makeBytes(),
      } as Response);
      const { result } = renderPrecise({
        taskId: "t1",
        frameIndex: 5,
        enabled: true,
        bitmapBudgetBytes: 4_000_000,
        chunkBudgetBytes: 8_000_000,
        prefetchFrames: 0,
      });
      await waitFor(() => expect(result.current.sourceState).toBe("fallback"));
      const query = queryClient.getQueryCache().findAll({
        queryKey: [stage === "chunk" ? "video-chunk" : "video-chunk-samples"],
      })[0];
      expect(query?.state.status).toBe("error");
      expect(query?.state.fetchStatus).toBe("idle");
      expect(query?.state.fetchFailureCount).toBe(1);
      expect(request).toHaveBeenCalledTimes(1);
      expect(result.current.bitmap).toBeNull();
      expect(decoders).toHaveLength(0);
    },
  );

  it("flag 关闭(decoder inactive)→ manifest/chunk/samples/bytes 零请求,sourceState=disabled", async () => {
    vi.unstubAllGlobals(); // jsdom 无 WebCodecs → decoder inactive
    const { result } = renderPrecise({
      taskId: "t1",
      frameIndex: 5,
      enabled: true,
      bitmapBudgetBytes: 4_000_000,
      chunkBudgetBytes: 8_000_000,
      prefetchFrames: 0,
    });
    expect(result.current.sourceState).toBe("disabled");
    expect(result.current.active).toBe(false);
    // 等 React Query 空转稳定。
    await waitFor(() => expect(getManifestV2).not.toHaveBeenCalled());
    expect(getChunk).not.toHaveBeenCalled();
    expect(getSamples).not.toHaveBeenCalled();
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("enabled=false(播放 / seeking 中)→ manifest 不请求", async () => {
    const { result } = renderPrecise({
      taskId: "t1",
      frameIndex: 5,
      enabled: false,
      bitmapBudgetBytes: 4_000_000,
      chunkBudgetBytes: 8_000_000,
      prefetchFrames: 0,
    });
    expect(result.current.sourceState).toBe("disabled");
    await waitFor(() => expect(getManifestV2).not.toHaveBeenCalled());
  });

  it("manifest chunk_size_frames 非法 → fallback api_unavailable", async () => {
    getManifestV2.mockResolvedValue({ ...manifest(0) });
    const { result } = renderPrecise({
      taskId: "t1",
      frameIndex: 5,
      enabled: true,
      bitmapBudgetBytes: 4_000_000,
      chunkBudgetBytes: 8_000_000,
      prefetchFrames: 0,
    });
    await waitFor(() => expect(result.current.fallbackReason).toBe("api_unavailable"));
    expect(getChunk).not.toHaveBeenCalled();
  });

  it("chunk failed → fallback chunk_failed", async () => {
    getManifestV2.mockResolvedValue(manifest());
    getChunk.mockResolvedValue(failedChunk());
    const { result } = renderPrecise({
      taskId: "t1",
      frameIndex: 5,
      enabled: true,
      bitmapBudgetBytes: 4_000_000,
      chunkBudgetBytes: 8_000_000,
      prefetchFrames: 0,
    });
    await waitFor(() => expect(result.current.fallbackReason).toBe("chunk_failed"));
  });

  it("chunk query reject → fallback api_unavailable", async () => {
    getManifestV2.mockResolvedValue(manifest());
    getChunk.mockRejectedValue(apiError(500, "chunk unavailable"));
    const { result } = renderPrecise({
      taskId: "t1",
      frameIndex: 5,
      enabled: true,
      bitmapBudgetBytes: 4_000_000,
      chunkBudgetBytes: 8_000_000,
      prefetchFrames: 0,
    });
    await waitFor(() => expect(result.current.fallbackReason).toBe("api_unavailable"));
    expect(result.current.sourceState).toBe("fallback");
  });

  it("samples 404 → fallback samples_unavailable,不重试", async () => {
    getManifestV2.mockResolvedValue(manifest());
    getChunk.mockResolvedValue(readyChunk());
    getSamples.mockRejectedValue(apiError(404, "samples_not_available"));
    const { result } = renderPrecise({
      taskId: "t1",
      frameIndex: 5,
      enabled: true,
      bitmapBudgetBytes: 4_000_000,
      chunkBudgetBytes: 8_000_000,
      prefetchFrames: 0,
    });
    await waitFor(() => expect(result.current.fallbackReason).toBe("samples_unavailable"));
    // retry:false → 只请求一次。
    expect(getSamples).toHaveBeenCalledTimes(1);
  });

  it("chunk bytes 500 → fallback chunk_fetch_failed", async () => {
    getManifestV2.mockResolvedValue(manifest());
    getChunk.mockResolvedValue(readyChunk());
    getSamples.mockResolvedValue(samples());
    vi.mocked(global.fetch).mockResolvedValue({ ok: false, status: 500 } as Response);
    const { result } = renderPrecise({
      taskId: "t1",
      frameIndex: 5,
      enabled: true,
      bitmapBudgetBytes: 4_000_000,
      chunkBudgetBytes: 8_000_000,
      prefetchFrames: 0,
    });
    await waitFor(() => expect(result.current.fallbackReason).toBe("chunk_fetch_failed"));
  });

  it("chunk pending → 按 retry_after 轮询 → ready 后继续 pipeline", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    getManifestV2.mockResolvedValue(manifest());
    getChunk.mockResolvedValueOnce(pendingChunk(3)).mockResolvedValueOnce(readyChunk());
    getSamples.mockResolvedValue(samples());
    vi.mocked(global.fetch).mockResolvedValue({
      ok: true,
      status: 200,
      arrayBuffer: () => Promise.resolve(makeBytes()),
    } as Response);
    decoderCtrl.decodeImpl = (d) => {
      d.emit(fakeFrame(166000).frame);
    };
    const { result } = renderPrecise({
      taskId: "t1",
      frameIndex: 5,
      enabled: true,
      bitmapBudgetBytes: 4_000_000,
      chunkBudgetBytes: 8_000_000,
      prefetchFrames: 0,
    });
    await waitFor(() => expect(result.current.sourceState).toBe("chunk-pending"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    await waitFor(() => expect(result.current.sourceState).toBe("ready"));
    expect(getChunk).toHaveBeenCalledTimes(2);
    vi.useRealTimers();
  });

  it("chunk bytes 403 → 等 metadata 新 URL 后只重取一次并恢复 ready", async () => {
    getManifestV2.mockResolvedValue(manifest());
    getChunk
      .mockResolvedValueOnce(readyChunk("http://storage/expired.mp4"))
      .mockResolvedValueOnce(readyChunk("http://storage/fresh.mp4"));
    getSamples.mockResolvedValue(samples());
    vi.mocked(global.fetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("expired")) return { ok: false, status: 403 } as Response;
      return {
        ok: true,
        status: 200,
        arrayBuffer: () => Promise.resolve(makeBytes()),
      } as Response;
    });
    decoderCtrl.decodeImpl = (d) => {
      d.emit(fakeFrame(166000).frame);
    };

    const { result } = renderPrecise({
      taskId: "t1",
      frameIndex: 5,
      enabled: true,
      bitmapBudgetBytes: 4_000_000,
      chunkBudgetBytes: 8_000_000,
      prefetchFrames: 0,
    });

    await waitFor(() => expect(result.current.sourceState).toBe("ready"));
    expect(getChunk).toHaveBeenCalledTimes(2);
    expect(global.fetch).toHaveBeenCalledTimes(2);
    expect(vi.mocked(global.fetch).mock.calls.map(([url]) => String(url))).toEqual([
      "http://storage/expired.mp4",
      "http://storage/fresh.mp4",
    ]);
    expect(result.current.diagnostics.urlRefreshed).toBe(true);
  });

  it("刷新后的 signed URL 再次 403 → 不循环刷新并 fallback", async () => {
    getManifestV2.mockResolvedValue(manifest());
    getChunk
      .mockResolvedValueOnce(readyChunk("http://storage/expired.mp4"))
      .mockResolvedValueOnce(readyChunk("http://storage/still-expired.mp4"));
    getSamples.mockResolvedValue(samples());
    vi.mocked(global.fetch).mockResolvedValue({ ok: false, status: 403 } as Response);

    const { result } = renderPrecise({
      taskId: "t1",
      frameIndex: 5,
      enabled: true,
      bitmapBudgetBytes: 4_000_000,
      chunkBudgetBytes: 8_000_000,
      prefetchFrames: 0,
    });

    await waitFor(() => expect(result.current.fallbackReason).toBe("chunk_fetch_failed"));
    expect(getChunk).toHaveBeenCalledTimes(2);
    expect(global.fetch).toHaveBeenCalledTimes(2);
  });

  it("decode 成功 → sourceState ready + bitmap frameIndex 匹配", async () => {
    getManifestV2.mockResolvedValue(manifest());
    getChunk.mockResolvedValue(readyChunk());
    getSamples.mockResolvedValue(samples(5, 166));
    vi.mocked(global.fetch).mockResolvedValue({
      ok: true,
      status: 200,
      arrayBuffer: () => Promise.resolve(makeBytes()),
    } as Response);
    decoderCtrl.decodeImpl = (d) => {
      d.emit(fakeFrame(166000).frame);
    };
    const { result } = renderPrecise({
      taskId: "t1",
      frameIndex: 5,
      enabled: true,
      bitmapBudgetBytes: 4_000_000,
      chunkBudgetBytes: 8_000_000,
      prefetchFrames: 0,
    });
    await waitFor(() => expect(result.current.sourceState).toBe("ready"));
    expect(result.current.bitmap).not.toBeNull();
    expect(result.current.bitmap?.frameIndex).toBe(5);
  });

  it("换帧 demux 失败时立即清空上一帧的 GOP / PTS / codec 诊断", async () => {
    getManifestV2.mockResolvedValue(manifest());
    getChunk.mockResolvedValue(readyChunk());
    // samples 只有 frame 5；切到 frame 6 后 buildGopPlan 必须失败。
    getSamples.mockResolvedValue(samples(5, 166));
    vi.mocked(global.fetch).mockResolvedValue({
      ok: true,
      status: 200,
      arrayBuffer: () => Promise.resolve(makeBytes()),
    } as Response);
    decoderCtrl.decodeImpl = (decoder) => {
      decoder.emit(fakeFrame(166000).frame);
    };
    const { result, rerender } = renderPrecise({
      taskId: "t1",
      frameIndex: 5,
      enabled: true,
      bitmapBudgetBytes: 4_000_000,
      chunkBudgetBytes: 8_000_000,
      prefetchFrames: 0,
    });
    await waitFor(() => expect(result.current.sourceState).toBe("ready"));
    expect(result.current.performance).toMatchObject({
      gopStartDecodeIndex: 0,
      targetTimestampUs: 166000,
      codec: "avc1.4d001e",
    });

    rerender({
      taskId: "t1",
      frameIndex: 6,
      enabled: true,
      bitmapBudgetBytes: 4_000_000,
      chunkBudgetBytes: 8_000_000,
      prefetchFrames: 0,
    });
    expect(result.current.performance).toMatchObject({
      gopStartDecodeIndex: null,
      targetTimestampUs: null,
      codec: null,
    });
    await waitFor(() => expect(result.current.fallbackReason).toBe("invalid_sample_range"));
    expect(result.current.performance).toMatchObject({
      gopStartDecodeIndex: null,
      targetTimestampUs: null,
      codec: null,
    });
  });

  it("同一帧号切换任务时不会短暂发布上一任务的 plan 诊断", async () => {
    getManifestV2.mockResolvedValue(manifest());
    getChunk.mockResolvedValue(readyChunk());
    getSamples.mockResolvedValue(samples(5, 166));
    vi.mocked(global.fetch).mockResolvedValue({
      ok: true,
      status: 200,
      arrayBuffer: () => Promise.resolve(makeBytes()),
    } as Response);
    decoderCtrl.decodeImpl = (decoder) => {
      decoder.emit(fakeFrame(166000).frame);
    };
    const { result, rerender } = renderPrecise({
      taskId: "t1",
      frameIndex: 5,
      enabled: true,
      bitmapBudgetBytes: 4_000_000,
      chunkBudgetBytes: 8_000_000,
      prefetchFrames: 0,
    });
    await waitFor(() => expect(result.current.sourceState).toBe("ready"));
    expect(result.current.performance.targetTimestampUs).toBe(166000);

    rerender({
      taskId: "t2",
      frameIndex: 5,
      enabled: true,
      bitmapBudgetBytes: 4_000_000,
      chunkBudgetBytes: 8_000_000,
      prefetchFrames: 0,
    });
    expect(result.current.performance).toMatchObject({
      gopStartDecodeIndex: null,
      targetTimestampUs: null,
      codec: null,
    });
  });

  it("chunk byte-LRU 按累计字节预算淘汰，跨 chunk 返回时不会由 React Query 无限保留", async () => {
    getManifestV2.mockResolvedValue(manifest(10));
    getChunk.mockImplementation(async (_datasetItemId, chunkId) =>
      readyChunk(`http://storage/chunk-${chunkId}.mp4`, chunkId),
    );
    getSamples.mockImplementation(async (_datasetItemId, chunkId) => {
      const frame = chunkId * 10 + 5;
      return samples(frame, frame * 33, chunkId);
    });
    vi.mocked(global.fetch).mockImplementation(
      async () =>
        ({
          ok: true,
          status: 200,
          arrayBuffer: () => Promise.resolve(makeBytes(64)),
        }) as Response,
    );

    const { result, rerender } = renderPrecise({
      taskId: "t1",
      frameIndex: 5,
      enabled: true,
      bitmapBudgetBytes: 4_000_000,
      chunkBudgetBytes: 80,
      prefetchFrames: 0,
    });
    await waitFor(() => expect(result.current.bitmap?.frameIndex).toBe(5));

    await act(async () => {
      rerender({
        taskId: "t1",
        frameIndex: 15,
        enabled: true,
        bitmapBudgetBytes: 4_000_000,
        chunkBudgetBytes: 80,
        prefetchFrames: 0,
      });
    });
    await waitFor(() => expect(result.current.bitmap?.frameIndex).toBe(15));
    expect(result.current.performance.chunkBytes).toBeLessThanOrEqual(80);
    expect(result.current.performance.evictions).toBeGreaterThanOrEqual(1);

    await act(async () => {
      rerender({
        taskId: "t1",
        frameIndex: 5,
        enabled: true,
        bitmapBudgetBytes: 4_000_000,
        chunkBudgetBytes: 80,
        prefetchFrames: 0,
      });
    });
    await waitFor(() => expect(result.current.bitmap?.frameIndex).toBe(5));
    await waitFor(() => expect(global.fetch).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(result.current.performance.bytesFetched).toBe(64 * 3));
  });

  it("chunk byte cache 的 60 秒 TTL 不因命中续期，到期后释放缓存引用", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    getManifestV2.mockResolvedValue(manifest());
    getChunk.mockResolvedValue(readyChunk());
    getSamples.mockResolvedValue(samples(5, 166));
    vi.mocked(global.fetch).mockResolvedValue({
      ok: true,
      status: 200,
      arrayBuffer: () => Promise.resolve(makeBytes(64)),
    } as Response);
    const { result } = renderPrecise({
      taskId: "t1",
      frameIndex: 5,
      enabled: true,
      bitmapBudgetBytes: 4_000_000,
      chunkBudgetBytes: 80,
      prefetchFrames: 0,
    });
    await waitFor(() => expect(result.current.sourceState).toBe("ready"));
    expect(result.current.performance.chunkBytes).toBe(64);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_001);
    });
    expect(result.current.performance.chunkBytes).toBe(0);
    vi.useRealTimers();
  });

  it("decode 失败(decoder error)→ fallback decode_failed", async () => {
    getManifestV2.mockResolvedValue(manifest());
    getChunk.mockResolvedValue(readyChunk());
    getSamples.mockResolvedValue(samples(5, 166));
    vi.mocked(global.fetch).mockResolvedValue({
      ok: true,
      status: 200,
      arrayBuffer: () => Promise.resolve(makeBytes()),
    } as Response);
    decoderCtrl.decodeError = new DOMException("decode error");
    const { result } = renderPrecise({
      taskId: "t1",
      frameIndex: 5,
      enabled: true,
      bitmapBudgetBytes: 4_000_000,
      chunkBudgetBytes: 8_000_000,
      prefetchFrames: 0,
    });
    await waitFor(() => expect(result.current.fallbackReason).toBe("decode_failed"));
    expect(result.current.bitmap).toBeNull();
  });

  it("codec config unsupported → 保留 codec_unsupported reason", async () => {
    getManifestV2.mockResolvedValue(manifest());
    getChunk.mockResolvedValue(readyChunk());
    getSamples.mockResolvedValue(samples(5, 166));
    vi.mocked(global.fetch).mockResolvedValue({
      ok: true,
      status: 200,
      arrayBuffer: () => Promise.resolve(makeBytes()),
    } as Response);
    decoderCtrl.supported = false;

    const { result } = renderPrecise({
      taskId: "t1",
      frameIndex: 5,
      enabled: true,
      bitmapBudgetBytes: 4_000_000,
      chunkBudgetBytes: 8_000_000,
      prefetchFrames: 0,
    });
    await waitFor(() => expect(result.current.fallbackReason).toBe("codec_unsupported"));
    expect(result.current.sourceState).toBe("fallback");
    expect(decoders).toHaveLength(0);
  });

  it("卸载时中止在途 manifest 请求", async () => {
    const requestSignal: { current: AbortSignal | null } = { current: null };
    getManifestV2.mockImplementation(
      (_taskId, init) =>
        new Promise<VideoManifestV2Response>((_resolve, reject) => {
          requestSignal.current = init?.signal ?? null;
          requestSignal.current?.addEventListener("abort", () => {
            reject(new DOMException("aborted", "AbortError"));
          });
        }),
    );
    const { unmount } = renderPrecise({
      taskId: "t1",
      frameIndex: 5,
      enabled: true,
      bitmapBudgetBytes: 4_000_000,
      chunkBudgetBytes: 8_000_000,
      prefetchFrames: 0,
    });
    await waitFor(() => expect(getManifestV2).toHaveBeenCalledTimes(1));
    unmount();
    expect(requestSignal.current?.aborted).toBe(true);
  });

  it("frame 切换后旧 precise bitmap 不显示(frameIndex 守卫,防 stale)", async () => {
    getManifestV2.mockResolvedValue(manifest());
    getChunk.mockResolvedValue(readyChunk());
    getSamples.mockResolvedValue(samples(5, 166));
    vi.mocked(global.fetch).mockResolvedValue({
      ok: true,
      status: 200,
      arrayBuffer: () => Promise.resolve(makeBytes()),
    } as Response);
    decoderCtrl.decodeImpl = (d) => {
      d.emit(fakeFrame(166000).frame);
    };
    const { result, rerender } = renderPrecise({
      taskId: "t1",
      frameIndex: 5,
      enabled: true,
      bitmapBudgetBytes: 4_000_000,
      chunkBudgetBytes: 8_000_000,
      prefetchFrames: 0,
    });
    await waitFor(() => expect(result.current.bitmap?.frameIndex).toBe(5));
    // 快速 seek 到 frame 6:即便旧 frame 5 的 bitmap 仍在 decoder 缓存,frameIndex 守卫确保它
    // 不被当作当前帧显示(bitmap.frameIndex 必须严格匹配当前 frameIndex)。
    act(() => {
      rerender({
        taskId: "t1",
        frameIndex: 6,
        enabled: true,
        bitmapBudgetBytes: 4_000_000,
        chunkBudgetBytes: 8_000_000,
        prefetchFrames: 0,
      });
    });
    expect(result.current.bitmap).toBeNull();
  });

  it("standard 档前进方向预取同 GOP 后续 2 帧,后续命中计入 prefetchHits", async () => {
    getManifestV2.mockResolvedValue(manifest(10));
    getChunk.mockResolvedValue(readyChunk(undefined, 0, 116));
    getSamples.mockResolvedValue(multiSamples(10));
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: true,
      arrayBuffer: () => Promise.resolve(makeBytes(116)),
    } as unknown as Response);
    const { result, rerender } = renderPrecise({
      taskId: "t1",
      frameIndex: 3,
      enabled: true,
      bitmapBudgetBytes: 4_000_000,
      chunkBudgetBytes: 8_000_000,
      prefetchFrames: 2,
    });
    await waitFor(() => expect(result.current.bitmap?.frameIndex).toBe(3));
    expect(result.current.performance.prefetchRequests).toBe(0);
    // 前进 3 → 4:方向 +1,预取同 GOP(frame0 key)的 5、6。
    await act(async () => {
      rerender({
        taskId: "t1",
        frameIndex: 4,
        enabled: true,
        bitmapBudgetBytes: 4_000_000,
        chunkBudgetBytes: 8_000_000,
        prefetchFrames: 2,
      });
    });
    await waitFor(() => expect(result.current.bitmap?.frameIndex).toBe(4));
    await waitFor(() => expect(result.current.performance.prefetchRequests).toBe(2));
    // 导航到预取过的 6 → 命中预取缓存。
    await act(async () => {
      rerender({
        taskId: "t1",
        frameIndex: 6,
        enabled: true,
        bitmapBudgetBytes: 4_000_000,
        chunkBudgetBytes: 8_000_000,
        prefetchFrames: 2,
      });
    });
    await waitFor(() => expect(result.current.bitmap?.frameIndex).toBe(6));
    expect(result.current.performance.prefetchHits).toBeGreaterThanOrEqual(1);
  });

  it("原生 seek 未稳定时立即展示已缓存精确帧，不启动新的 decode", async () => {
    getManifestV2.mockResolvedValue(manifest(10));
    getChunk.mockResolvedValue(readyChunk(undefined, 0, 116));
    getSamples.mockResolvedValue(multiSamples(10));
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: true,
      arrayBuffer: () => Promise.resolve(makeBytes(116)),
    } as unknown as Response);
    const base = {
      taskId: "t1",
      enabled: true,
      decodeEnabled: true,
      bitmapBudgetBytes: 4_000_000,
      chunkBudgetBytes: 8_000_000,
      prefetchFrames: 0,
    };
    const { result, rerender } = renderPrecise({ ...base, frameIndex: 3 });
    await waitFor(() => expect(result.current.bitmap?.frameIndex).toBe(3));
    await act(async () => rerender({ ...base, frameIndex: 4 }));
    await waitFor(() => expect(result.current.bitmap?.frameIndex).toBe(4));
    const decodeCalls = decoders.reduce(
      (count, decoder) => count + decoder.decode.mock.calls.length,
      0,
    );

    await act(async () =>
      rerender({
        ...base,
        frameIndex: 3,
        decodeEnabled: false,
      }),
    );

    await waitFor(() => expect(result.current.bitmap?.frameIndex).toBe(3));
    await waitFor(() => expect(result.current.performance.targetTimestampUs).toBe(99_000));
    expect(decoders.reduce((count, decoder) => count + decoder.decode.mock.calls.length, 0)).toBe(
      decodeCalls,
    );
    act(() => result.current.markFramePainted(3));
    expect(result.current.performance.paintedFrameIndex).toBe(3);
    expect(result.current.performance.lastPaintMs).not.toBeNull();
  });

  it("light 档(prefetchFrames=0)与播放态(enabled=false)均不预取", async () => {
    getManifestV2.mockResolvedValue(manifest(10));
    getChunk.mockResolvedValue(readyChunk(undefined, 0, 116));
    getSamples.mockResolvedValue(multiSamples(10));
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: true,
      arrayBuffer: () => Promise.resolve(makeBytes(116)),
    } as unknown as Response);
    const { result, rerender } = renderPrecise({
      taskId: "t1",
      frameIndex: 3,
      enabled: true,
      bitmapBudgetBytes: 4_000_000,
      chunkBudgetBytes: 8_000_000,
      prefetchFrames: 0,
    });
    await waitFor(() => expect(result.current.bitmap?.frameIndex).toBe(3));
    await act(async () => {
      rerender({
        taskId: "t1",
        frameIndex: 4,
        enabled: true,
        bitmapBudgetBytes: 4_000_000,
        chunkBudgetBytes: 8_000_000,
        prefetchFrames: 0,
      });
    });
    await waitFor(() => expect(result.current.bitmap?.frameIndex).toBe(4));
    expect(result.current.performance.prefetchRequests).toBe(0); // light 不预取
    // 切到播放态:即便 prefetchFrames=2 也不预取(enabled=false → pipelineEnabled=false)。
    await act(async () => {
      rerender({
        taskId: "t1",
        frameIndex: 5,
        enabled: false,
        bitmapBudgetBytes: 4_000_000,
        chunkBudgetBytes: 8_000_000,
        prefetchFrames: 2,
      });
    });
    expect(result.current.performance.prefetchRequests).toBe(0);
  });

  it("rejects packet offsets from another output before downloading or decoding", async () => {
    getManifestV2.mockResolvedValue(manifest());
    getChunk.mockResolvedValue(readyChunk());
    getSamples.mockResolvedValue({
      ...samples(),
      output_id: "20000000-0000-4000-8000-000000000099",
    });
    const { result } = renderPrecise({
      taskId: "t1",
      frameIndex: 5,
      enabled: true,
      bitmapBudgetBytes: 4_000_000,
      chunkBudgetBytes: 8_000_000,
      prefetchFrames: 0,
    });
    await waitFor(() => expect(result.current.fallbackReason).toBe("api_unavailable"));
    expect(getSamples.mock.calls[0][3]).toBe(readyChunk().output_id);
    expect(global.fetch).not.toHaveBeenCalled();
    expect(decoders).toHaveLength(0);
  });

  it("rejects mutated bytes of the same length without admitting them to caches", async () => {
    getManifestV2.mockResolvedValue(manifest());
    getChunk.mockResolvedValue(readyChunk());
    getSamples.mockResolvedValue(samples());
    const corrupt = makeBytes();
    new Uint8Array(corrupt)[63] ^= 1;
    vi.mocked(global.fetch).mockResolvedValue({
      ok: true,
      status: 200,
      arrayBuffer: async () => corrupt,
    } as Response);
    const { result } = renderPrecise({
      taskId: "t1",
      frameIndex: 5,
      enabled: true,
      bitmapBudgetBytes: 4_000_000,
      chunkBudgetBytes: 8_000_000,
      prefetchFrames: 0,
    });
    await waitFor(() => expect(result.current.fallbackReason).toBe("chunk_fetch_failed"));
    expect(decoders).toHaveLength(0);
    expect(result.current.performance.chunkBytes).toBe(0);
    expect(result.current.bitmap).toBeNull();
  });

  it("binds new offsets and bytes when a 403 lease refresh publishes a new output", async () => {
    const renewedBytes = makeBytes();
    new Uint8Array(renewedBytes)[63] ^= 1;
    const renewedIdentity = {
      ...identity(),
      output_id: "20000000-0000-4000-8000-000000000002",
      sha256: createHash("sha256").update(new Uint8Array(renewedBytes)).digest("hex"),
    };
    getManifestV2.mockResolvedValue(manifest());
    getChunk
      .mockResolvedValueOnce(readyChunk("http://storage/expired.mp4"))
      .mockResolvedValue({ ...readyChunk("http://storage/renewed.mp4"), ...renewedIdentity });
    getSamples
      .mockResolvedValueOnce(samples())
      .mockResolvedValue({ ...samples(5, 200), ...renewedIdentity });
    vi.mocked(global.fetch).mockImplementation(async (url) =>
      String(url).includes("expired")
        ? ({ ok: false, status: 403 } as Response)
        : ({ ok: true, status: 200, arrayBuffer: async () => renewedBytes } as Response),
    );
    const { result } = renderPrecise({
      taskId: "t1",
      frameIndex: 5,
      enabled: true,
      bitmapBudgetBytes: 4_000_000,
      chunkBudgetBytes: 8_000_000,
      prefetchFrames: 0,
    });
    await waitFor(() => expect(result.current.sourceState).toBe("ready"));
    expect(getSamples.mock.calls.map((call) => call[3])).toEqual([
      identity().output_id,
      renewedIdentity.output_id,
    ]);
    expect(
      decoders.flatMap((decoder) => decoder.decode.mock.calls).map(([chunk]) => chunk.timestamp),
    ).toEqual([200_000]);
    expect(result.current.performance.bytesFetched).toBe(64);
    for (const query of queryClient.getQueryCache().getAll()) {
      expect(JSON.stringify(query.queryKey)).not.toContain("http://storage");
      expect(JSON.stringify(query.queryKey)).not.toContain("video-test-token");
    }
  });

  it("does not publish or cache late bytes from retired credentials", async () => {
    let resolveOld: ((buffer: ArrayBuffer) => void) | undefined;
    getManifestV2.mockResolvedValue(manifest());
    getChunk.mockResolvedValue(readyChunk());
    getSamples.mockResolvedValue(samples());
    vi.mocked(global.fetch)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        arrayBuffer: () =>
          new Promise<ArrayBuffer>((resolve) => {
            resolveOld = resolve;
          }),
      } as Response)
      .mockResolvedValue({
        ok: true,
        status: 200,
        arrayBuffer: async () => makeBytes(),
      } as Response);
    const { result } = renderPrecise({
      taskId: "t1",
      frameIndex: 5,
      enabled: true,
      bitmapBudgetBytes: 4_000_000,
      chunkBudgetBytes: 8_000_000,
      prefetchFrames: 0,
    });
    await waitFor(() => expect(resolveOld).toBeDefined());
    const oldSignal = vi.mocked(global.fetch).mock.calls[0][1]?.signal;
    act(() => useAuthStore.getState().setToken("renewed-video-token"));
    await waitFor(() => expect(result.current.sourceState).toBe("ready"));
    expect(oldSignal?.aborted).toBe(true);
    expect(getManifestV2).toHaveBeenCalledTimes(2);
    expect(getSamples).toHaveBeenCalledTimes(2);
    const beforeDecode = decoders.reduce((n, decoder) => n + decoder.decode.mock.calls.length, 0);
    await act(async () => {
      resolveOld?.(makeBytes());
      await Promise.resolve();
    });
    expect(decoders.reduce((n, decoder) => n + decoder.decode.mock.calls.length, 0)).toBe(
      beforeDecode,
    );
    expect(result.current.performance.bytesFetched).toBe(64);
  });

  it("retires decoder bitmaps and caches when metadata regenerates for the same task", async () => {
    let generation = METADATA_GENERATION;
    let output = identity().output_id;
    getManifestV2.mockImplementation(async () => ({
      ...manifest(),
      metadata_generation_id: generation,
    }));
    getChunk.mockImplementation(async () => ({
      ...readyChunk(),
      metadata_generation_id: generation,
      output_id: output,
    }));
    getSamples.mockImplementation(async () => ({
      ...samples(),
      metadata_generation_id: generation,
      output_id: output,
    }));
    vi.mocked(global.fetch).mockResolvedValue({
      ok: true,
      status: 200,
      arrayBuffer: async () => makeBytes(),
    } as Response);
    const props = {
      taskId: "t1",
      frameIndex: 5,
      enabled: true,
      bitmapBudgetBytes: 4_000_000,
      chunkBudgetBytes: 8_000_000,
      prefetchFrames: 0,
    };
    const { result, rerender } = renderPrecise(props);
    await waitFor(() => expect(result.current.sourceState).toBe("ready"));
    const oldBitmap = result.current.bitmap?.bitmap;
    generation = "10000000-0000-4000-8000-000000000002";
    output = "20000000-0000-4000-8000-000000000002";
    rerender({ ...props, metadataGenerationId: generation });
    expect(result.current.bitmap).toBeNull();
    expect(oldBitmap?.close).toHaveBeenCalledTimes(1);
    await waitFor(() => {
      expect(result.current.sourceState).toBe("ready");
      expect(result.current.bitmap?.bitmap).not.toBe(oldBitmap);
    });
    expect(getSamples.mock.calls.map((call) => call[3])).toEqual([identity().output_id, output]);
    expect(global.fetch).toHaveBeenCalledTimes(2);
    expect(decoders).toHaveLength(2);
    expect(result.current.performance.chunkBytes).toBe(64);
  });

  it("rejects an old manifest-v2 generation immediately after the main manifest changes", async () => {
    getManifestV2.mockResolvedValue(manifest());
    const { result } = renderPrecise({
      taskId: "t1",
      metadataGenerationId: "10000000-0000-4000-8000-000000000002",
      frameIndex: 5,
      enabled: true,
      bitmapBudgetBytes: 4_000_000,
      chunkBudgetBytes: 8_000_000,
      prefetchFrames: 0,
    });
    await waitFor(() => expect(result.current.fallbackReason).toBe("api_unavailable"));
    expect(getManifestV2).toHaveBeenCalledTimes(1);
    expect(result.current.bitmap).toBeNull();
    expect(getChunk).not.toHaveBeenCalled();
    expect(decoders).toHaveLength(0);
  });

  it("closes a late G1 bitmap after the main generation switches to G2 at the same task/frame", async () => {
    let generation = METADATA_GENERATION;
    let output = identity().output_id;
    getManifestV2.mockImplementation(async () => ({
      ...manifest(),
      metadata_generation_id: generation,
    }));
    getChunk.mockImplementation(async () => ({
      ...readyChunk(),
      metadata_generation_id: generation,
      output_id: output,
    }));
    getSamples.mockImplementation(async () => ({
      ...samples(),
      metadata_generation_id: generation,
      output_id: output,
    }));
    vi.mocked(global.fetch).mockResolvedValue({
      ok: true,
      status: 200,
      arrayBuffer: async () => makeBytes(),
    } as Response);
    let resolveOld!: (bitmap: ImageBitmap) => void;
    const oldBitmap = fakeBitmap();
    const nextBitmap = fakeBitmap();
    vi.stubGlobal(
      "createImageBitmap",
      vi
        .fn()
        .mockImplementationOnce(
          () =>
            new Promise((resolve) => {
              resolveOld = resolve;
            }),
        )
        .mockResolvedValue(nextBitmap),
    );
    const props = {
      taskId: "t1",
      frameIndex: 5,
      enabled: true,
      bitmapBudgetBytes: 4_000_000,
      chunkBudgetBytes: 8_000_000,
      prefetchFrames: 0,
    };
    const { result, rerender } = renderPrecise(props);
    await waitFor(() => expect(resolveOld).toBeTypeOf("function"));
    generation = "10000000-0000-4000-8000-000000000002";
    output = "20000000-0000-4000-8000-000000000002";
    rerender({ ...props, metadataGenerationId: generation });
    expect(result.current.bitmap).toBeNull();
    await waitFor(() => expect(result.current.bitmap?.bitmap).toBe(nextBitmap));
    await act(async () => {
      resolveOld(oldBitmap);
    });
    await waitFor(() => expect(oldBitmap.close).toHaveBeenCalledTimes(1));
    expect(result.current.bitmap?.bitmap).toBe(nextBitmap);
    expect(nextBitmap.close).not.toHaveBeenCalled();
    expect(getManifestV2).toHaveBeenCalledTimes(2);
    expect(getSamples.mock.calls.map((call) => call[3])).toEqual([identity().output_id, output]);
  });

  // latest-request-wins decode 竞态(decode 进行中切帧/task):由 decode effect 的 `cancelled`
  // cleanup + `latestRef.{taskId,frameIndex,enabled}` 检查实现;同类竞态(task 切换丢弃旧
  // bitmap、staleResults 计数)已在 useVideoChunkDecoder.test.ts 覆盖。
});

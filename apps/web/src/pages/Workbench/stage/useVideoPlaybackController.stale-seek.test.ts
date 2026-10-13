// Issue #114 复现 harness：真实 useFrameClock + 真实 useVideoPlaybackController 集成。
// 既有 useVideoPlaybackController.test.ts 把 useFrameClock 整体 mock 掉，无法覆盖
// 「时间轴点击 seek → 媒体管线积压 → 连续步进 → 迟到的 seeked/rVFC 回放」交错；
// 本文件只 mock 位图/精确帧/预览等子 hook，帧钟走真实现，fake <video> 手动驱动
// 媒体事件时序，断言帧号（宿主受控态）不被迟到的旧帧回写。
import { act, fireEvent, renderHook } from "@testing-library/react";
import { useRef, useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const videoHookMocks = vi.hoisted(() => ({
  activeBitmap: null as {
    frameIndex: number;
    bitmap: ImageBitmap;
    width: number;
    height: number;
  } | null,
  preciseBitmap: null as {
    frameIndex: number;
    bitmap: ImageBitmap;
    width: number;
    height: number;
  } | null,
  capture: vi.fn(),
  showFrame: vi.fn(),
}));

vi.mock("./useVideoBitmapCache", () => ({
  useVideoBitmapCache: () => ({
    activeBitmap: videoHookMocks.activeBitmap,
    cachedRanges: [],
    capture: videoHookMocks.capture,
    showFrame: videoHookMocks.showFrame,
  }),
}));
vi.mock("./useVideoPreciseFrame", () => ({
  useVideoPreciseFrame: () => ({
    active: false,
    bitmap: videoHookMocks.preciseBitmap,
    sourceState: "disabled",
    fallbackReason: null,
    diagnostics: {
      supported: false,
      webcodecsEnabled: false,
      decoderActive: false,
      chunkId: null,
      datasetItemId: null,
      chunkSizeFrames: null,
      decodeRequests: 0,
      decoderErrors: 0,
      urlRefreshed: false,
    },
    performance: {
      manifestCacheHits: 0,
      samplesCacheHits: 0,
      chunkByteCacheHits: 0,
      bitmapCacheHits: 0,
      bytesFetched: 0,
      bitmapBytes: 0,
      bitmapBudgetBytes: 0,
      activeDecoders: 0,
      liveVideoFrames: 0,
      chunkBytes: 0,
      chunkBudgetBytes: 0,
      sessionCreates: 0,
      sessionResets: 0,
      sessionDisposals: 0,
      encodedChunksSubmitted: 0,
      staleResults: 0,
      prefetchRequests: 0,
      prefetchHits: 0,
      evictions: 0,
      lastManifestMs: null,
      lastSamplesMs: null,
      lastChunkFetchMs: null,
      lastDemuxMs: null,
      lastQueueMs: null,
      lastCodecMs: null,
      lastDecodeMs: null,
      lastBitmapMs: null,
      lastDecodeMode: null,
      lastPaintMs: null,
      lastVisibleMs: null,
      paintedFrameIndex: null,
      gopStartDecodeIndex: null,
      targetTimestampUs: null,
      codec: null,
    },
    markFramePainted: vi.fn(),
  }),
}));
vi.mock("./useVideoFramePreview", () => ({
  useVideoFramePreview: () => ({ preview: null, previewFor: vi.fn(), prefetch: vi.fn() }),
}));
vi.mock("./useVideoTrackActions", () => ({
  useVideoTrackActions: () => ({
    selectedTrackLocked: false,
    toggleSelectedTrackOutside: vi.fn(),
    toggleSelectedTrackOccluded: vi.fn(),
    toggleSelectedTrackHidden: vi.fn(),
    toggleSelectedTrackLocked: vi.fn(),
    propagateSelectedTrack: vi.fn(),
  }),
}));
vi.mock("@/utils/imageBitmapToJpeg", () => ({
  imageBitmapToJpeg: vi.fn().mockResolvedValue(null),
  videoElementToJpeg: vi.fn().mockResolvedValue(null),
}));

import { useVideoPlaybackController } from "./useVideoPlaybackController";
import { frameToSeekTime } from "./frameTimebase";
import type { TaskVideoFrameTimetableResponse } from "@/types";

const FPS = 30;
const FRAME_COUNT = 200;

const MANIFEST = {
  task_id: "T1",
  video_url: "http://x/v.mp4",
  metadata: { fps: FPS, frame_count: FRAME_COUNT },
} as never;

const TIMETABLE = {
  source: "ffprobe",
  fps: FPS,
  frame_count: FRAME_COUNT,
  frames: Array.from({ length: FRAME_COUNT }, (_, index) => ({
    frame_index: index,
    pts_ms: Math.round((index / FPS) * 1000),
  })),
} as unknown as TaskVideoFrameTimetableResponse;

type VideoFrameCallback = (now: number, metadata: { mediaTime: number }) => void;

/** jsdom <video> 替身：rVFC 句柄表 + 手动驱动的媒体管线（seeked/rVFC 由测试控制时序）。 */
function mediaElement() {
  const video = document.createElement("video");
  Object.defineProperties(video, {
    readyState: { configurable: true, value: HTMLMediaElement.HAVE_CURRENT_DATA },
    videoWidth: { configurable: true, value: 640 },
    videoHeight: { configurable: true, value: 360 },
    seeking: { configurable: true, writable: true, value: false },
  });
  video.play = vi.fn(() => undefined as unknown as Promise<void>);
  video.pause = vi.fn();
  const callbacks = new Map<number, VideoFrameCallback>();
  let nextHandle = 0;
  const withRvfc = video as HTMLVideoElement & {
    requestVideoFrameCallback?: (cb: VideoFrameCallback) => number;
    cancelVideoFrameCallback?: (handle: number) => void;
  };
  withRvfc.requestVideoFrameCallback = (cb) => {
    const handle = nextHandle++;
    // DOM 原生回调接受更宽的 metadata；useFrameClock 运行时只读 mediaTime，收窄安全。
    callbacks.set(handle, cb as VideoFrameCallback);
    return handle;
  };
  withRvfc.cancelVideoFrameCallback = (handle) => {
    callbacks.delete(handle);
  };
  /** 媒体管线呈现 frame：currentTime 对齐该帧 PTS，并派发全部挂起的 rVFC。 */
  const present = (frame: number) => {
    video.currentTime = frameToSeekTime(frame, {
      fps: FPS,
      frameCount: FRAME_COUNT,
      source: "ffprobe",
      ptsMs: TIMETABLE.frames.map((f) => f.pts_ms),
    });
    for (const [handle, cb] of [...callbacks]) {
      callbacks.delete(handle);
      cb(performance.now(), { mediaTime: frame / FPS });
    }
  };
  return { video, callbacks, present };
}

/** 宿主模型：受控 frameIndex + 记录全部 onFrameIndexChange 写入（检测迟到回写）。 */
function setupHost(
  video: HTMLVideoElement,
  overrides: Partial<Parameters<typeof useVideoPlaybackController>[0]> = {},
) {
  const writes: number[] = [];
  const utils = renderHook(() => {
    const videoRef = useRef<HTMLVideoElement | null>(video);
    const [frameIndex, setFrameIndex] = useState(0);
    const controller = useVideoPlaybackController({
      manifest: MANIFEST,
      frameTimetable: TIMETABLE,
      videoRef,
      controlledFrameIndex: frameIndex,
      onFrameIndexChange: (frame) => {
        writes.push(frame);
        setFrameIndex(frame);
      },
      annotations: [],
      selectedId: null,
      selectedTrack: null,
      hiddenTrackIds: new Set<string>(),
      lockedTrackIds: new Set<string>(),
      readOnly: false,
      drag: null,
      currentFrameEntries: [],
      onUpdate: vi.fn(),
      ...overrides,
    });
    return controller;
  });
  return { ...utils, writes };
}

describe("useVideoPlaybackController × useFrameClock stale-seek 交错", () => {
  beforeEach(() => {
    videoHookMocks.activeBitmap = null;
    videoHookMocks.preciseBitmap = null;
    videoHookMocks.capture.mockReset().mockResolvedValue(null);
    videoHookMocks.showFrame.mockReset();
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("点击帧未呈现期间连续步进：pending 超时后迟到的点击帧 seeked/rVFC 不得回写帧号", async () => {
    const native = mediaElement();
    const { result, writes } = setupHost(native.video);

    // 时间轴点击（量化命中 F105），媒体管线积压：不呈现。
    act(() => result.current.controls.seekToFrame(105));
    // 连续键盘步进到 F120。
    for (let frame = 106; frame <= 120; frame += 1) {
      act(() => result.current.controls.microStep(1));
    }
    expect(result.current.frameIndex).toBe(120);
    expect(writes[writes.length - 1]).toBe(120);
    const stepMark = writes.length;

    // 300ms seek 超时：pending 清空（CI 高负载下媒体远未跟上）。
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });

    // 浏览器最终只完成了点击那一次 seek：元素落回 t105，seeked 与 rVFC 迟到派发。
    native.video.currentTime = 105 / FPS;
    act(() => {
      fireEvent.seeked(native.video);
    });
    act(() => native.present(105));

    expect(result.current.frameIndex).toBe(120);
    expect(writes.slice(stepMark)).toEqual([]);
  });

  it("pending 存活期间迟到的点击帧 rVFC 与目标不匹配：计为 stale，不回写", () => {
    const native = mediaElement();
    const { result, writes } = setupHost(native.video);

    act(() => result.current.controls.seekToFrame(105));
    for (let frame = 106; frame <= 120; frame += 1) {
      act(() => result.current.controls.microStep(1));
    }

    // pending(120) 仍在：迟到的 F105 呈现必须被 pending 身份校验拒绝。
    act(() => native.present(105));
    expect(result.current.frameIndex).toBe(120);
    expect(writes[writes.length - 1]).toBe(120);

    // 目标帧真正呈现后才结算 ready。
    act(() => native.present(120));
    expect(result.current.frameIndex).toBe(120);
  });

  it("点击帧在第一步之后才呈现（元素落在旧帧）：旧帧证据不得推进帧号或改写步进目标", () => {
    const native = mediaElement();
    const { result, writes } = setupHost(native.video);

    act(() => result.current.controls.seekToFrame(105));
    act(() => result.current.controls.microStep(1)); // F106
    const stepMark = writes.length;
    // 浏览器完成的是点击那次 seek：呈现 F105（新 seek 尚在队列）。
    act(() => native.present(105));
    expect(result.current.frameIndex).toBe(106);

    for (let frame = 107; frame <= 120; frame += 1) {
      act(() => result.current.controls.microStep(1));
    }
    act(() => native.present(120));
    expect(result.current.frameIndex).toBe(120);
    // 点击时的合法写入之后，F105 不得再次出现。
    const clickWrite = writes.indexOf(105);
    expect(writes.slice(clickWrite + 1)).not.toContain(105);
    expect(writes.slice(stepMark).every((frame) => frame >= 106)).toBe(true);
  });
});

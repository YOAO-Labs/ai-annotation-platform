import { act, fireEvent, renderHook } from "@testing-library/react";
import { useLayoutEffect, useRef, useState } from "react";
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

import {
  useVideoPlaybackController,
  type UseVideoPlaybackControllerOptions,
  type UseVideoPlaybackControllerResult,
} from "./useVideoPlaybackController";
import { frameToSeekTime } from "./frameTimebase";
import type { TaskVideoFrameTimetableResponse } from "@/types";
import type { VideoFrameSeekResult } from "./videoStageControls";

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
    callbacks.set(handle, cb as VideoFrameCallback);
    return handle;
  };
  withRvfc.cancelVideoFrameCallback = (handle) => {
    callbacks.delete(handle);
  };
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

function setupHost(
  video: HTMLVideoElement,
  overrides: Partial<UseVideoPlaybackControllerOptions> = {},
  onCommit?: (controller: UseVideoPlaybackControllerResult) => void,
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
    useLayoutEffect(() => onCommit?.(controller));
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

  it("keeps a checked seek pending when the preceding frame commits", async () => {
    const native = mediaElement();
    let pending: Promise<VideoFrameSeekResult> | undefined;
    let outcome: VideoFrameSeekResult | undefined;
    const { result, rerender } = setupHost(native.video, {}, (controller) => {
      if (pending || controller.frameSource !== "video-bitmap") return;
      pending = controller.controls.seekToFrameReady(143);
      void pending.then((value) => {
        outcome = value;
      });
    });
    expect(result.current.frameIndex).toBe(0);

    videoHookMocks.activeBitmap = {
      frameIndex: 0,
      bitmap: { width: 640, height: 360 } as ImageBitmap,
      width: 640,
      height: 360,
    };
    await act(async () => rerender());
    expect(result.current.frameIndex).toBe(143);
    expect(outcome).toBeUndefined();

    act(() => native.present(143));
    const presentation = result.current.framePresentation!;
    expect(presentation).toMatchObject({
      frameIndex: 143,
      source: "video-element",
      image: native.video,
    });
    expect(outcome).toBeUndefined();
    act(() => result.current.markFramePresented(presentation));
    await expect(pending).resolves.toEqual({
      status: "ready",
      frameIndex: 143,
      source: "video-element",
    });
  });

  it("点击帧未呈现期间连续步进：pending 超时后迟到的点击帧 seeked/rVFC 不得回写帧号", async () => {
    const native = mediaElement();
    const { result, writes } = setupHost(native.video);

    act(() => result.current.controls.seekToFrame(105));
    for (let frame = 106; frame <= 120; frame += 1) {
      act(() => result.current.controls.microStep(1));
    }
    expect(result.current.frameIndex).toBe(120);
    expect(writes[writes.length - 1]).toBe(120);
    const stepMark = writes.length;

    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });

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

    act(() => native.present(105));
    expect(result.current.frameIndex).toBe(120);
    expect(writes[writes.length - 1]).toBe(120);

    act(() => native.present(120));
    expect(result.current.frameIndex).toBe(120);
  });

  it("点击帧在第一步之后才呈现（元素落在旧帧）：旧帧证据不得推进帧号或改写步进目标", () => {
    const native = mediaElement();
    const { result, writes } = setupHost(native.video);

    act(() => result.current.controls.seekToFrame(105));
    act(() => result.current.controls.microStep(1));
    const stepMark = writes.length;
    act(() => native.present(105));
    expect(result.current.frameIndex).toBe(106);

    for (let frame = 107; frame <= 120; frame += 1) {
      act(() => result.current.controls.microStep(1));
    }
    act(() => native.present(120));
    expect(result.current.frameIndex).toBe(120);
    const clickWrite = writes.indexOf(105);
    expect(writes.slice(clickWrite + 1)).not.toContain(105);
    expect(writes.slice(stepMark).every((frame) => frame >= 106)).toBe(true);
  });
});

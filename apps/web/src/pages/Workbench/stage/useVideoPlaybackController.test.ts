// 视频播放控制器单测(子 hook 全 mock):聚焦确定性 state ——
// 播放浮层 show/2s 自动隐藏(v0.16.x 回归修复)、loopRegion 规范化/清除、书签开合、派生 maxFrame。
// Source/draw admission uses explicit child-hook receipts; actual pixels remain a browser gate.
import { act, renderHook } from "@testing-library/react";
import { useState } from "react";
import type { TaskVideoManifestResponse, TaskVideoFrameTimetableResponse } from "@/types";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const videoHookMocks = vi.hoisted(() => ({
  clockOptions: vi.fn(),
  bitmapOptions: vi.fn(),
  preciseOptions: vi.fn(),
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
  seekToAsync: vi.fn(),
  onFrameChange: null as ((frame: number) => void) | null,
  getFrameEvidence: vi.fn(),
  nativeFrame: null as import("./useFrameClock").NativeVideoFrameEvidence | null,
  isSeeking: false,
  capture: vi.fn(),
  showFrame: vi.fn(),
  imageBitmapToJpeg: vi.fn().mockResolvedValue(null),
  videoElementToJpeg: vi.fn().mockResolvedValue(null),
}));

vi.mock("./useFrameClock", () => ({
  useFrameClock: (options: { onFrameChange: (frame: number) => void }) => {
    videoHookMocks.clockOptions(options);
    videoHookMocks.onFrameChange = options.onFrameChange;
    return {
      seekToAsync: videoHookMocks.seekToAsync,
      getFrameEvidence: videoHookMocks.getFrameEvidence,
      nativeFrame: videoHookMocks.nativeFrame,
      isSeeking: videoHookMocks.isSeeking,
    };
  },
}));
vi.mock("./useVideoBitmapCache", () => ({
  useVideoBitmapCache: (options: unknown) => (
    videoHookMocks.bitmapOptions(options),
    {
      activeBitmap: videoHookMocks.activeBitmap,
      cachedRanges: [],
      capture: videoHookMocks.capture,
      showFrame: videoHookMocks.showFrame,
    }
  ),
}));
vi.mock("./useVideoPreciseFrame", () => ({
  useVideoPreciseFrame: (options: unknown) => (
    videoHookMocks.preciseOptions(options),
    {
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
    }
  ),
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
  imageBitmapToJpeg: videoHookMocks.imageBitmapToJpeg,
  videoElementToJpeg: videoHookMocks.videoElementToJpeg,
}));

import type { VideoFrameSeekResult } from "./videoStageControls";
import {
  useVideoPlaybackController,
  VIDEO_FRAME_READY_TIMEOUT_MS,
} from "./useVideoPlaybackController";

const MANIFEST = {
  task_id: "T1",
  video_url: "http://x/v.mp4",
  metadata: { fps: 30, frame_count: 100 },
} as TaskVideoManifestResponse;

/** 最小可用的 <video> 替身:满足 hook 各 effect 挂监听 / 读 readyState / 调 load()。 */
function mockVideo(readyState = 0) {
  return {
    readyState,
    isConnected: true,
    videoWidth: 0,
    videoHeight: 0,
    error: null,
    paused: true,
    currentTime: 0,
    duration: 0,
    playbackRate: 1,
    load: vi.fn(),
    play: vi.fn(),
    pause: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  };
}

function setup(
  videoRef: { current: unknown } = { current: null },
  overrides: Partial<Parameters<typeof useVideoPlaybackController>[0]> = {},
) {
  return renderHook(() =>
    useVideoPlaybackController({
      manifest: MANIFEST,
      videoRef: videoRef as never,
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
    }),
  );
}

describe("useVideoPlaybackController", () => {
  beforeEach(() => {
    localStorage.clear();
    videoHookMocks.clockOptions.mockClear();
    videoHookMocks.bitmapOptions.mockClear();
    videoHookMocks.preciseOptions.mockClear();
    videoHookMocks.activeBitmap = null;
    videoHookMocks.preciseBitmap = null;
    videoHookMocks.seekToAsync.mockReset().mockImplementation(async (frameIndex: number) => {
      videoHookMocks.onFrameChange?.(frameIndex);
      return { status: "ready", frameIndex, source: "rvfc" };
    });
    videoHookMocks.getFrameEvidence.mockReset().mockReturnValue(null);
    videoHookMocks.nativeFrame = null;
    videoHookMocks.isSeeking = false;
    videoHookMocks.capture.mockReset().mockResolvedValue(null);
    videoHookMocks.showFrame.mockReset();
    videoHookMocks.imageBitmapToJpeg.mockClear();
    videoHookMocks.videoElementToJpeg.mockClear();
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    localStorage.clear();
  });

  it("preserves a selection made before the first manifest, but clears it on a real task change", () => {
    const videoRef = { current: null };
    const { result, rerender } = renderHook(
      ({ manifest }: { manifest: TaskVideoManifestResponse | undefined }) => {
        const [selection, select] = useState<string | null>(null);
        useVideoPlaybackController({
          manifest,
          videoRef,
          annotations: [],
          selectedId: selection,
          selectedTrack: null,
          hiddenTrackIds: new Set<string>(),
          lockedTrackIds: new Set<string>(),
          readOnly: false,
          drag: null,
          currentFrameEntries: [],
          onUpdate: vi.fn(),
          onSelect: select,
        });
        return { selection, select };
      },
      { initialProps: { manifest: undefined as TaskVideoManifestResponse | undefined } },
    );
    act(() => result.current.select("pred-T1"));
    rerender({ manifest: MANIFEST });
    expect(result.current.selection).toBe("pred-T1");
    rerender({ manifest: { ...MANIFEST, task_id: "T2" } });
    expect(result.current.selection).toBeNull();
  });

  it("派生 maxFrame = frame_count - 1", () => {
    const { result } = setup();
    expect(result.current.maxFrame).toBe(99);
    expect(result.current.fps).toBe(30);
  });

  it("partitions precise/native caches by main generation without resetting the frame or original source", () => {
    const oldGeneration = "10000000-0000-4000-8000-000000000001";
    const nextGeneration = "10000000-0000-4000-8000-000000000002";
    const overrides = {
      manifest: { ...MANIFEST, metadata_generation_id: oldGeneration },
      controlledFrameIndex: 5,
    };
    const { result, rerender } = setup(undefined, overrides);
    const sourceKey = videoHookMocks.clockOptions.mock.lastCall?.[0].sourceKey;
    const oldCacheKey = videoHookMocks.bitmapOptions.mock.lastCall?.[0].sourceKey;
    expect(videoHookMocks.preciseOptions.mock.lastCall?.[0].metadataGenerationId).toBe(
      oldGeneration,
    );
    overrides.manifest = { ...MANIFEST, metadata_generation_id: nextGeneration };
    rerender();
    expect(result.current.frameIndex).toBe(5);
    expect(videoHookMocks.clockOptions.mock.lastCall?.[0].sourceKey).toBe(sourceKey);
    expect(videoHookMocks.bitmapOptions.mock.lastCall?.[0].sourceKey).not.toBe(oldCacheKey);
    expect(videoHookMocks.preciseOptions.mock.lastCall?.[0].metadataGenerationId).toBe(
      nextGeneration,
    );
  });

  it("ignores an old timetable and recaptures when this generation's PTS table arrives", () => {
    const generation = "10000000-0000-4000-8000-000000000002";
    const oldGeneration = "10000000-0000-4000-8000-000000000001";
    const overrides = {
      manifest: { ...MANIFEST, metadata_generation_id: generation },
      controlledFrameIndex: 5,
      frameTimetable: {
        metadata_generation_id: oldGeneration,
        task_id: "T1",
        fps: 30,
        frame_count: 100,
        source: "ffprobe",
        frames: Array.from({ length: 100 }, (_, i) => ({
          frame_index: i,
          pts_ms: i * 37,
          pict_type: i === 0 ? "I" : "P",
          byte_offset: null,
          is_keyframe: i === 0,
        })),
      } as TaskVideoFrameTimetableResponse,
    };
    const { result, rerender } = setup(undefined, overrides);
    expect(videoHookMocks.clockOptions.mock.lastCall?.[0].timebase.ptsMs).toBeNull();
    const oldCacheKey = videoHookMocks.bitmapOptions.mock.lastCall?.[0].sourceKey;
    overrides.frameTimetable = { ...overrides.frameTimetable, metadata_generation_id: generation };
    rerender();
    expect(videoHookMocks.clockOptions.mock.lastCall?.[0].timebase.ptsMs).toEqual(
      overrides.frameTimetable.frames.map((frame) => frame.pts_ms),
    );
    expect(videoHookMocks.bitmapOptions.mock.lastCall?.[0].sourceKey).not.toBe(oldCacheKey);
    expect(result.current.frameIndex).toBe(5);
  });

  it("原件源未就绪时不交付缓存JPEG或QC定位成功", async () => {
    videoHookMocks.preciseBitmap = {
      frameIndex: 0,
      bitmap: { close: vi.fn(), width: 4, height: 4 } as unknown as ImageBitmap,
      width: 4,
      height: 4,
    };
    const ready = vi.fn(() => false);
    const { result } = setup(undefined, { isSourceReady: ready });
    expect(result.current.controls.isSourceReady?.()).toBe(false);
    await expect(result.current.controls.captureCurrentFrameJpeg()).resolves.toBeNull();
    let pending!: Promise<VideoFrameSeekResult>;
    act(() => {
      pending = result.current.controls.seekToFrameReady(5);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(VIDEO_FRAME_READY_TIMEOUT_MS);
    });
    expect(await pending).toMatchObject({ status: "timeout", frameIndex: 5 });
  });

  it("原生错误交由租约HEAD恢复处理，不预先判为编码错误", () => {
    const video = mockVideo(HTMLMediaElement.HAVE_METADATA);
    const recover = vi.fn();
    const { result } = setup({ current: video }, { onSourceRecover: recover });
    const listener = video.addEventListener.mock.calls.find(
      ([event]) => event === "error",
    )?.[1] as () => void;
    act(() => listener());
    expect(recover).toHaveBeenCalledOnce();
    expect(result.current.playbackError).toBeNull();
  });

  it("显式暂停取消续租后的自动播放意图", () => {
    const cancelResume = vi.fn();
    const { result } = setup(undefined, { onPauseIntent: cancelResume });
    act(() => result.current.controls.pausePlayback({ snapToGrid: false }));
    expect(cancelResume).toHaveBeenCalledOnce();
  });

  it("估算时间表在普通定位完成后可续播，不伪造精确QC回执", async () => {
    videoHookMocks.seekToAsync.mockResolvedValue({
      status: "timeout",
      frameIndex: 0,
      source: "timeout",
    });
    const video = {
      ...mockVideo(HTMLMediaElement.HAVE_CURRENT_DATA),
      videoWidth: 320,
      videoHeight: 240,
      seeking: false,
      currentTime: 0.25 / 30,
    };
    const primed = vi.fn();
    setup({ current: video }, { onSourcePrimed: primed });
    await act(async () => {
      await Promise.resolve();
    });
    expect(primed).toHaveBeenCalledOnce();
    expect(videoHookMocks.getFrameEvidence()).toBeNull();
  });

  it("JPEG编码完成时原件租约已失效，不返回晚到字节", async () => {
    let finish!: (blob: Blob) => void;
    videoHookMocks.imageBitmapToJpeg.mockReturnValueOnce(
      new Promise<Blob>((resolve) => {
        finish = resolve;
      }),
    );
    videoHookMocks.preciseBitmap = {
      frameIndex: 0,
      bitmap: { close: vi.fn(), width: 4, height: 4 } as unknown as ImageBitmap,
      width: 4,
      height: 4,
    };
    let ready = true;
    const { result } = setup(undefined, { isSourceReady: () => ready });
    const capture = result.current.controls.captureCurrentFrameJpeg();
    ready = false;
    finish(new Blob(["old frame"]));
    await expect(capture).resolves.toBeNull();
  });

  it.each([
    { seeking: true, currentTime: 0 },
    { seeking: false, currentTime: 17 / 30 },
  ])("原生像素未与逻辑帧对齐时不开始JPEG编码: %j", async (position) => {
    const video = {
      ...mockVideo(HTMLMediaElement.HAVE_CURRENT_DATA),
      videoWidth: 320,
      videoHeight: 240,
      ...position,
    };
    const { result } = setup({ current: video }, { isSourceReady: () => true });
    await expect(result.current.controls.captureCurrentFrameJpeg()).resolves.toBeNull();
    expect(videoHookMocks.videoElementToJpeg).not.toHaveBeenCalled();
  });

  it("原生JPEG编码期间节点跳帧时丢弃字节", async () => {
    let finish!: (blob: Blob) => void;
    videoHookMocks.videoElementToJpeg.mockReturnValueOnce(
      new Promise<Blob>((resolve) => {
        finish = resolve;
      }),
    );
    const video = {
      ...mockVideo(HTMLMediaElement.HAVE_CURRENT_DATA),
      videoWidth: 320,
      videoHeight: 240,
      seeking: false,
    };
    const { result } = setup({ current: video });
    const capture = result.current.controls.captureCurrentFrameJpeg();
    video.currentTime = 17 / 30;
    finish(new Blob(["old frame"]));
    await expect(capture).resolves.toBeNull();
  });

  it("租约失效后的晚到精确像素不能结算QC呈现", async () => {
    videoHookMocks.preciseBitmap = {
      frameIndex: 0,
      bitmap: { close: vi.fn(), width: 4, height: 4 } as unknown as ImageBitmap,
      width: 4,
      height: 4,
    };
    let ready = true;
    const { result } = setup(undefined, { isSourceReady: () => ready });
    let pending!: Promise<import("./videoStageControls").VideoFrameSeekResult>;
    act(() => {
      pending = result.current.controls.seekToFrameReady(0);
    });
    await act(async () => {
      await Promise.resolve();
    });
    const presentation = result.current.framePresentation!;
    expect(presentation).not.toBeNull();
    ready = false;
    expect(presentation.isCurrent()).toBe(false);
    act(() => {
      result.current.markFramePresented(presentation);
    });
    await act(async () => {
      vi.advanceTimersByTime(3000);
    });
    await expect(pending).resolves.toMatchObject({ status: "timeout" });
  });

  it.each(["lease", "timetable"] as const)(
    "旧源的首帧晚到不会恢复新来源的播放: %s",
    async (change) => {
      let resolveFirst!: (result: import("./useFrameClock").FrameSeekResult) => void;
      let resolveSecond!: (result: import("./useFrameClock").FrameSeekResult) => void;
      const first = new Promise<import("./useFrameClock").FrameSeekResult>((resolve) => {
        resolveFirst = resolve;
      });
      const second = new Promise<import("./useFrameClock").FrameSeekResult>((resolve) => {
        resolveSecond = resolve;
      });
      videoHookMocks.seekToAsync.mockReset().mockReturnValueOnce(first).mockReturnValueOnce(second);
      const video = mockVideo(HTMLMediaElement.HAVE_METADATA);
      const videoRef = { current: video };
      const primed = vi.fn();
      const { rerender } = renderHook(
        ({ manifest, frameTimetable }) =>
          useVideoPlaybackController({
            manifest,
            frameTimetable,
            videoRef: videoRef as never,
            onSourcePrimed: primed,
            annotations: [],
            selectedId: null,
            selectedTrack: null,
            hiddenTrackIds: new Set<string>(),
            lockedTrackIds: new Set<string>(),
            readOnly: false,
            drag: null,
            currentFrameEntries: [],
            onUpdate: vi.fn(),
          }),
        {
          initialProps: {
            manifest: MANIFEST,
            frameTimetable: undefined as TaskVideoFrameTimetableResponse | undefined,
          },
        },
      );
      rerender({
        manifest:
          change === "lease"
            ? { ...MANIFEST, video_url: "/api/v1/media/original-playback/new" }
            : MANIFEST,
        frameTimetable:
          change === "timetable"
            ? {
                task_id: "T1",
                fps: 30,
                frame_count: 100,
                source: "ffprobe",
                frames: [
                  {
                    frame_index: 0,
                    pts_ms: 0,
                    is_keyframe: true,
                    pict_type: null,
                    byte_offset: null,
                  },
                ],
              }
            : undefined,
      });
      await act(async () => resolveFirst({ status: "ready", frameIndex: 0, source: "rvfc" }));
      expect(primed).not.toHaveBeenCalled();
      await act(async () => resolveSecond({ status: "ready", frameIndex: 0, source: "rvfc" }));
      expect(primed).toHaveBeenCalledOnce();
    },
  );

  it("暂停态仅显示当前帧 bitmap，不复用上一帧缓存", () => {
    videoHookMocks.activeBitmap = {
      frameIndex: 1,
      bitmap: { close: vi.fn() } as unknown as ImageBitmap,
      width: 4,
      height: 4,
    };
    const { result } = setup();

    expect(result.current.frameIndex).toBe(0);
    expect(result.current.displayBitmap).toBeNull();
    expect(result.current.frameSource).toBe("video-element");
  });

  it("暂停态 precise bitmap 优先于同帧 native bitmap，并作为 AI 取帧源", async () => {
    const nativeBitmap = { close: vi.fn() } as unknown as ImageBitmap;
    const preciseBitmap = { close: vi.fn() } as unknown as ImageBitmap;
    videoHookMocks.activeBitmap = {
      frameIndex: 0,
      bitmap: nativeBitmap,
      width: 4,
      height: 4,
    };
    videoHookMocks.preciseBitmap = {
      frameIndex: 0,
      bitmap: preciseBitmap,
      width: 4,
      height: 4,
    };
    const { result } = setup();

    expect(result.current.displayBitmap?.bitmap).toBe(preciseBitmap);
    expect(result.current.frameSource).toBe("webcodecs");
    await act(async () => {
      await result.current.controls.captureCurrentFrameJpeg();
    });
    expect(videoHookMocks.imageBitmapToJpeg).toHaveBeenCalledWith(preciseBitmap, undefined);
    expect(videoHookMocks.videoElementToJpeg).not.toHaveBeenCalled();
  });

  it("播放态强制使用 video 元素，AI 取帧不读取 bitmap", async () => {
    videoHookMocks.activeBitmap = {
      frameIndex: 0,
      bitmap: { close: vi.fn() } as unknown as ImageBitmap,
      width: 4,
      height: 4,
    };
    const video = {
      ...mockVideo(HTMLMediaElement.HAVE_CURRENT_DATA),
      videoWidth: 320,
      videoHeight: 240,
      seeking: false,
    };
    const { result } = setup({ current: video });
    expect(result.current.frameSource).toBe("video-bitmap");

    act(() => result.current.controls.togglePlayback());
    expect(result.current.isPlaybackActive).toBe(true);
    expect(result.current.displayBitmap).toBeNull();
    expect(result.current.frameSource).toBe("video-element");

    await act(async () => {
      await result.current.controls.captureCurrentFrameJpeg();
    });
    expect(videoHookMocks.videoElementToJpeg).toHaveBeenCalledWith(video, undefined);
    expect(videoHookMocks.imageBitmapToJpeg).not.toHaveBeenCalled();
  });

  it("settings pause preserves an off-grid frame while ordinary pause still snaps", async () => {
    const video = mockVideo(HTMLMediaElement.HAVE_METADATA);
    const { result } = setup(
      { current: video },
      {
        controlledFrameIndex: 7,
        videoSampling: { mode: "step", frame_step: 5 },
      },
    );
    act(() => result.current.controls.togglePlayback());
    expect(result.current.isPlaybackActive).toBe(true);
    videoHookMocks.seekToAsync.mockClear();

    act(() => result.current.controls.pausePlayback({ snapToGrid: false }));
    expect(result.current.isPlaybackActive).toBe(false);
    expect(result.current.frameIndex).toBe(7);
    expect(videoHookMocks.seekToAsync).not.toHaveBeenCalled();
    expect(video.pause).toHaveBeenCalled();

    await act(async () => result.current.controls.pausePlayback());
    expect(videoHookMocks.seekToAsync).toHaveBeenCalledWith(5);
  });

  it("播放浮层:默认显示 → schedule 后 2s 自动隐藏 → show 再次唤出并取消隐藏", () => {
    const { result } = setup();
    expect(result.current.playbackOverlayVisible).toBe(true);

    act(() => result.current.schedulePlaybackOverlayHide());
    expect(result.current.playbackOverlayVisible).toBe(true); // 尚未到点
    act(() => vi.advanceTimersByTime(2000));
    expect(result.current.playbackOverlayVisible).toBe(false);

    act(() => result.current.showPlaybackOverlay());
    expect(result.current.playbackOverlayVisible).toBe(true);
    // show 已清掉隐藏计时器:再推进时间不应又隐藏。
    act(() => vi.advanceTimersByTime(5000));
    expect(result.current.playbackOverlayVisible).toBe(true);
  });

  it("loopRegion:设区间被规范化(裁剪到 [0,maxFrame] 且首尾有序);clear 置空", () => {
    const { result } = setup();
    expect(result.current.loopRegion).toBeNull();
    act(() => result.current.setNormalizedLoopRegion({ startFrame: 80, endFrame: 999 }));
    expect(result.current.loopRegion).toEqual({ startFrame: 80, endFrame: 99 });
    act(() => result.current.clearLoopRegion());
    expect(result.current.loopRegion).toBeNull();
  });

  it("书签:toggle 在当前帧加书签,再 toggle 移除", () => {
    const { result } = setup();
    expect(result.current.bookmarks).toHaveLength(0);
    act(() => result.current.toggleBookmark());
    expect(result.current.bookmarks).toHaveLength(1);
    act(() => result.current.toggleBookmark());
    expect(result.current.bookmarks).toHaveLength(0);
  });

  it("加载卡死看门狗:video 卡在 readyState 0 → 每 5s video.load() 重踢, 至多 3 次", () => {
    const video = mockVideo(0);
    setup({ current: video });
    expect(video.load).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(5000));
    expect(video.load).toHaveBeenCalledTimes(1);
    act(() => vi.advanceTimersByTime(5000));
    expect(video.load).toHaveBeenCalledTimes(2);
    act(() => vi.advanceTimersByTime(5000));
    expect(video.load).toHaveBeenCalledTimes(3);
    // 达到 MAX_RETRIES 后不再重踢(避免无限重试)。
    act(() => vi.advanceTimersByTime(15000));
    expect(video.load).toHaveBeenCalledTimes(3);
  });

  it("加载卡死看门狗:期间拿到元数据(readyState≥HAVE_METADATA)则不再重踢", () => {
    const video = mockVideo(0);
    setup({ current: video });
    video.readyState = HTMLMediaElement.HAVE_METADATA; // 加载恢复
    act(() => vi.advanceTimersByTime(15000));
    expect(video.load).not.toHaveBeenCalled();
  });

  function bitmapFrame(frameIndex: number) {
    return {
      frameIndex,
      bitmap: { width: 640, height: 360, close: vi.fn() } as unknown as ImageBitmap,
      width: 640,
      height: 360,
    };
  }

  it("requires a new completed media draw for each exact cached-frame request", async () => {
    videoHookMocks.preciseBitmap = bitmapFrame(17);
    const { result } = setup();
    let first!: Promise<VideoFrameSeekResult>;
    let finished = false;
    act(() => {
      first = result.current.controls.seekToFrameReady(17);
      void first.then(() => {
        finished = true;
      });
    });
    await act(async () => {
      await Promise.resolve();
    });
    expect(finished).toBe(false);
    const firstPresentation = result.current.framePresentation!;
    expect(firstPresentation).toMatchObject({
      frameIndex: 17,
      source: "webcodecs",
      image: videoHookMocks.preciseBitmap.bitmap,
    });
    await act(async () => {
      result.current.markFramePresented(firstPresentation);
      await first;
    });
    expect(await first).toEqual({ status: "ready", frameIndex: 17, source: "webcodecs" });
    let second!: Promise<VideoFrameSeekResult>;
    act(() => {
      second = result.current.controls.seekToFrameReady(17);
    });
    const secondPresentation = result.current.framePresentation!;
    expect(secondPresentation.requestId).not.toBe(firstPresentation.requestId);
    act(() => result.current.markFramePresented(firstPresentation));
    expect(result.current.framePresentation).toBe(secondPresentation);
    await act(async () => {
      result.current.markFramePresented(secondPresentation);
      await second;
    });
    expect(await second).toEqual({ status: "ready", frameIndex: 17, source: "webcodecs" });
  });

  it("keeps an unprimed current source pending until an authorized media draw arrives", async () => {
    videoHookMocks.preciseBitmap = bitmapFrame(17);
    let ready = false;
    const { result, rerender } = setup(undefined, { isSourceReady: () => ready });
    let pending!: Promise<VideoFrameSeekResult>;
    let settled = false;
    act(() => {
      pending = result.current.controls.seekToFrameReady(17).then((value) => {
        settled = true;
        return value;
      });
    });
    await act(async () => vi.advanceTimersByTimeAsync(100));
    if (result.current.framePresentation) {
      act(() => result.current.markFramePresented(result.current.framePresentation!));
    }
    expect(settled).toBe(false);
    ready = true;
    rerender();
    await act(async () => {
      result.current.markFramePresented(result.current.framePresentation!);
      await pending;
    });
    expect(await pending).toEqual({ status: "ready", frameIndex: 17, source: "webcodecs" });
  });

  it("accepts a verified native bitmap after its own media draw", async () => {
    videoHookMocks.activeBitmap = bitmapFrame(17);
    const { result } = setup();
    let pending!: Promise<VideoFrameSeekResult>;
    act(() => {
      pending = result.current.controls.seekToFrameReady(17);
    });
    expect(result.current.framePresentation?.source).toBe("video-bitmap");
    await act(async () => {
      result.current.markFramePresented(result.current.framePresentation!);
      await pending;
    });
    expect(await pending).toEqual({ status: "ready", frameIndex: 17, source: "video-bitmap" });
  });

  it("uses exact observed native source pixels for video-element fallback, then waits for draw", async () => {
    const video = mockVideo(HTMLMediaElement.HAVE_CURRENT_DATA);
    video.videoWidth = 640;
    video.videoHeight = 360;
    const evidence = {
      frameIndex: 17,
      mediaTime: 17 / 30,
      video: video as unknown as HTMLVideoElement,
      ownerEpoch: 0,
    };
    videoHookMocks.getFrameEvidence.mockImplementation((frame) => (frame === 17 ? evidence : null));
    videoHookMocks.nativeFrame = evidence;
    const { result } = setup({ current: video });
    let pending!: Promise<VideoFrameSeekResult>;
    act(() => {
      pending = result.current.controls.seekToFrameReady(17);
    });
    expect(result.current.framePresentation).toMatchObject({
      frameIndex: 17,
      source: "video-element",
      image: video,
    });
    await act(async () => {
      result.current.markFramePresented(result.current.framePresentation!);
      await pending;
    });
    expect(await pending).toEqual({ status: "ready", frameIndex: 17, source: "video-element" });
  });

  it("rechecks an existing native receipt when seeking ends without a captured bitmap", async () => {
    const video = { ...mockVideo(HTMLMediaElement.HAVE_CURRENT_DATA), seeking: true };
    video.videoWidth = 640;
    video.videoHeight = 360;
    const evidence = {
      frameIndex: 17,
      mediaTime: 17 / 30,
      video: video as unknown as HTMLVideoElement,
      ownerEpoch: 0,
    };
    videoHookMocks.nativeFrame = evidence;
    videoHookMocks.isSeeking = true;
    videoHookMocks.getFrameEvidence.mockImplementation((frame) =>
      frame === 17 && !video.seeking ? evidence : null,
    );
    const { result, rerender } = setup({ current: video });
    let pending!: Promise<VideoFrameSeekResult>;
    act(() => {
      pending = result.current.controls.seekToFrameReady(17);
    });
    expect(result.current.framePresentation).toBeNull();

    video.seeking = false;
    videoHookMocks.isSeeking = false;
    rerender();
    expect(result.current.displayBitmap).toBeNull();
    expect(result.current.framePresentation).toMatchObject({
      frameIndex: 17,
      source: "video-element",
      image: video,
    });
    await act(async () => {
      result.current.markFramePresented(result.current.framePresentation!);
      await pending;
    });
    expect(await pending).toEqual({ status: "ready", frameIndex: 17, source: "video-element" });
  });

  it("keeps a cold precise decode eligible after native timeout and accepts its later draw", async () => {
    videoHookMocks.seekToAsync.mockImplementation(async (frameIndex) => {
      videoHookMocks.onFrameChange?.(frameIndex);
      return { status: "timeout", frameIndex, source: "timeout" };
    });
    const { result, rerender } = setup();
    let pending!: Promise<VideoFrameSeekResult>;
    act(() => {
      pending = result.current.controls.seekToFrameReady(17);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(result.current.framePresentation).toBeNull();
    expect(videoHookMocks.capture).not.toHaveBeenCalled();
    videoHookMocks.preciseBitmap = bitmapFrame(17);
    rerender();
    await act(async () => {
      result.current.markFramePresented(result.current.framePresentation!);
      await pending;
    });
    expect(await pending).toEqual({ status: "ready", frameIndex: 17, source: "webcodecs" });
  });

  it("a native seek result without source evidence or a completed draw times out", async () => {
    const { result } = setup();
    let pending!: Promise<VideoFrameSeekResult>;
    act(() => {
      pending = result.current.controls.seekToFrameReady(17);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(VIDEO_FRAME_READY_TIMEOUT_MS);
    });
    expect(await pending).toEqual({ status: "timeout", frameIndex: 17, source: null });
    expect(videoHookMocks.capture).not.toHaveBeenCalled();
    expect(result.current.framePresentation).toBeNull();
  });

  it("rejects disabled admission and invalid anchors without clamping them into success", async () => {
    const overrides = {
      drag: { kind: "draw" as const, start: { x: 0, y: 0 }, current: { x: 1, y: 1 } },
    };
    const { result } = setup(undefined, overrides);
    await act(async () => {
      expect(await result.current.controls.seekToFrameReady(17)).toEqual({
        status: "unavailable",
        frameIndex: 17,
        source: null,
      });
    });
    expect(videoHookMocks.seekToAsync).not.toHaveBeenCalled();
    const idle = setup();
    await act(async () => {
      expect(await idle.result.current.controls.seekToFrameReady(120)).toEqual({
        status: "unavailable",
        frameIndex: 120,
        source: null,
      });
      expect(await idle.result.current.controls.seekToFrameReady(1.5)).toEqual({
        status: "unavailable",
        frameIndex: 1.5,
        source: null,
      });
    });
    expect(videoHookMocks.seekToAsync).not.toHaveBeenCalled();
  });

  it("new navigation cancels the old request and rejects its delayed draw receipt", async () => {
    videoHookMocks.preciseBitmap = bitmapFrame(17);
    const { result } = setup();
    let first!: Promise<VideoFrameSeekResult>;
    act(() => {
      first = result.current.controls.seekToFrameReady(17);
    });
    const oldPresentation = result.current.framePresentation!;
    act(() => result.current.controls.seekToFrame(18));
    expect(await first).toEqual({ status: "cancelled", frameIndex: 17, source: null });
    expect(oldPresentation.isCurrent()).toBe(false);
    act(() => result.current.markFramePresented(oldPresentation));
    expect(result.current.framePresentation).toBeNull();
  });

  it("cancels task A→B→A and unmount work while preserving last-request-wins", async () => {
    videoHookMocks.preciseBitmap = bitmapFrame(17);
    const overrides: Partial<Parameters<typeof useVideoPlaybackController>[0]> = {
      manifest: MANIFEST,
    };
    const { result, rerender, unmount } = setup(undefined, overrides);
    let first!: Promise<VideoFrameSeekResult>;
    act(() => {
      first = result.current.controls.seekToFrameReady(17);
    });
    const oldPresentation = result.current.framePresentation!;
    overrides.manifest = {
      task_id: "T2",
      video_url: "http://x/v.mp4",
      metadata: { fps: 30, frame_count: 100 },
    } as never;
    rerender();
    overrides.manifest = MANIFEST;
    rerender();
    expect(await first).toEqual({ status: "cancelled", frameIndex: 17, source: null });
    expect(oldPresentation.isCurrent()).toBe(false);
    let second!: Promise<VideoFrameSeekResult>;
    let third!: Promise<VideoFrameSeekResult>;
    act(() => {
      second = result.current.controls.seekToFrameReady(17);
    });
    act(() => {
      third = result.current.controls.seekToFrameReady(18);
    });
    expect(await second).toEqual({ status: "cancelled", frameIndex: 17, source: null });
    unmount();
    expect(await third).toEqual({ status: "cancelled", frameIndex: 18, source: null });
  });

  it("checked pause keeps off-grid source frame 17 and does not snap to the sampling grid", async () => {
    videoHookMocks.preciseBitmap = bitmapFrame(17);
    const video = mockVideo(HTMLMediaElement.HAVE_CURRENT_DATA);
    const { result } = setup(
      { current: video },
      { videoSampling: { mode: "step", frame_step: 5 } },
    );
    act(() => result.current.controls.togglePlayback());
    videoHookMocks.seekToAsync.mockClear();
    let pending!: Promise<VideoFrameSeekResult>;
    act(() => {
      pending = result.current.controls.seekToFrameReady(17);
    });
    expect(result.current.isPlaybackActive).toBe(false);
    expect(video.pause).toHaveBeenCalled();
    expect(videoHookMocks.seekToAsync).toHaveBeenCalledWith(17);
    expect(videoHookMocks.seekToAsync).not.toHaveBeenCalledWith(15);
    await act(async () => {
      result.current.markFramePresented(result.current.framePresentation!);
      await pending;
    });
    expect((await pending).frameIndex).toBe(17);
  });

  it("an old task control cannot start or cancel a newer task's checked seek", async () => {
    videoHookMocks.preciseBitmap = bitmapFrame(17);
    const overrides: Partial<Parameters<typeof useVideoPlaybackController>[0]> = {
      manifest: MANIFEST,
    };
    const { result, rerender } = setup(undefined, overrides);
    const staleSeek = result.current.controls.seekToFrameReady;
    overrides.manifest = {
      task_id: "T2",
      video_url: "http://x/other.mp4",
      metadata: { fps: 30, frame_count: 100 },
    } as never;
    rerender();
    let current!: Promise<VideoFrameSeekResult>;
    act(() => {
      current = result.current.controls.seekToFrameReady(17);
    });
    const presentation = result.current.framePresentation!;
    expect(await staleSeek(17)).toEqual({ status: "cancelled", frameIndex: 17, source: null });
    expect(presentation.isCurrent()).toBe(true);
    await act(async () => {
      result.current.markFramePresented(presentation);
      await current;
    });
    expect((await current).status).toBe("ready");
  });

  it("paused frame-clock writes only land on the latest navigation target or its echo (Issue #114)", () => {
    const video = mockVideo(HTMLMediaElement.HAVE_METADATA);
    const { result } = setup({ current: video });

    // 时间轴点击 seek：导航目标 = 17，乐观写落地。
    act(() => result.current.controls.seekToFrame(17));
    expect(result.current.frameIndex).toBe(17);

    // 高负载交错下迟到的旧帧回报（时间轴点击量化帧、被步进超越）：必须被栅栏丢弃。
    act(() => videoHookMocks.onFrameChange?.(9));
    expect(result.current.frameIndex).toBe(17);
    // 非导航来源的任意新帧同样不允许改写暂停态选中帧。
    act(() => videoHookMocks.onFrameChange?.(18));
    expect(result.current.frameIndex).toBe(17);
    // 导航目标回声照常接受（幂等）。
    act(() => videoHookMocks.onFrameChange?.(17));
    expect(result.current.frameIndex).toBe(17);

    // 新的显式导航（键盘步进）正常推进。
    act(() => result.current.controls.seekToFrame(18));
    expect(result.current.frameIndex).toBe(18);

    // 播放态逐帧推进不受栅栏限制。
    act(() => result.current.controls.togglePlayback());
    expect(result.current.isPlaybackActive).toBe(true);
    act(() => videoHookMocks.onFrameChange?.(25));
    expect(result.current.frameIndex).toBe(25);
  });

  it("播放推进后暂停，旧导航目标的迟到回报不得拽回旧帧（PR #118 评审跟进）", () => {
    const video = mockVideo(HTMLMediaElement.HAVE_METADATA);
    const { result } = setup({ current: video });

    // 纯净评审序列:seek 17(此后无任何新导航)→ 播放推进到 25 → 暂停。
    act(() => result.current.controls.seekToFrame(17));
    expect(result.current.frameIndex).toBe(17);

    act(() => result.current.controls.togglePlayback());
    expect(result.current.isPlaybackActive).toBe(true);
    act(() => videoHookMocks.onFrameChange?.(25));
    expect(result.current.frameIndex).toBe(25);

    act(() => result.current.controls.togglePlayback());
    expect(result.current.isPlaybackActive).toBe(false);

    // 暂停后,导航目标 17 的迟到回报不得把帧号拽回旧帧。
    act(() => videoHookMocks.onFrameChange?.(17));
    expect(result.current.frameIndex).toBe(25);
  });

  it("host 外部改写受控帧后，旧导航目标的迟到回报不得拽回旧帧（PR #118 评审跟进）", () => {
    const video = mockVideo(HTMLMediaElement.HAVE_METADATA);
    // ref 对象必须稳定:内联字面量会让「首帧预热」效应每次渲染重跑并顺手刷新导航目标,
    // 掩盖真实的栅栏缺口(真实宿主的 ref 是稳定对象)。
    const videoRef = { current: video };
    let controlled = 0;
    const onFrameIndexChange = vi.fn((frame: number) => {
      controlled = frame;
    });
    const { result, rerender } = renderHook(() =>
      useVideoPlaybackController({
        manifest: MANIFEST,
        videoRef: videoRef as never,
        annotations: [],
        selectedId: null,
        selectedTrack: null,
        hiddenTrackIds: new Set<string>(),
        lockedTrackIds: new Set<string>(),
        readOnly: false,
        drag: null,
        currentFrameEntries: [],
        onUpdate: vi.fn(),
        controlledFrameIndex: controlled,
        onFrameIndexChange,
      }),
    );

    // seek 17:登记导航目标并乐观写 17,受控 prop 跟进。
    act(() => result.current.controls.seekToFrame(17));
    expect(result.current.frameIndex).toBe(17);
    expect(controlled).toBe(17);

    // 宿主外部跳转(章节/段落/任务恢复)直接改受控帧,不经 seekFrameAsync。
    controlled = 40;
    rerender();
    expect(result.current.frameIndex).toBe(40);

    // 旧导航目标 17 的迟到回报:必须被栅栏丢弃,受控帧不得回写。
    const writesBefore = onFrameIndexChange.mock.calls.length;
    act(() => videoHookMocks.onFrameChange?.(17));
    expect(result.current.frameIndex).toBe(40);
    expect(onFrameIndexChange.mock.calls.length).toBe(writesBefore);
  });
});

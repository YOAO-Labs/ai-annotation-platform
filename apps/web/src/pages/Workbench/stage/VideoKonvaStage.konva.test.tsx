/**
 * v0.16.1 · 视频 Konva 栈容器(VideoKonvaStage)konva-mock 测试。
 *
 * 验证容器层装配:渲染隐藏 `<video>` 解码源 + media-bg Layer + Konva.Image(world 尺寸按
 * manifest 固有宽高),并经转发的 VideoStageControls 暴露播放控制(供工作台热键驱动)。
 * 真实 canvas 渲染 / 播放重绘交给 Playwright,本测试只验装配与 props(决策 C)。
 */
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { createRef } from "react";
import { act, fireEvent, render, renderHook, screen, within } from "@testing-library/react";
import { DEFAULT_ANNOTATION_VISUAL } from "./annotationVisual";
import { VideoKonvaStage } from "./VideoKonvaStage";
import type {
  VideoDrawingDraft,
  VideoIssueViewRestoreResult,
  VideoStageControls,
} from "./videoStageControls";
import type { AnnotationResponse, TaskVideoManifestResponse, VideoTrackGeometry } from "@/types";
import type { UseMaskEditorReturn } from "../state/useMaskEditor";
import { useWorkbenchHotkeys, type UseWorkbenchHotkeysArgs } from "../state/useWorkbenchHotkeys";

vi.mock("./useVideoPreciseFrame", () => ({
  useVideoPreciseFrame: () => ({
    active: false,
    bitmap: null,
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

const playMock = vi.fn();
const pauseMock = vi.fn();
const viewportObservers = new Map<Element, ResizeObserverCallback>();

function resizeViewport(stage: HTMLElement) {
  const rect = new DOMRect(0, 0, 800, 600);
  vi.spyOn(stage, "getBoundingClientRect").mockReturnValue(rect);
  const callback = viewportObservers.get(stage);
  expect(callback).toBeDefined();
  act(() =>
    callback!(
      [
        {
          target: stage,
          contentRect: rect,
          borderBoxSize: [],
          contentBoxSize: [],
          devicePixelContentBoxSize: [],
        },
      ],
      {} as ResizeObserver,
    ),
  );
}

function makeMaskEditor(overrides: Partial<UseMaskEditorReturn> = {}) {
  return {
    active: true,
    dirty: true,
    phase: "dirty",
    buffer: null,
    tool: "brush",
    mode: "brush",
    radius: 8,
    backend: "dense",
    operationPreview: null,
    instanceOperationPreview: null,
    canUndo: true,
    canRedo: true,
    setMode: vi.fn(),
    undo: vi.fn(),
    redo: vi.fn(),
    confirmOperation: vi.fn(),
    cancelOperation: vi.fn(),
    ...overrides,
  } as unknown as UseMaskEditorReturn;
}

const manifest: TaskVideoManifestResponse = {
  task_id: "task-1",
  video_url: "http://storage.local/video.mp4",
  poster_url: "http://storage.local/poster.webp",
  expires_in: 3600,
  metadata: {
    duration_ms: 1000,
    fps: 10,
    frame_count: 10,
    width: 1000,
    height: 500,
    codec: "h264",
    playback_path: null,
    playback_codec: null,
    playback_error: null,
    poster_frame_path: "poster.webp",
    probe_error: null,
    poster_error: null,
    frame_timetable_frame_count: null,
    frame_timetable_error: null,
  },
};

describe("VideoKonvaStage · konva mock", () => {
  beforeEach(() => {
    // The viewport hook uses observer notifications, not window resize, when available.
    vi.stubGlobal(
      "ResizeObserver",
      class {
        private target: Element | null = null;
        constructor(private callback: ResizeObserverCallback) {}
        observe(target: Element) {
          if (target.getAttribute("data-testid") === "video-konva-stage") {
            this.target = target;
            viewportObservers.set(target, this.callback);
          }
        }
        unobserve(target: Element) {
          if (target === this.target) this.disconnect();
        }
        disconnect() {
          if (this.target) viewportObservers.delete(this.target);
          this.target = null;
        }
      },
    );
  });
  afterEach(() => {
    viewportObservers.clear();
    vi.unstubAllGlobals();
  });

  beforeAll(() => {
    Object.defineProperty(HTMLMediaElement.prototype, "pause", {
      configurable: true,
      value: pauseMock,
    });
    Object.defineProperty(HTMLMediaElement.prototype, "play", {
      configurable: true,
      value: playMock,
    });
    Object.defineProperty(HTMLMediaElement.prototype, "readyState", {
      configurable: true,
      get: () => HTMLMediaElement.HAVE_CURRENT_DATA,
    });
  });
  afterAll(() => {
    playMock.mockReset();
    pauseMock.mockReset();
  });

  it("渲染隐藏 video 源 + media-bg Layer + Konva.Image(world 尺寸 = manifest 固有宽高)", () => {
    render(<VideoKonvaStage manifest={manifest} />);

    expect(document.querySelector('[data-testid="video-konva-stage"]')).not.toBeNull();
    const source = document.querySelector(
      '[data-testid="video-konva-source"]',
    ) as HTMLVideoElement | null;
    expect(source).not.toBeNull();
    expect(source!.getAttribute("src")).toBe(manifest.video_url);

    const layer = document.querySelector('[data-konva="Layer"]');
    expect(layer?.getAttribute("data-testid")).toBe("media-bg");
    const image = document.querySelector('[data-konva="Image"]');
    expect(image).not.toBeNull();
    expect(image!.getAttribute("data-width")).toBe("1000");
    expect(image!.getAttribute("data-height")).toBe("500");
    // data-video-frame-source 暴露当前显示来源(webcodecs / native bitmap / video element),供 E2E 与诊断。
    const stage = document.querySelector('[data-testid="video-konva-stage"]') as HTMLElement;
    expect(["webcodecs", "native-bitmap", "video"]).toContain(
      stage.getAttribute("data-video-frame-source"),
    );
  });

  it("renders a current-frame annotation comment badge and removes it when counts are gated off", () => {
    const annotation: AnnotationResponse = {
      id: "video-ann-1",
      task_id: "task-1",
      project_id: "project-1",
      user_id: "user-1",
      source: "manual",
      annotation_type: "video_bbox",
      class_name: "car",
      geometry: {
        type: "video_bbox",
        frame_index: 0,
        x: 0.1,
        y: 0.2,
        w: 0.3,
        h: 0.2,
      },
      confidence: null,
      parent_prediction_id: null,
      parent_annotation_id: null,
      lead_time: null,
      is_active: true,
      ground_truth: false,
      created_at: "2026-01-01T00:00:00Z",
      updated_at: null,
    };
    const view = render(
      <VideoKonvaStage
        manifest={manifest}
        annotations={[annotation]}
        annotationCommentCounts={{ [annotation.id]: 1 }}
      />,
    );
    expect(screen.getByRole("button", { name: `标注 ${annotation.id} 有 1 条评论` })).toBeVisible();
    view.rerender(
      <VideoKonvaStage
        manifest={manifest}
        annotations={[annotation]}
        annotationCommentCounts={undefined}
      />,
    );
    expect(screen.queryByTestId("annotation-comment-badges")).toBeNull();
    view.rerender(
      <VideoKonvaStage
        manifest={manifest}
        annotations={[annotation]}
        annotationCommentCounts={{ [annotation.id]: 1 }}
      />,
    );
    expect(screen.getByRole("button", { name: `标注 ${annotation.id} 有 1 条评论` })).toBeVisible();
  });

  it("loading 态显示占位,不渲染 Stage", () => {
    render(<VideoKonvaStage manifest={undefined} isLoading />);
    expect(document.querySelector('[data-testid="video-konva-stage"]')).toBeNull();
    expect(document.querySelector('[data-konva="Stage"]')).toBeNull();
  });

  it("restores through the real Stage/Overlay owners after selection without a later focus overwrite", async () => {
    const ref = createRef<VideoStageControls>();
    const longManifest = {
      ...manifest,
      metadata: { ...manifest.metadata, frame_count: 180 },
    };
    const annotation = contextAnnotation("video_track_bbox");
    const props = {
      manifest: longManifest,
      annotations: [annotation],
      focusSelectionEnabled: true,
    };
    const view = render(<VideoKonvaStage {...props} ref={ref} />);
    const stage = view.getByTestId("video-konva-stage");
    resizeViewport(stage);
    expect(stage).toHaveAttribute("data-video-view-ready", "true");
    expect(await ref.current!.waitForIssueViewReady!(new AbortController().signal)).toBe(true);
    let restored!: Promise<VideoIssueViewRestoreResult>;
    act(() => {
      restored = ref.current!.beginIssueRestore!(() => true)!.restore(
        {
          viewport: { center_x: 0.3, center_y: 0.7, zoom: 1.5 },
          timeline_window: { from: 64.25, to: 144.75 },
        },
        annotation.id,
      );
    });
    view.rerender(<VideoKonvaStage {...props} ref={ref} selectedId={annotation.id} />);
    expect(await restored).toEqual({ status: "restored", clamped: false });
    expect(ref.current!.captureIssueView!()).toMatchObject({
      taskId: manifest.task_id,
      frameIndex: 0,
      timeline_window: { from: 64.25, to: 144.75 },
    });
    expect(Number(stage.dataset.videoViewCenterX)).toBeCloseTo(0.3);
    expect(Number(stage.dataset.videoViewCenterY)).toBeCloseTo(0.7);
    expect(Number(stage.dataset.videoViewZoom)).toBeCloseTo(1.5);
    view.rerender(<VideoKonvaStage {...props} ref={ref} selectedId={annotation.id} />);
    expect(Number(stage.dataset.videoViewZoom)).toBeCloseTo(1.5);
  });

  it("does not certify fallback dimensions and admits native metadata only for the current source", () => {
    const ref = createRef<VideoStageControls>();
    const noSize = { ...manifest, metadata: { ...manifest.metadata, width: null, height: null } };
    const view = render(<VideoKonvaStage manifest={noSize} ref={ref} />);
    const stage = view.getByTestId("video-konva-stage");
    resizeViewport(stage);
    expect(ref.current!.captureIssueView!()).toBeNull();
    expect(stage).toHaveAttribute("data-video-view-ready", "false");
    const video = view.getByTestId("video-konva-source");
    Object.defineProperties(video, {
      currentSrc: { configurable: true, value: noSize.video_url },
      videoWidth: { configurable: true, value: 640 },
      videoHeight: { configurable: true, value: 360 },
    });
    fireEvent.loadedMetadata(video);
    expect(ref.current!.captureIssueView!()?.viewport.zoom).toBe(1);
    expect(stage).toHaveAttribute("data-video-view-ready", "true");
    const old = ref.current!;
    view.rerender(
      <VideoKonvaStage
        manifest={{ ...noSize, task_id: "task-B", video_url: "http://storage.local/B.mp4" }}
        ref={ref}
      />,
    );
    expect(ref.current!.captureIssueView!()).toBeNull();
    expect(old.captureIssueView!()).toBeNull();
  });

  it("notifies navigation interrupts for user commands while internal seek and pause stay quiet", async () => {
    const ref = createRef<VideoStageControls>();
    const view = render(<VideoKonvaStage manifest={manifest} ref={ref} />);
    const stage = view.getByTestId("video-konva-stage");
    resizeViewport(stage);
    const listener = vi.fn();
    const unsubscribe = ref.current!.subscribeIssueNavigationInterrupt!(listener);
    let seeking!: ReturnType<VideoStageControls["seekToFrameReady"]>;
    act(() => {
      ref.current!.pausePlayback({ snapToGrid: false });
      seeking = ref.current!.seekToFrameReady(0);
    });
    expect(listener).not.toHaveBeenCalled();
    act(() => ref.current!.seekToFrame(1));
    expect(listener).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(window, { key: "F", shiftKey: true });
    expect(listener).toHaveBeenCalledTimes(2);
    fireEvent.wheel(stage, { clientX: 300, clientY: 300, ctrlKey: true, deltaY: -1 });
    expect(listener).toHaveBeenCalledTimes(3);
    unsubscribe();
    act(() => ref.current!.seekToFrame(2));
    expect(listener).toHaveBeenCalledTimes(3);
    view.unmount();
    expect((await seeking).status).toBe("cancelled");
  });

  it("allows the first Issue drop above annotation handles in a review canvas", () => {
    const view = render(
      <VideoKonvaStage manifest={manifest} readOnly issuePinDropArmed onIssuePinDrop={vi.fn()} />,
    );
    const catcher = screen.getByTestId("video-issue-drop-catcher");
    const stage = document.querySelector('[data-konva="Stage"]')!;
    expect(stage.lastElementChild).toBe(catcher.closest('[data-konva="Layer"]'));
    view.rerender(<VideoKonvaStage manifest={manifest} readOnly issuePinDropArmed={false} />);
    expect(screen.queryByTestId("video-issue-drop-catcher")).toBeNull();
  });

  it("keeps preparing Issue navigation above canvas writers after the first drop is disarmed", () => {
    const onIssuePinDrop = vi.fn();
    const ref = createRef<VideoStageControls>();
    render(
      <VideoKonvaStage
        ref={ref}
        manifest={manifest}
        videoTool="polygon"
        issuePinDropArmed={false}
        issueNavigationPending
        onIssuePinDrop={onIssuePinDrop}
      />,
    );
    const catcher = screen.getByTestId("video-issue-drop-catcher");
    const stage = document.querySelector('[data-konva="Stage"]')!;
    expect(stage.lastElementChild).toBe(catcher.closest('[data-konva="Layer"]'));
    fireEvent.pointerDown(stage, { clientX: 250, clientY: 200, button: 0 });
    expect(ref.current?.getDrawingDraft?.()).toBeNull();
    expect(onIssuePinDrop).not.toHaveBeenCalled();
  });

  it("工具与审阅浮层位于轨迹条下方，共享画布坐标而不冒泡到绘制容器", () => {
    render(
      <VideoKonvaStage
        manifest={manifest}
        overlays={<button data-testid="tool-overlay">工具</button>}
      />,
    );
    const canvas = screen.getByTestId("video-konva-stage");
    const bar = screen.getByTestId("video-track-context-bar");
    const overlay = screen.getByTestId("tool-overlay");
    expect(canvas.contains(overlay)).toBe(false);
    expect(canvas.parentElement).toBe(overlay.parentElement);
    expect(canvas.parentElement?.previousElementSibling).toBe(bar);
  });

  function contextAnnotation(
    type: "video_track_bbox" | "video_track_polygon" | "video_track_polyline",
  ): AnnotationResponse {
    const shape =
      type === "video_track_bbox"
        ? { bbox: { x: 0.1, y: 0.2, w: 0.3, h: 0.4 } }
        : {
            points: [
              [0.1, 0.1],
              [0.4, 0.1],
              [0.4, 0.4],
            ],
          };
    return {
      id: "context-annotation",
      task_id: "task-1",
      source: "manual",
      annotation_type: type,
      class_name: "car",
      is_active: true,
      attributes: {},
      confidence: 1,
      parent_prediction_id: null,
      created_at: "2026-09-07T00:00:00Z",
      updated_at: null,
      geometry: {
        type,
        track_id: "trk_context",
        keyframes: [0, 8].map((frame_index) => ({ frame_index, source: "manual", ...shape })),
      },
    } as AnnotationResponse;
  }

  it.each(["armed", "preparing"])(
    "%s Issue input also blocks native context-menu selection",
    (phase) => {
      const annotation = contextAnnotation("video_track_bbox");
      const onSelect = vi.fn();
      const view = render(
        <VideoKonvaStage
          manifest={manifest}
          annotations={[annotation]}
          onSelect={onSelect}
          issuePinDropArmed={phase === "armed"}
          issueNavigationPending={phase === "preparing"}
        />,
      );
      drawingSurface();
      onSelect.mockClear();
      fireEvent.contextMenu(screen.getByTestId("video-konva-stage"), {
        clientX: 200,
        clientY: 150,
      });
      expect(onSelect).not.toHaveBeenCalled();
      view.rerender(
        <VideoKonvaStage manifest={manifest} annotations={[annotation]} onSelect={onSelect} />,
      );
      fireEvent.contextMenu(screen.getByTestId("video-konva-stage"), {
        clientX: 200,
        clientY: 150,
      });
      expect(onSelect).toHaveBeenCalledWith(annotation.id);
    },
  );

  it.each(["video_track_bbox", "video_track_polygon", "video_track_polyline"] as const)(
    "%s 的轨迹条将真实插值物化为人工关键帧，保留原端点",
    (type) => {
      const annotation = contextAnnotation(type);
      const onUpdate = vi.fn();
      render(
        <VideoKonvaStage
          manifest={manifest}
          annotations={[annotation]}
          selectedId={annotation.id}
          frameIndex={4}
          onUpdate={onUpdate}
        />,
      );
      const bar = within(screen.getByTestId("video-track-context-bar"));
      expect(bar.getByTestId("video-track-context-state")).toHaveAttribute(
        "data-state",
        "interpolated",
      );
      fireEvent.click(bar.getByRole("button", { name: "补关键帧" }));
      expect(onUpdate).toHaveBeenCalledOnce();
      const [owner, geometry] = onUpdate.mock.calls[0];
      expect(owner).toBe(annotation);
      expect(geometry.type).toBe(type);
      expect(
        geometry.keyframes.map((keyframe: { frame_index: number }) => keyframe.frame_index),
      ).toEqual([0, 4, 8]);
      expect(geometry.keyframes[1].source).toBe("manual");
      expect("keyframes" in annotation.geometry).toBe(true);
      if ("keyframes" in annotation.geometry) {
        expect(geometry.keyframes[0]).toEqual(annotation.geometry.keyframes[0]);
        expect(geometry.keyframes[2]).toEqual(annotation.geometry.keyframes[1]);
      }
    },
  );

  it.each(["readOnly", "annotationLock", "trackLock", "draft", "maskDraft"] as const)(
    "%s 阻止轨迹条写入，读取帧状态仍可用",
    (guard) => {
      const annotation = contextAnnotation("video_track_bbox");
      if (guard === "annotationLock") annotation.is_locked = true;
      const onUpdate = vi.fn();
      const onPropagateTrack = vi.fn();
      const ref = createRef<VideoStageControls>();
      render(
        <VideoKonvaStage
          ref={ref}
          manifest={manifest}
          annotations={[annotation]}
          selectedId={annotation.id}
          frameIndex={4}
          onUpdate={onUpdate}
          onPropagateTrack={onPropagateTrack}
          readOnly={guard === "readOnly"}
          lockedTrackIds={guard === "trackLock" ? new Set(["trk_context"]) : new Set()}
          pendingDrawing={
            guard === "draft"
              ? {
                  kind: "video_bbox",
                  frameIndex: 4,
                  geom: { x: 0.1, y: 0.1, w: 0.2, h: 0.2 },
                  anchor: { left: 0, top: 0 },
                }
              : null
          }
          maskEditor={guard === "maskDraft" ? makeMaskEditor() : undefined}
        />,
      );
      const bar = within(screen.getByTestId("video-track-context-bar"));
      expect(bar.queryByRole("button", { name: "补关键帧" })).toBeNull();
      expect(bar.queryByRole("button", { name: "标记 outside" })).toBeNull();
      expect(bar.queryByRole("button", { name: "延展轨迹" })).toBeNull();
      expect(bar.getByRole("button", { name: "下一关键帧" })).toBeEnabled();
      if (["readOnly", "annotationLock", "trackLock"].includes(guard)) {
        act(() => ref.current?.toggleSelectedTrackOutside());
        act(() => ref.current?.propagateSelectedTrack());
        expect(onUpdate).not.toHaveBeenCalled();
        expect(onPropagateTrack).not.toHaveBeenCalled();
      }
    },
  );

  it("转发的 VideoStageControls 暴露播放控制(togglePlayback 触发 video.play)", () => {
    const ref = createRef<VideoStageControls>();
    render(<VideoKonvaStage ref={ref} manifest={manifest} />);
    expect(typeof ref.current?.togglePlayback).toBe("function");
    expect(typeof ref.current?.seekToFrameReady).toBe("function");
    expect(typeof ref.current?.focusRegion).toBe("function");
    expect(ref.current?.deleteSelectedTrackKeyframe()).toBe(false); // 轨迹类 no-op
    playMock.mockClear();
    act(() => ref.current?.togglePlayback());
    expect(playMock).toHaveBeenCalled();
  });

  function pointer(target: Element | Window, type: string, x: number, y: number, button = 0) {
    fireEvent(target, new MouseEvent(type, { clientX: x, clientY: y, button, bubbles: true }));
  }

  function drawingSurface() {
    const container = screen.getByTestId("video-konva-stage");
    vi.spyOn(container, "getBoundingClientRect").mockReturnValue(new DOMRect(0, 0, 1000, 500));
    return container.querySelector('[data-konva="Stage"]')!;
  }

  it.each(["polygon", "polygon-track", "polyline", "polyline-track"] as const)(
    "%s previews cursor geometry and responds to fill opacity settings",
    (videoTool) => {
      const visual = { ...DEFAULT_ANNOTATION_VISUAL, fillOpacity: 0.37 };
      const onCreatePoints = vi.fn();
      const props = { manifest, videoTool, visual, onCreatePoints };
      const view = render(<VideoKonvaStage {...props} />);
      const stage = drawingSurface();
      pointer(stage, "pointerdown", 100, 100);
      pointer(stage, "pointerdown", 300, 100);
      pointer(stage, "pointermove", 300, 300);
      const line = () => screen.getByTestId("points-draft").querySelector('[data-konva="Line"]')!;
      const closed = videoTool.startsWith("polygon");
      expect(line()).toHaveAttribute("data-closed", String(closed));
      expect(JSON.parse(line().getAttribute("data-points")!)).toEqual([
        100, 100, 300, 100, 300, 300,
      ]);
      if (closed) expect(line().getAttribute("data-fill")).toContain("0.37");
      else expect(line()).not.toHaveAttribute("data-fill");
      view.rerender(<VideoKonvaStage {...props} visual={{ ...visual, fillOpacity: 0 }} />);
      if (closed) expect(line().getAttribute("data-fill")).toMatch(/,\s*0\)$/);
      expect(onCreatePoints).not.toHaveBeenCalled();
    },
  );

  it.each([
    ["polygon", "video_polygon", false],
    ["polygon-track", "video_track_polygon", true],
    ["polyline", "video_polyline", false],
    ["polyline-track", "video_track_polyline", true],
  ] as const)(
    "%s exposes the original partial draft and resumes its exact points",
    (tool, kind, track) => {
      const ref = createRef<VideoStageControls>();
      const onCreate = vi.fn();
      render(
        <VideoKonvaStage
          ref={ref}
          manifest={manifest}
          frameIndex={4}
          videoTool={tool}
          onCreatePoints={onCreate}
          onCreatePointsTrack={onCreate}
        />,
      );
      const stage = drawingSurface();
      pointer(stage, "pointerdown", 100, 100);
      pointer(stage, "pointerdown", 300, 100);
      expect(ref.current?.getDrawingDraft?.()).toEqual({ kind: "points", tool, frameIndex: 4 });
      const hint = screen.getByTestId("video-creation-scope-hint");
      expect(hint).toHaveTextContent(track ? "新建轨迹，从当前源帧开始" : "仅当前源帧");
      expect(hint).toHaveAttribute("data-source-frame-index", "4");
      // A declined scope command only reads the bridge; it does not reconstruct the draft.
      expect(ref.current?.getDrawingDraft?.()).toEqual({ kind: "points", tool, frameIndex: 4 });
      pointer(stage, "pointerdown", 300, 300);
      fireEvent.keyDown(window, { key: "Enter" });
      expect(onCreate).toHaveBeenCalledOnce();
      expect(onCreate).toHaveBeenCalledWith(kind, 4, [
        [0.1, 0.2],
        [0.3, 0.2],
        [0.3, 0.6],
      ]);
      expect(ref.current?.getDrawingDraft?.()).toBeNull();
      expect(screen.queryByTestId("video-creation-scope-hint")).toBeNull();
    },
  );

  it("discard clears points and prevents the subsequent Enter from creating an object", () => {
    const ref = createRef<VideoStageControls>();
    const onCreatePoints = vi.fn();
    render(
      <VideoKonvaStage
        ref={ref}
        manifest={manifest}
        videoTool="polygon"
        onCreatePoints={onCreatePoints}
      />,
    );
    const stage = drawingSurface();
    pointer(stage, "pointerdown", 100, 100);
    pointer(stage, "pointerdown", 300, 100);
    pointer(stage, "pointerdown", 300, 300);
    act(() => {
      ref.current?.discardDrawingDraft?.();
      expect(ref.current?.getDrawingDraft?.()).toBeNull();
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", cancelable: true }));
    });
    expect(onCreatePoints).not.toHaveBeenCalled();
    expect(screen.queryByTestId("video-creation-scope-hint")).toBeNull();
  });

  it("partial keypoints can be inspected and discarded without retaining an old node", () => {
    const ref = createRef<VideoStageControls>();
    const onCreateKeypoints = vi.fn();
    render(
      <VideoKonvaStage
        ref={ref}
        manifest={manifest}
        frameIndex={4}
        videoTool="keypoint"
        keypointSchema={{ nodes: [{ name: "head" }, { name: "tail" }], edges: [[0, 1]] }}
        onCreateKeypoints={onCreateKeypoints}
      />,
    );
    const stage = drawingSurface();
    pointer(stage, "pointerdown", 100, 100);
    expect(ref.current?.getDrawingDraft?.()).toEqual({
      kind: "keypoint",
      tool: "keypoint",
      frameIndex: 4,
    });
    expect(screen.getByTestId("video-creation-scope-hint")).toHaveTextContent("仅当前源帧");
    act(() => ref.current?.discardDrawingDraft?.());
    expect(ref.current?.getDrawingDraft?.()).toBeNull();
    pointer(stage, "pointerdown", 300, 100);
    expect(onCreateKeypoints).not.toHaveBeenCalled();
    pointer(stage, "pointerdown", 300, 300);
    expect(onCreateKeypoints).toHaveBeenCalledOnce();
    expect(onCreateKeypoints).toHaveBeenCalledWith(4, [
      { x: 0.3, y: 0.2, v: 2 },
      { x: 0.3, y: 0.6, v: 2 },
    ]);
  });

  it("discarding a creating drag blocks a trailing pointerup synchronously", () => {
    const ref = createRef<VideoStageControls>();
    const onPendingDraw = vi.fn();
    render(
      <VideoKonvaStage
        ref={ref}
        manifest={manifest}
        frameIndex={4}
        videoTool="track"
        onPendingDraw={onPendingDraw}
      />,
    );
    const stage = drawingSurface();
    pointer(stage, "pointerdown", 600, 100);
    expect(ref.current?.getDrawingDraft?.()).toEqual({ kind: "box", tool: "track", frameIndex: 4 });
    expect(screen.getByTestId("video-creation-scope-hint")).toHaveTextContent(
      "新建轨迹，从当前源帧开始",
    );
    pointer(window, "pointermove", 800, 300);
    act(() => {
      ref.current?.discardDrawingDraft?.();
      expect(ref.current?.getDrawingDraft?.()).toBeNull();
      window.dispatchEvent(new MouseEvent("pointerup", { clientX: 800, clientY: 300 }));
    });
    expect(onPendingDraw).not.toHaveBeenCalled();
    expect(screen.queryByTestId("video-creation-scope-hint")).toBeNull();
  });

  it("the creation bridge leaves an existing object move intact", () => {
    const annotation = contextAnnotation("video_track_bbox");
    const ref = createRef<VideoStageControls>();
    const onUpdate = vi.fn();
    render(
      <VideoKonvaStage
        ref={ref}
        manifest={manifest}
        annotations={[annotation]}
        selectedId={annotation.id}
        videoTool="select"
        onUpdate={onUpdate}
      />,
    );
    const stage = drawingSurface();
    pointer(stage, "pointerdown", 200, 150);
    expect(ref.current?.getDrawingDraft?.()).toBeNull();
    expect(screen.queryByTestId("video-creation-scope-hint")).toBeNull();
    act(() => ref.current?.discardDrawingDraft?.());
    pointer(window, "pointerup", 300, 200);
    expect(onUpdate).toHaveBeenCalledOnce();
    expect(onUpdate.mock.calls[0][0].id).toBe(annotation.id);
  });

  it("continuing an existing bbox track never advertises a new track", () => {
    const annotation = contextAnnotation("video_track_bbox");
    const ref = createRef<VideoStageControls>();
    const onUpdate = vi.fn();
    const onPendingDraw = vi.fn();
    render(
      <VideoKonvaStage
        ref={ref}
        manifest={manifest}
        frameIndex={4}
        annotations={[annotation]}
        selectedId={annotation.id}
        videoTool="track"
        onUpdate={onUpdate}
        onPendingDraw={onPendingDraw}
      />,
    );
    const stage = drawingSurface();
    pointer(stage, "pointerdown", 600, 100);
    expect(ref.current?.getDrawingDraft?.()).toEqual({
      kind: "box",
      tool: "track",
      frameIndex: 4,
      continuingTrack: true,
    });
    expect(screen.queryByTestId("video-creation-scope-hint")).toBeNull();
    pointer(window, "pointerup", 800, 300);
    expect(onPendingDraw).not.toHaveBeenCalled();
    expect(onUpdate).toHaveBeenCalledOnce();
    expect(
      onUpdate.mock.calls[0][1].keyframes.some(
        (kf: { frame_index: number }) => kf.frame_index === 4,
      ),
    ).toBe(true);
  });

  it("ends the continuing draft before keyframe writes and automatic next-track selection", () => {
    const annotation = contextAnnotation("video_track_bbox");
    const nextTrack: AnnotationResponse = {
      ...annotation,
      id: "next-track",
      geometry: {
        type: "video_track_bbox",
        track_id: "trk_next",
        keyframes: [
          {
            frame_index: 3,
            bbox: { x: 0.1, y: 0.1, w: 0.2, h: 0.2 },
            source: "manual",
            occluded: false,
          },
        ],
      },
    };
    const ref = createRef<VideoStageControls>();
    const observedDrafts: Array<VideoDrawingDraft | null | undefined> = [];
    const onUpdate = vi.fn();
    const onSelect = vi.fn();
    onUpdate.mockImplementation(() => observedDrafts.push(ref.current?.getDrawingDraft?.()));
    onSelect.mockImplementation(() => observedDrafts.push(ref.current?.getDrawingDraft?.()));
    render(
      <VideoKonvaStage
        ref={ref}
        manifest={manifest}
        frameIndex={4}
        annotations={[annotation, nextTrack]}
        selectedId={annotation.id}
        videoTool="track"
        trackContinueAutoAdvance
        onUpdate={onUpdate}
        onSelect={onSelect}
      />,
    );
    const stage = drawingSurface();
    observedDrafts.length = 0;
    onSelect.mockClear();

    pointer(stage, "pointerdown", 600, 100);
    pointer(window, "pointerup", 800, 300);

    expect(onUpdate).toHaveBeenCalledOnce();
    expect(onUpdate.mock.calls[0][0]).toBe(annotation);
    const updated = onUpdate.mock.calls[0][1] as VideoTrackGeometry;
    expect(updated.keyframes.map((keyframe) => keyframe.frame_index)).toEqual([0, 4, 8]);
    expect(updated.keyframes[1]).toMatchObject({ source: "manual", occluded: false });
    expect(updated.keyframes[1].bbox).toMatchObject({ x: 0.6, y: 0.2 });
    expect(updated.keyframes[1].bbox.w).toBeCloseTo(0.2);
    expect(updated.keyframes[1].bbox.h).toBeCloseTo(0.4);
    expect(onSelect).toHaveBeenCalledOnce();
    expect(onSelect).toHaveBeenCalledWith(nextTrack.id);
    expect(observedDrafts).toEqual([null, null]);
    expect(ref.current?.getDrawingDraft?.()).toBeNull();
  });

  it("pending preview scope follows its captured payload instead of the current tool", () => {
    const view = render(
      <VideoKonvaStage
        manifest={manifest}
        videoTool="box"
        frameIndex={4}
        pendingDrawing={{
          kind: "video_track_polygon",
          frameIndex: 4,
          geom: { x: 0.1, y: 0.2, w: 0.2, h: 0.3 },
          anchor: { left: 100, top: 200 },
          points: [
            [0.1, 0.2],
            [0.3, 0.2],
            [0.3, 0.5],
          ],
        }}
      />,
    );
    expect(screen.getByTestId("video-creation-scope-hint")).toHaveTextContent(
      "新建轨迹，从当前源帧开始",
    );
    view.rerender(
      <VideoKonvaStage
        manifest={manifest}
        videoTool="track"
        frameIndex={4}
        pendingDrawing={{
          kind: "video_bbox",
          frameIndex: 4,
          geom: { x: 0.1, y: 0.2, w: 0.2, h: 0.3 },
          anchor: { left: 100, top: 200 },
        }}
      />,
    );
    expect(screen.getByTestId("video-creation-scope-hint")).toHaveTextContent("仅当前源帧");
    view.rerender(
      <VideoKonvaStage
        manifest={manifest}
        videoTool="track"
        frameIndex={5}
        pendingDrawing={{
          kind: "video_bbox",
          frameIndex: 4,
          geom: { x: 0.1, y: 0.2, w: 0.2, h: 0.3 },
          anchor: { left: 100, top: 200 },
        }}
      />,
    );
    expect(screen.queryByTestId("video-creation-scope-hint")).toBeNull();
  });

  it.each([
    [0, false, true],
    [1, false, false],
    [2, false, false],
    [0, true, false],
  ] as const)(
    "neutral blank pointer button=%s spacePan=%s clears only a normal left selection",
    (button, spacePan, clears) => {
      const onSelect = vi.fn();
      render(
        <VideoKonvaStage
          manifest={manifest}
          videoTool="select"
          onSelect={onSelect}
          spacePan={spacePan}
        />,
      );
      const stage = drawingSurface();
      onSelect.mockClear();
      pointer(stage, "pointerdown", 700, 100, button);
      expect(onSelect.mock.calls).toEqual(clears ? [[null]] : []);
      pointer(window, "pointerup", 700, 100, button);
    },
  );

  it("settings scrolling and draft keys leave the underlying video stage untouched", () => {
    const view = render(<VideoKonvaStage manifest={manifest} videoTool="polygon" />);
    const stage = view.getByTestId("video-konva-stage");
    vi.spyOn(stage, "getBoundingClientRect").mockReturnValue(new DOMRect(0, 0, 1000, 500));
    const settings = document.createElement("button");
    settings.dataset.workbenchSettings = "";
    settings.dataset.state = "open";
    document.body.append(settings);
    const events = [
      new WheelEvent("wheel", {
        clientX: 100,
        clientY: 100,
        ctrlKey: true,
        deltaY: -10,
        bubbles: true,
        cancelable: true,
      }),
      ...["Enter", "Escape", "Backspace"].map(
        (key) => new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true }),
      ),
    ];
    act(() => events.forEach((event) => settings.dispatchEvent(event)));
    expect(events.every((event) => !event.defaultPrevented)).toBe(true);
    settings.remove();

    const wheel = new WheelEvent("wheel", {
      clientX: 100,
      clientY: 100,
      ctrlKey: true,
      deltaY: -10,
      cancelable: true,
    });
    const enter = new KeyboardEvent("keydown", { key: "Enter", cancelable: true });
    act(() => {
      window.dispatchEvent(wheel);
      window.dispatchEvent(enter);
    });
    expect(wheel.defaultPrevented).toBe(true);
    expect(enter.defaultPrevented).toBe(true);
  });

  it("a tool menu consumes Enter while the existing points draft remains available", () => {
    const ref = createRef<VideoStageControls>();
    const onCreatePoints = vi.fn();
    render(
      <VideoKonvaStage
        ref={ref}
        manifest={manifest}
        videoTool="polygon"
        onCreatePoints={onCreatePoints}
      />,
    );
    const stage = drawingSurface();
    pointer(stage, "pointerdown", 100, 100);
    pointer(stage, "pointerdown", 300, 100);
    pointer(stage, "pointerdown", 300, 300);
    const menu = document.createElement("button");
    menu.dataset.workbenchToolMenu = "";
    menu.dataset.state = "open";
    document.body.append(menu);
    try {
      fireEvent.keyDown(menu, { key: "Enter" });
      expect(onCreatePoints).not.toHaveBeenCalled();
      expect(ref.current?.getDrawingDraft?.()).toEqual({
        kind: "points",
        tool: "polygon",
        frameIndex: 0,
      });
    } finally {
      menu.remove();
    }
    fireEvent.keyDown(window, { key: "Enter" });
    expect(onCreatePoints).toHaveBeenCalledWith("video_polygon", 0, [
      [0.1, 0.2],
      [0.3, 0.2],
      [0.3, 0.6],
    ]);
  });

  it.each(["mask", "mask-track"] as const)(
    "new %s preview keeps its first stroke scope through the pending class step",
    (tool) => {
      const editor = makeMaskEditor({ beginStroke: vi.fn(), paintAt: vi.fn(), endStroke: vi.fn() });
      const ref = createRef<VideoStageControls>();
      const view = render(
        <VideoKonvaStage
          ref={ref}
          manifest={manifest}
          videoTool={tool}
          frameIndex={4}
          maskEditor={editor}
        />,
      );
      const stage = drawingSurface();
      pointer(stage, "pointerdown", 100, 100);
      expect(ref.current?.getDrawingDraft?.()).toBeNull(); // Mask retains its existing navigation owner.
      const text = tool === "mask-track" ? "新建轨迹，从当前源帧开始" : "仅当前源帧";
      expect(screen.getByTestId("video-creation-scope-hint")).toHaveTextContent(text);
      view.rerender(
        <VideoKonvaStage
          ref={ref}
          manifest={manifest}
          videoTool="select"
          frameIndex={4}
          maskEditor={editor}
          pendingDrawing={{
            kind: "video_mask",
            frameIndex: 4,
            geom: { x: 0.1, y: 0.2, w: 0.2, h: 0.3 },
            anchor: { left: 100, top: 200 },
          }}
        />,
      );
      expect(screen.getByTestId("video-creation-scope-hint")).toHaveTextContent(text);
      expect(screen.getByTestId("video-creation-scope-hint")).toHaveAttribute(
        "data-source-frame-index",
        "4",
      );
    },
  );

  it.each(["mask", "mask-track"] as const)(
    "%s sends every editor phase to the primary/secondary owner",
    (videoTool) => {
      const onMaskCommit = vi.fn();
      const onMaskCancel = vi.fn();
      const states: Partial<UseMaskEditorReturn>[] = [
        { active: false, dirty: false, phase: "idle" },
        { dirty: false, phase: "ready" },
        { phase: "dirty" },
        { phase: "saving" },
        { operationPreview: { id: 1, alpha: new Uint8Array(0) } as never },
        { instanceOperationPreview: { id: 2, plan: { focusAlpha: new Uint8Array(0) } } as never },
      ];
      const view = render(<VideoKonvaStage manifest={manifest} />);
      states.forEach((state, index) => {
        const editor = makeMaskEditor(state);
        view.rerender(
          <VideoKonvaStage
            manifest={manifest}
            videoTool={videoTool}
            maskEditor={editor}
            onMaskCommit={onMaskCommit}
            onMaskCancel={onMaskCancel}
          />,
        );
        act(() => {
          window.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", cancelable: true }));
          window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", cancelable: true }));
        });
        expect(onMaskCommit).toHaveBeenCalledTimes(index + 1);
        expect(onMaskCancel).toHaveBeenCalledTimes(index + 1);
        expect(editor.confirmOperation).not.toHaveBeenCalled();
        expect(editor.cancelOperation).not.toHaveBeenCalled();
      });
    },
  );

  it("pixel read-only blocks brush/undo while the primary owner decides whether saving is allowed", () => {
    const editor = makeMaskEditor();
    const onMaskCommit = vi.fn();
    const view = render(
      <VideoKonvaStage
        manifest={manifest}
        videoTool="mask"
        maskEditor={editor}
        onMaskCommit={onMaskCommit}
        readOnly
      />,
    );
    const key = (key: string, ctrlKey = false) =>
      window.dispatchEvent(new KeyboardEvent("keydown", { key, ctrlKey, cancelable: true }));
    act(() => {
      key("b");
      key("e");
      key("z", true);
      key("Enter");
    });
    expect(onMaskCommit).toHaveBeenCalledTimes(1);
    expect(editor.setMode).not.toHaveBeenCalled();
    expect(editor.undo).not.toHaveBeenCalled();
    view.rerender(<VideoKonvaStage manifest={manifest} videoTool="mask" maskEditor={editor} />);
    act(() => {
      key("b");
      key("e");
      key("z", true);
      key("y", true);
    });
    expect(editor.setMode).toHaveBeenNthCalledWith(1, "brush");
    expect(editor.setMode).toHaveBeenNthCalledWith(2, "erase");
    expect(editor.undo).toHaveBeenCalledTimes(1);
    expect(editor.redo).toHaveBeenCalledTimes(1);
  });

  it("lets global history own undo when the video Mask session has no local command", () => {
    const editor = makeMaskEditor({
      dirty: false,
      phase: "ready",
      canUndo: false,
      canRedo: false,
    });
    const globalUndo = vi.fn((event: KeyboardEvent) => {
      if (["z", "y"].includes(event.key.toLowerCase()) && (event.ctrlKey || event.metaKey))
        event.preventDefault();
    });
    const view = render(
      <VideoKonvaStage manifest={manifest} videoTool="mask" maskEditor={editor} />,
    );
    window.addEventListener("keydown", globalUndo);
    try {
      const undo = new KeyboardEvent("keydown", { key: "z", ctrlKey: true, cancelable: true });
      const redo = new KeyboardEvent("keydown", { key: "y", ctrlKey: true, cancelable: true });
      act(() => {
        window.dispatchEvent(undo);
        window.dispatchEvent(redo);
      });
      expect(globalUndo).toHaveBeenCalledTimes(2);
      expect(editor.undo).not.toHaveBeenCalled();
      expect(editor.redo).not.toHaveBeenCalled();
      expect(undo.defaultPrevented).toBe(true);
      expect(redo.defaultPrevented).toBe(true);
    } finally {
      window.removeEventListener("keydown", globalUndo);
      view.unmount();
    }
  });

  it.each([
    "input",
    "textarea",
    "select",
    "contenteditable",
    "combobox",
    "listbox",
    "menu",
    "dialog",
    "button",
  ])("video Mask leaves %s keys with the focused control", (kind) => {
    const onMaskCommit = vi.fn();
    const onMaskCancel = vi.fn();
    render(
      <VideoKonvaStage
        manifest={manifest}
        videoTool="mask"
        maskEditor={makeMaskEditor()}
        onMaskCommit={onMaskCommit}
        onMaskCancel={onMaskCancel}
      />,
    );
    const element = document.createElement(
      ["input", "textarea", "select", "button"].includes(kind) ? kind : "div",
    );
    if (kind === "contenteditable") element.setAttribute("contenteditable", "true");
    else if (!["input", "textarea", "select", "button"].includes(kind))
      element.setAttribute("role", kind);
    const target = ["contenteditable", "combobox", "listbox", "menu", "dialog", "button"].includes(
      kind,
    )
      ? element.appendChild(document.createElement("span"))
      : element;
    const ownKeys = vi.fn();
    element.addEventListener("keydown", ownKeys);
    document.body.append(element);
    try {
      const keys = kind === "button" ? ["Enter"] : ["Enter", "Escape"];
      const events = keys.map(
        (key) => new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true }),
      );
      act(() => events.forEach((event) => target.dispatchEvent(event)));
      expect(ownKeys).toHaveBeenCalledTimes(keys.length);
      expect(events.every((event) => !event.defaultPrevented)).toBe(true);
      expect(onMaskCommit).not.toHaveBeenCalled();
      expect(onMaskCancel).not.toHaveBeenCalled();
    } finally {
      element.remove();
    }
  });

  it.each([
    ["long press", { repeat: true }],
    ["IME composition", { isComposing: true }],
    ["IME legacy key", { keyCode: 229 }],
    ["already handled", {}],
  ] as const)("video Mask ignores %s", (name, init) => {
    const editor = makeMaskEditor();
    const onMaskCommit = vi.fn();
    const onMaskCancel = vi.fn();
    render(
      <VideoKonvaStage
        manifest={manifest}
        videoTool="mask-track"
        maskEditor={editor}
        onMaskCommit={onMaskCommit}
        onMaskCancel={onMaskCancel}
      />,
    );
    act(() => {
      for (const key of ["Enter", "Escape", "b", "e", "z"]) {
        const event = new KeyboardEvent("keydown", {
          ...init,
          key,
          ctrlKey: key === "z",
          cancelable: true,
        });
        if (name === "already handled") event.preventDefault();
        window.dispatchEvent(event);
      }
    });
    expect(onMaskCommit).not.toHaveBeenCalled();
    expect(onMaskCancel).not.toHaveBeenCalled();
    expect(editor.setMode).not.toHaveBeenCalled();
    expect(editor.undo).not.toHaveBeenCalled();
  });

  it("video Mask lets an unfocused context menu receive Esc", () => {
    const onMaskCancel = vi.fn();
    const setVideoTool = vi.fn();
    render(
      <VideoKonvaStage
        manifest={manifest}
        videoTool="mask"
        maskEditor={makeMaskEditor()}
        onMaskCancel={onMaskCancel}
      />,
    );
    renderHook(() =>
      useWorkbenchHotkeys({
        videoMode: true,
        s: { tool: "select", videoTool: "mask", selectedIds: [], setVideoTool },
        stageGeom: { imgW: 1000, imgH: 500 },
      } as unknown as UseWorkbenchHotkeysArgs),
    );
    const menu = document.createElement("div");
    menu.setAttribute("role", "menu");
    document.body.append(menu);
    const close = vi.fn(() => menu.remove());
    document.addEventListener("keydown", close);
    try {
      act(() =>
        document.body.dispatchEvent(
          new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }),
        ),
      );
      expect(close).toHaveBeenCalledTimes(1);
      expect(onMaskCancel).not.toHaveBeenCalled();
      expect(setVideoTool).not.toHaveBeenCalled();
    } finally {
      menu.remove();
      document.removeEventListener("keydown", close);
    }
  });
});

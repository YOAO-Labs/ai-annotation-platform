import { act, fireEvent, render, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import type { ComponentProps } from "react";
import { createRef } from "react";
import { describe, expect, it, vi } from "vitest";
import { projectTrackerReview } from "@/hooks/videoTrackerReviewScope";
import {
  VideoPlaybackOverlay,
  densityBinGradient,
  resolveLargeFrameStep,
} from "./VideoPlaybackOverlay";
import { getTrackColor } from "./colors";
import { resolveVideoTimelineRangePurpose } from "../state/useWorkbenchShellModel.helpers";
import { buildFrameTimebase } from "./frameTimebase";
import type { VideoTimelineDensityBin, VideoTrackTimeline } from "./videoTrackTimeline";
import type { VideoTimelineWindowControls } from "./videoStageControls";

const overlayCss = readFileSync(
  resolve(process.cwd(), "src/pages/Workbench/stage/VideoPlaybackOverlay.module.css"),
  "utf8",
);

const timebase = buildFrameTimebase({
  duration_ms: 1000,
  fps: 10,
  frame_count: 10,
  width: 1000,
  height: 500,
  codec: "h264",
  playback_path: null,
  playback_codec: null,
  playback_error: null,
  poster_frame_path: null,
  probe_error: null,
  poster_error: null,
  frame_timetable_frame_count: null,
  frame_timetable_error: null,
});

function setRect(el: Element) {
  vi.spyOn(el, "getBoundingClientRect").mockReturnValue({
    left: 0,
    top: 0,
    width: 1000,
    height: 40,
    right: 1000,
    bottom: 40,
    x: 0,
    y: 0,
    toJSON: () => ({}),
  });
}

function pointerMove(clientX: number) {
  const event = new Event("pointermove", { bubbles: true, cancelable: true });
  Object.defineProperty(event, "clientX", { value: clientX });
  return event;
}

function pointerDown(clientX: number, shiftKey = false) {
  const event = new Event("pointerdown", { bubbles: true, cancelable: true });
  Object.defineProperty(event, "clientX", { value: clientX });
  Object.defineProperty(event, "pointerId", { value: 1 });
  Object.defineProperty(event, "shiftKey", { value: shiftKey });
  return event;
}

function pointerUp(clientX: number) {
  const event = new Event("pointerup", { bubbles: true, cancelable: true });
  Object.defineProperty(event, "clientX", { value: clientX });
  Object.defineProperty(event, "pointerId", { value: 1 });
  return event;
}

function renderOverlay(extra: Partial<ComponentProps<typeof VideoPlaybackOverlay>> = {}) {
  return render(
    <VideoPlaybackOverlay
      frameIndex={0}
      maxFrame={9}
      timebase={timebase}
      isPlaying={false}
      currentFrameEntryCount={0}
      visible
      onSeek={() => {}}
      onSeekByFrames={() => {}}
      onTogglePlay={() => {}}
      {...extra}
    />,
  );
}

function reviewProjection() {
  return projectTrackerReview(
    {
      job_id: "review-job",
      status: "pending_review",
      annotation_id: "annotation-1",
      grid_step: 1,
      output_geometry: "bbox",
      results: [
        { instance_id: "B", frame_index: 0, geometry: { type: "bbox", x: 0, y: 0, w: 1, h: 1 } },
        { instance_id: "A", frame_index: 3, geometry: { type: "bbox", x: 0, y: 0, w: 1, h: 1 } },
        { instance_id: "A", frame_index: 7, geometry: { type: "bbox", x: 0, y: 0, w: 1, h: 1 } },
      ],
    },
    { instanceIds: ["A"], fromFrame: 2, toFrame: 7, intentRevision: 1 },
    1,
  );
}

describe("VideoPlaybackOverlay", () => {
  it("captures fractional windows, preserves them across collapse, and rejects handles from A→B→A", () => {
    const ref = createRef<VideoTimelineWindowControls>();
    const onInteraction = vi.fn();
    const props = {
      frameIndex: 120,
      maxFrame: 179,
      timebase,
      isPlaying: false,
      currentFrameEntryCount: 0,
      visible: true,
      onSeek: vi.fn(),
      onSeekByFrames: vi.fn(),
      onTogglePlay: vi.fn(),
      windowControlsRef: ref,
      onViewInteraction: onInteraction,
    };
    const view = render(<VideoPlaybackOverlay {...props} sourceKey="A" />);
    const old = ref.current!;
    act(() => {
      expect(ref.current!.restore({ from: 60.25, to: 144.75 })).toEqual({
        window: { from: 60.25, to: 144.75 },
        clamped: false,
      });
    });
    expect(ref.current!.capture()).toEqual({ from: 60.25, to: 144.75 });
    expect(onInteraction).not.toHaveBeenCalled();
    expect(view.getByTestId("video-playback-overlay")).toHaveAttribute(
      "data-timeline-from",
      "60.25",
    );
    fireEvent.click(view.getByTestId("video-timeline-toggle"));
    expect(view.getByTestId("video-playback-overlay")).toHaveAttribute(
      "data-timeline-to",
      "144.75",
    );
    view.rerender(<VideoPlaybackOverlay {...props} sourceKey="B" />);
    expect(ref.current!.capture()).toEqual({ from: 0, to: 179 });
    view.rerender(<VideoPlaybackOverlay {...props} sourceKey="A" />);
    expect(old.capture()).toBeNull();
    expect(old.restore({ from: 20, to: 100 })).toBeNull();
    expect(ref.current!.capture()).toEqual({ from: 0, to: 179 });
    fireEvent.click(view.getByRole("button", { name: "放大时间轴" }));
    expect(onInteraction).toHaveBeenCalledTimes(1);
    view.unmount();
    expect(old.capture()).toBeNull();
  });

  it("restores through the density-derived minimum span and current media bounds", () => {
    const ref = createRef<VideoTimelineWindowControls>();
    renderOverlay({
      maxFrame: 179,
      windowControlsRef: ref,
      globalTimelineDensity: [{ index: 0, from: 0, to: 11, density: 1, tracks: [] }],
    });
    act(() => {
      expect(ref.current!.restore({ from: 160.5, to: 170.5 })).toEqual({
        window: { from: 107, to: 179 },
        clamped: true,
      });
    });
    expect(ref.current!.capture()).toEqual({ from: 107, to: 179 });
    expect(ref.current!.restore({ from: NaN, to: 120 })).toBeNull();
    expect(ref.current!.restore({ from: 120, to: 60 })).toBeNull();
    expect(ref.current!.capture()).toEqual({ from: 107, to: 179 });
  });

  it("renders the active jog playback rate when provided", () => {
    const { getByTestId } = renderOverlay({ isPlaying: true, playbackRateLabel: "-2x" });

    expect(getByTestId("video-playback-rate")).toHaveTextContent("-2x");
  });

  it("renders collaborative work and core ranges as distinct timeline bands", () => {
    const { getAllByTestId, getByTestId } = renderOverlay({
      segmentRange: {
        coreStartFrame: 2,
        coreEndFrame: 6,
        workStartFrame: 1,
        workEndFrame: 7,
      },
    });

    expect(getAllByTestId("video-segment-work-range")).toHaveLength(1);
    expect(getAllByTestId("video-segment-core-range")).toHaveLength(1);
    fireEvent.click(getByTestId("video-timeline-toggle"));
    expect(getByTestId("video-timeline-lane-segment")).toBeInTheDocument();
    expect(getAllByTestId("video-segment-work-range")).toHaveLength(1);
    expect(getAllByTestId("video-segment-core-range")).toHaveLength(1);
  });

  it("starts with an accessible collapsed summary and hides empty optional lanes when expanded", async () => {
    const user = userEvent.setup();
    const { getByRole, getByTestId, getByText, queryByTestId, queryByText } = renderOverlay({
      currentFrameEntryCount: 4,
    });
    const toggle = getByTestId("video-timeline-toggle");

    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveAttribute("aria-controls", "video-timeline-details");
    expect(toggle).toHaveAccessibleName("展开时间轴详情");
    expect(getByTestId("video-current-frame-entry-count")).toHaveTextContent("当前帧 4 个标注");
    expect(getByTestId("video-time-readout")).toHaveTextContent("00:00.000 / 00:01.000");
    expect(getByTestId("video-timeline-window-readout")).toHaveTextContent("窗口：全部 · F0–9");
    expect(getByTestId("video-timeline-window-overview")).toBeInTheDocument();
    expect(getByRole("button", { name: "播放 / 暂停" })).toBeInTheDocument();
    expect(queryByText("AI 预测密度")).toBeNull();

    await user.click(toggle);

    const expandedToggle = getByTestId("video-timeline-toggle");
    expect(expandedToggle).toHaveAttribute("aria-expanded", "true");
    expect(expandedToggle).toHaveAccessibleName("收起时间轴详情");
    expect(expandedToggle).toHaveFocus();
    expect(getByTestId("video-timeline-details")).toBeInTheDocument();
    expect(getByText("标注密度")).toBeInTheDocument();
    expect(queryByTestId("video-timeline-lane-chapters")).toBeNull();
    expect(queryByTestId("video-timeline-lane-bookmarks")).toBeNull();
    expect(queryByTestId("video-timeline-lane-issues")).toBeNull();
    expect(queryByTestId("video-timeline-lane-predictions")).toBeNull();
    expect(queryByTestId("video-timeline-lane-track")).toBeNull();
    expect(queryByTestId("video-timeline-lane-propagation")).toBeNull();
    expect(queryByTestId("video-timeline-lane-loop")).toBeNull();
    expect(getByTestId("video-timeline-navigator")).toBeInTheDocument();
    expect(getByTestId("video-timeline-window-readout")).toHaveTextContent("窗口：全部 · F0–9");

    await user.click(expandedToggle);

    const collapsedToggle = getByTestId("video-timeline-toggle");
    expect(collapsedToggle).toHaveAttribute("aria-expanded", "false");
    expect(collapsedToggle).toHaveFocus();
  });

  it("merges compact playback controls into the expanded bottom bar", () => {
    const { getByTestId } = renderOverlay();

    fireEvent.click(getByTestId("video-timeline-toggle"));

    const bottomBar = getByTestId("video-timeline-bottom-bar");
    const playbackControls = within(bottomBar).getByTestId("video-playback-controls");
    const zoomControls = within(bottomBar).getByTestId("video-timeline-zoom-controls");
    const toggle = within(bottomBar).getByTestId("video-timeline-toggle");
    expect(playbackControls).toBeInTheDocument();
    expect(within(bottomBar).getByTitle("播放 / 暂停 (Space)")).toBeInTheDocument();
    expect(within(bottomBar).getByTestId("video-timeline-navigator")).toBeInTheDocument();
    expect(within(playbackControls).queryByTestId("video-timeline-zoom-in")).toBeNull();
    expect(zoomControls.nextElementSibling).toBe(toggle);
    expect(overlayCss).toMatch(
      /\.(?:expandedControls \.controlButton|zoomControls \.controlButton)[^{]*\{[^}]*width:\s*24px;[^}]*height:\s*24px;/s,
    );
    expect(overlayCss).not.toMatch(/\.expandedControls\s*\{[^}]*background:/s);
    expect(overlayCss).toMatch(/\.overlayExpanded\s*\{[^}]*bottom:\s*12px;/s);
  });

  it("uses a view transition for expand and collapse when motion is allowed", () => {
    const startViewTransition = vi.fn((update: () => void) => {
      update();
      return {
        finished: Promise.resolve(),
        ready: Promise.resolve(),
        updateCallbackDone: Promise.resolve(),
        skipTransition: vi.fn(),
      };
    });
    Object.defineProperty(document, "startViewTransition", {
      configurable: true,
      value: startViewTransition,
    });

    try {
      const { getByTestId } = renderOverlay();
      fireEvent.click(getByTestId("video-timeline-toggle"));

      expect(startViewTransition).toHaveBeenCalledOnce();
      expect(getByTestId("video-playback-overlay")).toHaveAttribute("data-state", "expanded");
      expect(overlayCss).toMatch(/view-transition-name:\s*video-timeline-overlay/);
      expect(overlayCss).toMatch(/@media\s*\(prefers-reduced-motion:\s*reduce\)/);
    } finally {
      Reflect.deleteProperty(document, "startViewTransition");
    }
  });

  it("shows populated optional lanes when expanded", () => {
    const selectedTrackTimeline: VideoTrackTimeline = {
      trackId: "trk",
      keyframes: [{ frame: 0, source: "manual", occluded: false }],
      outside: [],
      interpolated: [],
    };
    const { getByTestId, getByText } = renderOverlay({
      chapters: [{ id: "chapter", startFrame: 1, endFrame: 4, title: "路口" }],
      bookmarks: [{ id: "bookmark", frameIndex: 2, label: "转弯", createdAt: 1 }],
      issueFrames: [3],
      predictionDensity: [{ index: 0, from: 0, to: 1, count: 1 }],
      selectedTrackTimeline,
      propagateRange: { startFrame: 2, endFrame: 6 },
      loopRegion: { startFrame: 1, endFrame: 5 },
    });

    fireEvent.click(getByTestId("video-timeline-toggle"));

    expect(getByTestId("video-timeline-lane-chapters")).toBeInTheDocument();
    expect(getByTestId("video-timeline-lane-bookmarks")).toBeInTheDocument();
    expect(getByTestId("video-timeline-lane-issues")).toBeInTheDocument();
    expect(getByTestId("video-timeline-lane-predictions")).toBeInTheDocument();
    expect(getByTestId("video-timeline-lane-track")).toBeInTheDocument();
    expect(getByTestId("video-timeline-lane-propagation")).toBeInTheDocument();
    expect(getByTestId("video-timeline-lane-loop")).toBeInTheDocument();
    expect(getByText("问题")).toBeInTheDocument();
    expect(getByText("所选轨迹")).toBeInTheDocument();
    expect(getByText("AI 影响范围")).toBeInTheDocument();
    expect(getByText("循环区间")).toBeInTheDocument();
  });

  it("keeps the full-window selection visible and carries zoom context into the collapsed summary", () => {
    const { getByTestId } = renderOverlay({ maxFrame: 100 });

    const collapsedSelection = getByTestId("video-timeline-collapsed-window-selection");
    expect(collapsedSelection).toHaveAttribute("data-full-window", "true");
    expect(collapsedSelection.style.getPropertyValue("--timeline-left")).toBe("0%");
    expect(collapsedSelection.style.getPropertyValue("--timeline-width")).toBe("100%");

    fireEvent.click(getByTestId("video-timeline-toggle"));
    expect(getByTestId("video-timeline-navigator")).toBeInTheDocument();
    expect(getByTestId("video-timeline-navigator-window")).toHaveAttribute(
      "data-full-window",
      "true",
    );
    expect(getByTestId("video-timeline-window-readout")).toHaveTextContent("窗口：全部 · F0–100");

    fireEvent.click(getByTestId("video-timeline-zoom-in"));
    expect(getByTestId("video-timeline-navigator")).toBeInTheDocument();
    expect(getByTestId("video-timeline-navigator-window")).toHaveAttribute(
      "data-full-window",
      "false",
    );
    expect(getByTestId("video-timeline-window-readout")).toHaveTextContent("窗口：F20–80");

    fireEvent.click(getByTestId("video-timeline-toggle"));
    const zoomedCollapsedSelection = getByTestId("video-timeline-collapsed-window-selection");
    expect(zoomedCollapsedSelection).toHaveAttribute("data-full-window", "false");
    expect(zoomedCollapsedSelection.style.getPropertyValue("--timeline-left")).toBe("20%");
    expect(zoomedCollapsedSelection.style.getPropertyValue("--timeline-width")).toBe("60%");
    expect(getByTestId("video-timeline-window-readout")).toHaveTextContent("窗口：F20–80");

    fireEvent.click(getByTestId("video-timeline-toggle"));
    fireEvent.click(getByTestId("video-timeline-zoom-reset"));
    expect(getByTestId("video-timeline-navigator")).toBeInTheDocument();
    expect(getByTestId("video-timeline-navigator-window")).toHaveAttribute(
      "data-full-window",
      "true",
    );
    expect(getByTestId("video-timeline-window-readout")).toHaveTextContent("窗口：全部 · F0–100");
  });

  it("keeps collapsed metadata on a separate row so the timeline remains dominant", () => {
    expect(overlayCss).toMatch(
      /\.overlayCollapsed\s*\{[^}]*grid-template-areas:\s*"play timeline toggle"\s*"status status status"/s,
    );
    expect(overlayCss).toMatch(
      /\.collapsedTimelineShell\s*\{[^}]*grid-area:\s*timeline;[^}]*min-width:\s*280px;/s,
    );
  });

  it("keeps manual and prediction density visible when a track is selected", () => {
    const selectedTrackTimeline: VideoTrackTimeline = {
      trackId: "trk",
      keyframes: [{ frame: 0, source: "manual", occluded: false }],
      outside: [],
      interpolated: [],
    };
    const { getByTestId } = renderOverlay({
      selectedTrackTimeline,
      globalTimelineDensity: [{ index: 0, from: 0, to: 0, density: 1, tracks: [] }],
      predictionDensity: [{ index: 1, from: 1, to: 1, count: 1 }],
    });

    fireEvent.click(getByTestId("video-timeline-toggle"));

    expect(getByTestId("video-timeline-density").querySelector("span")).toBeInTheDocument();
    expect(
      getByTestId("video-timeline-prediction-density").querySelector("span"),
    ).toBeInTheDocument();
    expect(getByTestId("video-track-timeline")).toBeInTheDocument();
  });

  it("keeps sampling grid markers away from timeline edges", () => {
    const { queryAllByTestId } = renderOverlay({ maxFrame: 9, samplingStep: 3 });

    expect(queryAllByTestId("video-timeline-grid-tick")).toHaveLength(2);
  });

  it("hides the off-grid helper marker during playback", () => {
    const playing = renderOverlay({ frameIndex: 1, samplingStep: 3, isPlaying: true });

    expect(playing.queryByTestId("video-timeline-offgrid-marker")).toBeNull();
    playing.unmount();

    const paused = renderOverlay({ frameIndex: 1, samplingStep: 3, isPlaying: false });

    expect(paused.getByTestId("video-timeline-offgrid-marker")).toBeInTheDocument();
  });

  it("reports hover frame changes and renders ready frame previews", () => {
    const onHoverFrameChange = vi.fn();
    const { getByLabelText, getByTestId } = renderOverlay({
      hoverPreview: {
        frameIndex: 5,
        status: "ready",
        url: "/frame-5.webp",
        width: 320,
        format: "webp",
        error: null,
      },
      onHoverFrameChange,
    });
    getByLabelText("视频帧时间轴");
    const shell = getByTestId("video-timeline-shell");
    setRect(shell);

    fireEvent(shell, pointerMove(560));

    expect(onHoverFrameChange).toHaveBeenCalledWith(5);
    expect(getByTestId("video-frame-preview-popover")).toHaveTextContent("F 5");
    expect(getByTestId("video-frame-preview-image")).toHaveAttribute("src", "/frame-5.webp");
  });

  it("does not report duplicate hover changes for the same frame", () => {
    const onHoverFrameChange = vi.fn();
    const { getByTestId } = renderOverlay({ onHoverFrameChange });
    const shell = getByTestId("video-timeline-shell");
    setRect(shell);

    fireEvent(shell, pointerMove(560));
    fireEvent(shell, pointerMove(561));

    expect(onHoverFrameChange).toHaveBeenCalledTimes(1);
    expect(onHoverFrameChange).toHaveBeenCalledWith(5);
  });

  it("seeks from the timeline shell instead of relying on native range pointer focus", () => {
    const onSeek = vi.fn();
    const { getByTestId } = renderOverlay({ onSeek });
    const shell = getByTestId("video-timeline-shell");
    setRect(shell);

    fireEvent(shell, pointerDown(560));

    expect(onSeek).toHaveBeenCalledWith(5);
  });

  it("renders pending and error preview fallbacks without hiding frame context", () => {
    const { getByLabelText, getByTestId, rerender } = renderOverlay({
      hoverPreview: {
        frameIndex: 4,
        status: "pending",
        url: null,
        width: 320,
        format: "webp",
        error: null,
      },
    });
    getByLabelText("视频帧时间轴");
    const shell = getByTestId("video-timeline-shell");
    setRect(shell);

    fireEvent(shell, pointerMove(440));

    expect(getByTestId("video-frame-preview-popover")).toHaveTextContent("Loading F 4");

    rerender(
      <VideoPlaybackOverlay
        frameIndex={0}
        maxFrame={9}
        timebase={timebase}
        isPlaying={false}
        currentFrameEntryCount={0}
        visible
        hoverPreview={{
          frameIndex: 4,
          status: "error",
          url: null,
          width: 320,
          format: "webp",
          error: "failed",
        }}
        onSeek={() => {}}
        onSeekByFrames={() => {}}
        onTogglePlay={() => {}}
      />,
    );

    fireEvent(shell, pointerMove(440));

    expect(getByTestId("video-frame-preview-popover")).toHaveTextContent("Preview unavailable");
    expect(getByTestId("video-frame-preview-popover")).toHaveTextContent("F 4");
  });

  it("uses overlay frame stepping for range arrow keys", () => {
    const onSeekByFrames = vi.fn();
    const { getByLabelText } = renderOverlay({ onSeekByFrames });
    const range = getByLabelText("视频帧时间轴");

    const dispatched = fireEvent.keyDown(range, { key: "ArrowRight" });
    fireEvent.keyDown(range, { key: "ArrowLeft", shiftKey: true });

    expect(dispatched).toBe(false);
    expect(onSeekByFrames).toHaveBeenNthCalledWith(1, 1);
    expect(onSeekByFrames).toHaveBeenNthCalledWith(2, -10);
  });

  it("uses configured large frame step for Shift + range arrow keys", () => {
    const onSeekByFrames = vi.fn();
    const { getByLabelText } = renderOverlay({
      largeFrameStep: "grid",
      samplingStep: 5,
      onSeekByFrames,
    });
    const range = getByLabelText("视频帧时间轴");

    fireEvent.keyDown(range, { key: "ArrowRight", shiftKey: true });

    expect(onSeekByFrames).toHaveBeenCalledWith(5);
  });

  it("moves keyboard focus from the range to the timeline shell after pointer interaction", () => {
    const { getByLabelText, getByTestId } = renderOverlay();
    const shell = getByTestId("video-timeline-shell") as HTMLDivElement;
    const range = getByLabelText("视频帧时间轴") as HTMLInputElement;
    const focus = vi.spyOn(shell, "focus");
    const raf = vi.spyOn(window, "requestAnimationFrame").mockImplementation((cb) => {
      cb(0);
      return 1;
    });

    fireEvent.focus(range);
    fireEvent.pointerUp(range);

    expect(shell.tabIndex).toBe(0);
    expect(range.tabIndex).toBe(-1);
    expect(focus).toHaveBeenCalledTimes(2);
    raf.mockRestore();
  });

  it("captures arrow keys when the timeline shell owns focus", () => {
    const onSeekByFrames = vi.fn();
    const { getByTestId } = renderOverlay({ onSeekByFrames });
    const shell = getByTestId("video-timeline-shell") as HTMLDivElement;

    shell.focus();
    fireEvent.keyDown(window, { key: "ArrowRight" });
    fireEvent.keyDown(window, { key: "ArrowLeft", shiftKey: true });

    expect(onSeekByFrames).toHaveBeenNthCalledWith(1, 1);
    expect(onSeekByFrames).toHaveBeenNthCalledWith(2, -10);
  });

  // 区间选择基建: 默认 purpose="loop" 时 Shift+拖仍提交循环区间 (零回归)。
  it("commits a loop region on Shift+drag by default (range-select purpose loop)", () => {
    const onLoopRegionChange = vi.fn();
    const onRangeSelect = vi.fn();
    const { getByTestId } = renderOverlay({ onLoopRegionChange, onRangeSelect });
    const shell = getByTestId("video-timeline-shell");
    setRect(shell);

    fireEvent(shell, pointerDown(100, true));
    expect(getByTestId("video-loop-region-preview")).toBeInTheDocument();
    fireEvent(shell, pointerMove(600));
    fireEvent(shell, pointerUp(600));

    expect(onLoopRegionChange).toHaveBeenCalledWith({ startFrame: 1, endFrame: 5 });
    expect(onRangeSelect).not.toHaveBeenCalled();
  });

  // 区间选择基建: purpose="chapter-draft" 时 Shift+拖走通用 onRangeSelect, 草稿带用章节样式。
  it("routes a chapter-draft brush to onRangeSelect with the purpose tag", () => {
    const onLoopRegionChange = vi.fn();
    const onRangeSelect = vi.fn();
    const { getByTestId } = renderOverlay({
      rangeSelectPurpose: "chapter-draft",
      onLoopRegionChange,
      onRangeSelect,
    });
    const shell = getByTestId("video-timeline-shell");
    setRect(shell);

    fireEvent(shell, pointerDown(200, true));
    expect(getByTestId("video-timeline-chapter-draft")).toBeInTheDocument();
    fireEvent(shell, pointerMove(900));
    fireEvent(shell, pointerUp(900));

    expect(onRangeSelect).toHaveBeenCalledWith("chapter-draft", { startFrame: 2, endFrame: 8 });
    expect(onLoopRegionChange).not.toHaveBeenCalled();
  });

  // propagate-range 用途保留普通拖 seek, 仅 Shift+拖才圈选 (对话框开着仍能 scrub 预览)。
  it("keeps plain-drag seek under propagate-range purpose; only Shift+drag brushes", () => {
    const onSeek = vi.fn();
    const onRangeSelect = vi.fn();
    const { getByTestId } = renderOverlay({
      rangeSelectPurpose: "propagate-range",
      onSeek,
      onRangeSelect,
    });
    const shell = getByTestId("video-timeline-shell");
    setRect(shell);

    // 普通拖 → seek, 不圈选。
    fireEvent(shell, pointerDown(300));
    expect(onSeek).toHaveBeenCalledWith(3);
    expect(onRangeSelect).not.toHaveBeenCalled();
    fireEvent(shell, pointerUp(300));

    // Shift+拖 → 圈选 propagate-range。
    fireEvent(shell, pointerDown(200, true));
    fireEvent(shell, pointerMove(800));
    fireEvent(shell, pointerUp(800));
    expect(onRangeSelect).toHaveBeenCalledWith("propagate-range", { startFrame: 2, endFrame: 7 });
  });

  it.each([false, true])(
    "gives an explicitly armed chapter brush priority over a retained propagation session (expanded=%s)",
    (expanded) => {
      const onSeek = vi.fn();
      const onRangeSelect = vi.fn();
      const { getByTestId } = renderOverlay({
        rangeSelectPurpose: resolveVideoTimelineRangePurpose(true, true),
        onSeek,
        onRangeSelect,
      });
      if (expanded) fireEvent.click(getByTestId("video-timeline-toggle"));
      const shell = getByTestId("video-timeline-shell");
      setRect(shell);

      fireEvent(shell, pointerDown(200));
      fireEvent(shell, pointerMove(600));
      fireEvent(shell, pointerUp(600));

      expect(onSeek).not.toHaveBeenCalled();
      expect(onRangeSelect).toHaveBeenCalledTimes(1);
      expect(onRangeSelect).toHaveBeenCalledWith("chapter-draft", {
        startFrame: 2,
        endFrame: 5,
      });
    },
  );

  it("returns Shift+drag to propagation after the chapter arm is cleared", () => {
    const onSeek = vi.fn();
    const onRangeSelect = vi.fn();
    const overlay = (chapterDraftArmed: boolean) => (
      <VideoPlaybackOverlay
        frameIndex={0}
        maxFrame={9}
        timebase={timebase}
        isPlaying={false}
        currentFrameEntryCount={0}
        visible
        onSeek={onSeek}
        onSeekByFrames={() => {}}
        onTogglePlay={() => {}}
        rangeSelectPurpose={resolveVideoTimelineRangePurpose(chapterDraftArmed, true)}
        onRangeSelect={onRangeSelect}
      />
    );
    const { getByTestId, rerender } = render(overlay(true));
    fireEvent.click(getByTestId("video-timeline-toggle"));
    const shell = getByTestId("video-timeline-shell");
    setRect(shell);
    fireEvent(shell, pointerDown(200));
    fireEvent(shell, pointerMove(600));
    fireEvent(shell, pointerUp(600));
    expect(onRangeSelect).toHaveBeenCalledWith("chapter-draft", {
      startFrame: 2,
      endFrame: 5,
    });
    onRangeSelect.mockClear();

    // The shell clears its chapter arm when the user opens propagation again.
    rerender(overlay(false));
    fireEvent(shell, pointerDown(300, true));
    fireEvent(shell, pointerMove(900));
    fireEvent(shell, pointerUp(900));

    expect(onSeek).not.toHaveBeenCalled();
    expect(onRangeSelect).toHaveBeenCalledTimes(1);
    expect(onRangeSelect).toHaveBeenCalledWith("propagate-range", {
      startFrame: 3,
      endFrame: 8,
    });
    fireEvent(shell, pointerDown(400));
    fireEvent(shell, pointerUp(400));
    expect(onSeek).toHaveBeenCalledWith(4);
  });

  // 拖章节条右边界 → 松手 onChapterResize 落新起止帧 (拖动中本地预览)。
  it("resizes a chapter by dragging its end handle and commits on pointer up", () => {
    const onChapterResize = vi.fn();
    const { getByTestId } = renderOverlay({
      chapters: [{ id: "ch1", startFrame: 1, endFrame: 5, title: "A", color: null }],
      onChapterResize,
    });
    const shell = getByTestId("video-timeline-shell");
    setRect(shell);
    const endHandle = getByTestId("video-chapter-resize-end");

    fireEvent(endHandle, pointerDown(500));
    fireEvent(endHandle, pointerMove(900));
    // 拖动中不落库, 仅预览。
    expect(onChapterResize).not.toHaveBeenCalled();
    fireEvent(endHandle, pointerUp(900));

    expect(onChapterResize).toHaveBeenCalledWith("ch1", { startFrame: 1, endFrame: 8 });
  });

  it("does not render resize handles without an onChapterResize handler", () => {
    const { queryByTestId } = renderOverlay({
      chapters: [{ id: "ch1", startFrame: 1, endFrame: 5, title: "A", color: null }],
    });
    expect(queryByTestId("video-chapter-resize-end")).toBeNull();
  });

  // AI 传播对话框打开时在时间轴受控高亮影响范围。
  it("renders the propagate range highlight when provided", () => {
    const { getByTestId, queryByTestId, rerender } = renderOverlay();
    expect(queryByTestId("video-propagate-range")).toBeNull();
    rerender(
      <VideoPlaybackOverlay
        frameIndex={0}
        maxFrame={9}
        timebase={timebase}
        isPlaying={false}
        currentFrameEntryCount={0}
        visible
        propagateRange={{ startFrame: 2, endFrame: 7 }}
        onSeek={() => {}}
        onSeekByFrames={() => {}}
        onTogglePlay={() => {}}
      />,
    );
    const band = getByTestId("video-propagate-range");
    expect(band.style.getPropertyValue("--timeline-left")).toBe(`${(2 / 9) * 100}%`);
  });

  it("keeps active range feedback in the collapsed timeline summary", () => {
    const { getByTestId } = renderOverlay({
      loopRegion: { startFrame: 1, endFrame: 5 },
      propagateRange: { startFrame: 2, endFrame: 7 },
    });
    const summary = getByTestId("video-timeline-summary-overlays");

    expect(within(summary).getByTestId("video-loop-region")).toBeInTheDocument();
    expect(within(summary).getByTestId("video-propagate-range")).toBeInTheDocument();
  });

  it("keeps review scope visible in compact and expanded modes with independent navigation", async () => {
    const user = userEvent.setup();
    const onSeekReviewFrame = vi.fn();
    const onSeek = vi.fn();
    const onLoopRegionChange = vi.fn();
    const onRangeSelect = vi.fn();
    const { getByTestId, getAllByTestId } = renderOverlay({
      trackerReview: reviewProjection(),
      onSeekReviewFrame,
      onSeek,
      onLoopRegionChange,
      onRangeSelect,
      loopRegion: { startFrame: 1, endFrame: 5 },
      propagateRange: { startFrame: 4, endFrame: 8 },
      chapters: [{ id: "chapter-1", startFrame: 3, endFrame: 6, title: "章节" }],
    });
    const expectSummary = () => {
      expect(getAllByTestId("timeline-tracker-review-scope")).toHaveLength(1);
      expect(getByTestId("timeline-tracker-review-scope")).toHaveTextContent(
        "审阅 1 个目标 · F2–F7 · 所选待审 2 · 全部待审 3",
      );
      expect(getByTestId("timeline-tracker-review-scope")).toHaveAttribute(
        "data-review-job-id",
        "review-job",
      );
    };
    expectSummary();
    await user.click(getByTestId("timeline-tracker-review-remaining-0-0"));
    expect(onSeekReviewFrame).toHaveBeenCalledWith(0);
    await user.click(getByTestId("video-timeline-toggle"));
    expectSummary();
    const lane = getByTestId("video-timeline-lane-tracker-review");
    const band = within(lane).getByTestId("timeline-tracker-review-window");
    expect(band.style.getPropertyValue("--timeline-left")).toBe(`${(2 / 9) * 100}%`);
    expect(getByTestId("video-timeline-lane-loop")).toBeInTheDocument();
    expect(getByTestId("video-timeline-lane-propagation")).toBeInTheDocument();
    expect(getByTestId("video-timeline-chapter")).toBeInTheDocument();
    await user.click(getByTestId("timeline-tracker-review-remaining-7-7"));
    expect(onSeekReviewFrame).toHaveBeenLastCalledWith(7);
    expect(onSeek).not.toHaveBeenCalled();
    expect(onLoopRegionChange).not.toHaveBeenCalled();
    expect(onRangeSelect).not.toHaveBeenCalled();
  });

  it("uses the owner projection when the inspected frame changes and when the review job changes", () => {
    const trackerReview = reviewProjection();
    const base = {
      frameIndex: 0,
      maxFrame: 9,
      timebase,
      isPlaying: false,
      currentFrameEntryCount: 0,
      visible: true,
      onSeek: vi.fn(),
      onSeekByFrames: vi.fn(),
      onTogglePlay: vi.fn(),
      onSeekReviewFrame: vi.fn(),
      trackerReview,
    };
    const { rerender, getByTestId } = render(<VideoPlaybackOverlay {...base} />);
    rerender(<VideoPlaybackOverlay {...base} frameIndex={9} currentFrameEntryCount={4} />);
    expect(getByTestId("timeline-tracker-review-scope")).toHaveTextContent(
      "审阅 1 个目标 · F2–F7 · 所选待审 2 · 全部待审 3",
    );
    const nextReview = projectTrackerReview(
      { ...trackerReview.preview, job_id: "other-job" },
      { instanceIds: ["B"], fromFrame: 0, toFrame: 1, intentRevision: 2 },
      1,
    );
    rerender(<VideoPlaybackOverlay {...base} trackerReview={nextReview} />);
    expect(getByTestId("timeline-tracker-review-scope")).toHaveTextContent(
      "审阅 1 个目标 · F0–F1 · 所选待审 1 · 全部待审 3",
    );
    expect(getByTestId("timeline-tracker-review-scope")).toHaveAttribute(
      "data-review-job-id",
      "other-job",
    );
    expect(base.onSeekReviewFrame).not.toHaveBeenCalled();
    expect(base.onSeek).not.toHaveBeenCalled();
  });

  it.each([0, 9])("shows a single-frame review window at source-frame endpoint F%s", (frame) => {
    const initial = reviewProjection();
    const trackerReview = projectTrackerReview(
      initial.preview,
      { ...initial.scope, fromFrame: frame, toFrame: frame },
      1,
    );
    const { getByTestId } = renderOverlay({ trackerReview });
    fireEvent.click(getByTestId("video-timeline-toggle"));
    const band = getByTestId("timeline-tracker-review-window");
    const left = parseFloat(band.style.getPropertyValue("--timeline-left"));
    const width = parseFloat(band.style.getPropertyValue("--timeline-width"));
    expect(width).toBeGreaterThan(0);
    expect(left).toBeGreaterThanOrEqual(0);
    expect(left + width).toBeLessThanOrEqual(100);
    expect(getByTestId("timeline-tracker-review-scope")).toHaveTextContent(`F${frame}–F${frame}`);
  });

  it.each([false, true])(
    "review links require their guarded callback and an interactive overlay (%s)",
    (interactive) => {
      const onSeek = vi.fn();
      const onSeekReviewFrame = interactive ? undefined : vi.fn();
      const { getByTestId } = renderOverlay({
        trackerReview: reviewProjection(),
        interactive,
        onSeek,
        onSeekReviewFrame,
      });
      const link = getByTestId("timeline-tracker-review-remaining-0-0");
      expect(link).toBeDisabled();
      fireEvent.click(link);
      expect(onSeek).not.toHaveBeenCalled();
      if (onSeekReviewFrame) expect(onSeekReviewFrame).not.toHaveBeenCalled();
    },
  );

  it("review feedback leaves source-frame seeking and propagation brushing with their existing owners", () => {
    const onSeek = vi.fn();
    const onRangeSelect = vi.fn();
    const onSeekReviewFrame = vi.fn();
    const { getByTestId } = renderOverlay({
      trackerReview: reviewProjection(),
      rangeSelectPurpose: "propagate-range",
      onSeek,
      onRangeSelect,
      onSeekReviewFrame,
    });
    const shell = getByTestId("video-timeline-shell");
    setRect(shell);
    expect(shell.closest("[data-workbench-tracker-review]")).toBeNull();
    fireEvent(shell, pointerDown(300));
    fireEvent(shell, pointerUp(300));
    expect(onSeek).toHaveBeenCalledWith(3);
    fireEvent(shell, pointerDown(200, true));
    fireEvent(shell, pointerMove(800));
    fireEvent(shell, pointerUp(800));
    expect(onRangeSelect).toHaveBeenCalledWith("propagate-range", { startFrame: 2, endFrame: 7 });
    expect(onSeekReviewFrame).not.toHaveBeenCalled();
    expect(getByTestId("timeline-tracker-review-scope")).toHaveTextContent("F2–F7");
  });

  it("does not hide collapsed active-range feedback in CSS", () => {
    const hiddenSelectors = [...overlayCss.matchAll(/([^{}]+)\{[^{}]*display:\s*none;?[^{}]*\}/g)]
      .map((match) => match[1])
      .filter((selectors) => selectors.includes(".overlayCollapsed"))
      .join(",");

    expect(hiddenSelectors).not.toMatch(
      /\.(loopRegion|propagateRegion|chapterDraftRegion|propagateDraftRegion)\b/,
    );
  });

  // 时间轴章节条 hover 上报 + 受控高亮 (与侧栏行双向联动)。
  it("reports chapter hover and reflects the controlled hovered chapter", () => {
    const onHoverChapter = vi.fn();
    const { getByTestId, rerender } = renderOverlay({
      chapters: [{ id: "ch1", startFrame: 1, endFrame: 5, title: "A", color: null }],
      onHoverChapter,
    });
    const bar = getByTestId("video-timeline-chapter");
    fireEvent.pointerEnter(bar);
    expect(onHoverChapter).toHaveBeenCalledWith("ch1");
    fireEvent.pointerLeave(bar);
    expect(onHoverChapter).toHaveBeenCalledWith(null);

    rerender(
      <VideoPlaybackOverlay
        frameIndex={0}
        maxFrame={9}
        timebase={timebase}
        isPlaying={false}
        currentFrameEntryCount={0}
        visible
        chapters={[{ id: "ch1", startFrame: 1, endFrame: 5, title: "A", color: null }]}
        hoveredChapterId="ch1"
        onSeek={() => {}}
        onSeekByFrames={() => {}}
        onTogglePlay={() => {}}
      />,
    );
    expect(getByTestId("video-timeline-chapter")).toHaveAttribute("data-hovered", "true");
  });

  // 回归: 人工关键帧密度与 AI 候选密度必须共用同一计数基准, 柱高才真实反映数量占比。
  // 修复前两条 lane 各自独立归一化, 会把「1 个关键帧」画得比「8 个候选」还高 (倒挂)。
  it("scales manual and prediction density on a shared count basis so bar height reflects real ratio", () => {
    const px = (el: HTMLElement) => parseFloat(el.style.getPropertyValue("--density-height"));
    const { getByTestId } = renderOverlay({
      globalTimelineDensity: [{ index: 0, from: 0, to: 0, density: 1, tracks: [] }],
      predictionDensity: [{ index: 0, from: 0, to: 0, count: 8 }],
    });

    const manualBin = getByTestId("video-timeline-density").querySelector("span") as HTMLElement;
    const predBin = getByTestId("video-timeline-prediction-density").querySelector(
      "span",
    ) as HTMLElement;

    // 8 个候选的柱必须显著高于 1 个关键帧的柱 (共享 max=8 下: 9px vs 2px), 不再倒挂。
    expect(px(predBin)).toBeGreaterThan(px(manualBin));
    expect(px(predBin)).toBeGreaterThan(px(manualBin) * 3);
  });
});

describe("densityBinGradient", () => {
  const bin = (
    density: number,
    tracks: VideoTimelineDensityBin["tracks"],
  ): VideoTimelineDensityBin => ({ index: 0, from: 0, to: 1, density, tracks });

  it("fills with accent when bin has density but no track ownership (legacy bbox)", () => {
    const gradient = densityBinGradient(bin(3, []));

    expect(gradient).toMatch(/^linear-gradient\(to top, /);
    // 没有任何 track 段, 只有 accent 兜底覆盖 0%~100%
    const stops = gradient.match(/color-mix\([^)]+\)/g) ?? [];
    expect(stops).toHaveLength(2);
    expect(stops[0]).toContain("var(--sc-brand)");
    expect(gradient).toContain("0.00%");
    expect(gradient).toContain("100%");
  });

  it("tops up the remainder with accent when tracks cover only part of the density", () => {
    // density=4, 但只有 2 个 track keyframe (差额 2 是 legacy bbox)
    const gradient = densityBinGradient(bin(4, [{ trackId: "t1", count: 2 }]));

    expect(gradient).toContain(densityHelpers.color("t1"));
    expect(gradient).toContain("var(--sc-brand)");
    // t1 占 0%~50%, accent 占 50%~100%
    expect(gradient).toContain("0.00%");
    expect(gradient).toContain("50.00%");
    expect(gradient).toContain("100%");
  });

  it("respects overrides when provided", () => {
    const overrides = { t1: "oklch(0.7 0.2 200)" };
    const gradient = densityBinGradient(bin(2, [{ trackId: "t1", count: 2 }]), overrides);

    expect(gradient).toContain("oklch(0.7 0.2 200)");
    // 完全覆盖时不应出现 accent 兜底
    expect(gradient).not.toContain("var(--sc-brand)");
  });

  it("stacks tracks in iteration order (stable for same-count tracks)", () => {
    // 两个轨迹同 count, 按传入顺序堆叠 (Map 插入序 + 稳定 sort)
    const gradient = densityBinGradient(
      bin(4, [
        { trackId: "tA", count: 2 },
        { trackId: "tB", count: 2 },
      ]),
    );

    const colorA = densityHelpers.color("tA");
    const colorB = densityHelpers.color("tB");
    // tA 应排在 tB 前 (自底向上: tA 从 0% 开始)
    const idxA = gradient.indexOf(colorA);
    const idxB = gradient.indexOf(colorB);
    expect(idxA).toBeGreaterThan(-1);
    expect(idxB).toBeGreaterThan(idxA);
    // 边界: 0% → 50% (tA) → 100% (tB); 全覆盖时尾部 stop 是 pct(total) = "100.00%"
    expect(gradient).toContain("0.00%");
    expect(gradient).toContain("50.00%");
    expect(gradient).toContain("100.00%");
  });
});

describe("resolveLargeFrameStep", () => {
  it("resolves grid to sampling step, falling back to 10 without sampling grid", () => {
    expect(resolveLargeFrameStep("grid", 5)).toBe(5);
    expect(resolveLargeFrameStep("grid", 1)).toBe(10);
    expect(resolveLargeFrameStep(30, 5)).toBe(30);
  });
});

const densityHelpers = {
  color: (trackId: string) => getTrackColor(trackId, ""),
};

describe("Issue frame navigation", () => {
  it.each([false, true])(
    "routes Issue anchors through checked readiness (expanded=%s)",
    (expanded) => {
      const onSeek = vi.fn();
      const onSeekIssueFrame = vi.fn();
      const view = renderOverlay({ issueFrames: [3], onSeek, onSeekIssueFrame });
      if (expanded) fireEvent.click(view.getByRole("button", { name: "展开时间轴详情" }));
      fireEvent.click(view.getByTestId("video-issue-marker"));
      expect(onSeekIssueFrame).toHaveBeenCalledWith(3);
      expect(onSeek).not.toHaveBeenCalled();
    },
  );
});

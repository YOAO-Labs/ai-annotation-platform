import { niceFrameStep } from "./timelineCoords";
import { useEffect, useMemo, useRef, useState } from "react";
import type {
  ButtonHTMLAttributes,
  HTMLAttributes,
  KeyboardEvent as ReactKeyboardEvent,
  PointerEvent as ReactPointerEvent,
  Ref,
} from "react";
import { flushSync } from "react-dom";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import type { TrackerReviewProjection } from "@/hooks/videoTrackerReviewScope";
import { getTrackColor } from "./colors";
import { frameTimebaseDuration, frameToTime, type FrameTimebase } from "./frameTimebase";
import {
  frameToPct,
  pctToFrame,
  isFullWindow,
  zoomWindow,
  panWindow,
  clampWindow,
  MIN_VISIBLE_SPAN,
  ZOOM_WHEEL_K,
  type TimelineWindow,
} from "./timelineCoords";
import type { VideoBookmark, VideoLoopRegion } from "./videoNavigationState";
import type { VideoFramePreview } from "./useVideoFramePreview";
import { useVideoTimelineWindow } from "./useVideoTimelineWindow";
import type { VideoTimelineWindowControls } from "./videoStageControls";
import type {
  PredictionDensityBin,
  VideoTimelineDensityBin,
  VideoTrackTimeline,
} from "./videoTrackTimeline";
import styles from "./VideoPlaybackOverlay.module.css";

type HighlightAction = "prev" | "next" | "play" | null;
type CSSVars = Record<`--${string}`, string | number>;
type TimelineViewTransitionDocument = Document & {
  startViewTransition?: (update: () => void) => unknown;
};

const DENSITY_BAR_MAX_PX = 9;
export type VideoLargeFrameStep = 5 | 10 | 30 | "grid";
const DEFAULT_CHAPTER_COLORS = [
  "rgba(116, 158, 87, 0.82)",
  "rgba(173, 111, 44, 0.84)",
  "rgba(78, 98, 174, 0.84)",
];

export interface VideoTimelineChapter {
  id: string;
  startFrame: number;
  endFrame: number;
  title: string;
  color?: string | null;
}

export interface VideoSegmentTimelineRange {
  coreStartFrame: number;
  coreEndFrame: number;
  workStartFrame: number;
  workEndFrame: number;
}

export type TimelineRangePurpose = "loop" | "chapter-draft" | "propagate-range";

interface TimelineRangeDraft {
  region: VideoLoopRegion;
  purpose: TimelineRangePurpose;
}

export interface VideoTimelineChapterControls {
  rangeSelectPurpose: TimelineRangePurpose;
  onRangeSelect: (purpose: TimelineRangePurpose, region: VideoLoopRegion) => void;
  hoveredChapterId?: string | null;
  onHoverChapter?: (chapterId: string | null) => void;
  onResizeChapter?: (chapterId: string, region: VideoLoopRegion) => void;
}

const RANGE_DRAFT_TESTID: Record<TimelineRangePurpose, string> = {
  loop: "video-loop-region-preview",
  "chapter-draft": "video-timeline-chapter-draft",
  "propagate-range": "video-timeline-propagate-draft",
};

interface VideoPlaybackOverlayProps {
  sourceKey?: string;
  windowControlsRef?: Ref<VideoTimelineWindowControls>;
  onViewInteraction?: () => void;
  frameIndex: number;
  maxFrame: number;
  samplingStep?: number;
  largeFrameStep?: VideoLargeFrameStep;
  timebase: FrameTimebase;
  isPlaying: boolean;
  playbackRateLabel?: string;
  selectedTrackTimeline?: VideoTrackTimeline | null;
  trackColor?: string | null;
  globalTimelineDensity?: VideoTimelineDensityBin[];
  predictionDensity?: PredictionDensityBin[];
  onSeekPredicted?: (dir: -1 | 1) => void;
  trackColorOverrides?: Record<string, string>;
  loopRegion?: VideoLoopRegion | null;
  propagateRange?: VideoLoopRegion | null;
  trackerReview?: TrackerReviewProjection | null;
  onSeekReviewFrame?: (frameIndex: number) => void;
  segmentRange?: VideoSegmentTimelineRange | null;
  rangeSelectPurpose?: TimelineRangePurpose;
  bookmarks?: VideoBookmark[];
  chapters?: VideoTimelineChapter[];
  issueFrames?: number[];
  onSeekIssueFrame?: (frameIndex: number) => void;
  hoverPreview?: VideoFramePreview | null;
  currentFrameEntryCount: number;
  visible: boolean;
  interactive?: boolean;
  highlightAction?: HighlightAction;
  onSeek: (frameIndex: number) => void;
  onSeekByFrames: (delta: number) => void;
  onTogglePlay: () => void;
  onLoopRegionChange?: (region: VideoLoopRegion) => void;
  onClearLoopRegion?: () => void;
  onSeekBookmark?: (frameIndex: number) => void;
  onHoverFrameChange?: (frameIndex: number | null) => void;
  onSeekChapter?: (chapterId: string, frameIndex: number) => void;
  onRangeSelect?: (purpose: TimelineRangePurpose, region: VideoLoopRegion) => void;
  hoveredChapterId?: string | null;
  onHoverChapter?: (chapterId: string | null) => void;
  onChapterResize?: (chapterId: string, region: VideoLoopRegion) => void;
}

function formatTime(seconds: number) {
  if (!Number.isFinite(seconds)) return "00:00.000";
  const m = Math.floor(seconds / 60);
  const s = seconds - m * 60;
  return `${String(m).padStart(2, "0")}:${s.toFixed(3).padStart(6, "0")}`;
}

function cn(...classes: Array<string | false | null | undefined>) {
  return classes.filter(Boolean).join(" ");
}

function densityTint(color: string) {
  return `color-mix(in oklch, ${color} 68%, transparent)`;
}

export function resolveLargeFrameStep(
  largeFrameStep: VideoLargeFrameStep,
  samplingStep: number,
): number {
  if (largeFrameStep !== "grid") return largeFrameStep;
  return samplingStep > 1 ? samplingStep : 10;
}

export function densityBinGradient(
  bin: VideoTimelineDensityBin,
  overrides?: Record<string, string>,
): string {
  const total = bin.density > 0 ? bin.density : 1;
  const pct = (n: number) => `${((n / total) * 100).toFixed(2)}%`;
  const stops: string[] = [];
  let acc = 0;
  for (const share of bin.tracks) {
    const tint = densityTint(getTrackColor(share.trackId, "", overrides));
    const start = acc;
    acc += share.count;
    stops.push(`${tint} ${pct(start)}`, `${tint} ${pct(acc)}`);
  }
  if (acc < total) {
    const tint = densityTint("var(--sc-brand)");
    stops.push(`${tint} ${pct(acc)}`, `${tint} 100%`);
  }
  return `linear-gradient(to top, ${stops.join(", ")})`;
}

function useCssVars<T extends HTMLElement>(vars: CSSVars) {
  const ref = useRef<T>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    Object.entries(vars).forEach(([key, value]) => {
      el.style.setProperty(key, String(value));
    });
  }, [vars]);
  return ref;
}

function TimelineSpan({ vars, ...props }: HTMLAttributes<HTMLSpanElement> & { vars: CSSVars }) {
  const ref = useCssVars<HTMLSpanElement>(vars);
  return <span ref={ref} {...props} />;
}

function TimelineButton({
  vars,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { vars: CSSVars }) {
  const ref = useCssVars<HTMLButtonElement>(vars);
  return <button ref={ref} {...props} />;
}

function TimelineDiv({ vars, ...props }: HTMLAttributes<HTMLDivElement> & { vars: CSSVars }) {
  const ref = useCssVars<HTMLDivElement>(vars);
  return <div ref={ref} {...props} />;
}

export function VideoPlaybackOverlay({
  sourceKey = "",
  windowControlsRef,
  onViewInteraction,
  frameIndex,
  maxFrame,
  samplingStep = 1,
  largeFrameStep = 10,
  timebase,
  isPlaying,
  playbackRateLabel,
  selectedTrackTimeline = null,
  trackColor = null,
  globalTimelineDensity = [],
  predictionDensity = [],
  onSeekPredicted,
  trackColorOverrides,
  loopRegion = null,
  propagateRange = null,
  trackerReview = null,
  onSeekReviewFrame,
  segmentRange = null,
  rangeSelectPurpose = "loop",
  bookmarks = [],
  chapters = [],
  issueFrames = [],
  onSeekIssueFrame,
  hoverPreview = null,
  currentFrameEntryCount,
  visible,
  interactive = true,
  highlightAction = null,
  onSeek,
  onSeekByFrames,
  onTogglePlay,
  onLoopRegionChange,
  onClearLoopRegion,
  onSeekBookmark,
  onHoverFrameChange,
  onSeekChapter,
  onRangeSelect,
  hoveredChapterId = null,
  onHoverChapter,
  onChapterResize,
}: VideoPlaybackOverlayProps) {
  const [hoverFrame, setHoverFrame] = useState<number | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [rangeDraft, setRangeDraft] = useState<TimelineRangeDraft | null>(null);
  const [chapterResizePreview, setChapterResizePreview] = useState<{
    id: string;
    startFrame: number;
    endFrame: number;
  } | null>(null);
  const chapterResizeRef = useRef<{
    id: string;
    edge: "start" | "end";
    startFrame: number;
    endFrame: number;
  } | null>(null);
  const rangeDraftRef = useRef<TimelineRangeDraft | null>(null);
  const seekDragRef = useRef(false);
  const timelineShellRef = useRef<HTMLDivElement | null>(null);
  const overlayRef = useRef<HTMLDivElement | null>(null);
  const timelineToggleRef = useRef<HTMLButtonElement | null>(null);
  const restoreToggleFocusRef = useRef(false);
  const hoverFrameRef = useRef<number | null>(null);
  useEffect(() => {
    if (!restoreToggleFocusRef.current) return;
    timelineToggleRef.current?.focus({ preventScroll: true });
    restoreToggleFocusRef.current = false;
  }, [expanded]);
  const frameTooltip = useMemo(() => {
    if (hoverFrame === null) return null;
    return `F ${hoverFrame} · ${formatTime(frameToTime(hoverFrame, timebase))}`;
  }, [hoverFrame, timebase]);
  const densityScaleMax = useMemo(
    () =>
      Math.max(
        1,
        ...globalTimelineDensity.map((bin) => bin.density),
        ...predictionDensity.map((bin) => bin.count),
      ),
    [globalTimelineDensity, predictionDensity],
  );
  const minTimelineSpan = useMemo(() => {
    const densityBinSpan = Math.max(
      1,
      ...globalTimelineDensity.map((bin) => bin.to - bin.from + 1),
      ...predictionDensity.map((bin) => bin.to - bin.from + 1),
    );
    return Math.max(MIN_VISIBLE_SPAN, densityBinSpan * 6);
  }, [globalTimelineDensity, predictionDensity]);
  const { timelineWindow, timelineWindowRef, setTimelineWindow } = useVideoTimelineWindow({
    sourceKey,
    maxFrame,
    minSpan: minTimelineSpan,
    controlsRef: windowControlsRef,
    onInteraction: onViewInteraction,
  });
  const gridTicks = useMemo(() => {
    if (samplingStep <= 1 || maxFrame <= 0) return [];
    const from = Math.max(0, Math.floor(timelineWindow.from));
    const to = Math.min(maxFrame, Math.ceil(timelineWindow.to));
    const firstI = Math.ceil(from / samplingStep);
    const lastI = Math.floor(to / samplingStep);
    const visibleCount = Math.max(0, lastI - firstI + 1);
    const stride = visibleCount > 200 ? Math.ceil(visibleCount / 200) : 1;
    const ticks: number[] = [];
    for (let i = firstI; i <= lastI; i += stride) {
      const frame = i * samplingStep;
      if (frame <= 0 || frame >= maxFrame) continue;
      ticks.push(frame);
    }
    return ticks;
  }, [maxFrame, samplingStep, timelineWindow]);
  const rulerTicks = useMemo(() => {
    if (maxFrame <= 0) return [] as number[];
    const span = timelineWindow.to - timelineWindow.from;
    if (span <= 0) return [];
    const step = niceFrameStep(span);
    const first = Math.ceil(timelineWindow.from / step) * step;
    const ticks: number[] = [];
    for (let f = first; f <= timelineWindow.to; f += step) {
      if (f < 0 || f > maxFrame) continue;
      const pct = frameToPct(f, timelineWindow);
      if (f !== 0 && f !== maxFrame && (pct < 4 || pct > 96)) continue;
      ticks.push(f);
    }
    return ticks;
  }, [maxFrame, timelineWindow]);
  const rulerMinorTicks = useMemo(() => {
    if (maxFrame <= 0) return [] as number[];
    const span = timelineWindow.to - timelineWindow.from;
    if (span <= 0) return [];
    const step = Math.max(1, Math.round(niceFrameStep(span) / 5));
    const first = Math.ceil(timelineWindow.from / step) * step;
    const ticks: number[] = [];
    const maxTicks = 72;
    for (let f = first; f <= timelineWindow.to && ticks.length < maxTicks; f += step) {
      if (f <= 0 || f >= maxFrame) continue;
      ticks.push(f);
    }
    return ticks;
  }, [maxFrame, timelineWindow]);
  const currentFrameOffGrid = !isPlaying && samplingStep > 1 && frameIndex % samplingStep !== 0;
  const isZoomed = !isFullWindow(timelineWindow, maxFrame);
  const timeReadoutText = `${formatTime(frameToTime(frameIndex, timebase))} / ${formatTime(frameTimebaseDuration(timebase))}`;
  const windowReadoutText = isZoomed
    ? `窗口：F${Math.round(timelineWindow.from)}–${Math.round(timelineWindow.to)}`
    : `窗口：全部 · F0–${maxFrame}`;
  const resetTimelineWindow = () => setTimelineWindow({ from: 0, to: maxFrame });
  const zoomTimeline = (factor: number) =>
    setTimelineWindow((win) => zoomWindow(win, maxFrame, 0.5, factor, minTimelineSpan));

  const navRef = useRef<HTMLDivElement | null>(null);
  const navDragRef = useRef<{
    mode: "pan" | "left" | "right";
    startX: number;
    startWin: TimelineWindow;
  } | null>(null);
  const navPct = (frame: number) => (maxFrame > 0 ? (frame / maxFrame) * 100 : 0);
  const beginNavDrag = (e: ReactPointerEvent<HTMLDivElement>) => {
    const el = navRef.current;
    if (!el || maxFrame <= 0) return;
    e.preventDefault();
    e.stopPropagation();
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0) return;
    const px = e.clientX - rect.left;
    const leftPx = (timelineWindow.from / maxFrame) * rect.width;
    const rightPx = (timelineWindow.to / maxFrame) * rect.width;
    const EDGE = 7;
    let mode: "pan" | "left" | "right";
    let startWin = timelineWindow;
    if (Math.abs(px - leftPx) <= EDGE) mode = "left";
    else if (Math.abs(px - rightPx) <= EDGE) mode = "right";
    else if (px >= leftPx && px <= rightPx) mode = "pan";
    else {
      const span = timelineWindow.to - timelineWindow.from;
      const clickedFrame = (px / rect.width) * maxFrame;
      startWin = clampWindow(
        { from: clickedFrame - span / 2, to: clickedFrame + span / 2 },
        maxFrame,
        minTimelineSpan,
      );
      setTimelineWindow(startWin);
      mode = "pan";
    }
    navDragRef.current = { mode, startX: e.clientX, startWin };
    el.setPointerCapture?.(e.pointerId);
  };
  const moveNavDrag = (e: ReactPointerEvent<HTMLDivElement>) => {
    const st = navDragRef.current;
    const el = navRef.current;
    if (!st || !el) return;
    e.preventDefault();
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0) return;
    const deltaFrames = ((e.clientX - st.startX) / rect.width) * maxFrame;
    if (st.mode === "pan") {
      setTimelineWindow(panWindow(st.startWin, maxFrame, deltaFrames, minTimelineSpan));
    } else if (st.mode === "left") {
      const from = Math.max(
        0,
        Math.min(st.startWin.from + deltaFrames, st.startWin.to - minTimelineSpan),
      );
      setTimelineWindow({ from, to: st.startWin.to });
    } else {
      const to = Math.min(
        maxFrame,
        Math.max(st.startWin.to + deltaFrames, st.startWin.from + minTimelineSpan),
      );
      setTimelineWindow({ from: st.startWin.from, to });
    }
  };
  const endNavDrag = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!navDragRef.current) return;
    navDragRef.current = null;
    e.currentTarget.releasePointerCapture?.(e.pointerId);
  };
  const frameLeft = (frame: number) => `${frameToPct(frame, timelineWindow)}%`;
  const frameFromPointer = (clientX: number, rect: DOMRect) => {
    const pointerX = Number.isFinite(clientX) ? clientX : rect.left;
    const ratio = rect.width > 0 ? (pointerX - rect.left) / rect.width : 0;
    return pctToFrame(ratio, timelineWindow);
  };
  const beginChapterResize = (
    e: ReactPointerEvent<HTMLElement>,
    chapter: VideoTimelineChapter,
    edge: "start" | "end",
  ) => {
    e.preventDefault();
    e.stopPropagation();
    chapterResizeRef.current = {
      id: chapter.id,
      edge,
      startFrame: chapter.startFrame,
      endFrame: chapter.endFrame,
    };
    setChapterResizePreview({
      id: chapter.id,
      startFrame: chapter.startFrame,
      endFrame: chapter.endFrame,
    });
    e.currentTarget.setPointerCapture?.(e.pointerId);
  };
  const moveChapterResize = (e: ReactPointerEvent<HTMLElement>) => {
    const st = chapterResizeRef.current;
    const shell = timelineShellRef.current;
    if (!st || !shell) return;
    e.preventDefault();
    e.stopPropagation();
    const frame = frameFromPointer(e.clientX, shell.getBoundingClientRect());
    const next =
      st.edge === "start"
        ? { ...st, startFrame: Math.max(0, Math.min(frame, st.endFrame)) }
        : { ...st, endFrame: Math.min(maxFrame, Math.max(frame, st.startFrame)) };
    chapterResizeRef.current = next;
    setChapterResizePreview({ id: next.id, startFrame: next.startFrame, endFrame: next.endFrame });
  };
  const endChapterResize = (e: ReactPointerEvent<HTMLElement>) => {
    const st = chapterResizeRef.current;
    chapterResizeRef.current = null;
    setChapterResizePreview(null);
    e.currentTarget.releasePointerCapture?.(e.pointerId);
    if (!st) return;
    const original = chapters.find((c) => c.id === st.id);
    if (original && (original.startFrame !== st.startFrame || original.endFrame !== st.endFrame)) {
      onChapterResize?.(st.id, { startFrame: st.startFrame, endFrame: st.endFrame });
    }
  };
  const normalizeLoop = (from: number, to: number): VideoLoopRegion => ({
    startFrame: Math.min(from, to),
    endFrame: Math.max(from, to),
  });
  const updateHoverFrame = (nextFrame: number | null) => {
    if (hoverFrameRef.current === nextFrame) return;
    hoverFrameRef.current = nextFrame;
    setHoverFrame(nextFrame);
    onHoverFrameChange?.(nextFrame);
  };
  const rangeStyle = (from: number, to: number): CSSVars => {
    const rawLeft = frameToPct(from, timelineWindow);
    const rawRight = frameToPct(to, timelineWindow);
    if (rawRight <= 0 || rawLeft >= 100) {
      return { "--timeline-left": "0%", "--timeline-width": "0%" };
    }
    const left = Math.max(0, rawLeft);
    const right = Math.min(100, rawRight);
    return {
      "--timeline-left": `${left}%`,
      "--timeline-width": `${Math.max(0.5, right - left)}%`,
    };
  };
  const frameInWindow = (frame: number) =>
    frame >= timelineWindow.from && frame <= timelineWindow.to;
  const reviewRangeStyle = (from: number, to: number): CSSVars => {
    if (from === to && frameInWindow(from)) {
      return {
        "--timeline-left": `${Math.min(99.5, frameToPct(from, timelineWindow))}%`,
        "--timeline-width": "0.5%",
      };
    }
    return rangeStyle(from, to);
  };
  const binWindowStyle = (from: number, to: number): CSSVars | null => {
    const rawLeft = frameToPct(from, timelineWindow);
    const rawRight = frameToPct(to + 1, timelineWindow);
    if (rawRight <= 0 || rawLeft >= 100) return null;
    const left = Math.max(0, rawLeft);
    const right = Math.min(100, rawRight);
    return {
      "--timeline-left": `${left}%`,
      "--timeline-width": `${Math.max(0.5, right - left)}%`,
    };
  };
  const stepTimelineByKey = (e: ReactKeyboardEvent, target: HTMLElement) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return false;
    e.preventDefault();
    e.stopPropagation();
    const step = e.shiftKey ? resolveLargeFrameStep(largeFrameStep, samplingStep) : 1;
    onSeekByFrames(e.key === "ArrowRight" ? step : -step);
    target.focus({ preventScroll: true });
    return true;
  };
  const focusTimelineShell = (target: HTMLElement) => {
    const shell = target.closest('[data-testid="video-timeline-shell"]') as HTMLElement | null;
    if (!shell) return;
    window.requestAnimationFrame(() => {
      shell.focus({ preventScroll: true });
    });
  };
  useEffect(() => {
    if (!visible || !interactive) return;
    const onKeyDown = (e: KeyboardEvent) => {
      const shell = timelineShellRef.current;
      const active = document.activeElement;
      if (!(active instanceof HTMLElement) || !shell) return;
      const isTimelineFocus = active === shell || active.classList.contains("video-timeline-range");
      if (!isTimelineFocus) return;
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      e.preventDefault();
      e.stopPropagation();
      const step = e.shiftKey ? resolveLargeFrameStep(largeFrameStep, samplingStep) : 1;
      onSeekByFrames(e.key === "ArrowRight" ? step : -step);
    };
    window.addEventListener("keydown", onKeyDown, true);
    return () => window.removeEventListener("keydown", onKeyDown, true);
  }, [interactive, largeFrameStep, onSeekByFrames, samplingStep, visible]);
  const hoverLeft = frameToPct(hoverFrame ?? 0, timelineWindow);
  const hoverPopoverLeft = `${Math.max(12, Math.min(88, hoverLeft))}%`;
  const currentFramePct = Math.max(0, Math.min(100, frameToPct(frameIndex, timelineWindow)));

  const isInteractive = visible && interactive;

  const handleShellKeyDown = (e: ReactKeyboardEvent<HTMLElement>) => {
    stepTimelineByKey(e, e.currentTarget);
  };
  const handleShellPointerDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (
      e.target instanceof Element &&
      e.target.closest("[data-chapter-resize], [data-workbench-issue-navigation]")
    )
      return;
    e.preventDefault();
    e.stopPropagation();
    const rect = e.currentTarget.getBoundingClientRect();
    const frame = frameFromPointer(e.clientX, rect);
    focusTimelineShell(e.currentTarget);
    const brushEnabled =
      rangeSelectPurpose === "loop" ? Boolean(onLoopRegionChange) : Boolean(onRangeSelect);
    const plainDragBrushes = rangeSelectPurpose === "chapter-draft";
    const wantsBrush = brushEnabled && (plainDragBrushes || e.shiftKey);
    if (!wantsBrush) {
      seekDragRef.current = true;
      onSeek(frame);
      e.currentTarget.setPointerCapture?.(e.pointerId);
      return;
    }
    const next: TimelineRangeDraft = {
      region: { startFrame: frame, endFrame: frame },
      purpose: rangeSelectPurpose,
    };
    rangeDraftRef.current = next;
    setRangeDraft(next);
    e.currentTarget.setPointerCapture?.(e.pointerId);
  };
  const handleShellPointerMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    const draft = rangeDraftRef.current;
    if (e.buttons > 0 && !seekDragRef.current && !draft) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const frame = frameFromPointer(e.clientX, rect);
    updateHoverFrame(frame);
    if (draft) {
      e.preventDefault();
      const next: TimelineRangeDraft = {
        region: normalizeLoop(draft.region.startFrame, frame),
        purpose: draft.purpose,
      };
      rangeDraftRef.current = next;
      setRangeDraft(next);
      return;
    }
    if (seekDragRef.current) {
      e.preventDefault();
      onSeek(frame);
    }
  };
  const handleShellPointerUp = (e: ReactPointerEvent<HTMLDivElement>) => {
    const draft = rangeDraftRef.current;
    if (draft) {
      e.preventDefault();
      if (draft.purpose === "loop") onLoopRegionChange?.(draft.region);
      else onRangeSelect?.(draft.purpose, draft.region);
      rangeDraftRef.current = null;
      setRangeDraft(null);
      e.currentTarget.releasePointerCapture?.(e.pointerId);
      return;
    }
    if (seekDragRef.current) {
      e.preventDefault();
      seekDragRef.current = false;
      focusTimelineShell(e.currentTarget);
      e.currentTarget.releasePointerCapture?.(e.pointerId);
    }
  };
  const handleShellPointerCancel = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!rangeDraftRef.current && !seekDragRef.current) return;
    seekDragRef.current = false;
    rangeDraftRef.current = null;
    setRangeDraft(null);
    e.currentTarget.releasePointerCapture?.(e.pointerId);
  };
  const handleShellPointerLeave = () => {
    if (seekDragRef.current || rangeDraftRef.current) return;
    updateHoverFrame(null);
  };
  const handleShellDoubleClick = () => {
    if (isZoomed) resetTimelineWindow();
  };

  useEffect(() => {
    const root = overlayRef.current;
    const shell = timelineShellRef.current;
    if (!root || !shell || !isInteractive || maxFrame <= 0) return;
    const onWheel = (e: WheelEvent) => {
      const rect = shell.getBoundingClientRect();
      if (rect.width <= 0) return;
      const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
      if (e.ctrlKey || e.metaKey) {
        e.preventDefault();
        const factor = Math.exp(e.deltaY * ZOOM_WHEEL_K);
        setTimelineWindow((win) => zoomWindow(win, maxFrame, ratio, factor, minTimelineSpan));
        return;
      }
      const win = timelineWindowRef.current;
      if (isFullWindow(win, maxFrame)) return;
      e.preventDefault();
      const span = win.to - win.from;
      const panPx = e.deltaX !== 0 ? e.deltaX : e.deltaY;
      const deltaFrames = (panPx / rect.width) * span;
      setTimelineWindow((prev) => panWindow(prev, maxFrame, deltaFrames, minTimelineSpan));
    };
    root.addEventListener("wheel", onWheel, { passive: false });
    return () => root.removeEventListener("wheel", onWheel);
  }, [expanded, isInteractive, maxFrame, minTimelineSpan, setTimelineWindow, timelineWindowRef]);

  const playbackRateText = playbackRateLabel ?? "1x";
  const hasPredictionDensity = predictionDensity.some((bin) => bin.count > 0);
  const showChapterLane = chapters.length > 0 || rangeDraft?.purpose === "chapter-draft";
  const showPropagationLane = Boolean(propagateRange) || rangeDraft?.purpose === "propagate-range";
  const showSegmentLane = Boolean(segmentRange);
  const showLoopLane = Boolean(loopRegion) || rangeDraft?.purpose === "loop";
  const reviewSummary = trackerReview && (
    <div
      data-workbench-tracker-review
      className={cn(
        "col-span-full flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 rounded bg-card px-2 py-1 text-2xs text-card-foreground",
        isInteractive && styles.interactive,
      )}
      onPointerDown={(event) => event.stopPropagation()}
      onMouseDown={(event) => event.stopPropagation()}
      onClick={(event) => event.stopPropagation()}
    >
      <span data-testid="timeline-tracker-review-scope" data-review-job-id={trackerReview.jobId}>
        审阅 {trackerReview.scope.instanceIds.length} 个目标 · F{trackerReview.scope.fromFrame}–F
        {trackerReview.scope.toFrame} · 所选待审 {trackerReview.selectedPending} · 全部待审{" "}
        {trackerReview.jobPending}
      </span>
      {trackerReview.remainingIntervals.length > 0 && (
        <div className="flex min-w-0 max-w-full items-center gap-1 overflow-x-auto">
          <span className="shrink-0 text-muted-foreground">剩余</span>
          {trackerReview.remainingIntervals.map(({ fromFrame, toFrame }) => (
            <Button
              key={`${fromFrame}:${toFrame}`}
              type="button"
              size="xs"
              variant="ghost"
              disabled={!isInteractive || !onSeekReviewFrame}
              data-testid={`timeline-tracker-review-remaining-${fromFrame}-${toFrame}`}
              aria-label={`查看剩余区间 F${fromFrame}–F${toFrame}`}
              onClick={() => onSeekReviewFrame?.(fromFrame)}
              className="shrink-0 tabular-nums"
            >
              F{fromFrame}–F{toFrame}
            </Button>
          ))}
        </div>
      )}
    </div>
  );
  const toggleTimelineDetails = () => {
    restoreToggleFocusRef.current = true;
    const update = () => setExpanded((value) => !value);
    const transitionDocument = document as TimelineViewTransitionDocument;
    const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
    if (!reduceMotion && transitionDocument.startViewTransition) {
      transitionDocument.startViewTransition(() => flushSync(update));
      return;
    }
    update();
  };

  const timelineToggleButton = (
    <Button
      key="video-timeline-toggle"
      ref={timelineToggleRef}
      size={expanded ? "xs" : "sm"}
      title={expanded ? "收起时间轴详情" : "展开时间轴详情"}
      aria-label={expanded ? "收起时间轴详情" : "展开时间轴详情"}
      aria-expanded={expanded}
      aria-controls="video-timeline-details"
      data-testid="video-timeline-toggle"
      onClick={toggleTimelineDetails}
      className={cn(styles.controlButton, styles.timelineToggleButton)}
    >
      <Icon name={expanded ? "chevDown" : "chevUp"} size={13} />
    </Button>
  );

  const transportControls = (
    <div
      data-testid="video-playback-controls"
      className={cn(styles.expandedControls, isInteractive && styles.interactive)}
    >
      <Button
        size="xs"
        title="回到首帧"
        aria-label="回到首帧"
        onClick={() => onSeek(0)}
        className={cn(styles.controlButton, styles.skipButton, styles.skipStart)}
      >
        <Icon name="chevLeft" size={14} />
      </Button>
      <Button
        size="xs"
        title="上一帧"
        aria-label="上一帧"
        onClick={() => onSeekByFrames(-1)}
        className={cn(
          styles.controlButton,
          highlightAction === "prev" && styles.controlButtonActive,
        )}
      >
        <Icon name="chevLeft" size={14} />
      </Button>
      <Button
        size="xs"
        title="播放 / 暂停 (Space)"
        aria-label="播放 / 暂停"
        onClick={onTogglePlay}
        className={cn(
          styles.controlButton,
          styles.compactPlayButton,
          highlightAction === "play" && styles.controlButtonActive,
        )}
      >
        <Icon name={isPlaying ? "pause" : "play"} size={13} />
      </Button>
      <Button
        size="xs"
        title="下一帧"
        aria-label="下一帧"
        onClick={() => onSeekByFrames(1)}
        className={cn(
          styles.controlButton,
          highlightAction === "next" && styles.controlButtonActive,
        )}
      >
        <Icon name="chevRight" size={14} />
      </Button>
      <Button
        size="xs"
        title="跳到末帧"
        aria-label="跳到末帧"
        onClick={() => onSeek(maxFrame)}
        className={cn(styles.controlButton, styles.skipButton, styles.skipEnd)}
      >
        <Icon name="chevRight" size={14} />
      </Button>
      {onSeekPredicted && (
        <div className={styles.expandedGroup}>
          <Button
            size="xs"
            title="上一个有预测的帧"
            aria-label="上一个有预测的帧"
            data-testid="video-seek-prev-predicted"
            onClick={() => onSeekPredicted(-1)}
            className={cn(styles.controlButton, styles.controlButtonPredicted)}
          >
            <Icon name="chevLeft" size={14} />
          </Button>
          <Button
            size="xs"
            title="下一个有预测的帧"
            aria-label="下一个有预测的帧"
            data-testid="video-seek-next-predicted"
            onClick={() => onSeekPredicted(1)}
            className={cn(styles.controlButton, styles.controlButtonPredicted)}
          >
            <Icon name="chevRight" size={14} />
          </Button>
        </div>
      )}
    </div>
  );

  const zoomControls = (
    <div
      data-testid="video-timeline-zoom-controls"
      className={cn(styles.zoomControls, isInteractive && styles.interactive)}
    >
      <Button
        size="xs"
        title="缩小时间轴"
        aria-label="缩小时间轴"
        data-testid="video-timeline-zoom-out"
        onClick={() => zoomTimeline(1.6)}
        className={styles.controlButton}
      >
        <Icon name="zoomOut" size={14} />
      </Button>
      <Button
        size="xs"
        title="放大时间轴 (长视频精细定位)"
        aria-label="放大时间轴"
        data-testid="video-timeline-zoom-in"
        onClick={() => zoomTimeline(0.6)}
        className={styles.controlButton}
      >
        <Icon name="zoomIn" size={14} />
      </Button>
      <Button
        size="xs"
        title="适配全部帧 (退出时间轴缩放)"
        aria-label="适配全部帧"
        data-testid="video-timeline-zoom-reset"
        onClick={resetTimelineWindow}
        className={styles.controlButton}
      >
        <Icon name="scan" size={14} />
      </Button>
    </div>
  );

  const statusBar = (
    <div data-testid="video-timeline-bottom-bar" className={cn("mono", styles.expandedStatus)}>
      {transportControls}
      <span>
        F {frameIndex} / {maxFrame}
      </span>
      <span
        data-testid="video-time-readout"
        aria-label={`当前时间 ${formatTime(frameToTime(frameIndex, timebase))}，总时长 ${formatTime(frameTimebaseDuration(timebase))}`}
      >
        {timeReadoutText}
      </span>
      <span data-testid="video-playback-rate">{playbackRateText}</span>
      <span className={styles.statusSpacer} />
      {loopRegion && (
        <span className={styles.loopChip}>
          <span data-testid="video-loop-region-label">
            循环 F{loopRegion.startFrame}–F{loopRegion.endFrame}
          </span>
          <button
            type="button"
            title="清除循环区间 (Alt+L)"
            onClick={onClearLoopRegion}
            className={cn(styles.clearLoopButton, isInteractive && styles.interactive)}
          >
            清除
          </button>
        </span>
      )}
      <span data-testid="video-timeline-window-readout" className={styles.windowReadout}>
        {windowReadoutText}
      </span>
      {maxFrame > 0 && (
        <div
          data-testid="video-timeline-navigator"
          ref={navRef}
          className={cn(
            styles.navigator,
            styles.navigatorInline,
            isInteractive && styles.interactive,
          )}
          onPointerDown={beginNavDrag}
          onPointerMove={moveNavDrag}
          onPointerUp={endNavDrag}
          onPointerCancel={endNavDrag}
          title="概览导航：拖窗口平移 · 拖边缩放 · 点空白跳转"
        >
          {globalTimelineDensity.map((bin) =>
            bin.density > 0 ? (
              <TimelineSpan
                key={`inav-density-${bin.index}`}
                className={styles.navDensityBin}
                vars={{
                  "--timeline-left": `${navPct(bin.from)}%`,
                  "--timeline-width": `${Math.max(0.4, navPct(bin.to - bin.from + 1))}%`,
                }}
              />
            ) : null,
          )}
          <TimelineSpan
            data-testid="video-timeline-navigator-window"
            data-full-window={isZoomed ? "false" : "true"}
            className={styles.navWindow}
            vars={{
              "--timeline-left": `${navPct(timelineWindow.from)}%`,
              "--timeline-width": `${Math.max(1.5, navPct(timelineWindow.to - timelineWindow.from))}%`,
            }}
          />
          <TimelineSpan
            className={styles.navPlayhead}
            vars={{ "--timeline-left": `${navPct(frameIndex)}%` }}
          />
        </div>
      )}
      <span data-testid="video-current-frame-entry-count">
        当前帧 {currentFrameEntryCount} 个标注
      </span>
      {zoomControls}
      {timelineToggleButton}
    </div>
  );

  if (expanded) {
    return (
      <div
        data-testid="video-playback-overlay"
        data-timeline-from={timelineWindow.from}
        data-timeline-to={timelineWindow.to}
        data-state="expanded"
        ref={overlayRef}
        className={cn(
          styles.overlay,
          styles.overlayExpanded,
          visible ? styles.overlayVisible : styles.overlayHidden,
        )}
      >
        <div
          id="video-timeline-details"
          data-testid="video-timeline-details"
          className={styles.laneStack}
        >
          <div className={styles.gridlines} aria-hidden>
            {rulerTicks.map((frame) => (
              <TimelineSpan
                key={`xgridline-${frame}`}
                className={styles.gridline}
                vars={{ "--timeline-left": frameLeft(frame) }}
              />
            ))}
            <TimelineSpan
              className={styles.gridlinePlayhead}
              vars={{ "--timeline-left": frameLeft(frameIndex) }}
            />
          </div>
          <div className={styles.laneRow}>
            <span className={styles.laneLabel} />
            <div className={styles.rulerBody}>
              <div className={styles.rulerMinorTrack} aria-hidden>
                {rulerMinorTicks.map((frame) => (
                  <TimelineSpan
                    key={`xminor-${frame}`}
                    className={cn(
                      styles.rulerMinorTick,
                      rulerTicks.includes(frame) && styles.rulerMinorTickMajor,
                    )}
                    vars={{ "--timeline-left": frameLeft(frame) }}
                  />
                ))}
              </div>
              {rulerTicks.map((frame) => (
                <TimelineSpan
                  key={`xruler-${frame}`}
                  className={styles.rulerMajor}
                  vars={{ "--timeline-left": frameLeft(frame) }}
                >
                  {frame}
                </TimelineSpan>
              ))}
            </div>
          </div>

          <div className={styles.laneRow}>
            <span className={styles.laneLabel} />
            <div
              data-testid="video-timeline-shell"
              ref={timelineShellRef}
              tabIndex={isInteractive ? 0 : -1}
              className={cn(styles.scrubberBody, isInteractive && styles.interactive)}
              onKeyDown={handleShellKeyDown}
              onPointerDownCapture={handleShellPointerDown}
              onPointerMove={handleShellPointerMove}
              onPointerUp={handleShellPointerUp}
              onPointerCancel={handleShellPointerCancel}
              onPointerLeave={handleShellPointerLeave}
              onDoubleClick={handleShellDoubleClick}
            >
              <span className={styles.scrubberRail} />
              <TimelineSpan
                className={styles.scrubberFill}
                vars={{ "--timeline-progress": currentFramePct / 100 }}
              />
              <TimelineSpan
                className={styles.scrubberKnob}
                vars={{ "--timeline-progress": currentFramePct / 100 }}
              />
              <input
                className={cn("video-timeline-range", styles.rangeInput, styles.rangeInputLarge)}
                aria-label="视频帧时间轴"
                type="range"
                min={0}
                max={10000}
                tabIndex={-1}
                value={Math.round(frameToPct(frameIndex, timelineWindow) * 100)}
                onChange={(e) =>
                  onSeek(pctToFrame(Number(e.currentTarget.value) / 10000, timelineWindow))
                }
                onFocus={(e) => focusTimelineShell(e.currentTarget)}
                onKeyDown={handleShellKeyDown}
              />
              {frameTooltip && (
                <TimelineDiv
                  data-testid={hoverPreview ? "video-frame-preview-popover" : "video-frame-tooltip"}
                  className={cn(
                    styles.tooltip,
                    hoverPreview ? styles.previewTooltip : styles.frameTooltip,
                  )}
                  vars={{ "--tooltip-left": hoverPreview ? hoverPopoverLeft : `${hoverLeft}%` }}
                >
                  {hoverPreview ? (
                    <div className={styles.previewContent}>
                      <div
                        data-testid="video-frame-preview-image-shell"
                        className={styles.previewImageShell}
                      >
                        {hoverPreview.status === "ready" ? (
                          <img
                            data-testid="video-frame-preview-image"
                            src={hoverPreview.url}
                            alt=""
                            className={styles.previewImage}
                          />
                        ) : hoverPreview.status === "pending" ? (
                          <span>Loading F {hoverPreview.frameIndex}</span>
                        ) : (
                          <span>Preview unavailable</span>
                        )}
                      </div>
                      <div className={styles.previewMeta}>
                        <span>{frameTooltip}</span>
                        <span className={styles.previewFormat}>
                          {hoverPreview.status === "ready"
                            ? hoverPreview.format.toUpperCase()
                            : hoverPreview.status}
                        </span>
                      </div>
                    </div>
                  ) : (
                    frameTooltip
                  )}
                </TimelineDiv>
              )}
            </div>
          </div>

          {showChapterLane && (
            <div data-testid="video-timeline-lane-chapters" className={styles.laneRow}>
              <span className={styles.laneLabel}>章节</span>
              <div className={styles.laneBody}>
                {rangeDraft?.purpose === "chapter-draft" && (
                  <TimelineSpan
                    data-testid={RANGE_DRAFT_TESTID[rangeDraft.purpose]}
                    className={styles.chapterDraftRegion}
                    vars={rangeStyle(rangeDraft.region.startFrame, rangeDraft.region.endFrame)}
                  />
                )}
                {chapters.map((chapter, chapterIndex) => {
                  const preview =
                    chapterResizePreview?.id === chapter.id ? chapterResizePreview : null;
                  const startFrame = preview ? preview.startFrame : chapter.startFrame;
                  const endFrame = preview ? preview.endFrame : chapter.endFrame;
                  const leftPct = frameToPct(startFrame, timelineWindow);
                  const rightPct = frameToPct(endFrame, timelineWindow);
                  const chapterStyle: CSSVars = {
                    ...rangeStyle(startFrame, endFrame),
                    "--chapter-color":
                      chapter.color ??
                      DEFAULT_CHAPTER_COLORS[chapterIndex % DEFAULT_CHAPTER_COLORS.length],
                  };
                  const resizable = Boolean(onChapterResize) && isInteractive;
                  return (
                    <div key={chapter.id} className={styles.chapterEntry}>
                      <TimelineButton
                        type="button"
                        data-testid="video-timeline-chapter"
                        data-hovered={hoveredChapterId === chapter.id ? "true" : undefined}
                        title={`${chapter.title} · F${startFrame}-F${endFrame}`}
                        onClick={(e) => {
                          e.preventDefault();
                          e.stopPropagation();
                          onSeekChapter?.(chapter.id, startFrame);
                        }}
                        onPointerEnter={() => onHoverChapter?.(chapter.id)}
                        onPointerLeave={() => onHoverChapter?.(null)}
                        className={cn(
                          styles.chapterBar,
                          isInteractive && styles.interactive,
                          hoveredChapterId === chapter.id && styles.chapterMarkerHovered,
                        )}
                        vars={chapterStyle}
                      >
                        <span className={styles.chapterBarLabel}>{chapter.title}</span>
                      </TimelineButton>
                      {resizable && (
                        <>
                          {frameInWindow(startFrame) && (
                            <TimelineSpan
                              data-testid="video-chapter-resize-start"
                              data-chapter-resize="start"
                              className={styles.chapterResizeHandle}
                              vars={{ "--timeline-left": `${leftPct}%` }}
                              onPointerDown={(e) => beginChapterResize(e, chapter, "start")}
                              onPointerMove={moveChapterResize}
                              onPointerUp={endChapterResize}
                              onPointerCancel={endChapterResize}
                            />
                          )}
                          {frameInWindow(endFrame) && (
                            <TimelineSpan
                              data-testid="video-chapter-resize-end"
                              data-chapter-resize="end"
                              className={styles.chapterResizeHandle}
                              vars={{ "--timeline-left": `${rightPct}%` }}
                              onPointerDown={(e) => beginChapterResize(e, chapter, "end")}
                              onPointerMove={moveChapterResize}
                              onPointerUp={endChapterResize}
                              onPointerCancel={endChapterResize}
                            />
                          )}
                        </>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {bookmarks.length > 0 && (
            <div data-testid="video-timeline-lane-bookmarks" className={styles.laneRow}>
              <span className={styles.laneLabel}>书签</span>
              <div className={styles.laneBody}>
                {bookmarks
                  .filter((b) => frameInWindow(b.frameIndex))
                  .map((bookmark) => (
                    <TimelineButton
                      key={bookmark.id}
                      type="button"
                      data-testid="video-bookmark-marker"
                      title={bookmark.label ?? `F ${bookmark.frameIndex}`}
                      onClick={(e) => {
                        e.preventDefault();
                        e.stopPropagation();
                        onSeekBookmark?.(bookmark.frameIndex);
                      }}
                      className={cn(
                        styles.bookmarkMarker,
                        styles.bookmarkMarkerLane,
                        isInteractive && styles.interactive,
                      )}
                      vars={{ "--timeline-left": frameLeft(bookmark.frameIndex) }}
                    />
                  ))}
              </div>
            </div>
          )}

          {issueFrames.length > 0 && (
            <div data-testid="video-timeline-lane-issues" className={styles.laneRow}>
              <span className={styles.laneLabel}>问题</span>
              <div className={styles.laneBody}>
                {issueFrames
                  .filter((f) => frameInWindow(f))
                  .map((frame) => (
                    <TimelineButton
                      key={`xissue-${frame}`}
                      type="button"
                      data-testid="video-issue-marker"
                      data-workbench-issue-navigation
                      title={`问题 · F ${frame}`}
                      onClick={(e) => {
                        e.preventDefault();
                        e.stopPropagation();
                        (onSeekIssueFrame ?? onSeek)(frame);
                      }}
                      className={cn(
                        styles.issueMarker,
                        styles.issueMarkerLane,
                        isInteractive && styles.interactive,
                      )}
                      vars={{ "--timeline-left": frameLeft(frame) }}
                    />
                  ))}
              </div>
            </div>
          )}

          {hasPredictionDensity && (
            <div data-testid="video-timeline-lane-predictions" className={styles.laneRow}>
              <span className={styles.laneLabel}>AI 预测密度</span>
              <div
                className={cn(styles.laneBody, styles.densityLane)}
                data-testid="video-timeline-prediction-density"
              >
                {predictionDensity.map((bin) => {
                  if (bin.count <= 0) return null;
                  const pos = binWindowStyle(bin.from, bin.to);
                  if (!pos) return null;
                  return (
                    <TimelineSpan
                      key={`xpred-${bin.index}`}
                      className={styles.predictionBinLane}
                      vars={{
                        ...pos,
                        "--density-height": `${Math.max(2, (bin.count / densityScaleMax) * DENSITY_BAR_MAX_PX)}px`,
                      }}
                    />
                  );
                })}
              </div>
            </div>
          )}

          <div data-testid="video-timeline-lane-density" className={styles.laneRow}>
            <span className={styles.laneLabel}>标注密度</span>
            <div
              className={cn(styles.laneBody, styles.densityLane)}
              data-testid="video-timeline-density"
            >
              {globalTimelineDensity.map((bin) => {
                if (bin.density <= 0) return null;
                const pos = binWindowStyle(bin.from, bin.to);
                if (!pos) return null;
                return (
                  <TimelineSpan
                    key={`xdens-${bin.index}`}
                    className={styles.densityBinLane}
                    vars={{
                      ...pos,
                      "--density-height": `${Math.max(2, (bin.density / densityScaleMax) * DENSITY_BAR_MAX_PX)}px`,
                      "--density-gradient": densityBinGradient(bin, trackColorOverrides),
                    }}
                  />
                );
              })}
            </div>
          </div>

          {selectedTrackTimeline && (
            <div data-testid="video-timeline-lane-track" className={styles.laneRow}>
              <span className={styles.laneLabel}>所选轨迹</span>
              <div className={styles.laneBody}>
                <TimelineDiv
                  data-testid="video-track-timeline"
                  className={styles.trackTimelineLane}
                  vars={trackColor ? { "--track-keyframe-color": trackColor } : {}}
                >
                  {selectedTrackTimeline.interpolated.map((segment) => (
                    <TimelineSpan
                      key={`xinterp-${segment.from}-${segment.to}`}
                      data-testid="video-timeline-interpolated"
                      className={cn(
                        styles.trackSegment,
                        segment.kind === "held" ? styles.heldSegment : styles.interpolatedSegment,
                        segment.hasPrediction && styles.predictedSegment,
                      )}
                      title={
                        segment.kind === "held" ? "关键帧之间保持上一/最近 Mask" : "关键帧之间插值"
                      }
                      vars={rangeStyle(segment.from, segment.to)}
                    />
                  ))}
                  {selectedTrackTimeline.outside.map((segment) => (
                    <TimelineSpan
                      key={`xoutside-${segment.from}-${segment.to}`}
                      data-testid="video-timeline-outside"
                      className={cn(
                        styles.trackSegment,
                        styles.outsideSegment,
                        segment.source === "prediction" && styles.outsidePrediction,
                      )}
                      vars={rangeStyle(segment.from, segment.to)}
                    />
                  ))}
                  {selectedTrackTimeline.keyframes
                    .filter((k) => frameInWindow(k.frame))
                    .map((keyframe) => (
                      <TimelineSpan
                        key={`xkf-${keyframe.frame}`}
                        data-testid="video-timeline-track-keyframe"
                        className={cn(
                          styles.trackKeyframe,
                          keyframe.source === "prediction" && styles.trackKeyframePrediction,
                          keyframe.occluded && styles.trackKeyframeOccluded,
                        )}
                        vars={{ "--timeline-left": frameLeft(keyframe.frame) }}
                      />
                    ))}
                </TimelineDiv>
              </div>
            </div>
          )}

          {gridTicks.length > 0 && (
            <div className={styles.laneRow}>
              <span className={styles.laneLabel}>采样网格</span>
              <div className={styles.laneBody} data-testid="video-timeline-grid">
                {gridTicks.map((frame) => (
                  <TimelineSpan
                    key={`xgrid-${frame}`}
                    data-testid="video-timeline-grid-tick"
                    className={styles.gridTickLane}
                    vars={{ "--timeline-left": frameLeft(frame) }}
                  />
                ))}
              </div>
            </div>
          )}

          {showSegmentLane && segmentRange && (
            <div data-testid="video-timeline-lane-segment" className={styles.laneRow}>
              <span className={styles.laneLabel}>协同分段</span>
              <div className={styles.laneBody}>
                <TimelineSpan
                  data-testid="video-segment-work-range"
                  className={styles.segmentWorkRegion}
                  vars={rangeStyle(segmentRange.workStartFrame, segmentRange.workEndFrame)}
                />
                <TimelineSpan
                  data-testid="video-segment-core-range"
                  className={styles.segmentCoreRegion}
                  vars={rangeStyle(segmentRange.coreStartFrame, segmentRange.coreEndFrame)}
                />
              </div>
            </div>
          )}

          {trackerReview && (
            <div
              data-testid="video-timeline-lane-tracker-review"
              data-review-job-id={trackerReview.jobId}
              className={styles.laneRow}
            >
              <span className={styles.laneLabel}>追踪审阅</span>
              <div className={styles.laneBody}>
                <TimelineSpan
                  data-testid="timeline-tracker-review-window"
                  title={`审阅 F${trackerReview.scope.fromFrame}–F${trackerReview.scope.toFrame}`}
                  className={styles.reviewRegion}
                  vars={reviewRangeStyle(
                    trackerReview.scope.fromFrame,
                    trackerReview.scope.toFrame,
                  )}
                />
              </div>
            </div>
          )}

          {showPropagationLane && (
            <div data-testid="video-timeline-lane-propagation" className={styles.laneRow}>
              <span className={styles.laneLabel}>AI 影响范围</span>
              <div className={styles.laneBody}>
                {propagateRange && (
                  <TimelineSpan
                    data-testid="video-propagate-range"
                    className={styles.propagateRegion}
                    vars={rangeStyle(propagateRange.startFrame, propagateRange.endFrame)}
                  />
                )}
                {rangeDraft?.purpose === "propagate-range" && (
                  <TimelineSpan
                    data-testid={RANGE_DRAFT_TESTID[rangeDraft.purpose]}
                    className={styles.propagateDraftRegion}
                    vars={rangeStyle(rangeDraft.region.startFrame, rangeDraft.region.endFrame)}
                  />
                )}
              </div>
            </div>
          )}

          {showLoopLane && (
            <div data-testid="video-timeline-lane-loop" className={styles.laneRow}>
              <span className={styles.laneLabel}>循环区间</span>
              <div className={styles.laneBody}>
                {loopRegion && rangeDraft?.purpose !== "loop" && (
                  <TimelineSpan
                    data-testid="video-loop-region"
                    className={styles.loopRegion}
                    vars={rangeStyle(loopRegion.startFrame, loopRegion.endFrame)}
                  />
                )}
                {rangeDraft?.purpose === "loop" && (
                  <TimelineSpan
                    data-testid={RANGE_DRAFT_TESTID[rangeDraft.purpose]}
                    className={cn(styles.loopRegion, styles.loopRegionDraft)}
                    vars={rangeStyle(rangeDraft.region.startFrame, rangeDraft.region.endFrame)}
                  />
                )}
              </div>
            </div>
          )}
        </div>

        {reviewSummary}
        {statusBar}
      </div>
    );
  }

  return (
    <div
      data-testid="video-playback-overlay"
      data-timeline-from={timelineWindow.from}
      data-timeline-to={timelineWindow.to}
      data-state="collapsed"
      ref={overlayRef}
      className={cn(
        styles.overlay,
        styles.overlayCollapsed,
        visible ? styles.overlayVisible : styles.overlayHidden,
      )}
    >
      <Button
        size="sm"
        title="播放 / 暂停 (Space)"
        aria-label="播放 / 暂停"
        onClick={onTogglePlay}
        className={cn(
          styles.controlButton,
          styles.collapsedPlayButton,
          highlightAction === "play" && styles.controlButtonActive,
        )}
      >
        <Icon name={isPlaying ? "pause" : "play"} size={13} />
      </Button>
      <div className={cn("mono", styles.collapsedStatus)}>
        <span>
          F {frameIndex} / {maxFrame}
        </span>
        <span
          data-testid="video-time-readout"
          aria-label={`当前时间 ${formatTime(frameToTime(frameIndex, timebase))}，总时长 ${formatTime(frameTimebaseDuration(timebase))}`}
        >
          {timeReadoutText}
        </span>
        <span data-testid="video-playback-rate">{playbackRateText}</span>
        <span data-testid="video-timeline-window-readout" className={styles.windowReadout}>
          {windowReadoutText}
        </span>
        <span data-testid="video-current-frame-entry-count" className={styles.collapsedEntryCount}>
          当前帧 {currentFrameEntryCount} 个标注
        </span>
      </div>

      <div
        data-testid="video-timeline-shell"
        ref={timelineShellRef}
        tabIndex={isInteractive ? 0 : -1}
        className={cn(
          styles.timelineShell,
          styles.collapsedTimelineShell,
          isInteractive && styles.interactive,
        )}
        onKeyDown={handleShellKeyDown}
        onPointerDownCapture={handleShellPointerDown}
        onPointerMove={handleShellPointerMove}
        onPointerUp={handleShellPointerUp}
        onPointerCancel={handleShellPointerCancel}
        onPointerLeave={handleShellPointerLeave}
        onDoubleClick={handleShellDoubleClick}
      >
        <input
          className={cn("video-timeline-range", styles.rangeInput)}
          aria-label="视频帧时间轴"
          type="range"
          min={0}
          max={10000}
          tabIndex={-1}
          value={Math.round(frameToPct(frameIndex, timelineWindow) * 100)}
          onChange={(e) =>
            onSeek(pctToFrame(Number(e.currentTarget.value) / 10000, timelineWindow))
          }
          onFocus={(e) => focusTimelineShell(e.currentTarget)}
          onKeyDown={(e) => {
            stepTimelineByKey(e, e.currentTarget);
          }}
          onPointerMove={(e) => {
            const rect = e.currentTarget.getBoundingClientRect();
            const nextFrame = frameFromPointer(e.clientX, rect);
            updateHoverFrame(nextFrame);
          }}
          onPointerLeave={() => {
            updateHoverFrame(null);
          }}
          onPointerUp={(e) => focusTimelineShell(e.currentTarget)}
        />
        <div data-testid="video-timeline-summary-overlays" className={styles.timelineLayer}>
          {gridTicks.length > 0 && (
            <div data-testid="video-timeline-grid" className={styles.gridTrack}>
              {gridTicks.map((frame) => (
                <TimelineSpan
                  key={`grid-${frame}`}
                  data-testid="video-timeline-grid-tick"
                  className={styles.gridTick}
                  vars={{ "--timeline-left": frameLeft(frame) }}
                />
              ))}
            </div>
          )}
          {currentFrameOffGrid && frameInWindow(frameIndex) && (
            <TimelineSpan
              data-testid="video-timeline-offgrid-marker"
              className={styles.offGridMarker}
              vars={{ "--timeline-left": frameLeft(frameIndex) }}
            />
          )}
          {loopRegion && rangeDraft?.purpose !== "loop" && (
            <TimelineSpan
              data-testid="video-loop-region"
              className={styles.loopRegion}
              vars={rangeStyle(loopRegion.startFrame, loopRegion.endFrame)}
            />
          )}
          {propagateRange && (
            <TimelineSpan
              data-testid="video-propagate-range"
              className={styles.propagateRegion}
              vars={rangeStyle(propagateRange.startFrame, propagateRange.endFrame)}
            />
          )}
          {segmentRange && (
            <>
              <TimelineSpan
                data-testid="video-segment-work-range"
                className={styles.segmentWorkRegion}
                vars={rangeStyle(segmentRange.workStartFrame, segmentRange.workEndFrame)}
              />
              <TimelineSpan
                data-testid="video-segment-core-range"
                className={styles.segmentCoreRegion}
                vars={rangeStyle(segmentRange.coreStartFrame, segmentRange.coreEndFrame)}
              />
            </>
          )}
          {rangeDraft && (
            <TimelineSpan
              data-testid={RANGE_DRAFT_TESTID[rangeDraft.purpose]}
              className={cn(
                rangeDraft.purpose === "loop" && cn(styles.loopRegion, styles.loopRegionDraft),
                rangeDraft.purpose === "chapter-draft" && styles.chapterDraftRegion,
                rangeDraft.purpose === "propagate-range" && styles.propagateDraftRegion,
              )}
              vars={rangeStyle(rangeDraft.region.startFrame, rangeDraft.region.endFrame)}
            />
          )}
          {chapters.length > 0 && (
            <div data-testid="video-timeline-chapters" className={styles.chaptersTrack}>
              {chapters.map((chapter) => {
                const preview =
                  chapterResizePreview?.id === chapter.id ? chapterResizePreview : null;
                const startFrame = preview ? preview.startFrame : chapter.startFrame;
                const endFrame = preview ? preview.endFrame : chapter.endFrame;
                const leftPct = frameToPct(startFrame, timelineWindow);
                const rightPct = frameToPct(endFrame, timelineWindow);
                const chapterStyle: CSSVars = {
                  ...rangeStyle(startFrame, endFrame),
                  "--chapter-color": chapter.color ?? "oklch(0.62 0.18 252)",
                };
                const resizable = Boolean(onChapterResize) && isInteractive;
                return (
                  <div key={chapter.id} className={styles.chapterEntry}>
                    <TimelineButton
                      type="button"
                      data-testid="video-timeline-chapter"
                      data-hovered={hoveredChapterId === chapter.id ? "true" : undefined}
                      title={`${chapter.title} · F${startFrame}-F${endFrame}`}
                      onClick={(e) => {
                        e.preventDefault();
                        e.stopPropagation();
                        onSeekChapter?.(chapter.id, startFrame);
                      }}
                      onPointerEnter={() => onHoverChapter?.(chapter.id)}
                      onPointerLeave={() => onHoverChapter?.(null)}
                      className={cn(
                        styles.chapterMarker,
                        isInteractive && styles.interactive,
                        hoveredChapterId === chapter.id && styles.chapterMarkerHovered,
                      )}
                      vars={chapterStyle}
                    />
                    {resizable && (
                      <>
                        {frameInWindow(startFrame) && (
                          <TimelineSpan
                            data-testid="video-chapter-resize-start"
                            data-chapter-resize="start"
                            className={styles.chapterResizeHandle}
                            vars={{ "--timeline-left": `${leftPct}%` }}
                            onPointerDown={(e) => beginChapterResize(e, chapter, "start")}
                            onPointerMove={moveChapterResize}
                            onPointerUp={endChapterResize}
                            onPointerCancel={endChapterResize}
                          />
                        )}
                        {frameInWindow(endFrame) && (
                          <TimelineSpan
                            data-testid="video-chapter-resize-end"
                            data-chapter-resize="end"
                            className={styles.chapterResizeHandle}
                            vars={{ "--timeline-left": `${rightPct}%` }}
                            onPointerDown={(e) => beginChapterResize(e, chapter, "end")}
                            onPointerMove={moveChapterResize}
                            onPointerUp={endChapterResize}
                            onPointerCancel={endChapterResize}
                          />
                        )}
                      </>
                    )}
                  </div>
                );
              })}
            </div>
          )}
          {bookmarks
            .filter((bookmark) => frameInWindow(bookmark.frameIndex))
            .map((bookmark) => (
              <TimelineButton
                key={bookmark.id}
                type="button"
                data-testid="video-bookmark-marker"
                title={bookmark.label ?? `F ${bookmark.frameIndex}`}
                onClick={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  onSeekBookmark?.(bookmark.frameIndex);
                }}
                className={cn(styles.bookmarkMarker, isInteractive && styles.interactive)}
                vars={{ "--timeline-left": frameLeft(bookmark.frameIndex) }}
              />
            ))}
          {issueFrames
            .filter((frame) => frameInWindow(frame))
            .map((frame) => (
              <TimelineButton
                key={`issue-${frame}`}
                type="button"
                data-testid="video-issue-marker"
                data-workbench-issue-navigation
                title={`问题 · F ${frame}`}
                onClick={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  (onSeekIssueFrame ?? onSeek)(frame);
                }}
                className={cn(styles.issueMarker, isInteractive && styles.interactive)}
                vars={{ "--timeline-left": frameLeft(frame) }}
              />
            ))}
          {globalTimelineDensity.some((bin) => bin.density > 0) && (
            <div data-testid="video-timeline-density" className={styles.densityTrack}>
              {globalTimelineDensity.map((bin) => {
                if (bin.density <= 0) return null;
                const pos = binWindowStyle(bin.from, bin.to);
                if (!pos) return null;
                const binStyle: CSSVars = {
                  ...pos,
                  "--density-height": `${Math.max(2, (bin.density / densityScaleMax) * DENSITY_BAR_MAX_PX)}px`,
                  "--density-gradient": densityBinGradient(bin, trackColorOverrides),
                };
                return (
                  <TimelineSpan key={bin.index} className={styles.densityBin} vars={binStyle} />
                );
              })}
            </div>
          )}
          {hasPredictionDensity && (
            <div data-testid="video-timeline-prediction-density" className={styles.predictionTrack}>
              {predictionDensity.map((bin) => {
                if (bin.count <= 0) return null;
                const pos = binWindowStyle(bin.from, bin.to);
                if (!pos) return null;
                const binStyle: CSSVars = {
                  ...pos,
                  "--density-height": `${Math.max(2, (bin.count / densityScaleMax) * DENSITY_BAR_MAX_PX)}px`,
                };
                return (
                  <TimelineSpan key={bin.index} className={styles.predictionBin} vars={binStyle} />
                );
              })}
            </div>
          )}
          {selectedTrackTimeline && (
            <TimelineDiv
              data-testid="video-track-timeline"
              className={styles.trackTimeline}
              vars={trackColor ? { "--track-keyframe-color": trackColor } : {}}
            >
              {selectedTrackTimeline.interpolated.map((segment) => (
                <TimelineSpan
                  key={`interpolated-${segment.from}-${segment.to}`}
                  data-testid="video-timeline-interpolated"
                  className={cn(
                    styles.trackSegment,
                    segment.kind === "held" ? styles.heldSegment : styles.interpolatedSegment,
                    segment.hasPrediction && styles.predictedSegment,
                  )}
                  title={
                    segment.kind === "held" ? "关键帧之间保持上一/最近 Mask" : "关键帧之间插值"
                  }
                  vars={rangeStyle(segment.from, segment.to)}
                />
              ))}
              {selectedTrackTimeline.outside.map((segment) => (
                <TimelineSpan
                  key={`track-outside-${segment.from}-${segment.to}`}
                  data-testid="video-timeline-outside"
                  className={cn(
                    styles.trackSegment,
                    styles.outsideSegment,
                    segment.source === "prediction" && styles.outsidePrediction,
                  )}
                  vars={rangeStyle(segment.from, segment.to)}
                />
              ))}
              {selectedTrackTimeline.keyframes
                .filter((keyframe) => frameInWindow(keyframe.frame))
                .map((keyframe) => (
                  <TimelineSpan
                    key={`track-keyframe-${keyframe.frame}`}
                    data-testid="video-timeline-track-keyframe"
                    className={cn(
                      styles.trackKeyframe,
                      keyframe.source === "prediction" && styles.trackKeyframePrediction,
                      keyframe.occluded && styles.trackKeyframeOccluded,
                    )}
                    vars={{ "--timeline-left": frameLeft(keyframe.frame) }}
                  />
                ))}
            </TimelineDiv>
          )}
        </div>
        {maxFrame > 0 && (
          <div
            data-testid="video-timeline-window-overview"
            className={styles.collapsedWindowOverview}
            role="img"
            aria-label={`${windowReadoutText}，全片 F0–${maxFrame}`}
          >
            <TimelineSpan
              data-testid="video-timeline-collapsed-window-selection"
              data-full-window={isZoomed ? "false" : "true"}
              className={styles.collapsedWindowSelection}
              vars={{
                "--timeline-left": `${navPct(timelineWindow.from)}%`,
                "--timeline-width": `${Math.max(1.5, navPct(timelineWindow.to - timelineWindow.from))}%`,
              }}
            />
          </div>
        )}
        {frameTooltip && (
          <TimelineDiv
            data-testid={hoverPreview ? "video-frame-preview-popover" : "video-frame-tooltip"}
            className={cn(
              styles.tooltip,
              hoverPreview ? styles.previewTooltip : styles.frameTooltip,
            )}
            vars={{ "--tooltip-left": hoverPreview ? hoverPopoverLeft : `${hoverLeft}%` }}
          >
            {hoverPreview ? (
              <div className={styles.previewContent}>
                <div
                  data-testid="video-frame-preview-image-shell"
                  className={styles.previewImageShell}
                >
                  {hoverPreview.status === "ready" ? (
                    <img
                      data-testid="video-frame-preview-image"
                      src={hoverPreview.url}
                      alt=""
                      className={styles.previewImage}
                    />
                  ) : hoverPreview.status === "pending" ? (
                    <span>Loading F {hoverPreview.frameIndex}</span>
                  ) : (
                    <span>Preview unavailable</span>
                  )}
                </div>
                <div className={styles.previewMeta}>
                  <span>{frameTooltip}</span>
                  <span className={styles.previewFormat}>
                    {hoverPreview.status === "ready"
                      ? hoverPreview.format.toUpperCase()
                      : hoverPreview.status}
                  </span>
                </div>
              </div>
            ) : (
              frameTooltip
            )}
          </TimelineDiv>
        )}
      </div>
      {timelineToggleButton}
      {reviewSummary}
    </div>
  );
}

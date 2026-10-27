import { originalOwnerKey } from "@/api/originals";
import { pointCloudManifestQueryKey } from "@/api/pointCloudOriginals";
import { useOriginalAssetOwner } from "./useOriginalAssetOwner";
import { CopyableError } from "@/components/ui/CopyableError";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useVirtualizer, type VirtualItem } from "@tanstack/react-virtual";

import type { SceneTimelineFrameSummary } from "@/api/generated";
import { tasksApi } from "@/api/tasks";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Tooltip } from "@/components/ui/Tooltip";
import { useElementStyle } from "@/components/ui/useElementStyle";
import { TASK_STATUS_LABELS } from "@/constants/roles";
import { useSceneTimeline, SCENE_TIMELINE_QUERY_KEY } from "@/hooks/useSceneTimeline";
import { cn } from "@/lib/utils";
import {
  beginPointCloudNavigationTrace,
  pointCloudNavigationGenerationForTask,
  publishPointCloudNavigationTrace,
} from "@/utils/pointCloudNavigationDiagnostics";

import { frameToPct, niceFrameStep } from "../../stage/timelineCoords";
import {
  SCENE_TIMELINE_CELL_WIDTH,
  SCENE_TIMELINE_MAX_WINDOW,
  SCENE_TIMELINE_OVERSCAN,
  SCENE_TIMELINE_ZOOM_LEVELS,
  densityRatio,
  timelineInitialRange,
  timelineQueryRange,
  timelineZoomOffset,
  type TimelineFrameRange,
} from "./sceneTimelineVirtualization";
import { prefetchPointCloudFrameAssets } from "./pointCloudAssetCache";
import { CrossFrameJobCenter } from "./CrossFrameJobCenter";
import {
  useScenePlayback,
  type ScenePlaybackFrameState,
  type ScenePlaybackRate,
  type ScenePlaybackTarget,
} from "./useScenePlayback";

// 人工快速点选约为 8–10 次/秒，只提交停手后的最终意图；自动播放不支付此防抖。
const FRAME_NAVIGATION_SETTLE_MS = 160;

interface SceneTimelineProps {
  taskId: string | null;
  trackId: string | null;
  prefetchDepthRasters?: boolean;
  prefetchDecimateThreshold?: number;
  selectedAnnotationIds?: string[];
  boxCount?: number;
  readOnly?: boolean;
  onNavigateFrame: (taskId: string, annotationId: string | null) => Promise<boolean>;
  qualityMarkers?: Record<number, "blocker" | "warning" | "info">;
  qualityIssueCount?: number;
  onOpenQuality?: () => void;
  frameState?: ScenePlaybackFrameState;
  playbackActive?: boolean;
  onPlaybackActiveChange?: (active: boolean) => void;
  playbackBlockedReason?: string | null;
  onPlaybackTrackChange?: (trackId: string | null) => void;
  visible?: boolean;
  onRetryFrame?: () => void;
}

function TimelineSizer({
  totalWidth,
  start,
  height,
  children,
}: {
  totalWidth: number;
  start: number;
  height: number;
  children: ReactNode;
}) {
  const ref = useElementStyle<HTMLDivElement>(
    useMemo(
      () =>
        ({
          "--timeline-total-width": `${totalWidth}px`,
          "--timeline-start": `${start}px`,
          "--timeline-height": `${height}px`,
        }) as CSSProperties,
      [totalWidth, start, height],
    ),
  );
  return (
    <div
      ref={ref}
      className="relative min-w-full w-[var(--timeline-total-width)] h-[var(--timeline-height)]"
      data-testid="scene-timeline-virtual-canvas"
    >
      <div className="absolute inset-y-0 left-0 flex translate-x-[var(--timeline-start)]">
        {children}
      </div>
    </div>
  );
}

function TimelineCell({
  item,
  frameIndex,
  summary,
  current,
  maxDensity,
  major,
  showTrack,
  showQuality,
  qualitySeverity,
  onNavigate,
  onPrefetch,
  onOpenQuality,
}: {
  item: VirtualItem;
  frameIndex: number;
  summary: SceneTimelineFrameSummary | undefined;
  current: boolean;
  maxDensity: number;
  major: boolean;
  showTrack: boolean;
  showQuality: boolean;
  qualitySeverity?: "blocker" | "warning" | "info";
  onNavigate: (summary: SceneTimelineFrameSummary) => void;
  onPrefetch: (summary: SceneTimelineFrameSummary) => void;
  onOpenQuality?: () => void;
}) {
  const available = summary?.state === "available" && !!summary.task_id;
  const stateLabel = !summary
    ? "摘要加载中"
    : summary.state === "missing"
      ? "缺失"
      : summary.state === "unavailable"
        ? "不可访问"
        : (TASK_STATUS_LABELS[summary.task_status as keyof typeof TASK_STATUS_LABELS] ?? "待处理");
  const count = summary?.annotation_count ?? 0;
  const role = summary?.selected_track?.temporal_role ?? "sample";
  const label = `帧 ${frameIndex}，${stateLabel}${available ? `，${count} 个 3D 标注` : ""}${summary?.selected_track_present ? "，当前对象在存在区间内" : ""}${summary?.selected_track ? `，${role} 成员已物化` : ""}`;
  const ref = useElementStyle<HTMLDivElement>(
    useMemo(
      () =>
        ({
          "--timeline-cell-width": `${item.size}px`,
          "--timeline-density-height": `${Math.round(densityRatio(count, maxDensity) * 19)}px`,
        }) as CSSProperties,
      [item.size, count, maxDensity],
    ),
  );
  return (
    <div
      ref={ref}
      className={cn(
        "relative flex shrink-0 flex-col w-[var(--timeline-cell-width)]",
        current && "bg-brand/10",
      )}
    >
      {current && (
        <span
          className="pointer-events-none absolute inset-y-0 left-1/2 z-base w-px bg-brand"
          aria-hidden="true"
        />
      )}
      <button
        type="button"
        data-index={item.index}
        data-testid={`scene-timeline-frame-${frameIndex}`}
        aria-label={label}
        aria-current={current ? "step" : undefined}
        disabled={!available}
        title={label}
        className="relative flex w-full flex-col items-center text-xs tabular-nums text-muted-foreground hover:bg-muted/60 focus-visible:z-base focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-brand disabled:cursor-default"
        onClick={() => summary && onNavigate(summary)}
        onPointerEnter={() => summary && onPrefetch(summary)}
        onFocus={() => summary && onPrefetch(summary)}
      >
        <span
          className={cn(
            "relative z-base flex h-6 w-full items-center justify-center border-b border-border",
            current && "font-semibold text-brand",
          )}
        >
          {current ? (
            `F${frameIndex}`
          ) : major ? (
            frameIndex
          ) : (
            <span className="mt-auto h-1 w-px bg-border" />
          )}
        </span>
        <span className="flex h-6 w-full items-center justify-center" aria-hidden="true">
          {!summary ? (
            <span className="text-muted-foreground">···</span>
          ) : summary.state === "missing" ? (
            <span className="w-3 border-t border-dashed border-muted-foreground" />
          ) : summary.state === "unavailable" ? (
            <Icon name="lock" size={12} />
          ) : (
            <span
              className={cn(
                "h-1.5 w-full rounded-sm mx-px",
                summary.task_status === "completed"
                  ? "bg-status-success/65"
                  : summary.task_status === "rejected"
                    ? "bg-status-danger/65"
                    : summary.task_status === "review"
                      ? "bg-status-caution/65"
                      : summary.task_status === "in_progress"
                        ? "bg-brand/55"
                        : "bg-muted-foreground/30",
              )}
            />
          )}
        </span>
        <span className="flex h-6 w-full items-end justify-center pb-0.5" aria-hidden="true">
          {available ? (
            count > 0 ? (
              <span
                className={cn(
                  "w-2 rounded-t-sm h-[var(--timeline-density-height)]",
                  current ? "bg-brand" : "bg-muted-foreground/50",
                )}
              />
            ) : (
              <span className="text-muted-foreground">0</span>
            )
          ) : (
            <span className="self-center text-muted-foreground">{summary ? "—" : "···"}</span>
          )}
        </span>
        {showTrack && (
          <span className="relative flex h-6 w-full items-center justify-center" aria-hidden="true">
            {summary?.selected_track_present && (
              <span
                data-testid={`scene-timeline-track-frame-${frameIndex}`}
                className="absolute inset-x-0 h-1.5 bg-brand/25"
              />
            )}
            {summary?.selected_track && (
              <span
                className={cn(
                  "relative z-base",
                  role === "keyframe"
                    ? "size-2 rounded-full bg-brand"
                    : role === "derived"
                      ? "size-2 rotate-45 bg-brand"
                      : "size-2 rounded-full border border-brand bg-card",
                )}
              />
            )}
          </span>
        )}
      </button>
      {showQuality && (
        <div className="flex h-6 items-center justify-center">
          {qualitySeverity && (
            <button
              type="button"
              data-testid={`scene-timeline-quality-${frameIndex}`}
              aria-label={`帧 ${frameIndex} 质检问题`}
              title={`帧 ${frameIndex} 质检问题`}
              className={cn(
                "relative z-base flex size-6 items-center justify-center rounded-sm focus-visible:outline-2 focus-visible:outline-brand",
                qualitySeverity === "blocker"
                  ? "text-status-danger"
                  : qualitySeverity === "warning"
                    ? "text-status-caution"
                    : "text-brand",
              )}
              onClick={() => {
                if (summary && available) onNavigate(summary);
                onOpenQuality?.();
              }}
            >
              <Icon name="warning" size={12} />
            </button>
          )}
        </div>
      )}
    </div>
  );
}

export function SceneTimeline({
  taskId,
  trackId,
  prefetchDepthRasters = false,
  prefetchDecimateThreshold,
  selectedAnnotationIds = [],
  boxCount = 0,
  readOnly = false,
  onNavigateFrame,
  qualityMarkers = {},
  qualityIssueCount = 0,
  onOpenQuality,
  frameState,
  playbackActive = false,
  onPlaybackActiveChange,
  playbackBlockedReason,
  onPlaybackTrackChange,
  visible = true,
  onRetryFrame,
}: SceneTimelineProps) {
  const queryClient = useQueryClient();
  const rootRef = useRef<HTMLElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const [expandedPreference, setExpandedPreference] = useState(false);
  const [hasVerticalSpace, setHasVerticalSpace] = useState(true);
  const [cellWidth, setCellWidth] = useState<number>(SCENE_TIMELINE_CELL_WIDTH);
  const [range, setRange] = useState<TimelineFrameRange>(timelineInitialRange);
  const [navigatingTaskId, setNavigatingTaskId] = useState<string | null>(null);
  const [jobCenterOpen, setJobCenterOpen] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [frameInput, setFrameInput] = useState("");
  const [seekFrame, setSeekFrame] = useState<number | null>(null);
  const [rate, setRate] = useState<ScenePlaybackRate>(2);
  const [followedTrackId, setFollowedTrackId] = useState<string | null>(null);
  const pendingNavigationRef = useRef<ScenePlaybackTarget | null>(null);
  const navigationTimerRef = useRef<number | null>(null);
  const navigationInFlightRef = useRef(false);
  const expectedTaskRef = useRef<string | null>(null);
  const navigationEpochRef = useRef(0);
  const pointerPrefetchTimerRef = useRef<number | null>(null);
  const manualRequestRef = useRef<AbortController | null>(null);
  const confirmedSceneRef = useRef<string | null | undefined>(undefined);
  const savedScrollRef = useRef(0);
  const manuallyBrowsedRef = useRef(false);
  const seekDraggingRef = useRef(false);
  const expanded = expandedPreference && hasVerticalSpace;
  const selectedTrack = playbackActive ? followedTrackId : trackId;
  const query = useSceneTimeline(taskId, range.startFrame, range.endFrame, selectedTrack);
  const data = query.data;
  const sceneStart = data?.scene_start_frame ?? 0;
  const sceneEnd = data?.scene_end_frame ?? -1;
  const currentFrame = data?.current_frame_index ?? null;
  const frameCount = sceneEnd >= sceneStart ? sceneEnd - sceneStart + 1 : 0;
  const showTrack = !!selectedTrack;
  const showQuality = Object.keys(qualityMarkers).length > 0;
  const trackHeight = 72 + (showTrack ? 24 : 0) + (showQuality ? 24 : 0);
  const virtualizer = useVirtualizer({
    horizontal: true,
    count: frameCount,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => cellWidth,
    overscan: SCENE_TIMELINE_OVERSCAN,
  });
  const virtualItems = virtualizer.getVirtualItems();
  const firstVirtualIndex = virtualItems[0]?.index ?? 0;
  const lastVirtualIndex = virtualItems[virtualItems.length - 1]?.index ?? 0;
  const frameByIndex = useMemo(
    () => new Map((data?.frames ?? []).map((frame) => [frame.frame_index, frame])),
    [data?.frames],
  );
  const maxDensity = Math.max(
    1,
    ...(data?.frames ?? []).map((frame) => frame.annotation_count ?? 0),
  );
  const overviewRef = useElementStyle<HTMLDivElement>(
    useMemo(() => {
      const window = { from: sceneStart, to: sceneEnd };
      const from = Math.max(0, Math.min(100, frameToPct(range.startFrame, window)));
      const to = Math.max(from, Math.min(100, frameToPct(range.endFrame, window)));
      return {
        "--scene-window-start": `${from}%`,
        "--scene-window-width": `${to - from}%`,
      } as CSSProperties;
    }, [range.endFrame, range.startFrame, sceneEnd, sceneStart]),
  );
  const majorStep = niceFrameStep(Math.max(1, (scrollRef.current?.clientWidth ?? 600) / cellWidth));

  useEffect(() => {
    const parent = rootRef.current?.parentElement;
    if (!parent || typeof ResizeObserver === "undefined") return;
    const resize = () =>
      setHasVerticalSpace(
        parent.clientHeight === 0 || parent.clientHeight >= 240 + trackHeight + 100,
      );
    resize();
    const observer = new ResizeObserver(resize);
    observer.observe(parent);
    return () => observer.disconnect();
  }, [frameCount, trackHeight]);

  useEffect(() => {
    if (query.isPlaceholderData || !data?.scene_id) return;
    if (confirmedSceneRef.current !== data.scene_id) {
      confirmedSceneRef.current = data.scene_id;
      savedScrollRef.current = Math.max(
        0,
        ((currentFrame ?? sceneStart) - sceneStart) * cellWidth -
          (scrollRef.current?.clientWidth ?? 600) / 2,
      );
      manuallyBrowsedRef.current = false;
      setRange({
        startFrame: Math.max(sceneStart, (currentFrame ?? sceneStart) - 30),
        endFrame: Math.min(sceneEnd, (currentFrame ?? sceneStart) + 49),
      });
      if (scrollRef.current) virtualizer.scrollToOffset(savedScrollRef.current);
    }
  }, [cellWidth, currentFrame, data, query.isPlaceholderData, sceneEnd, sceneStart, virtualizer]);

  useEffect(() => {
    setFrameInput(currentFrame == null ? "" : String(currentFrame));
    setSeekFrame(null);
  }, [currentFrame]);

  useEffect(() => {
    if (!expanded || frameCount <= 1 || virtualItems.length === 0) return;
    const next = timelineQueryRange({ sceneStart, sceneEnd, firstVirtualIndex, lastVirtualIndex });
    setRange((previous) =>
      previous.startFrame === next.startFrame && previous.endFrame === next.endFrame
        ? previous
        : next,
    );
  }, [
    expanded,
    firstVirtualIndex,
    frameCount,
    lastVirtualIndex,
    sceneEnd,
    sceneStart,
    virtualItems.length,
  ]);

  useEffect(() => {
    if (!expanded) return;
    virtualizer.measure();
    virtualizer.scrollToOffset(savedScrollRef.current);
  }, [cellWidth, expanded, taskId, virtualizer]);

  useEffect(() => {
    if (!expanded || !playbackActive || currentFrame == null) return;
    const viewport = scrollRef.current;
    if (!viewport) return;
    const left = (currentFrame - sceneStart) * cellWidth;
    if (
      left < viewport.scrollLeft ||
      left + cellWidth * 2 > viewport.scrollLeft + viewport.clientWidth
    )
      virtualizer.scrollToIndex(currentFrame - sceneStart, { align: "auto" });
  }, [cellWidth, currentFrame, expanded, playbackActive, sceneStart, virtualizer]);

  const originalOwner = useOriginalAssetOwner();
  const prefetchFrame = useCallback(
    (summary: SceneTimelineFrameSummary) => {
      if (summary.state !== "available" || !summary.task_id || summary.task_id === taskId)
        return Promise.resolve();
      return queryClient
        .fetchQuery({
          queryKey: pointCloudManifestQueryKey(summary.task_id),
          queryFn: ({ signal }) => tasksApi.getPointCloudManifest(summary.task_id!, { signal }),
          staleTime: 5 * 60 * 1000,
        })
        .then((manifest) =>
          originalOwner !== originalOwnerKey()
            ? undefined
            : prefetchPointCloudFrameAssets(manifest, {
                depthRasters: prefetchDepthRasters,
                ...(prefetchDecimateThreshold === undefined
                  ? {}
                  : { decimateThreshold: prefetchDecimateThreshold }),
              }),
        )
        .catch(() => {});
    },
    [originalOwner, prefetchDecimateThreshold, prefetchDepthRasters, queryClient, taskId],
  );

  const cancelManual = useCallback(() => {
    if (pointerPrefetchTimerRef.current !== null)
      window.clearTimeout(pointerPrefetchTimerRef.current);
    pointerPrefetchTimerRef.current = null;
    if (navigationTimerRef.current !== null) window.clearTimeout(navigationTimerRef.current);
    navigationTimerRef.current = null;
    pendingNavigationRef.current = null;
    manualRequestRef.current?.abort();
    manualRequestRef.current = null;
  }, []);

  const executeNavigation = useCallback(
    async (target: ScenePlaybackTarget) => {
      if (navigationInFlightRef.current) return false;
      navigationInFlightRef.current = true;
      expectedTaskRef.current = target.taskId;
      setNavigatingTaskId(target.taskId);
      const generation =
        pointCloudNavigationGenerationForTask(target.taskId) ??
        beginPointCloudNavigationTrace({
          source: "timeline",
          targetTaskId: target.taskId,
          frameIndex: target.frameIndex,
        });
      publishPointCloudNavigationTrace({
        source: "timeline",
        type: "navigation-start",
        generation,
        taskId: target.taskId,
        targetTaskId: target.taskId,
        frameIndex: target.frameIndex,
      });
      const epoch = navigationEpochRef.current;
      try {
        const summary = frameByIndex.get(target.frameIndex);
        if (summary) void prefetchFrame(summary);
        const allowed = await onNavigateFrame(target.taskId, target.annotationId);
        publishPointCloudNavigationTrace({
          source: "timeline",
          type: "navigation-resolved",
          generation,
          taskId: target.taskId,
          targetTaskId: target.taskId,
          frameIndex: target.frameIndex,
          allowed,
        });
        if (epoch !== navigationEpochRef.current) return false;
        if (allowed && !pendingNavigationRef.current) {
          for (let index = target.frameIndex + 1; frameByIndex.has(index); index += 1) {
            const next = frameByIndex.get(index)!;
            if (next.state === "available") {
              void prefetchFrame(next);
              break;
            }
          }
        }
        return allowed;
      } finally {
        navigationInFlightRef.current = false;
        setNavigatingTaskId(null);
      }
    },
    [frameByIndex, onNavigateFrame, prefetchFrame],
  );

  const runPendingNavigation: () => void = useCallback(() => {
    navigationTimerRef.current = null;
    if (navigationInFlightRef.current) {
      navigationTimerRef.current = window.setTimeout(
        runPendingNavigation,
        FRAME_NAVIGATION_SETTLE_MS,
      );
      return;
    }
    const target = pendingNavigationRef.current;
    pendingNavigationRef.current = null;
    if (!target) return;
    void executeNavigation(target).catch((error: unknown) =>
      setNotice(error instanceof Error ? error.message : "切帧失败，请重试"),
    );
  }, [executeNavigation]);

  const scheduleNavigation = useCallback(
    (summary: SceneTimelineFrameSummary) => {
      if (!summary.task_id || summary.task_id === taskId || summary.state !== "available") return;
      onPlaybackActiveChange?.(false);
      cancelManual();
      const generation = beginPointCloudNavigationTrace({
        source: "timeline",
        targetTaskId: summary.task_id,
        frameIndex: summary.frame_index,
      });
      pendingNavigationRef.current = {
        taskId: summary.task_id,
        annotationId: summary.selected_track?.annotation_id ?? null,
        frameIndex: summary.frame_index,
      };
      if (navigationInFlightRef.current)
        publishPointCloudNavigationTrace({
          source: "timeline",
          type: "navigation-intent-queued",
          generation,
          taskId: summary.task_id,
          targetTaskId: summary.task_id,
          frameIndex: summary.frame_index,
          pending: true,
        });
      navigationTimerRef.current = window.setTimeout(
        runPendingNavigation,
        FRAME_NAVIGATION_SETTLE_MS,
      );
    },
    [cancelManual, onPlaybackActiveChange, runPendingNavigation, taskId],
  );

  const schedulePointerPrefetch = useCallback(
    (summary: SceneTimelineFrameSummary) => {
      if (playbackActive) return;
      if (pointerPrefetchTimerRef.current !== null)
        window.clearTimeout(pointerPrefetchTimerRef.current);
      pointerPrefetchTimerRef.current = window.setTimeout(() => {
        pointerPrefetchTimerRef.current = null;
        void prefetchFrame(summary);
      }, FRAME_NAVIGATION_SETTLE_MS);
    },
    [playbackActive, prefetchFrame],
  );

  useEffect(() => {
    if (taskId == null && expectedTaskRef.current != null) return;
    if (expectedTaskRef.current !== taskId) {
      cancelManual();
      navigationEpochRef.current += 1;
    }
    expectedTaskRef.current = null;
  }, [cancelManual, taskId]);

  useEffect(
    () => () => {
      cancelManual();
      navigationEpochRef.current += 1;
      if (pointerPrefetchTimerRef.current !== null)
        window.clearTimeout(pointerPrefetchTimerRef.current);
    },
    [cancelManual],
  );

  const fetchFrames = useCallback(
    async (startFrame: number, endFrame: number, signal: AbortSignal) => {
      if (!taskId) throw new Error("当前任务不可用");
      const result = await queryClient.fetchQuery({
        queryKey: [
          SCENE_TIMELINE_QUERY_KEY,
          1,
          taskId,
          startFrame,
          endFrame,
          selectedTrack ?? null,
        ],
        queryFn: ({ signal: querySignal }) =>
          tasksApi.getSceneTimeline(taskId, startFrame, endFrame, selectedTrack, {
            signal: querySignal,
          }),
        staleTime: 30_000,
      });
      if (signal.aborted) throw new DOMException("Aborted", "AbortError");
      if (result.scene_id !== data?.scene_id) throw new Error("Scene 已改变，请重新定位");
      return result.frames ?? [];
    },
    [data?.scene_id, queryClient, selectedTrack, taskId],
  );

  const resolveAccessible = useCallback(
    async (start: number, direction: 1 | -1, signal: AbortSignal) => {
      let index = start;
      let skipped = 0;
      let frames = frameByIndex;
      while (index >= sceneStart && index <= sceneEnd) {
        if (signal.aborted) throw new DOMException("Aborted", "AbortError");
        let frame = frames.get(index);
        if (!frame) {
          const end = Math.min(sceneEnd, index + SCENE_TIMELINE_MAX_WINDOW - 1);
          const start = Math.max(sceneStart, index - SCENE_TIMELINE_MAX_WINDOW + 1);
          frames = new Map(
            (
              await fetchFrames(
                direction === 1 ? index : start,
                direction === 1 ? end : index,
                signal,
              )
            ).map((item) => [item.frame_index, item]),
          );
          frame = frames.get(index);
        }
        if (!frame) throw new Error(`帧 ${index} 的摘要不可用，请重试`);
        if (frame.state === "available" && frame.task_id) {
          if (skipped) setNotice(`已跳过 ${skipped} 个缺失或不可访问帧`);
          return frame;
        }
        skipped += 1;
        index += direction;
      }
      return null;
    },
    [fetchFrames, frameByIndex, sceneEnd, sceneStart],
  );

  const resolveNext = useCallback(
    async ({ restart, signal }: { taskId: string; restart: boolean; signal: AbortSignal }) => {
      const frame = await resolveAccessible(
        restart ? sceneStart : (currentFrame ?? sceneStart) + 1,
        1,
        signal,
      );
      return frame?.task_id
        ? {
            taskId: frame.task_id,
            annotationId: frame.selected_track?.annotation_id ?? null,
            frameIndex: frame.frame_index,
          }
        : null;
    },
    [currentFrame, resolveAccessible, sceneStart],
  );

  const handleActiveChange = useCallback(
    (active: boolean) => {
      if (!active) cancelManual();
      onPlaybackActiveChange?.(active);
    },
    [cancelManual, onPlaybackActiveChange],
  );
  const playback = useScenePlayback({
    active: playbackActive,
    onActiveChange: handleActiveChange,
    taskId,
    sceneId: !taskId || query.isPlaceholderData || !data ? undefined : data.scene_id,
    frameState: frameState ?? { taskId, status: "loading" },
    rate,
    visible,
    blocker: playbackBlockedReason ?? (query.isError ? "Scene 摘要加载失败，请重试" : null),
    atEnd: currentFrame === sceneEnd,
    resolveNext,
    navigate: executeNavigation,
  });
  useEffect(() => {
    if (!playbackActive) {
      setFollowedTrackId(null);
      onPlaybackTrackChange?.(null);
    }
  }, [onPlaybackTrackChange, playbackActive]);

  useEffect(() => {
    if (!taskId || query.isPlaceholderData || currentFrame == null) return;
    const controller = new AbortController();
    if (playbackActive) {
      void resolveAccessible(currentFrame + 1, 1, controller.signal)
        .then((next) => {
          if (next && !controller.signal.aborted) void prefetchFrame(next);
        })
        .catch(() => {});
    } else {
      for (let index = currentFrame + 1; frameByIndex.has(index); index += 1) {
        const next = frameByIndex.get(index)!;
        if (next.state === "available") {
          void prefetchFrame(next);
          break;
        }
      }
    }
    return () => controller.abort();
  }, [
    currentFrame,
    frameByIndex,
    playbackActive,
    prefetchFrame,
    query.isPlaceholderData,
    resolveAccessible,
    taskId,
  ]);

  useEffect(() => {
    if (!taskId) return;
    publishPointCloudNavigationTrace({
      source: "timeline",
      type: "timeline-state",
      generation: pointCloudNavigationGenerationForTask(taskId),
      taskId,
      frameIndex: currentFrame,
      status: query.isPlaceholderData
        ? "placeholder"
        : query.isFetching
          ? "fetching"
          : query.isError
            ? "error"
            : "ready",
      pending: navigationInFlightRef.current || pendingNavigationRef.current !== null,
    });
  }, [currentFrame, query.isError, query.isFetching, query.isPlaceholderData, taskId]);

  const manualNavigate = useCallback(
    async (index: number, direction?: 1 | -1) => {
      onPlaybackActiveChange?.(false);
      cancelManual();
      setNotice(null);
      const controller = new AbortController();
      manualRequestRef.current = controller;
      try {
        const frame = direction
          ? await resolveAccessible(index, direction, controller.signal)
          : (frameByIndex.get(index) ?? (await fetchFrames(index, index, controller.signal))[0]);
        if (controller.signal.aborted) return;
        setSeekFrame(null);
        if (
          !frame ||
          frame.frame_index !== (direction ? frame.frame_index : index) ||
          frame.state !== "available" ||
          !frame.task_id
        ) {
          setNotice(direction ? "已到可访问帧边界" : `帧 ${index} 缺失或不可访问`);
          return;
        }
        scheduleNavigation(frame);
      } catch (error) {
        if (!controller.signal.aborted) {
          setSeekFrame(null);
          setNotice(error instanceof Error ? error.message : "帧摘要加载失败，请重试");
        }
      }
    },
    [
      cancelManual,
      fetchFrames,
      frameByIndex,
      onPlaybackActiveChange,
      resolveAccessible,
      scheduleNavigation,
    ],
  );

  const togglePlayback = () => {
    if (playbackActive) {
      playback.pause();
      return;
    }
    if (
      playbackBlockedReason ||
      navigatingTaskId ||
      query.isPlaceholderData ||
      !frameState ||
      frameState.status !== "ready"
    )
      return;
    cancelManual();
    setNotice(null);
    setFollowedTrackId(trackId);
    onPlaybackTrackChange?.(trackId);
    onPlaybackActiveChange?.(true);
  };
  const onKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    const target = event.target as HTMLElement;
    if (
      event.altKey ||
      event.ctrlKey ||
      event.metaKey ||
      event.shiftKey ||
      target.closest(
        "input,select,textarea,[role=combobox],[role=menu],[role=dialog],[contenteditable=true]",
      ) ||
      (target.closest("button") && !target.closest("[data-testid^=scene-timeline-frame-]"))
    )
      return;
    if (![" ", "ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    event.stopPropagation();
    if (event.key === " ") togglePlayback();
    else if (event.key === "Home") void manualNavigate(sceneStart, 1);
    else if (event.key === "End") void manualNavigate(sceneEnd, -1);
    else
      void manualNavigate(
        (currentFrame ?? sceneStart) + (event.key === "ArrowRight" ? 1 : -1),
        event.key === "ArrowRight" ? 1 : -1,
      );
  };
  const zoom = (direction: 1 | -1) => {
    const index = SCENE_TIMELINE_ZOOM_LEVELS.findIndex((width) => width === cellWidth);
    const width = SCENE_TIMELINE_ZOOM_LEVELS[index + direction];
    if (!width) return;
    savedScrollRef.current = timelineZoomOffset({
      scrollLeft: scrollRef.current?.scrollLeft ?? savedScrollRef.current,
      viewportWidth: scrollRef.current?.clientWidth ?? 600,
      oldWidth: cellWidth,
      newWidth: width,
      currentIndex: (currentFrame ?? sceneStart) - sceneStart,
      followCurrent: !manuallyBrowsedRef.current,
    });
    setCellWidth(width);
  };

  if (query.isError && !data?.scene_id)
    return (
      <div className="flex h-8 shrink-0 items-center justify-between border-t border-border bg-card px-3 text-xs text-status-danger">
        <span>Scene 时间轴加载失败</span>
        <Button size="xs" variant="ghost" onClick={() => void query.refetch()}>
          重试
        </Button>
      </div>
    );
  if (!taskId || !data?.scene_id || currentFrame == null || frameCount <= 1) return null;
  const status =
    playback.error ??
    notice ??
    (query.isError
      ? "Scene 摘要加载失败"
      : playback.waiting
        ? "等待当前帧就绪"
        : playbackActive
          ? "只读预览"
          : null);
  const canNavigate = (direction: 1 | -1) => {
    for (
      let index = currentFrame + direction;
      index >= sceneStart && index <= sceneEnd;
      index += direction
    ) {
      const frame = frameByIndex.get(index);
      if (!frame || (frame.state === "available" && frame.task_id)) return true;
    }
    return false;
  };
  const playReason =
    playbackBlockedReason ??
    (query.isError ? "Scene 摘要加载失败，请重试" : null) ??
    (navigatingTaskId
      ? "正在切帧"
      : query.isPlaceholderData || frameState?.status !== "ready"
        ? "等待当前帧就绪"
        : null);

  return (
    <section
      ref={rootRef}
      className="@container min-w-0 shrink-0 border-t border-border bg-card text-xs text-foreground"
      aria-label="3D Scene 时间轴"
      data-testid="three-d-scene-timeline"
      data-scene-timeline
      data-expanded={expanded}
      tabIndex={0}
      onKeyDown={onKeyDown}
    >
      <div className="flex min-w-0 flex-col">
        <header className="flex h-7 min-w-0 items-center gap-2 px-2.5">
          <span className="min-w-0 truncate font-medium" title={data.scene_name ?? "Scene"}>
            {data.scene_name ?? "Scene"}
          </span>
          {showTrack && expanded && (
            <span className="hidden shrink-0 text-brand @[480px]:inline">当前对象轨迹</span>
          )}
          <span
            role="status"
            className={cn(
              "ml-auto min-w-0 truncate",
              notice || playback.error || query.isError
                ? "text-status-danger"
                : "text-muted-foreground",
            )}
            title={status ?? undefined}
          >
            {notice || playback.error ? <CopyableError message={status} truncate /> : status}
          </span>
          {(query.isError || playback.error || frameState?.status === "error") && (
            <Button
              size="xs"
              variant="ghost"
              onClick={() => {
                playback.pause();
                onRetryFrame?.();
                void query.refetch();
              }}
            >
              重试
            </Button>
          )}
          <Tooltip
            name={expanded ? "收起 Scene 时间轴" : "展开 Scene 时间轴"}
            desc={!hasVerticalSpace ? "增大画布高度后恢复展开" : undefined}
            side="top"
          >
            <Button
              size="xs"
              variant="ghost"
              className="size-6 shrink-0 p-0"
              aria-label={expanded ? "收起 Scene 时间轴" : "展开 Scene 时间轴"}
              aria-expanded={expanded}
              data-testid="scene-timeline-toggle"
              onClick={() => setExpandedPreference((value) => !value)}
            >
              <Icon name={expanded ? "chevDown" : "chevUp"} />
            </Button>
          </Tooltip>
        </header>
        {expanded && (
          <div className="flex min-w-0 border-y border-border" data-testid="scene-timeline-tracks">
            <div className="w-[72px] shrink-0 text-muted-foreground" aria-hidden="true">
              <div className="flex h-6 items-center border-b border-border px-2">帧</div>
              <div className="flex h-6 items-center px-2">任务状态</div>
              <div
                className="flex h-6 items-center px-2"
                title={`当前窗口最大 ${maxDensity} 个标注`}
              >
                标注数量
              </div>
              {showTrack && (
                <div
                  className="flex h-6 items-center px-2"
                  title="圆点：关键帧；菱形：派生；空心圆：采样。空白不代表对象缺席。"
                >
                  当前对象
                </div>
              )}
              {showQuality && <div className="flex h-6 items-center px-2">质检问题</div>}
            </div>
            <div
              ref={scrollRef}
              className="min-w-0 flex-1 overflow-x-auto overflow-y-hidden"
              aria-label="Scene 帧列表"
              tabIndex={0}
              onScroll={() => {
                savedScrollRef.current = scrollRef.current?.scrollLeft ?? 0;
              }}
              onWheel={() => {
                manuallyBrowsedRef.current = true;
              }}
              onPointerDown={() => {
                manuallyBrowsedRef.current = true;
              }}
            >
              <TimelineSizer
                totalWidth={virtualizer.getTotalSize()}
                start={virtualItems[0]?.start ?? 0}
                height={trackHeight}
              >
                {virtualItems.map((item) => (
                  <TimelineCell
                    key={item.key}
                    item={item}
                    frameIndex={sceneStart + item.index}
                    summary={frameByIndex.get(sceneStart + item.index)}
                    current={sceneStart + item.index === currentFrame}
                    maxDensity={maxDensity}
                    major={(sceneStart + item.index) % majorStep === 0}
                    showTrack={showTrack}
                    showQuality={showQuality}
                    qualitySeverity={qualityMarkers[sceneStart + item.index]}
                    onNavigate={scheduleNavigation}
                    onPrefetch={schedulePointerPrefetch}
                    onOpenQuality={onOpenQuality}
                  />
                ))}
              </TimelineSizer>
            </div>
          </div>
        )}
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 px-2 py-1 @[720px]:flex-nowrap">
          <div className="flex shrink-0 items-center gap-0.5">
            <Tooltip name="上一可访问帧" side="top">
              <Button
                size="xs"
                variant="ghost"
                className="size-6 p-0"
                aria-label="上一帧"
                disabled={!canNavigate(-1)}
                onClick={() => void manualNavigate(currentFrame - 1, -1)}
              >
                <Icon name="chevLeft" />
              </Button>
            </Tooltip>
            <Tooltip
              name={playbackActive ? "暂停" : "播放"}
              desc={playReason ?? "只读逐帧预览；就绪后按所选浏览速率停留"}
              side="top"
            >
              <span>
                <Button
                  size="xs"
                  variant={playbackActive ? "primary" : "ghost"}
                  className="size-6 p-0"
                  aria-label={playbackActive ? "暂停 Scene 播放" : "播放 Scene"}
                  data-testid="scene-timeline-play"
                  disabled={!playbackActive && (!!playReason || !onPlaybackActiveChange)}
                  onClick={togglePlayback}
                >
                  <Icon name={playbackActive ? "pause" : "play"} />
                </Button>
              </span>
            </Tooltip>
            <Tooltip name="下一可访问帧" side="top">
              <Button
                size="xs"
                variant="ghost"
                className="size-6 p-0"
                aria-label="下一帧"
                disabled={!canNavigate(1)}
                onClick={() => void manualNavigate(currentFrame + 1, 1)}
              >
                <Icon name="chevRight" />
              </Button>
            </Tooltip>
          </div>
          <form
            className="flex shrink-0 items-center gap-1 tabular-nums"
            onSubmit={(event) => {
              event.preventDefault();
              if (
                !/^-?\d+$/.test(frameInput) ||
                Number(frameInput) < sceneStart ||
                Number(frameInput) > sceneEnd
              ) {
                setNotice(`请输入 ${sceneStart}–${sceneEnd} 范围内的整数帧号`);
                return;
              }
              void manualNavigate(Number(frameInput));
            }}
          >
            <label htmlFor="scene-frame-input" className="text-brand">
              F
            </label>
            <input
              id="scene-frame-input"
              type="text"
              inputMode="numeric"
              aria-label="Scene 帧号"
              className="h-6 w-14 rounded-sm border border-border bg-background px-1 text-xs tabular-nums focus-visible:outline-2 focus-visible:outline-brand"
              value={frameInput}
              onChange={(event) => setFrameInput(event.target.value)}
            />
            <span className="text-muted-foreground">
              · {currentFrame - sceneStart + 1}/{frameCount}
            </span>
          </form>
          <select
            aria-label="Scene 浏览速率"
            title="最高浏览速率；加载较慢时实际速率降低"
            className="h-6 shrink-0 rounded-sm border border-border bg-card px-1 text-xs focus-visible:outline-2 focus-visible:outline-brand"
            value={rate}
            onChange={(event) => setRate(Number(event.target.value) as ScenePlaybackRate)}
          >
            <option value={1}>1 帧/秒</option>
            <option value={2}>2 帧/秒</option>
            <option value={4}>4 帧/秒</option>
          </select>
          <div className="flex basis-full items-center gap-1 @[720px]:ml-auto @[720px]:basis-auto">
            {expanded && (
              <>
                <Tooltip name="缩小时间轴" side="top">
                  <Button
                    size="xs"
                    variant="ghost"
                    className="size-6 p-0"
                    aria-label="缩小 Scene 时间轴"
                    disabled={cellWidth === 24}
                    onClick={() => zoom(-1)}
                  >
                    <Icon name="minus" />
                  </Button>
                </Tooltip>
                <Tooltip name="放大时间轴" side="top">
                  <Button
                    size="xs"
                    variant="ghost"
                    className="size-6 p-0"
                    aria-label="放大 Scene 时间轴"
                    disabled={cellWidth === 80}
                    onClick={() => zoom(1)}
                  >
                    <Icon name="plus" />
                  </Button>
                </Tooltip>
                <Tooltip name="定位当前帧" side="top">
                  <Button
                    size="xs"
                    variant="ghost"
                    className="size-6 p-0"
                    aria-label="定位当前帧"
                    onClick={() => {
                      manuallyBrowsedRef.current = false;
                      virtualizer.scrollToIndex(currentFrame - sceneStart, { align: "center" });
                    }}
                  >
                    <Icon name="target" />
                  </Button>
                </Tooltip>
                <span className="hidden tabular-nums text-muted-foreground @[480px]:inline">
                  窗口最大 {maxDensity}
                </span>
              </>
            )}
            <div className="ml-auto flex items-center gap-1">
              {onOpenQuality && (
                <Tooltip
                  name={`3D 质检${qualityIssueCount > 0 ? ` · ${qualityIssueCount}` : ""}`}
                  side="top"
                >
                  <Button
                    size="xs"
                    variant="ghost"
                    aria-label={`3D 质检${qualityIssueCount > 0 ? ` · ${qualityIssueCount}` : ""}`}
                    data-testid="scene-quality-open"
                    onClick={onOpenQuality}
                  >
                    <Icon name="shieldAlert" />
                    <span className="hidden @[480px]:inline">质检</span>
                    {qualityIssueCount > 0 && (
                      <span className="tabular-nums">{qualityIssueCount}</span>
                    )}
                  </Button>
                </Tooltip>
              )}
              <Tooltip name="跨帧任务" side="top">
                <Button
                  size="xs"
                  variant="ghost"
                  aria-label="跨帧任务"
                  data-testid="scene-cross-frame-job-center"
                  onClick={() => setJobCenterOpen(true)}
                >
                  <Icon name="layers" />
                  <span className="hidden @[480px]:inline">跨帧任务</span>
                </Button>
              </Tooltip>
            </div>
          </div>
        </div>
        <div className="flex h-4 items-center gap-2 px-3 pb-1 text-2xs tabular-nums text-muted-foreground">
          <span>F{sceneStart}</span>
          <div
            ref={overviewRef}
            className="relative flex min-w-0 flex-1 items-center"
            title={`摘要窗口 F${range.startFrame}–F${range.endFrame}；其他区间尚未加载`}
          >
            <span
              className="pointer-events-none absolute -bottom-0.5 h-0.5 bg-brand/40 left-[var(--scene-window-start)] w-[var(--scene-window-width)]"
              aria-hidden="true"
            />
            <input
              type="range"
              className="h-3 min-w-0 flex-1 cursor-pointer accent-brand"
              aria-label="Scene 全段位置"
              aria-valuetext={`帧 ${seekFrame ?? currentFrame}，共 ${frameCount} 帧`}
              min={sceneStart}
              max={sceneEnd}
              step={1}
              value={seekFrame ?? currentFrame}
              onPointerDown={() => {
                seekDraggingRef.current = true;
              }}
              onChange={(event) => {
                const value = Number(event.target.value);
                setSeekFrame(value);
                if (!seekDraggingRef.current) void manualNavigate(value);
              }}
              onPointerUp={(event) => {
                seekDraggingRef.current = false;
                void manualNavigate(Number(event.currentTarget.value));
              }}
              onPointerCancel={() => {
                seekDraggingRef.current = false;
                setSeekFrame(null);
              }}
            />
          </div>
          <span>{seekFrame == null ? `F${sceneEnd}` : `F${seekFrame}`}</span>
        </div>
      </div>
      <CrossFrameJobCenter
        open={jobCenterOpen}
        onClose={() => setJobCenterOpen(false)}
        taskId={taskId}
        currentFrame={currentFrame}
        sceneStartFrame={sceneStart}
        sceneEndFrame={sceneEnd}
        selectedAnnotationIds={selectedAnnotationIds}
        selectedTrackId={selectedTrack}
        boxCount={boxCount}
        readOnly={readOnly || playbackActive}
      />
    </section>
  );
}

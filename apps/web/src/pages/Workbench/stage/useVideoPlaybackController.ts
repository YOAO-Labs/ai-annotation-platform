import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { RefObject } from "react";
import type {
  AnnotationResponse,
  TaskVideoFrameTimetableResponse,
  TaskVideoManifestResponse,
  VideoSamplingConfig,
} from "@/types";
import type { AnnotationFeedback } from "@/api/feedbacks";
import type { WorkbenchPerformanceTier } from "../state/performanceTier";
import { resolveWorkbenchPerformanceTier } from "../state/performanceTier";
import { buildFrameTimebase, frameToTime, timeToFrame } from "./frameTimebase";
import type { FrameTimebase } from "./frameTimebase";
import { deriveSamplingStep, gridNext, gridPrev, microStep, snapToGrid } from "./videoSamplingGrid";
import { useFrameClock, type FrameSeekResult } from "./useFrameClock";
import { useVideoBitmapCache } from "./useVideoBitmapCache";
import type { CachedVideoBitmap } from "./useVideoBitmapCache";
import { useVideoPreciseFrame, type PreciseFrameSourceState } from "./useVideoPreciseFrame";
import {
  clearVideoPreciseFrameDiagnostics,
  publishVideoPreciseFrameDiagnostics,
  setActiveVideoWorkbenchTask,
} from "@/utils/videoWorkbenchDiagnostics";
import { imageBitmapToJpeg, videoElementToJpeg } from "@/utils/imageBitmapToJpeg";
import { useVideoFramePreview } from "./useVideoFramePreview";
import type { VideoFramePreview } from "./useVideoFramePreview";
import { useVideoTrackActions } from "./useVideoTrackActions";
import {
  emptyVideoJumpHistory,
  jumpVideoHistory,
  normalizeLoopRegion,
  parseStoredBookmarks,
  parseStoredJumpHistory,
  parseStoredLoopRegion,
  pushVideoJumpHistory,
  toggleVideoBookmark,
  videoNavigationStorageKey,
} from "./videoNavigationState";
import type { VideoBookmark, VideoJumpHistory, VideoLoopRegion } from "./videoNavigationState";
import {
  buildGlobalTimelineDensity,
  buildPredictionDensity,
  buildSelectedTrackTimeline,
  nextVisibleKeyframeFrame,
} from "./videoTrackTimeline";
import type {
  PredictionDensityBin,
  VideoTimelineDensityBin,
  VideoTrackTimeline,
} from "./videoTrackTimeline";
import { adjacentPredictedFrame } from "./aiBoxFrames";
import { getTrackColor } from "./colors";
import { modeFromDrag, getVideoStageModeGuard } from "./videoStageMode";
import {
  isVideoBbox,
  isVideoTrack,
  shapeIou,
  shortTrackId,
  sortedKeyframes,
} from "./videoStageGeometry";
import type { VideoStageGeom, VideoDragState, VideoTrackAnnotation } from "./videoStageTypes";
import type {
  VideoFramePresentation,
  VideoFrameSeekResult,
  VideoStageControls,
} from "./videoStageControls";

const VIDEO_PLAYBACK_RATES = [0.25, 0.5, 1, 2, 4] as const;
const DEFAULT_VIDEO_PLAYBACK_RATE: VideoPlaybackRate = 1;
export const VIDEO_FRAME_READY_TIMEOUT_MS = 3000;

interface PendingFramePresentation {
  requestId: number;
  frameIndex: number;
  ownerEpoch: number;
  navigationGeneration: number;
  timer: ReturnType<typeof setTimeout> | null;
  resolve: (result: VideoFrameSeekResult) => void;
}

type VideoPlaybackRate = (typeof VIDEO_PLAYBACK_RATES)[number];
type VideoJogPlayback = { direction: -1 | 0 | 1; rate: VideoPlaybackRate };

const DEFAULT_PAUSED_JOG_PLAYBACK: VideoJogPlayback = {
  direction: 0,
  rate: DEFAULT_VIDEO_PLAYBACK_RATE,
};

function nextHigherPlaybackRate(rate: VideoPlaybackRate): VideoPlaybackRate {
  const idx = VIDEO_PLAYBACK_RATES.indexOf(rate);
  return VIDEO_PLAYBACK_RATES[Math.min(VIDEO_PLAYBACK_RATES.length - 1, idx + 1)];
}

function nextLowerPlaybackRate(rate: VideoPlaybackRate): VideoPlaybackRate | null {
  const idx = VIDEO_PLAYBACK_RATES.indexOf(rate);
  return idx <= 0 ? null : VIDEO_PLAYBACK_RATES[idx - 1];
}

export interface UseVideoPlaybackControllerOptions {
  manifest: TaskVideoManifestResponse | undefined;
  onSourceRecover?: () => void;
  onSourcePrimed?: () => void;
  onPauseIntent?: () => void;
  isSourceReady?: () => boolean;
  frameTimetable?: TaskVideoFrameTimetableResponse;
  videoRef: RefObject<HTMLVideoElement | null>;
  controlledFrameIndex?: number;
  onFrameIndexChange?: (frame: number) => void;
  onSelect?: (id: string | null, options?: { source: "task-reset" }) => void;
  performanceTier?: WorkbenchPerformanceTier;
  videoSampling?: VideoSamplingConfig | null;
  defaultPlaybackRate?: VideoPlaybackRate;
  annotations: AnnotationResponse[];
  annotationWritesPending?: boolean;
  predictedFrames?: readonly number[];
  selectedId: string | null;
  selectedTrack: VideoTrackAnnotation | null;
  trackColorOverrides?: Record<string, string>;
  hiddenTrackIds: ReadonlySet<string>;
  lockedTrackIds: ReadonlySet<string>;
  readOnly: boolean;
  drag: VideoDragState;
  currentFrameEntries: Array<{ geom: VideoStageGeom; className: string }>;
  issuePixelFeedbacks?: AnnotationFeedback[];
  onUpdate: (annotation: AnnotationResponse, geometry: VideoTrackAnnotation["geometry"]) => void;
  onToggleHiddenTrack?: (id: string) => void;
  onToggleLockedTrack?: (id: string) => void;
  onPropagateTrack?: (annotation: VideoTrackAnnotation) => void;
}

export interface UseVideoPlaybackControllerResult {
  frameIndex: number;
  isPlaybackActive: boolean;
  isJogPlaying: boolean;
  jogPlayback: VideoJogPlayback;
  playbackError: string | null;
  activeBitmap: CachedVideoBitmap | null;
  cachedRanges: { from: number; to: number }[];
  displayBitmap: CachedVideoBitmap | null;
  frameSource: "webcodecs" | "video-bitmap" | "video-element";
  preciseSourceState: PreciseFrameSourceState;
  precisePaintedFrameIndex: number | null;
  markPreciseFramePainted: (frameIndex: number) => void;
  framePresentation: VideoFramePresentation | null;
  markFramePresented: (presentation: VideoFramePresentation) => void;
  framePreview: VideoFramePreview | null;
  previewFrame: (frameIndex: number | null) => void;
  samplingStep: number;
  maxFrame: number;
  timebase: FrameTimebase;
  fps: number;
  selectedTrackTimeline: VideoTrackTimeline | null;
  selectedTrackColor: string | null;
  selectedTrackKeyframes: ReturnType<typeof sortedKeyframes>;
  globalTimelineDensity: VideoTimelineDensityBin[];
  predictionDensity: PredictionDensityBin[];
  hasPredictedFrames: boolean;
  seekToAdjacentPredictedFrame: (dir: -1 | 1) => void;
  qualityWarnings: string[];
  issueFrames: number[];
  playbackOverlayVisible: boolean;
  highlightAction: "prev" | "next" | "play" | null;
  bookmarks: VideoBookmark[];
  loopRegion: VideoLoopRegion | null;
  trackActions: ReturnType<typeof useVideoTrackActions>;
  canDeleteSelectedTrackKeyframe: boolean;
  deleteSelectedTrackKeyframe: () => boolean;
  showPlaybackOverlay: () => void;
  schedulePlaybackOverlayHide: () => void;
  setNormalizedLoopRegion: (region: VideoLoopRegion) => void;
  clearLoopRegion: () => void;
  toggleBookmark: () => void;
  seekToFrame: (frameIndex: number, options?: { recordHistory?: boolean }) => void;
  seekOverlayByFrames: (delta: number, options?: { recordHistory?: boolean }) => void;
  pausePlayback: (options?: { snapToGrid?: boolean }) => void;
  controls: Omit<
    VideoStageControls,
    "cycleInCategory" | "stepCategory" | "focusObject" | "focusRegion" | "normToClient"
  >;
}

export function useVideoPlaybackController({
  manifest,
  onSourceRecover,
  onSourcePrimed,
  onPauseIntent,
  isSourceReady,
  frameTimetable,
  videoRef,
  controlledFrameIndex,
  onFrameIndexChange,
  onSelect,
  performanceTier = "standard",
  videoSampling = null,
  defaultPlaybackRate = DEFAULT_VIDEO_PLAYBACK_RATE,
  annotations,
  annotationWritesPending = false,
  predictedFrames = [],
  selectedTrack,
  trackColorOverrides,
  hiddenTrackIds,
  lockedTrackIds,
  readOnly,
  drag,
  currentFrameEntries,
  issuePixelFeedbacks,
  onUpdate,
  onToggleHiddenTrack,
  onToggleLockedTrack,
  onPropagateTrack,
}: UseVideoPlaybackControllerOptions): UseVideoPlaybackControllerResult {
  const pausedJogPlayback = useMemo<VideoJogPlayback>(
    () => ({ direction: 0, rate: defaultPlaybackRate }),
    [defaultPlaybackRate],
  );
  const pausedJogPlaybackRef = useRef<VideoJogPlayback>(pausedJogPlayback);
  pausedJogPlaybackRef.current = pausedJogPlayback;

  const [uncontrolledFrameIndex, setUncontrolledFrameIndex] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [jogPlayback, setJogPlayback] = useState<VideoJogPlayback>(() => pausedJogPlayback);
  const [playbackError, setPlaybackError] = useState<string | null>(null);
  const [, refreshNativeReadiness] = useState(0);
  const [playbackOverlayVisible, setPlaybackOverlayVisible] = useState(true);
  const [highlightAction, setHighlightAction] = useState<"prev" | "next" | "play" | null>(null);
  const [loopRegion, setLoopRegion] = useState<VideoLoopRegion | null>(null);
  const [bookmarks, setBookmarks] = useState<VideoBookmark[]>([]);
  const [jumpHistory, setJumpHistory] = useState<VideoJumpHistory>(() => emptyVideoJumpHistory());

  const lastResetTaskIdRef = useRef<string | null>(null);
  const overlayHideTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const highlightTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const frameIndexRef = useRef(0);
  const frameNavigationTargetRef = useRef<number | null>(null);
  const jogPlaybackRef = useRef<VideoJogPlayback>(DEFAULT_PAUSED_JOG_PLAYBACK);
  const onSelectRef = useRef(onSelect);
  const mountedRef = useRef(true);
  const navigationGenerationRef = useRef(0);
  const presentationSequenceRef = useRef(0);
  const pendingPresentationRef = useRef<PendingFramePresentation | null>(null);
  const [presentationRequest, setPresentationRequest] = useState<PendingFramePresentation | null>(
    null,
  );
  const sourceKey = JSON.stringify([manifest?.task_id ?? null, manifest?.video_url ?? null]);
  const sourceOwnerRef = useRef({ key: sourceKey, epoch: 0 });
  if (sourceOwnerRef.current.key !== sourceKey) {
    sourceOwnerRef.current = { key: sourceKey, epoch: sourceOwnerRef.current.epoch + 1 };
  }
  const sourceEpoch = sourceOwnerRef.current.epoch;
  const isSourceReadyRef = useRef(isSourceReady);
  isSourceReadyRef.current = isSourceReady;
  const playableSource = !isSourceReady || isSourceReady();

  const settlePresentation = useCallback(
    (request: PendingFramePresentation, result: VideoFrameSeekResult, publish = true) => {
      if (pendingPresentationRef.current !== request) return;
      pendingPresentationRef.current = null;
      if (request.timer !== null) clearTimeout(request.timer);
      request.resolve(result);
      if (publish && mountedRef.current) setPresentationRequest(null);
    },
    [],
  );
  const cancelPresentation = useCallback(
    (publish = true) => {
      const request = pendingPresentationRef.current;
      if (request)
        settlePresentation(
          request,
          { status: "cancelled", frameIndex: request.frameIndex, source: null },
          publish,
        );
    },
    [settlePresentation],
  );

  useEffect(() => {
    onSelectRef.current = onSelect;
  }, [onSelect]);

  const frameIndex = controlledFrameIndex ?? uncontrolledFrameIndex;
  frameIndexRef.current = frameIndex;
  frameNavigationTargetRef.current = frameIndex;
  jogPlaybackRef.current = jogPlayback;
  const isJogPlaying = jogPlayback.direction !== 0;
  const isPlaybackActive = isPlaying || isJogPlaying;
  const isPlaybackActiveRef = useRef(isPlaybackActive);
  isPlaybackActiveRef.current = isPlaybackActive;

  useEffect(() => {
    if (jogPlaybackRef.current.direction === 0) {
      setJogPlayback(pausedJogPlayback);
    }
    if (!isPlaybackActive && videoRef.current) {
      videoRef.current.playbackRate = defaultPlaybackRate;
    }
  }, [defaultPlaybackRate, isPlaybackActive, pausedJogPlayback, videoRef]);

  const setFrameIndex = useCallback(
    (nextFrame: number) => {
      frameIndexRef.current = nextFrame;
      if (controlledFrameIndex === undefined) setUncontrolledFrameIndex(nextFrame);
      onFrameIndexChange?.(nextFrame);
    },
    [controlledFrameIndex, onFrameIndexChange],
  );

  const performanceConfig = useMemo(
    () => resolveWorkbenchPerformanceTier(performanceTier),
    [performanceTier],
  );

  const timebase = useMemo(
    () =>
      buildFrameTimebase(
        manifest?.metadata,
        !manifest?.metadata_generation_id ||
          frameTimetable?.metadata_generation_id === manifest.metadata_generation_id
          ? frameTimetable
          : undefined,
      ),
    [frameTimetable, manifest?.metadata, manifest?.metadata_generation_id],
  );
  const fps = timebase.fps;
  const frameCount = timebase.frameCount;
  const maxFrame = Math.max(0, frameCount - 1);

  const samplingStep = useMemo(() => deriveSamplingStep(videoSampling, fps), [videoSampling, fps]);
  const samplingStepRef = useRef(samplingStep);
  samplingStepRef.current = samplingStep;

  const stageMode = modeFromDrag(drag);
  const stageModeGuard = getVideoStageModeGuard(stageMode);
  const canSetupFrameRef = useRef(stageModeGuard.canSetupFrame);
  canSetupFrameRef.current = stageModeGuard.canSetupFrame;

  const handleFrameClockChange = useCallback(
    (nextFrame: number) => {
      if (!stageModeGuard.canSetupFrame) {
        videoRef.current?.pause();
        setJogPlayback(pausedJogPlaybackRef.current);
        return;
      }
      if (isPlaybackActive && loopRegion && nextFrame > loopRegion.endFrame) {
        setFrameIndex(loopRegion.startFrame);
        const video = videoRef.current;
        if (video) video.currentTime = frameToTime(loopRegion.startFrame, timebase);
        return;
      }
      if (
        !isPlaybackActiveRef.current &&
        nextFrame !== frameNavigationTargetRef.current &&
        nextFrame !== frameIndexRef.current
      ) {
        return;
      }
      setFrameIndex(nextFrame);
    },
    [isPlaybackActive, loopRegion, setFrameIndex, stageModeGuard.canSetupFrame, timebase, videoRef],
  );

  const frameClock = useFrameClock({
    videoRef,
    sourceKey,
    frameIndex,
    timebase,
    isPlaying,
    onFrameChange: handleFrameClockChange,
  });
  const {
    getFrameEvidence,
    nativeFrame: nativeFrameEvidence,
    seekToAsync: seekNativeFrameAsync,
  } = frameClock;

  const {
    preview: framePreview,
    previewFor: previewFrame,
    prefetch: prefetchFrames,
  } = useVideoFramePreview({
    taskId: manifest?.task_id,
    metadataGenerationId: manifest?.metadata_generation_id,
    maxFrame,
    enabled: Boolean(manifest) && playableSource,
    width: 320,
    format: "webp",
    maxCacheItems: performanceConfig.previewCache,
    scrubPrefetchHalfWindow: performanceConfig.prefetchHalfWindow,
    anchorPrefetchCount: performanceConfig.anchorPrefetch,
  });
  const lastTrackPrefetchRef = useRef<{
    prefetch: typeof prefetchFrames;
    frames: Set<number>;
  }>();

  const {
    activeBitmap,
    cachedRanges,
    capture: captureBitmapFrame,
    showFrame: showCachedBitmapFrame,
  } = useVideoBitmapCache({
    taskId: manifest?.task_id,
    sourceKey: JSON.stringify([
      sourceKey,
      manifest?.metadata_generation_id ?? null,
      timebase.ptsMs ? "pts" : "fps",
    ]),
    maxItems: performanceConfig.videoBitmapCache,
  });

  const precise = useVideoPreciseFrame({
    taskId: manifest?.task_id,
    metadataGenerationId: manifest?.metadata_generation_id,
    frameIndex,
    enabled: !isPlaybackActive && playableSource,
    decodeEnabled: !frameClock.isSeeking || presentationRequest !== null,
    bitmapBudgetBytes: performanceConfig.videoDecoderBitmapCacheBytes,
    chunkBudgetBytes: performanceConfig.videoChunkByteCacheBytes,
    prefetchFrames: performanceConfig.videoDecodePrefetchFrames,
  });
  const preciseBitmap =
    precise.bitmap && precise.bitmap.frameIndex === frameIndex ? precise.bitmap : null;
  const nativeBitmap = activeBitmap && activeBitmap.frameIndex === frameIndex ? activeBitmap : null;
  const displayBitmap = isPlaybackActive ? null : (preciseBitmap ?? nativeBitmap);
  const displayBitmapRef = useRef(displayBitmap);
  displayBitmapRef.current = displayBitmap;
  const frameSource: "webcodecs" | "video-bitmap" | "video-element" = isPlaybackActive
    ? "video-element"
    : preciseBitmap
      ? "webcodecs"
      : nativeBitmap
        ? "video-bitmap"
        : "video-element";
  const frameSourceRef = useRef(frameSource);
  frameSourceRef.current = frameSource;

  const captureVerifiedFrame = useCallback(
    (video: HTMLVideoElement | null, targetFrame: number) => {
      const evidence = getFrameEvidence(targetFrame);
      if (!video || !evidence || evidence.video !== video) return Promise.resolve(null);
      const epoch = sourceOwnerRef.current.epoch;
      const generation = navigationGenerationRef.current;
      return captureBitmapFrame(video, targetFrame, {
        isCurrent: () =>
          mountedRef.current &&
          sourceOwnerRef.current.epoch === epoch &&
          navigationGenerationRef.current === generation &&
          videoRef.current === video &&
          !isPlaybackActiveRef.current &&
          video.paused &&
          frameIndexRef.current === targetFrame &&
          getFrameEvidence(targetFrame) === evidence,
      });
    },
    [captureBitmapFrame, getFrameEvidence, videoRef],
  );

  const currentNativeEvidence =
    nativeFrameEvidence?.frameIndex === frameIndex ? getFrameEvidence(frameIndex) : null;
  const framePresentation = useMemo<VideoFramePresentation | null>(() => {
    const request = presentationRequest;
    if (
      !request ||
      request !== pendingPresentationRef.current ||
      request.ownerEpoch !== sourceEpoch ||
      request.frameIndex !== frameIndex ||
      isPlaybackActive ||
      !stageModeGuard.canSetupFrame
    )
      return null;
    const shownBitmap =
      displayBitmap?.frameIndex === request.frameIndex ? displayBitmap.bitmap : null;
    const evidence = !shownBitmap ? currentNativeEvidence : null;
    const image = shownBitmap ?? evidence?.video ?? null;
    if (!image || (shownBitmap && (!shownBitmap.width || !shownBitmap.height))) return null;
    const source = frameSource;
    return {
      requestId: request.requestId,
      frameIndex: request.frameIndex,
      source,
      image,
      isCurrent: () =>
        mountedRef.current &&
        (isSourceReadyRef.current?.() ?? true) &&
        pendingPresentationRef.current === request &&
        sourceOwnerRef.current.epoch === request.ownerEpoch &&
        navigationGenerationRef.current === request.navigationGeneration &&
        frameIndexRef.current === request.frameIndex &&
        canSetupFrameRef.current &&
        !isPlaybackActiveRef.current &&
        frameSourceRef.current === source &&
        (shownBitmap
          ? displayBitmapRef.current?.bitmap === shownBitmap &&
            shownBitmap.width > 0 &&
            shownBitmap.height > 0
          : displayBitmapRef.current === null &&
            videoRef.current === image &&
            !!evidence &&
            evidence.video.paused &&
            getFrameEvidence(request.frameIndex) === evidence),
    };
  }, [
    displayBitmap,
    getFrameEvidence,
    currentNativeEvidence,
    frameIndex,
    frameSource,
    isPlaybackActive,
    presentationRequest,
    sourceEpoch,
    stageModeGuard.canSetupFrame,
    videoRef,
  ]);

  const markFramePresented = useCallback(
    (presentation: VideoFramePresentation) => {
      const request = pendingPresentationRef.current;
      if (
        !request ||
        !(isSourceReadyRef.current?.() ?? true) ||
        request.requestId !== presentation.requestId ||
        request.frameIndex !== presentation.frameIndex ||
        !presentation.isCurrent()
      )
        return;
      settlePresentation(request, {
        status: "ready",
        frameIndex: presentation.frameIndex,
        source: presentation.source,
      });
    },
    [settlePresentation],
  );

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      cancelPresentation(false);
    };
  }, [cancelPresentation]);

  useEffect(() => {
    cancelPresentation();
    return () => cancelPresentation(false);
  }, [cancelPresentation, sourceEpoch]);

  useEffect(() => {
    const request = pendingPresentationRef.current;
    if (!request) return;
    if (isPlaybackActive || request.frameIndex !== frameIndex) {
      cancelPresentation();
    } else if (
      !stageModeGuard.canSetupFrame ||
      (videoRef.current?.error &&
        !displayBitmap &&
        ["disabled", "fallback"].includes(precise.sourceState))
    ) {
      settlePresentation(request, {
        status: "unavailable",
        frameIndex: request.frameIndex,
        source: null,
      });
    }
  }, [
    cancelPresentation,
    displayBitmap,
    frameIndex,
    isPlaybackActive,
    playbackError,
    precise.sourceState,
    settlePresentation,
    stageModeGuard.canSetupFrame,
    videoRef,
  ]);

  const diagnosticsRoute =
    typeof window !== "undefined" ? `${window.location.pathname}${window.location.search}` : "";
  const diagnosticsTaskId = manifest?.task_id ?? null;
  useEffect(() => {
    if (!diagnosticsTaskId) return;
    setActiveVideoWorkbenchTask(diagnosticsTaskId);
    return () => {
      clearVideoPreciseFrameDiagnostics(diagnosticsTaskId);
    };
  }, [diagnosticsTaskId]);
  useEffect(() => {
    if (!diagnosticsTaskId) return;
    publishVideoPreciseFrameDiagnostics(
      diagnosticsTaskId,
      {
        enabled: precise.diagnostics.webcodecsEnabled,
        supported: precise.diagnostics.supported,
        state: precise.sourceState,
        source:
          frameSource === "webcodecs"
            ? "webcodecs"
            : frameSource === "video-bitmap"
              ? "native-bitmap"
              : "video",
        frameIndex,
        chunkId: precise.diagnostics.chunkId,
        gopStartDecodeIndex: precise.performance.gopStartDecodeIndex,
        targetTimestampUs: precise.performance.targetTimestampUs,
        codec: precise.performance.codec,
        fallbackReason: precise.fallbackReason,
        lastDemuxMs: precise.performance.lastDemuxMs,
        lastQueueMs: precise.performance.lastQueueMs,
        lastCodecMs: precise.performance.lastCodecMs,
        lastDecodeMs: precise.performance.lastDecodeMs,
        lastBitmapMs: precise.performance.lastBitmapMs,
        lastDecodeMode: precise.performance.lastDecodeMode,
        lastPaintMs: precise.performance.lastPaintMs,
        lastVisibleMs: precise.performance.lastVisibleMs,
        paintedFrameIndex: precise.performance.paintedFrameIndex,
        cache: {
          bitmapBytes: precise.performance.bitmapBytes,
          bitmapBudgetBytes: precise.performance.bitmapBudgetBytes,
          chunkBytes: precise.performance.chunkBytes,
          chunkBudgetBytes: precise.performance.chunkBudgetBytes,
        },
        counters: {
          activeDecoders: precise.performance.activeDecoders,
          liveVideoFrames: precise.performance.liveVideoFrames,
          sessionCreates: precise.performance.sessionCreates,
          sessionResets: precise.performance.sessionResets,
          encodedChunksSubmitted: precise.performance.encodedChunksSubmitted,
          staleResults: precise.performance.staleResults,
          prefetchRequests: precise.performance.prefetchRequests,
          prefetchHits: precise.performance.prefetchHits,
        },
      },
      diagnosticsRoute,
    );
  }, [
    diagnosticsTaskId,
    diagnosticsRoute,
    precise.diagnostics,
    precise.sourceState,
    precise.fallbackReason,
    precise.performance,
    frameSource,
    frameIndex,
  ]);

  const videoTracks = useMemo(() => annotations.filter(isVideoTrack), [annotations]);

  const selectedTrackTimeline = useMemo(
    () => (selectedTrack ? buildSelectedTrackTimeline(selectedTrack.geometry) : null),
    [selectedTrack],
  );

  const selectedTrackColor = useMemo(
    () =>
      selectedTrack
        ? getTrackColor(
            selectedTrack.geometry.track_id,
            selectedTrack.class_name,
            trackColorOverrides,
          )
        : null,
    [selectedTrack, trackColorOverrides],
  );

  const selectedTrackKeyframes = useMemo(
    () => (selectedTrack ? sortedKeyframes(selectedTrack.geometry) : []),
    [selectedTrack],
  );

  const manualBboxFrames = useMemo(
    () =>
      annotations.flatMap((ann) =>
        isVideoBbox(ann) && ann.source !== "prediction_based" ? [ann.geometry.frame_index] : [],
      ),
    [annotations],
  );

  const timelineDensityBins = Math.min(Math.max(maxFrame + 1, 1), 2000);

  const globalTimelineDensity = useMemo(
    () =>
      buildGlobalTimelineDensity(
        videoTracks.map((ann) => ann.geometry),
        maxFrame,
        timelineDensityBins,
        manualBboxFrames,
      ),
    [manualBboxFrames, maxFrame, timelineDensityBins, videoTracks],
  );

  const predictionDensity = useMemo(
    () => buildPredictionDensity(predictedFrames, maxFrame, timelineDensityBins),
    [predictedFrames, maxFrame, timelineDensityBins],
  );
  const hasPredictedFrames = predictedFrames.length > 0;

  const qualityWarnings = useMemo(() => {
    const warnings: string[] = [];
    const maxGap = Math.max(30, Math.round(fps * 2));
    for (const ann of videoTracks) {
      const kfs = sortedKeyframes(ann.geometry);
      for (let i = 1; i < kfs.length; i++) {
        const gap = kfs[i].frame_index - kfs[i - 1].frame_index;
        if (gap > maxGap) {
          warnings.push(
            `${ann.class_name} ${shortTrackId(ann.geometry.track_id)} 关键帧间隔 ${gap} 帧`,
          );
          break;
        }
      }
    }
    for (const entry of currentFrameEntries) {
      if (entry.geom.w < 0.003 || entry.geom.h < 0.003)
        warnings.push(`${entry.className} 当前帧存在极小框`);
    }
    for (let i = 0; i < currentFrameEntries.length; i++) {
      for (let j = i + 1; j < currentFrameEntries.length; j++) {
        const a = currentFrameEntries[i];
        const b = currentFrameEntries[j];
        if (a.className === b.className && shapeIou(a.geom, b.geom) > 0.9) {
          warnings.push(`${a.className} 当前帧存在高度重叠框`);
        }
      }
    }
    return [...new Set(warnings)].slice(0, 3);
  }, [currentFrameEntries, fps, videoTracks]);

  const issueFrames = useMemo(() => {
    const frames = new Set<number>();
    for (const f of issuePixelFeedbacks ?? []) {
      if (f.kind !== "issue" || f.anchor_type !== "pixel") continue;
      const frame = f.anchor_position?.frame;
      if (typeof frame === "number") frames.add(frame);
    }
    return [...frames].sort((a, b) => a - b);
  }, [issuePixelFeedbacks]);

  const trackActions = useVideoTrackActions({
    selectedTrack,
    frameIndex,
    readOnly,
    hiddenTrackIds: hiddenTrackIds as Set<string>,
    lockedTrackIds: lockedTrackIds as Set<string>,
    onUpdate: (annotation, geometry) => {
      if (geometry.type === "video_track_bbox") onUpdate(annotation, geometry);
    },
    onToggleHiddenTrack,
    onToggleLockedTrack,
    onPropagateTrack: onPropagateTrack
      ? (annotation) => {
          if (annotation.geometry.type === "video_track_bbox")
            onPropagateTrack(annotation as VideoTrackAnnotation);
        }
      : undefined,
  });

  const selectedTrackCurrentKeyframe = useMemo(
    () => selectedTrack?.geometry.keyframes.find((kf) => kf.frame_index === frameIndex) ?? null,
    [frameIndex, selectedTrack],
  );

  const canDeleteSelectedTrackKeyframe = Boolean(
    selectedTrack &&
    selectedTrackCurrentKeyframe &&
    !readOnly &&
    !trackActions.selectedTrackLocked &&
    selectedTrack.geometry.keyframes.length > 1,
  );

  const deleteSelectedTrackKeyframe = useCallback(() => {
    if (!selectedTrack || !selectedTrackCurrentKeyframe || !canDeleteSelectedTrackKeyframe)
      return false;
    onUpdate(selectedTrack, {
      ...selectedTrack.geometry,
      keyframes: sortedKeyframes(selectedTrack.geometry).filter(
        (kf) => kf.frame_index !== frameIndex,
      ),
    });
    return true;
  }, [
    canDeleteSelectedTrackKeyframe,
    frameIndex,
    onUpdate,
    selectedTrack,
    selectedTrackCurrentKeyframe,
  ]);

  const showPlaybackOverlay = useCallback(() => {
    if (overlayHideTimerRef.current) clearTimeout(overlayHideTimerRef.current);
    setPlaybackOverlayVisible(true);
  }, []);

  const schedulePlaybackOverlayHide = useCallback(() => {
    if (overlayHideTimerRef.current) clearTimeout(overlayHideTimerRef.current);
    overlayHideTimerRef.current = setTimeout(() => setPlaybackOverlayVisible(false), 2000);
  }, []);

  const flashPlaybackAction = useCallback((action: "prev" | "next" | "play") => {
    if (highlightTimerRef.current) clearTimeout(highlightTimerRef.current);
    setHighlightAction(action);
    highlightTimerRef.current = setTimeout(() => setHighlightAction(null), 180);
  }, []);

  const seekFrameAsync = useCallback(
    async (
      nextFrame: number,
      options?: { recordHistory?: boolean },
      checkedRequest?: PendingFramePresentation,
    ): Promise<FrameSeekResult> => {
      if (sourceOwnerRef.current.epoch !== sourceEpoch) {
        return { status: "cancelled", frameIndex: nextFrame, source: null };
      }
      if (!checkedRequest) cancelPresentation();
      const generation = checkedRequest?.navigationGeneration ?? ++navigationGenerationRef.current;
      const epoch = sourceOwnerRef.current.epoch;
      const targetFrame = Math.max(0, Math.min(maxFrame, Math.round(nextFrame)));
      if (!stageModeGuard.canSetupFrame || !manifest?.task_id || !Number.isFinite(nextFrame)) {
        return { status: "unavailable", frameIndex: nextFrame, source: null };
      }
      if (
        checkedRequest &&
        (pendingPresentationRef.current !== checkedRequest || checkedRequest.ownerEpoch !== epoch)
      ) {
        return { status: "cancelled", frameIndex: nextFrame, source: null };
      }
      if (options?.recordHistory) {
        setJumpHistory((history) => pushVideoJumpHistory(history, targetFrame));
      }
      frameNavigationTargetRef.current = targetFrame;
      showCachedBitmapFrame(targetFrame);
      let result: FrameSeekResult;
      try {
        result = await seekNativeFrameAsync(targetFrame);
      } catch {
        result = { status: "unavailable", frameIndex: targetFrame, source: null };
      }
      if (
        !mountedRef.current ||
        sourceOwnerRef.current.epoch !== epoch ||
        navigationGenerationRef.current !== generation
      ) {
        return { status: "cancelled", frameIndex: targetFrame, source: null };
      }
      if (result.status === "ready" && result.frameIndex === targetFrame) {
        void captureVerifiedFrame(videoRef.current, targetFrame);
      }
      return result;
    },
    [
      cancelPresentation,
      captureVerifiedFrame,
      seekNativeFrameAsync,
      manifest?.task_id,
      maxFrame,
      showCachedBitmapFrame,
      sourceEpoch,
      stageModeGuard.canSetupFrame,
      videoRef,
    ],
  );
  const seekFrameAsyncRef = useRef(seekFrameAsync);
  useEffect(() => {
    seekFrameAsyncRef.current = seekFrameAsync;
  }, [seekFrameAsync]);

  const pausePlayback = useCallback(
    (options?: { snapToGrid?: boolean }) => {
      onPauseIntent?.();
      const video = videoRef.current;
      if (video) {
        video.pause();
        video.playbackRate = defaultPlaybackRate;
      }
      setIsPlaying(false);
      setJogPlayback(pausedJogPlaybackRef.current);
      const step = samplingStepRef.current;
      if ((options?.snapToGrid ?? true) && step > 1) {
        const snapped = snapToGrid(frameIndexRef.current, step, maxFrame);
        if (snapped !== frameIndexRef.current) {
          void seekFrameAsyncRef.current(snapped, { recordHistory: false });
        }
      }
    },
    [defaultPlaybackRate, maxFrame, onPauseIntent, videoRef],
  );

  const togglePlayback = useCallback(() => {
    showPlaybackOverlay();
    flashPlaybackAction("play");
    const video = videoRef.current;
    if (!video) return;
    if (!video.paused || isPlaybackActive) {
      pausePlayback();
      return;
    }
    if (loopRegion && (frameIndex < loopRegion.startFrame || frameIndex > loopRegion.endFrame)) {
      void seekFrameAsync(loopRegion.startFrame, { recordHistory: true });
    }
    setPlaybackError(null);
    setJogPlayback(pausedJogPlaybackRef.current);
    video.playbackRate = defaultPlaybackRate;
    setIsPlaying(true);
    const playResult = video.play();
    if (playResult && typeof playResult.catch === "function") {
      void playResult.catch((err: unknown) => {
        setIsPlaying(false);
        setPlaybackError(err instanceof Error ? err.message : "视频无法播放");
      });
    }
  }, [
    defaultPlaybackRate,
    flashPlaybackAction,
    frameIndex,
    isPlaybackActive,
    loopRegion,
    pausePlayback,
    seekFrameAsync,
    showPlaybackOverlay,
    videoRef,
  ]);

  const jogPlaybackBy = useCallback(
    (dir: -1 | 1) => {
      showPlaybackOverlay();
      flashPlaybackAction(dir < 0 ? "prev" : "next");
      setPlaybackError(null);
      const current = jogPlaybackRef.current;
      const next =
        current.direction === 0
          ? { direction: dir, rate: pausedJogPlaybackRef.current.rate }
          : current.direction === dir
            ? { direction: dir, rate: nextHigherPlaybackRate(current.rate) }
            : (() => {
                const lower = nextLowerPlaybackRate(current.rate);
                return lower ? { ...current, rate: lower } : pausedJogPlaybackRef.current;
              })();
      if (next.direction === 0) {
        pausePlayback();
        return;
      }
      setJogPlayback(next);
    },
    [flashPlaybackAction, pausePlayback, showPlaybackOverlay],
  );

  const seekByFrames = useCallback(
    (delta: number, options?: { recordHistory?: boolean }) => {
      showPlaybackOverlay();
      flashPlaybackAction(delta < 0 ? "prev" : "next");
      pausePlayback({ snapToGrid: false });
      void seekFrameAsync(frameIndexRef.current + delta, {
        recordHistory: options?.recordHistory ?? true,
      });
    },
    [flashPlaybackAction, pausePlayback, seekFrameAsync, showPlaybackOverlay],
  );

  const seekGrid = useCallback(
    (dir: -1 | 1, options?: { recordHistory?: boolean }) => {
      const step = samplingStepRef.current;
      const cur = frameIndexRef.current;
      const target = dir < 0 ? gridPrev(cur, step, maxFrame) : gridNext(cur, step, maxFrame);
      showPlaybackOverlay();
      flashPlaybackAction(dir < 0 ? "prev" : "next");
      pausePlayback({ snapToGrid: false });
      void seekFrameAsync(target, { recordHistory: options?.recordHistory ?? true });
    },
    [flashPlaybackAction, maxFrame, pausePlayback, seekFrameAsync, showPlaybackOverlay],
  );

  const microStepBy = useCallback(
    (dir: -1 | 1, options?: { recordHistory?: boolean }) => {
      const target = microStep(frameIndexRef.current, dir, maxFrame);
      showPlaybackOverlay();
      flashPlaybackAction(dir < 0 ? "prev" : "next");
      pausePlayback({ snapToGrid: false });
      void seekFrameAsync(target, { recordHistory: options?.recordHistory ?? true });
    },
    [flashPlaybackAction, maxFrame, pausePlayback, seekFrameAsync, showPlaybackOverlay],
  );

  const seekToKeyframe = useCallback(
    (dir: -1 | 1, options?: { recordHistory?: boolean }) => {
      if (!selectedTrack) return;
      const nextFrame = nextVisibleKeyframeFrame(
        selectedTrack.geometry,
        frameIndexRef.current,
        dir,
      );
      if (nextFrame === null) return;
      showPlaybackOverlay();
      flashPlaybackAction(dir < 0 ? "prev" : "next");
      pausePlayback({ snapToGrid: false });
      void seekFrameAsync(nextFrame, { recordHistory: options?.recordHistory ?? true });
    },
    [flashPlaybackAction, pausePlayback, seekFrameAsync, selectedTrack, showPlaybackOverlay],
  );

  const seekOverlayByFrames = useCallback(
    (delta: number, options?: { recordHistory?: boolean }) => {
      if (samplingStepRef.current > 1 && Math.abs(delta) === 1) {
        seekGrid(delta < 0 ? -1 : 1, options);
        return;
      }
      seekByFrames(delta, options);
    },
    [seekByFrames, seekGrid],
  );

  const seekToFrame = useCallback(
    (nextFrame: number, options?: { recordHistory?: boolean }) => {
      showPlaybackOverlay();
      pausePlayback({ snapToGrid: false });
      void seekFrameAsync(nextFrame, { recordHistory: options?.recordHistory ?? true });
    },
    [pausePlayback, seekFrameAsync, showPlaybackOverlay],
  );

  const seekToFrameReady = useCallback(
    (nextFrame: number, options?: { recordHistory?: boolean }): Promise<VideoFrameSeekResult> => {
      if (sourceOwnerRef.current.epoch !== sourceEpoch) {
        return Promise.resolve({ status: "cancelled", frameIndex: nextFrame, source: null });
      }
      cancelPresentation();
      showPlaybackOverlay();
      pausePlayback({ snapToGrid: false });
      if (
        !mountedRef.current ||
        !manifest?.task_id ||
        !stageModeGuard.canSetupFrame ||
        !Number.isInteger(nextFrame) ||
        nextFrame < 0 ||
        nextFrame > maxFrame
      ) {
        return Promise.resolve({ status: "unavailable", frameIndex: nextFrame, source: null });
      }
      return new Promise<VideoFrameSeekResult>((resolve) => {
        const request: PendingFramePresentation = {
          requestId: ++presentationSequenceRef.current,
          frameIndex: nextFrame,
          ownerEpoch: sourceOwnerRef.current.epoch,
          navigationGeneration: ++navigationGenerationRef.current,
          timer: null,
          resolve,
        };
        pendingPresentationRef.current = request;
        request.timer = setTimeout(() => {
          settlePresentation(request, { status: "timeout", frameIndex: nextFrame, source: null });
        }, VIDEO_FRAME_READY_TIMEOUT_MS);
        setPresentationRequest(request);
        void seekFrameAsync(nextFrame, { recordHistory: options?.recordHistory ?? true }, request);
      });
    },
    [
      cancelPresentation,
      manifest?.task_id,
      maxFrame,
      pausePlayback,
      seekFrameAsync,
      settlePresentation,
      showPlaybackOverlay,
      sourceEpoch,
      stageModeGuard.canSetupFrame,
    ],
  );

  const seekToAdjacentPredictedFrame = useCallback(
    (dir: -1 | 1) => {
      const target = adjacentPredictedFrame(predictedFrames, frameIndex, dir);
      if (target !== null) seekToFrame(target, { recordHistory: true });
    },
    [predictedFrames, frameIndex, seekToFrame],
  );

  const toggleBookmark = useCallback(() => {
    showPlaybackOverlay();
    setBookmarks((current) => toggleVideoBookmark(current, frameIndex));
  }, [frameIndex, showPlaybackOverlay]);

  const jumpHistoryBy = useCallback(
    (dir: -1 | 1) => {
      const result = jumpVideoHistory(jumpHistory, dir);
      setJumpHistory(result.history);
      if (result.frameIndex === null) return;
      showPlaybackOverlay();
      pausePlayback({ snapToGrid: false });
      void seekFrameAsync(result.frameIndex, { recordHistory: false });
    },
    [jumpHistory, pausePlayback, seekFrameAsync, showPlaybackOverlay],
  );

  const clearLoopRegion = useCallback(() => {
    showPlaybackOverlay();
    setLoopRegion(null);
  }, [showPlaybackOverlay]);

  const setNormalizedLoopRegion = useCallback(
    (region: VideoLoopRegion) => {
      showPlaybackOverlay();
      setLoopRegion(normalizeLoopRegion(region.startFrame, region.endFrame, maxFrame));
    },
    [maxFrame, showPlaybackOverlay],
  );

  useEffect(() => {
    if (jogPlayback.direction !== 1) return;
    const video = videoRef.current;
    if (!video) return;
    if (
      loopRegion &&
      (frameIndexRef.current < loopRegion.startFrame || frameIndexRef.current > loopRegion.endFrame)
    ) {
      void seekFrameAsyncRef.current(loopRegion.startFrame, { recordHistory: true });
    }
    video.playbackRate = jogPlayback.rate;
    setIsPlaying(true);
    const playResult = video.play();
    if (playResult && typeof playResult.catch === "function") {
      void playResult.catch((err: unknown) => {
        setIsPlaying(false);
        setJogPlayback(pausedJogPlaybackRef.current);
        setPlaybackError(err instanceof Error ? err.message : "视频无法播放");
      });
    }
  }, [jogPlayback.direction, jogPlayback.rate, loopRegion, videoRef]);

  useEffect(() => {
    if (jogPlayback.direction !== -1) return;
    const video = videoRef.current;
    if (video) {
      video.pause();
      video.playbackRate = defaultPlaybackRate;
    }
    setIsPlaying(false);
    let raf = 0;
    let last = performance.now();
    let accumulator = 0;
    let seeking = false;
    let cancelled = false;
    const schedule =
      typeof window.requestAnimationFrame === "function"
        ? window.requestAnimationFrame.bind(window)
        : (cb: FrameRequestCallback) => window.setTimeout(() => cb(performance.now()), 16);
    const cancel =
      typeof window.cancelAnimationFrame === "function"
        ? window.cancelAnimationFrame.bind(window)
        : window.clearTimeout.bind(window);

    const tick = (now: number) => {
      if (cancelled) return;
      const elapsedMs = Math.min(250, Math.max(0, now - last));
      last = now;
      accumulator += (elapsedMs / 1000) * fps * jogPlayback.rate;
      const steps = Math.floor(accumulator);
      if (steps > 0 && !seeking) {
        accumulator -= steps;
        const current = frameIndexRef.current;
        let nextFrame = current - steps;
        if (loopRegion) {
          if (nextFrame < loopRegion.startFrame) nextFrame = loopRegion.endFrame;
        } else if (nextFrame < 0) {
          nextFrame = 0;
          setJogPlayback(pausedJogPlaybackRef.current);
        }
        seeking = true;
        void Promise.resolve(
          seekFrameAsyncRef.current(nextFrame, { recordHistory: false }),
        ).finally(() => {
          seeking = false;
        });
      }
      raf = schedule(tick);
    };

    raf = schedule(tick);
    return () => {
      cancelled = true;
      cancel(raf);
    };
  }, [defaultPlaybackRate, fps, jogPlayback.direction, jogPlayback.rate, loopRegion, videoRef]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    const onPlay = () => setIsPlaying(true);
    const onPause = () => setIsPlaying(false);
    const onEnded = () => {
      setIsPlaying(false);
      setJogPlayback(pausedJogPlaybackRef.current);
    };
    const onError = () => {
      setIsPlaying(false);
      setJogPlayback(pausedJogPlaybackRef.current);
      if (onSourceRecover) onSourceRecover();
      else setPlaybackError(video.error?.message || "当前浏览器无法播放该视频源");
    };
    video.addEventListener("play", onPlay);
    video.addEventListener("pause", onPause);
    video.addEventListener("ended", onEnded);
    video.addEventListener("error", onError);
    const onFrameReady = () => {
      refreshNativeReadiness((value) => value + 1);
      if (!isPlaybackActive) void captureVerifiedFrame(video, frameIndexRef.current);
    };
    video.addEventListener("seeked", onFrameReady);
    video.addEventListener("loadeddata", onFrameReady);
    return () => {
      video.removeEventListener("play", onPlay);
      video.removeEventListener("pause", onPause);
      video.removeEventListener("ended", onEnded);
      video.removeEventListener("error", onError);
      video.removeEventListener("seeked", onFrameReady);
      video.removeEventListener("loadeddata", onFrameReady);
    };
  }, [captureVerifiedFrame, isPlaybackActive, onSourceRecover, videoRef]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video || !manifest?.video_url) return;
    setPlaybackError(null);
    const epoch = sourceOwnerRef.current.epoch;
    let cancelled = false;
    let disposed = false;
    const primeFirstFrame = () => {
      if (cancelled || disposed) return;
      cancelled = true;
      void seekFrameAsyncRef
        .current(
          frameIndexRef.current,
          { recordHistory: false },
          pendingPresentationRef.current ?? undefined,
        )
        .then((result) => {
          const aligned =
            result.status === "ready" ||
            ((result.status === "timeout" || result.status === "unavailable") &&
              !video.seeking &&
              video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA &&
              video.videoWidth > 0 &&
              video.videoHeight > 0 &&
              timeToFrame(video.currentTime, timebase) === frameIndexRef.current);
          if (
            !disposed &&
            aligned &&
            sourceOwnerRef.current.epoch === epoch &&
            videoRef.current === video
          )
            onSourcePrimed?.();
        });
    };
    if (video.readyState >= HTMLMediaElement.HAVE_METADATA) {
      primeFirstFrame();
    } else {
      video.addEventListener("loadedmetadata", primeFirstFrame, { once: true });
    }
    return () => {
      disposed = true;
      cancelled = true;
      video.removeEventListener("loadedmetadata", primeFirstFrame);
    };
  }, [manifest?.video_url, onSourcePrimed, timebase, videoRef]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video || !manifest?.video_url) return;
    if (video.readyState >= HTMLMediaElement.HAVE_METADATA) return;
    const STALL_MS = 5000;
    const MAX_RETRIES = 3;
    let retries = 0;
    let timer: number | undefined = window.setTimeout(function check() {
      if (!video.isConnected || video.readyState >= HTMLMediaElement.HAVE_METADATA) return;
      if (retries >= MAX_RETRIES) {
        if (onSourceRecover) onSourceRecover();
        else setPlaybackError("视频加载超时,请刷新页面重试");
        return;
      }
      retries += 1;
      try {
        video.load();
      } catch {
        // load() 极少抛错; 抛了也只是本次重踢失败, 下一轮继续。
      }
      timer = window.setTimeout(check, STALL_MS);
    }, STALL_MS);
    return () => {
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [manifest?.video_url, onSourceRecover, videoRef]);

  useEffect(() => {
    if (isPlaybackActive || frameClock.isSeeking) return;
    const schedule =
      typeof window.requestAnimationFrame === "function"
        ? window.requestAnimationFrame.bind(window)
        : (cb: FrameRequestCallback) => window.setTimeout(() => cb(performance.now()), 16);
    const cancel =
      typeof window.cancelAnimationFrame === "function"
        ? window.cancelAnimationFrame.bind(window)
        : window.clearTimeout.bind(window);
    const raf = schedule(() => {
      void captureVerifiedFrame(videoRef.current, frameIndex);
    });
    return () => {
      cancel(raf);
    };
  }, [
    captureVerifiedFrame,
    frameClock.isSeeking,
    nativeFrameEvidence,
    frameIndex,
    isPlaybackActive,
    videoRef,
  ]);

  useEffect(() => {
    const taskId = manifest?.task_id ?? null;
    if (!taskId || lastResetTaskIdRef.current === taskId) return;
    const previousTaskId = lastResetTaskIdRef.current;
    lastResetTaskIdRef.current = taskId;
    setFrameIndex(0);
    setIsPlaying(false);
    setJogPlayback(pausedJogPlaybackRef.current);
    setPlaybackError(null);
    setPlaybackOverlayVisible(true);
    try {
      setLoopRegion(
        parseStoredLoopRegion(
          sessionStorage.getItem(videoNavigationStorageKey(taskId, "loop")),
          maxFrame,
        ),
      );
      setBookmarks(
        parseStoredBookmarks(
          sessionStorage.getItem(videoNavigationStorageKey(taskId, "bookmarks")),
          maxFrame,
        ),
      );
      setJumpHistory(
        parseStoredJumpHistory(
          sessionStorage.getItem(videoNavigationStorageKey(taskId, "history")),
          maxFrame,
        ),
      );
    } catch {
      setLoopRegion(null);
      setBookmarks([]);
      setJumpHistory(emptyVideoJumpHistory());
    }
    if (previousTaskId !== null) onSelectRef.current?.(null, { source: "task-reset" });
  }, [manifest?.task_id, maxFrame, setFrameIndex]);

  useEffect(() => {
    const taskId = manifest?.task_id;
    if (!taskId) return;
    try {
      const key = videoNavigationStorageKey(taskId, "loop");
      if (loopRegion) sessionStorage.setItem(key, JSON.stringify(loopRegion));
      else sessionStorage.removeItem(key);
    } catch {
      // sessionStorage 不可用时静默忽略
    }
  }, [loopRegion, manifest?.task_id]);

  useEffect(() => {
    const taskId = manifest?.task_id;
    if (!taskId) return;
    try {
      sessionStorage.setItem(
        videoNavigationStorageKey(taskId, "bookmarks"),
        JSON.stringify(bookmarks),
      );
    } catch {
      // noop
    }
  }, [bookmarks, manifest?.task_id]);

  useEffect(() => {
    const taskId = manifest?.task_id;
    if (!taskId) return;
    try {
      sessionStorage.setItem(
        videoNavigationStorageKey(taskId, "history"),
        JSON.stringify(jumpHistory),
      );
    } catch {
      // noop
    }
  }, [jumpHistory, manifest?.task_id]);

  useEffect(() => {
    if (annotationWritesPending || selectedTrackKeyframes.length === 0) return;
    const previous = lastTrackPrefetchRef.current;
    if (
      previous?.prefetch === prefetchFrames &&
      selectedTrackKeyframes.every(({ frame_index }) => previous.frames.has(frame_index))
    )
      return;
    const frames = selectedTrackKeyframes.map(({ frame_index }) => frame_index);
    lastTrackPrefetchRef.current = { prefetch: prefetchFrames, frames: new Set(frames) };
    prefetchFrames(frames);
  }, [annotationWritesPending, prefetchFrames, selectedTrackKeyframes]);

  useEffect(() => {
    prefetchFrames(bookmarks.map((bookmark) => bookmark.frameIndex));
  }, [bookmarks, prefetchFrames]);

  useEffect(() => {
    if (!loopRegion) return;
    prefetchFrames([loopRegion.startFrame, loopRegion.endFrame]);
  }, [loopRegion, prefetchFrames]);

  useEffect(() => {
    return () => {
      if (overlayHideTimerRef.current) clearTimeout(overlayHideTimerRef.current);
      if (highlightTimerRef.current) clearTimeout(highlightTimerRef.current);
    };
  }, []);

  const controls = useMemo<
    Omit<
      VideoStageControls,
      "cycleInCategory" | "stepCategory" | "focusObject" | "focusRegion" | "normToClient"
    >
  >(
    () => ({
      isSourceReady,
      togglePlayback,
      jogPlayback: jogPlaybackBy,
      pausePlayback,
      seekByFrames,
      seekGrid,
      microStep: microStepBy,
      seekToKeyframe,
      seekToFrame,
      seekToFrameReady,
      toggleBookmark,
      jumpHistory: jumpHistoryBy,
      clearLoopRegion,
      toggleSelectedTrackOutside: trackActions.toggleSelectedTrackOutside,
      toggleSelectedTrackOccluded: trackActions.toggleSelectedTrackOccluded,
      toggleSelectedTrackHidden: trackActions.toggleSelectedTrackHidden,
      toggleSelectedTrackLocked: trackActions.toggleSelectedTrackLocked,
      propagateSelectedTrack: trackActions.propagateSelectedTrack,
      deleteSelectedTrackKeyframe,
      captureCurrentFrameJpeg: async (quality?: number) => {
        const epoch = sourceOwnerRef.current.epoch;
        const frame = frameIndexRef.current;
        const generation = navigationGenerationRef.current;
        if (!(isSourceReadyRef.current?.() ?? true)) return null;
        const shown = displayBitmapRef.current;
        const el = videoRef.current;
        const nativeFrameCurrent = () =>
          videoRef.current === el &&
          !!el &&
          !el.seeking &&
          el.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA &&
          el.videoWidth > 0 &&
          el.videoHeight > 0 &&
          timeToFrame(el.currentTime, timebase) === frame;
        let result: Blob | null;
        if (shown) {
          if (shown.frameIndex !== frame) return null;
          result = await imageBitmapToJpeg(shown.bitmap, quality);
        } else {
          if (!nativeFrameCurrent()) return null;
          result = await videoElementToJpeg(el!, quality);
        }
        return mountedRef.current &&
          frameIndexRef.current === frame &&
          navigationGenerationRef.current === generation &&
          sourceOwnerRef.current.epoch === epoch &&
          (isSourceReadyRef.current?.() ?? true) &&
          (shown ? displayBitmapRef.current === shown : nativeFrameCurrent())
          ? result
          : null;
      },
    }),
    [
      clearLoopRegion,
      isSourceReady,
      timebase,
      deleteSelectedTrackKeyframe,
      jogPlaybackBy,
      jumpHistoryBy,
      microStepBy,
      pausePlayback,
      seekByFrames,
      seekGrid,
      seekToFrame,
      seekToFrameReady,
      seekToKeyframe,
      toggleBookmark,
      togglePlayback,
      trackActions.propagateSelectedTrack,
      trackActions.toggleSelectedTrackHidden,
      trackActions.toggleSelectedTrackLocked,
      videoRef,
      trackActions.toggleSelectedTrackOccluded,
      trackActions.toggleSelectedTrackOutside,
    ],
  );

  return {
    frameIndex,
    isPlaybackActive,
    isJogPlaying,
    jogPlayback,
    playbackError,
    activeBitmap,
    cachedRanges,
    displayBitmap,
    frameSource,
    preciseSourceState: precise.sourceState,
    precisePaintedFrameIndex: precise.performance.paintedFrameIndex,
    markPreciseFramePainted: precise.markFramePainted,
    framePresentation,
    markFramePresented,
    framePreview,
    previewFrame,
    samplingStep,
    maxFrame,
    timebase,
    fps,
    selectedTrackTimeline,
    selectedTrackColor,
    selectedTrackKeyframes,
    globalTimelineDensity,
    predictionDensity,
    hasPredictedFrames,
    seekToAdjacentPredictedFrame,
    qualityWarnings,
    issueFrames,
    playbackOverlayVisible,
    highlightAction,
    bookmarks,
    loopRegion,
    trackActions,
    canDeleteSelectedTrackKeyframe,
    deleteSelectedTrackKeyframe,
    showPlaybackOverlay,
    schedulePlaybackOverlayHide,
    setNormalizedLoopRegion,
    clearLoopRegion,
    toggleBookmark,
    seekToFrame,
    seekOverlayByFrames,
    pausePlayback,
    controls,
  };
}

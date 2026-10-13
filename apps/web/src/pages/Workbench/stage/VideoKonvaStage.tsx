import type { TrackerReviewProjection } from "@/hooks/videoTrackerReviewScope";
import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { isWorkbenchInteractionBlocked } from "../state/workbenchInteractionGuards";
import { isMaskHotkeyBlocked } from "../state/hotkeys";
import type {
  CSSProperties,
  ReactNode,
  MouseEvent as ReactMouseEvent,
  PointerEvent as ReactPointerEvent,
  Dispatch,
  SetStateAction,
} from "react";
import { Stage, Layer, Line, Circle, Group, Image as KonvaImage, Rect } from "react-konva";
import type Konva from "konva";
import { Icon } from "@/components/ui/Icon";
import { ContextMenu } from "@/components/ui/ContextMenu";
import type { DropdownItem } from "@/components/ui/DropdownMenu";
import type {
  AnnotationResponse,
  TaskVideoFrameTimetableResponse,
  TaskVideoManifestResponse,
  VideoBboxGeometry,
  VideoPolygonGeometry,
  VideoPolylineGeometry,
  VideoRotatedBboxGeometry,
  VideoKeypointGeometry,
  Keypoint,
  KeypointSchema,
  VideoSamplingConfig,
  VideoTrackGeometry,
  VideoTrackMaskGeometry,
  VideoTrackPolygonGeometry,
  VideoTrackPolylineGeometry,
} from "@/types";
import type { WorkbenchCommonPreferences } from "@/api/auth";
import type { AnnotationFeedback } from "@/api/feedbacks";
import { useElementSize, useViewportTransform } from "../state/useViewportTransform";
import type { Viewport } from "../state/useViewportTransform";
import { useVideoIssueView } from "./useVideoIssueView";
import { captureVideoIssueViewport } from "./videoIssueViewport";
import type { VideoTimelineWindowControls } from "./videoStageControls";
import type { PendingDrawing, VideoTool } from "../state/useWorkbenchState";
import type { DiffMode } from "../modes/types";
import { FloatingDock } from "../shell/FloatingDock";
import { Minimap } from "./Minimap";
import { VideoKonvaMediaLayer, pickMediaImageSource } from "./VideoKonvaMediaLayer";
import { VideoKonvaTracksLayer } from "./VideoKonvaTracksLayer";
import { VideoKonvaMaskLayer } from "./VideoKonvaMaskLayer";
import { MaskOverlayLayer } from "./overlays/MaskOverlayLayer";
import { MaskCompareTileLayer } from "./overlays/MaskCompareTileLayer";
import {
  maskCompareCompanionVisible,
  type MaskCompareTileStore,
} from "./shared/maskCompareTileStore";
import type { UseMaskEditorReturn } from "../state/useMaskEditor";
import { MaskBuffer } from "./shared/geometry/maskBuffer";
import { canEditMask } from "../state/canEditMask";
import { VideoKonvaOverlayLayer } from "./VideoKonvaOverlayLayer";
import { VideoKonvaIssueLayer } from "./VideoKonvaIssueLayer";
import {
  VideoKonvaInteractionLayer,
  type VideoHandleBox,
  type VideoPreviewBox,
  type VideoHandleObb,
  type VideoPreviewObb,
  type VideoHandleKeypoints,
} from "./VideoKonvaInteractionLayer";
import {
  VideoPlaybackOverlay,
  type VideoLargeFrameStep,
  type VideoTimelineChapter,
  type VideoTimelineChapterControls,
} from "./VideoPlaybackOverlay";
import { VideoQcWarnings } from "./VideoQcWarnings";
import { useVideoKonvaInteraction, isSamProbeTool } from "./videoKonvaInteraction";
import { videoIntrinsicSize, clientToVideoNorm, videoNormToClient } from "./videoKonvaCoordinates";
import { deriveVideoFrameViews, visibleInReviewMode, type VideoLabelView } from "./videoFrameViews";
import { useVideoReferenceConfig } from "./videoReferencePredict";
import { classColor, colorToHex, getTrackColor, hexToRgb, hexToRgba } from "./colors";
import { useVideoPolygonDraft } from "./useVideoPolygonDraft";
import { CLOSE_DISTANCE } from "./tools/PolygonTool";
import {
  deriveTrackNumber,
  isAnyVideoSingleFrame,
  isAnyVideoTrack,
  isVideoBbox,
  isVideoMask,
  isVideoPolygon,
  isVideoPolygonTrack,
  isVideoPolyline,
  isVideoPolylineTrack,
  isVideoTrack,
  normalizeGeom,
  resolveTrackAtFrame,
  resolveVideoMaskTrackAtFrame,
  resolveVideoPolygonTrackAtFrame,
  resolveVideoPolylineTrackAtFrame,
  shapeIou,
  shortTrackId,
  sortedKeyframes,
  upsertKeyframe,
  upsertPointsKeyframe,
} from "./videoStageGeometry";
import {
  buildSelectedTrackTimeline,
  nextVisibleKeyframeFrame,
  visibleKeyframesForTimeline,
} from "./videoTrackTimeline";
import { deriveVideoTrackContext } from "./videoTrackContext";
import { removeOutsideFrame } from "./videoTrackOutside";
import { pickTopVideoEntryAt, pickTopVideoMaskAt } from "./videoStagePicking";
import { useVideoMaskFrames, type VideoMaskCandidate } from "./videoMaskFrames";
import { useVideoTrackActions } from "./useVideoTrackActions";
import { buildVideoContextMenuItems } from "./videoContextMenuItems";
import type { VideoMaskKeyframeActionHandlers } from "./videoMaskKeyframeActions";
import { useCanvasContextMenu } from "./useCanvasContextMenu";
import type {
  VideoManagedTrackAnnotation,
  VideoTrackCompositionOptions,
  VideoTrackConversionOptions,
  VideoSamPrompt,
} from "./videoStageTypes";
import {
  buildTrackLabelText,
  DEFAULT_ANNOTATION_VISUAL,
  shouldShowLabel,
  type AnnotationVisualConfig,
} from "./annotationVisual";
import { clampScale } from "./shared/viewport/zoom";
import { fitNormalizedRegion } from "./shared/viewport/region";
import { useVideoPlaybackController } from "./useVideoPlaybackController";
import type { VideoLoopRegion } from "./videoNavigationState";
import { collectPredictedFrames, resolveAiBoxAtFrame } from "./aiBoxFrames";
import {
  collectFrameCategories,
  nextInCategory,
  nextCategory,
  type FrameObjectRef,
} from "./frameObjectCycle";
import type { VideoDrawingDraft, VideoStageControls } from "./videoStageControls";
import { VideoKonvaAiLayer } from "./VideoKonvaAiLayer";
import { VideoSamCandidateOverlay, type VideoSamCandidateShape } from "./VideoSamCandidateOverlay";
import { SelectionOverlay } from "./SelectionOverlay";
import { keypointColorByIndex } from "./ImageStageShapes";
import { pickTopRasterMaskAt, type RasterMaskRenderRecord } from "./shared/rasterMaskRender";
import { VideoTrackContextBar, type VideoTrackContextBarProps } from "./VideoTrackContextBar";
import { VideoStageCommentBadges } from "./VideoStageCommentBadges";
import type { AiBox } from "../state/transforms";
import styles from "./VideoKonvaStage.module.css";

/** v0.21.23 · SAM 提示框描边色，与图片侧 SAM_CANDIDATE_STROKE 同值（canvas 数据域颜色）。 */
const SAM_PROBE_STROKE = "#a855f7";
const EMPTY_SAM_CANDIDATES: VideoSamCandidateShape[] = [];
const EMPTY_SAM_MASK_RECORDS: RasterMaskRenderRecord<"interactive">[] = [];
const EMPTY_SESSION_POINTS: { pt: [number, number]; polarity: 1 | 0; obj?: number }[] = [];
const EMPTY_SESSION_BOXES: { bbox: [number, number, number, number]; obj?: number }[] = [];
const EMPTY_ANNOTATIONS: AnnotationResponse[] = [];
const EMPTY_AI_BOXES: AiBox[] = [];
const EMPTY_LOCKED = new Set<string>();
// 解构默认值写 `= []` 会每次渲染产生新引用, 把 frameViews 的 memo 打穿(视频画布逐帧重算)。
const EMPTY_SELECTED_IDS: string[] = [];
const EMPTY_MASK_CANDIDATES: VideoMaskCandidate[] = [];
const MASK_OPERATION_PREVIEW_COLOR = [245, 158, 11] as const;
const MASK_INSTANCE_PREVIEW_COLOR = [14, 165, 233] as const;

interface VideoKonvaStageProps {
  overlays?: ReactNode;
  maskCompareStore?: MaskCompareTileStore | null;
  manifest: TaskVideoManifestResponse | undefined;
  frameTimetable?: TaskVideoFrameTimetableResponse;
  isLoading?: boolean;
  error?: unknown;
  frameIndex?: number;
  autoFitOnResize?: boolean;
  /** v0.21.11 · 选中自动聚焦(common.focusSelectionEnabled); 关闭时选中不移动视口。 */
  focusSelectionEnabled?: boolean;
  /** v0.21.12 · 轨迹「续写后自动前进」(video.trackContinueAutoAdvance); 续写完自动选中下一条待续轨迹。 */
  trackContinueAutoAdvance?: boolean;
  performanceTier?: WorkbenchCommonPreferences["performanceTier"];
  onFrameIndexChange?: (frameIndex: number) => void;
  annotations?: AnnotationResponse[];
  /** v0.21.4 · AI 候选(全部帧); 舞台内按当前帧过滤视频几何渲染 + 采纳/驳回。 */
  aiBoxes?: AiBox[];
  selectedId?: string | null;
  hiddenTrackIds?: Set<string>;
  reviewDisplayMode?: DiffMode;
  trackColorOverrides?: Record<string, string>;
  activeClass?: string;
  keypointSchema?: KeypointSchema | null;
  pendingDrawing?: PendingDrawing;
  issuePixelFeedbacks?: AnnotationFeedback[];
  issueHighlightId?: string | null;
  annotationCommentCounts?: Record<string, number>;
  onOpenAnnotationComments?: (annotationId: string) => void;
  /** 单击 issue 图钉(Shell 据此高亮 + 切到讨论面板 issues tab)。 */
  onIssuePinClick?: (id: string) => void;
  issuePinDropArmed?: boolean;
  issueNavigationPending?: boolean;
  onIssuePinDrop?: (x: number, y: number, frame?: number) => void;
  onSeekIssueFrame?: (frame: number) => void;
  /** 共享视觉规格(线宽/填充/字号/标签);与图片同源。缺省回退默认值。 */
  visual?: AnnotationVisualConfig;
  videoTool?: VideoTool;
  /** 视频工具可用性谓词 (按几何单位 enabled + 单帧/轨迹子开关, 见 stage/videoToolUnits)。 */
  isVideoToolEnabled?: (t: VideoTool) => boolean;
  spacePan?: boolean;
  onSpacePanDragStart?: () => void;
  readOnly?: boolean;
  lockedTrackIds?: Set<string>;
  selectedIds?: string[];
  onSelect?: (id: string | null, opts?: { shift?: boolean; source?: "task-reset" }) => void;
  /** 光标归一化坐标上报(供状态栏坐标读出);离开画布时上报 null。 */
  onCursorMove?: (pt: { x: number; y: number } | null) => void;
  onCreate?: (frameIndex: number, geom: { x: number; y: number; w: number; h: number }) => void;
  /** v0.21.20 · 由绘制顶点新建 polygon/polyline track (单关键帧于当前帧)。 */
  onCreatePointsTrack?: (
    type: "video_track_polygon" | "video_track_polyline",
    frameIndex: number,
    points: [number, number][],
  ) => void;
  /** v0.21.21 · 由绘制顶点新建单帧 polygon/polyline (video_polygon/video_polyline)。 */
  onCreatePoints?: (
    type: "video_polygon" | "video_polyline",
    frameIndex: number,
    points: [number, number][],
  ) => void;
  onCreateKeypoints?: (frameIndex: number, points: Keypoint[]) => void;
  onPendingDraw?: (
    kind: "video_bbox" | "video_track_bbox" | "video_rotated_bbox",
    frameIndex: number,
    geom: { x: number; y: number; w: number; h: number },
    anchor: { left: number; top: number },
  ) => void;
  onUpdate?: (
    annotation: AnnotationResponse,
    geometry:
      | VideoBboxGeometry
      | VideoTrackGeometry
      | VideoTrackMaskGeometry
      | VideoPolygonGeometry
      | VideoPolylineGeometry
      | VideoRotatedBboxGeometry
      | VideoKeypointGeometry
      | VideoTrackPolygonGeometry
      | VideoTrackPolylineGeometry,
  ) => void;
  /** v0.21.23 · 交互式 SAM 提示松手 (归一化坐标)；由 shell 取当前帧图请求候选。 */
  onSamPrompt?: (prompt: VideoSamPrompt) => void;
  /** v0.21.23 · 交互式 SAM 的瞬态候选（不落库；采纳时才建标注）。 */
  samCandidates?: VideoSamCandidateShape[];
  samMaskRecords?: readonly RasterMaskRenderRecord<"interactive">[];
  onSelectSamMaskCandidate?: (candidateId: string) => void;
  samActiveIdx?: number;
  /** 当前点会话已落的正/负点（多点精修可视化）。 */
  samSessionPoints?: { pt: [number, number]; polarity: 1 | 0; obj?: number }[];
  /** v0.21.27 · 框修正 · 当前帧已落的 PVS 框种子（归一化 xyxy）。 */
  samSessionBoxes?: { bbox: [number, number, number, number]; obj?: number }[];
  /** 追踪任务尚未接受的 mask 候选；使用 job 级内容端点解码。 */
  maskCandidates?: VideoMaskCandidate[];
  maskEditor?: UseMaskEditorReturn;
  onMaskCommit?: () => void;
  onMaskCancel?: () => void;
  /** 工具条上的正/负切换; 与 Alt 等价。 */
  samPolarity?: "positive" | "negative";
  onChangeUserBoxClass?: (id: string) => void;
  onComposeTracks?: (options: VideoTrackCompositionOptions) => void;
  onConvertToBboxes?: (
    annotation: AnnotationResponse,
    options: VideoTrackConversionOptions,
  ) => void;
  onDelete?: (annotation: AnnotationResponse) => void;
  /** v0.21.4 · AI 候选采纳 / 驳回(贴框快捷条, 复用图片工作台的 handleAcceptPrediction/Reject)。 */
  onAcceptPrediction?: (b: AiBox) => void;
  onRejectPrediction?: (b: AiBox) => void;
  onPropagateTrack?: (annotation: VideoManagedTrackAnnotation) => void;
  onToggleHiddenTrack?: (trackId: string) => void;
  onToggleLockedTrack?: (trackId: string) => void;
  maskKeyframeActions?: VideoMaskKeyframeActionHandlers;
  /** 时间轴章节(从工作台 shell 透传)。 */
  chapters?: VideoTimelineChapter[];
  /** v0.21.13 · 章节 × 时间轴联动控制器 (刷选建章节 / resize / hover)。 */
  timelineChapterControls?: VideoTimelineChapterControls;
  /** v0.21.14 WS3 · AI 传播对话框打开时在时间轴高亮的影响范围。 */
  propagateRange?: VideoLoopRegion | null;
  trackerReview?: TrackerReviewProjection | null;
  reviewReference?: VideoTrackContextBarProps["reviewReference"];
  onSeekReviewFrame?: (frame: number) => void;
  segmentRange?: import("./VideoPlaybackOverlay").VideoSegmentTimelineRange | null;
  /** 采样配置(帧网格步进策略)。 */
  videoSampling?: VideoSamplingConfig | null;
  /** 默认播放速率。 */
  defaultPlaybackRate?: number;
  /** Shift+←/→ 大步进策略(默认 10)。 */
  largeFrameStep?: VideoLargeFrameStep;
}

const CONTEXT_MENU_DRAG_THRESHOLD_PX = 5;

const noop = () => {};

/**
 * 视频 Konva 渲染栈容器(v0.16.1–v0.16.5)。
 *
 * 底图/播放/标注/交互/右键菜单全栈进 Konva,并补齐旧 SVG 栈的 chrome 奇偶性:
 * VideoPlaybackOverlay(时间轴)、Minimap、VideoQcWarnings。关键帧跳转改由画布内
 * 选中卡(VideoTrackCardContent)承载,旧右上 <details> 快跳浮层已退役。
 * 所有播放/逐帧/书签/循环区间逻辑委托给 useVideoPlaybackController。
 */
export const VideoKonvaStage = forwardRef<VideoStageControls, VideoKonvaStageProps>(
  function VideoKonvaStage(
    {
      maskCompareStore,
      overlays,
      manifest,
      frameTimetable,
      isLoading = false,
      error,
      frameIndex: controlledFrameIndex,
      autoFitOnResize = true,
      focusSelectionEnabled = false,
      trackContinueAutoAdvance = false,
      performanceTier = "standard",
      onFrameIndexChange,
      annotations = EMPTY_ANNOTATIONS,
      aiBoxes = EMPTY_AI_BOXES,
      selectedId = null,
      hiddenTrackIds,
      reviewDisplayMode,
      trackColorOverrides,
      activeClass = "",
      keypointSchema = null,
      pendingDrawing = null,
      issuePixelFeedbacks,
      issueHighlightId,
      annotationCommentCounts,
      onOpenAnnotationComments,
      onIssuePinClick,
      issuePinDropArmed,
      issueNavigationPending,
      onIssuePinDrop,
      onSeekIssueFrame,
      visual = DEFAULT_ANNOTATION_VISUAL,
      videoTool = "select",
      isVideoToolEnabled,
      spacePan = false,
      onSpacePanDragStart,
      readOnly = false,
      lockedTrackIds = EMPTY_LOCKED,
      selectedIds = EMPTY_SELECTED_IDS,
      onSelect,
      onCursorMove,
      onCreate,
      onCreatePointsTrack,
      onCreatePoints,
      onCreateKeypoints,
      onPendingDraw,
      onUpdate,
      onSamPrompt,
      samCandidates = EMPTY_SAM_CANDIDATES,
      samMaskRecords = EMPTY_SAM_MASK_RECORDS,
      onSelectSamMaskCandidate,
      samActiveIdx = 0,
      samSessionPoints = EMPTY_SESSION_POINTS,
      samSessionBoxes = EMPTY_SESSION_BOXES,
      maskCandidates = EMPTY_MASK_CANDIDATES,
      maskEditor,
      onMaskCommit,
      onMaskCancel,
      samPolarity,
      onChangeUserBoxClass,
      onComposeTracks,
      onConvertToBboxes,
      onDelete,
      onAcceptPrediction,
      onRejectPrediction,
      onPropagateTrack,
      onToggleHiddenTrack,
      onToggleLockedTrack,
      maskKeyframeActions,
      chapters = [],
      timelineChapterControls,
      propagateRange = null,
      trackerReview,
      reviewReference,
      onSeekReviewFrame,
      segmentRange = null,
      videoSampling = null,
      defaultPlaybackRate,
      largeFrameStep = 10,
    },
    ref,
  ) {
    const containerRef = useRef<HTMLDivElement>(null);
    const stageRef = useRef<Konva.Stage>(null);
    const videoRef = useRef<HTMLVideoElement | null>(null);
    const [videoEl, setVideoEl] = useState<HTMLVideoElement | null>(null);
    const setVideoNode = useCallback((node: HTMLVideoElement | null) => {
      videoRef.current = node;
      setVideoEl(node);
    }, []);

    const { ref: setContainerNode, size: viewportSize } = useElementSize(containerRef);
    const { vp, vpRef, setVp: setViewport, fit, zoomAt: zoomViewportAt } = useViewportTransform();
    const sourceKey = JSON.stringify([manifest?.task_id ?? null, manifest?.video_url ?? null]);
    const sourceOwnerRef = useRef({ key: sourceKey, epoch: 0 });
    if (sourceOwnerRef.current.key !== sourceKey) {
      sourceOwnerRef.current = { key: sourceKey, epoch: sourceOwnerRef.current.epoch + 1 };
    }
    const sourceEpoch = sourceOwnerRef.current.epoch;
    const cancelIssueRestoreRef = useRef<() => void>(() => {});
    const interruptListenersRef = useRef(new Set<() => void>());
    const interruptMountedRef = useRef(false);
    useLayoutEffect(() => {
      interruptMountedRef.current = true;
      const listeners = interruptListenersRef.current;
      return () => {
        interruptMountedRef.current = false;
        listeners.clear();
      };
    }, []);
    const interruptIssueNavigation = useCallback(() => {
      cancelIssueRestoreRef.current();
      for (const listener of [...interruptListenersRef.current]) listener();
    }, []);
    const subscribeIssueNavigationInterrupt = useCallback(
      (listener: () => void) => {
        if (!interruptMountedRef.current || sourceOwnerRef.current.epoch !== sourceEpoch)
          return () => {};
        interruptListenersRef.current.add(listener);
        return () => {
          interruptListenersRef.current.delete(listener);
        };
      },
      [sourceEpoch],
    );
    const timelineWindowControlsRef = useRef<VideoTimelineWindowControls | null>(null);
    const runViewCommand = useCallback(
      (command: () => void) => {
        if (sourceOwnerRef.current.epoch !== sourceEpoch) return;
        interruptIssueNavigation();
        command();
      },
      [interruptIssueNavigation, sourceEpoch],
    );
    const setVp: Dispatch<SetStateAction<Viewport>> = useCallback(
      (next) => runViewCommand(() => setViewport(next)),
      [runViewCommand, setViewport],
    );
    const zoomAt = useCallback(
      (x: number, y: number, scale: number) => runViewCommand(() => zoomViewportAt(x, y, scale)),
      [runViewCommand, zoomViewportAt],
    );

    const [panning, setPanning] = useState(false);
    const panRef = useRef<{ x: number; y: number } | null>(null);
    const maskStrokeRef = useRef<{ lastX: number; lastY: number } | null>(null);
    const maskLassoRef = useRef<[number, number][] | null>(null);
    const [maskLassoPoints, setMaskLassoPoints] = useState<[number, number][]>([]);
    const [maskCursor, setMaskCursor] = useState<{ x: number; y: number } | null>(null);
    const maskCreationOwnerRef = useRef<{ tool: VideoTool; frameIndex: number } | null>(null);
    const maskToolActive = videoTool === "mask" || videoTool === "mask-track";

    // v0.16.3 · 交互:选中轨迹(供 track 工具画框落关键帧 + ghost 可编辑判定)。
    const selectedTrack = useMemo(() => {
      const a = annotations.find((x) => x.id === selectedId);
      return a && isVideoTrack(a) ? a : null;
    }, [annotations, selectedId]);
    // Selection identity remains available even when the current frame has no visible geometry.
    const selectedContextTrack = useMemo(() => {
      const annotation = annotations.find((item) => item.id === selectedId);
      return annotation && isAnyVideoTrack(annotation) ? annotation : null;
    }, [annotations, selectedId]);
    const selectedManagedTrack = useMemo<VideoManagedTrackAnnotation | null>(() => {
      const annotation = annotations.find((item) => item.id === selectedId);
      return annotation &&
        (isVideoTrack(annotation) || annotation.geometry.type === "video_track_mask")
        ? (annotation as VideoManagedTrackAnnotation)
        : null;
    }, [annotations, selectedId]);
    const selectedMaskAnnotation = useMemo(() => {
      const annotation = annotations.find((item) => item.id === selectedId);
      return annotation &&
        (isVideoMask(annotation) || annotation.geometry.type === "video_track_mask")
        ? annotation
        : null;
    }, [annotations, selectedId]);

    const noopSelect = useCallback(() => {}, []);
    const noopCreate = useCallback(() => {}, []);
    const noopUpdate = useCallback(() => {}, []);

    const [nativeSize, setNativeSize] = useState<{
      epoch: number;
      video: HTMLVideoElement;
      w: number;
      h: number;
    } | null>(null);
    useEffect(() => {
      if (!videoEl || !manifest?.video_url) return;
      let disposed = false;
      const expectedSource = new URL(manifest.video_url, document.baseURI).href;
      const readSize = () => {
        if (
          disposed ||
          sourceOwnerRef.current.epoch !== sourceEpoch ||
          videoEl.currentSrc !== expectedSource ||
          videoEl.readyState < HTMLMediaElement.HAVE_METADATA ||
          videoEl.videoWidth <= 0 ||
          videoEl.videoHeight <= 0
        )
          return;
        setNativeSize({
          epoch: sourceEpoch,
          video: videoEl,
          w: videoEl.videoWidth,
          h: videoEl.videoHeight,
        });
      };
      readSize();
      videoEl.addEventListener("loadedmetadata", readSize);
      return () => {
        disposed = true;
        videoEl.removeEventListener("loadedmetadata", readSize);
      };
    }, [manifest?.video_url, sourceEpoch, videoEl]);
    const metadataWidth = manifest?.metadata.width;
    const metadataHeight = manifest?.metadata.height;
    const hasMetadataSize =
      typeof metadataWidth === "number" &&
      Number.isFinite(metadataWidth) &&
      metadataWidth > 0 &&
      typeof metadataHeight === "number" &&
      Number.isFinite(metadataHeight) &&
      metadataHeight > 0;
    const hasNativeSize = nativeSize?.epoch === sourceEpoch && nativeSize.video === videoEl;
    const hasRealMediaSize = hasMetadataSize || hasNativeSize;
    const size = useMemo(
      () =>
        hasMetadataSize
          ? { w: metadataWidth, h: metadataHeight }
          : hasNativeSize && nativeSize
            ? { w: nativeSize.w, h: nativeSize.h }
            : videoIntrinsicSize(metadataWidth, metadataHeight),
      [hasMetadataSize, hasNativeSize, metadataHeight, metadataWidth, nativeSize],
    );
    const maskCompareViewport = useMemo(() => {
      if (!maskCompareStore || vp.scale <= 0 || viewportSize.w <= 0 || viewportSize.h <= 0)
        return null;
      const x0 = Math.max(0, -vp.tx / vp.scale);
      const y0 = Math.max(0, -vp.ty / vp.scale);
      const x1 = Math.min(size.w, (viewportSize.w - vp.tx) / vp.scale);
      const y1 = Math.min(size.h, (viewportSize.h - vp.ty) / vp.scale);
      return x1 > x0 && y1 > y0 ? { x: x0, y: y0, width: x1 - x0, height: y1 - y0 } : null;
    }, [maskCompareStore, size.h, size.w, viewportSize.h, viewportSize.w, vp.scale, vp.tx, vp.ty]);
    const maskOperationPreviewBuffer = useMemo(() => {
      const preview = maskEditor?.operationPreview;
      if (!preview || preview.alpha.length !== size.w * size.h) return null;
      const buffer = new MaskBuffer({ width: size.w, height: size.h });
      buffer.replaceAlpha(preview.alpha);
      return buffer;
    }, [maskEditor?.operationPreview, size.h, size.w]);
    const maskInstancePreviewBuffer = useMemo(() => {
      const preview = maskEditor?.instanceOperationPreview;
      if (!preview || preview.plan.focusAlpha.length !== size.w * size.h) return null;
      const buffer = new MaskBuffer({ width: size.w, height: size.h });
      buffer.replaceAlpha(preview.plan.focusAlpha);
      return buffer;
    }, [maskEditor?.instanceOperationPreview, size.h, size.w]);

    // 右键上下文菜单状态
    const contextMenu = useCanvasContextMenu();
    const [contextMenuTargetId, setContextMenuTargetId] = useState<string | null>(null);
    const rightDownRef = useRef<{ x: number; y: number } | null>(null);
    const closeContextMenu = useCallback(() => {
      contextMenu.close();
      setContextMenuTargetId(null);
    }, [contextMenu]);

    // v0.21.9 · 预测帧集合 (video_bbox 帧号 + video_track_bbox 关键帧号, 去重升序); 喂时间轴预测密度轨。
    const predictedFrames = useMemo(() => collectPredictedFrames(aiBoxes), [aiBoxes]);

    // ---- useVideoPlaybackController ----
    // currentFrameEntries 供 QC 用,控制器内部用它做重叠率计算。
    // 此处传空数组占位 — 重叠率 QC 会不计分,属于可接受的简化(旧栈的 currentFrameEntries 走的
    // 是同一帧解析,与 frameViews.entries 语义等价;后续可回填)。
    const controller = useVideoPlaybackController({
      manifest,
      frameTimetable,
      videoRef,
      controlledFrameIndex,
      onFrameIndexChange,
      onSelect,
      performanceTier,
      videoSampling,
      defaultPlaybackRate: defaultPlaybackRate as (1 | 0.25 | 0.5 | 2 | 4) | undefined,
      annotations,
      predictedFrames,
      selectedId,
      selectedTrack,
      trackColorOverrides,
      hiddenTrackIds: hiddenTrackIds ?? EMPTY_LOCKED,
      lockedTrackIds,
      readOnly,
      drag: null,
      currentFrameEntries: [],
      issuePixelFeedbacks,
      onUpdate: (onUpdate ?? noopUpdate) as Parameters<
        typeof useVideoPlaybackController
      >[0]["onUpdate"],
      onToggleHiddenTrack,
      onToggleLockedTrack,
      onPropagateTrack,
    });

    const {
      frameIndex,
      isPlaybackActive,
      isJogPlaying,
      jogPlayback,
      playbackError,
      displayBitmap,
      frameSource,
      preciseSourceState,
      precisePaintedFrameIndex,
      markPreciseFramePainted,
      framePresentation,
      markFramePresented,
      cachedRanges,
      framePreview,
      previewFrame,
      samplingStep,
      maxFrame,
      timebase,
      globalTimelineDensity,
      predictionDensity,
      hasPredictedFrames,
      seekToAdjacentPredictedFrame: seekToAdjacentPredictedFrameInternal,
      issueFrames,
      playbackOverlayVisible,
      highlightAction,
      bookmarks,
      loopRegion,
      showPlaybackOverlay,
      schedulePlaybackOverlayHide,
      setNormalizedLoopRegion,
      clearLoopRegion,
      seekOverlayByFrames: seekOverlayByFramesInternal,
      controls: controllerControls,
    } = controller;
    const controls = useMemo<typeof controllerControls>(
      () => ({
        ...controllerControls,
        togglePlayback: () => runViewCommand(controllerControls.togglePlayback),
        jogPlayback: (direction) => runViewCommand(() => controllerControls.jogPlayback(direction)),
        pausePlayback: (options) => {
          if (options?.snapToGrid === false) {
            if (sourceOwnerRef.current.epoch === sourceEpoch)
              controllerControls.pausePlayback(options);
            return;
          }
          runViewCommand(() => controllerControls.pausePlayback(options));
        },
        seekByFrames: (delta, options) =>
          runViewCommand(() => controllerControls.seekByFrames(delta, options)),
        seekGrid: (direction, options) =>
          runViewCommand(() => controllerControls.seekGrid(direction, options)),
        microStep: (direction, options) =>
          runViewCommand(() => controllerControls.microStep(direction, options)),
        seekToKeyframe: (direction, options) =>
          runViewCommand(() => controllerControls.seekToKeyframe(direction, options)),
        seekToFrame: (frame, options) =>
          runViewCommand(() => controllerControls.seekToFrame(frame, options)),
        seekToFrameReady: (frame, options) => {
          if (sourceOwnerRef.current.epoch !== sourceEpoch)
            return Promise.resolve({ status: "cancelled", frameIndex: frame, source: null });
          cancelIssueRestoreRef.current();
          return controllerControls.seekToFrameReady(frame, options);
        },
        jumpHistory: (direction) => runViewCommand(() => controllerControls.jumpHistory(direction)),
      }),
      [controllerControls, runViewCommand, sourceEpoch],
    );
    const { seekToFrame, pausePlayback } = controls;
    const seekOverlayByFrames = useCallback(
      (...args: Parameters<typeof seekOverlayByFramesInternal>) =>
        runViewCommand(() => seekOverlayByFramesInternal(...args)),
      [runViewCommand, seekOverlayByFramesInternal],
    );
    const seekToAdjacentPredictedFrame = useCallback(
      (...args: Parameters<typeof seekToAdjacentPredictedFrameInternal>) =>
        runViewCommand(() => seekToAdjacentPredictedFrameInternal(...args)),
      [runViewCommand, seekToAdjacentPredictedFrameInternal],
    );

    const effectiveSelectedTrackTimeline = useMemo(
      () =>
        selectedContextTrack
          ? buildSelectedTrackTimeline(
              selectedContextTrack.geometry,
              selectedContextTrack.geometry.type === "video_track_mask" ? "held" : "interpolated",
            )
          : null,
      [selectedContextTrack],
    );
    const effectiveSelectedTrackColor = useMemo(
      () =>
        selectedContextTrack
          ? getTrackColor(
              selectedContextTrack.geometry.track_id,
              selectedContextTrack.class_name,
              trackColorOverrides,
            )
          : undefined,
      [selectedContextTrack, trackColorOverrides],
    );
    const trackContext = useMemo(
      () =>
        selectedContextTrack
          ? deriveVideoTrackContext(selectedContextTrack.geometry, frameIndex)
          : null,
      [frameIndex, selectedContextTrack],
    );

    // 当前帧的拖框 pending draft；OBB 初建角度为 0，可复用轴对齐预览。
    const pendingDraft = useMemo(() => {
      if (
        !pendingDrawing ||
        (pendingDrawing.kind !== "video_bbox" &&
          pendingDrawing.kind !== "video_track_bbox" &&
          pendingDrawing.kind !== "video_rotated_bbox") ||
        pendingDrawing.frameIndex !== frameIndex
      ) {
        return null;
      }
      return { geom: pendingDrawing.geom, className: activeClass || "未分类" };
    }, [activeClass, frameIndex, pendingDrawing]);

    const pendingPointsDraft = useMemo(() => {
      if (
        !pendingDrawing ||
        (pendingDrawing.kind !== "video_polygon" &&
          pendingDrawing.kind !== "video_polyline" &&
          pendingDrawing.kind !== "video_track_polygon" &&
          pendingDrawing.kind !== "video_track_polyline") ||
        pendingDrawing.frameIndex !== frameIndex ||
        !pendingDrawing.points?.length
      ) {
        return null;
      }
      return {
        points: pendingDrawing.points,
        closed:
          pendingDrawing.kind === "video_polygon" || pendingDrawing.kind === "video_track_polygon",
      };
    }, [frameIndex, pendingDrawing]);

    const pendingKeypoints = useMemo(
      () =>
        pendingDrawing?.kind === "video_keypoint" && pendingDrawing.frameIndex === frameIndex
          ? pendingDrawing.points
          : null,
      [frameIndex, pendingDrawing],
    );

    // 标注渲染派生(纯函数,与 VideoStage 现状对齐)。
    const referenceConfig = useVideoReferenceConfig();
    const frameViews = useMemo(
      () =>
        deriveVideoFrameViews({
          annotations,
          frameIndex,
          selectedId,
          selectedIds,
          hiddenTrackIds,
          lockedTrackIds,
          reviewDisplayMode,
          trackColorOverrides,
          visual,
          referenceConfig,
          pendingDraft,
          samplingStep,
        }),
      [
        annotations,
        frameIndex,
        hiddenTrackIds,
        lockedTrackIds,
        pendingDraft,
        referenceConfig,
        reviewDisplayMode,
        samplingStep,
        selectedId,
        selectedIds,
        trackColorOverrides,
        visual,
      ],
    );

    const visibleMaskAnnotations = useMemo(
      () =>
        annotations.filter((annotation) => {
          if (annotation.is_hidden) return false;
          if (annotation.geometry.type === "video_mask") {
            return (
              annotation.geometry.frame_index === frameIndex &&
              visibleInReviewMode("legacy", reviewDisplayMode)
            );
          }
          if (annotation.geometry.type !== "video_track_mask") return false;
          if (hiddenTrackIds?.has(annotation.geometry.track_id)) return false;
          const resolved = resolveVideoMaskTrackAtFrame(annotation.geometry, frameIndex);
          return resolved !== null && visibleInReviewMode(resolved.source, reviewDisplayMode);
        }),
      [annotations, frameIndex, hiddenTrackIds, reviewDisplayMode],
    );
    const maskColorForAnnotation = useCallback(
      (annotation: AnnotationResponse) => {
        if (annotation.geometry.type === "video_mask") {
          return colorToHex(classColor(annotation.class_name));
        }
        if (annotation.geometry.type !== "video_track_mask") return "#a855f7";
        return colorToHex(
          getTrackColor(annotation.geometry.track_id, annotation.class_name, trackColorOverrides),
        );
      },
      [trackColorOverrides],
    );
    const maskColorForPrediction = useCallback(
      (prediction: AiBox) => colorToHex(classColor(prediction.cls)),
      [],
    );
    const maskEditorColor = useMemo(
      () =>
        hexToRgb(
          selectedMaskAnnotation
            ? maskColorForAnnotation(selectedMaskAnnotation)
            : colorToHex(classColor(activeClass || "mask")),
        ),
      [activeClass, maskColorForAnnotation, selectedMaskAnnotation],
    );
    const maskRecords = useVideoMaskFrames({
      taskId: manifest?.task_id ?? null,
      annotations: visibleMaskAnnotations,
      candidates: maskCandidates,
      predictions: aiBoxes,
      frameIndex,
      selectedId,
      colorForAnnotation: maskColorForAnnotation,
      colorForPrediction: maskColorForPrediction,
    });
    const committedMaskRecords = useMemo(
      () => maskRecords.filter((record) => record.source === "annotation"),
      [maskRecords],
    );
    const selectableMaskRecords = useMemo(
      () => maskRecords.filter((record) => record.source !== "tracker"),
      [maskRecords],
    );
    const maskTrackNumbers = useMemo(
      () =>
        deriveTrackNumber(
          annotations.filter(isAnyVideoTrack) as Array<
            AnnotationResponse & {
              geometry:
                | VideoTrackGeometry
                | VideoTrackPolygonGeometry
                | VideoTrackPolylineGeometry
                | VideoTrackMaskGeometry;
            }
          >,
        ),
      [annotations],
    );
    const maskLabels = useMemo<VideoLabelView[]>(() => {
      const byId = new Map(annotations.map((annotation) => [annotation.id, annotation]));
      return committedMaskRecords.flatMap((record) => {
        const annotation = byId.get(record.id);
        if (
          !annotation ||
          !shouldShowLabel(record.selected, visual.labelVisibility) ||
          (annotation.geometry.type !== "video_mask" &&
            annotation.geometry.type !== "video_track_mask")
        )
          return [];
        const resolved =
          annotation.geometry.type === "video_track_mask"
            ? resolveVideoMaskTrackAtFrame(annotation.geometry, frameIndex)
            : null;
        const stateSuffix = resolved?.occluded
          ? "遮挡"
          : resolved && resolved.keyframeFrame !== frameIndex
            ? `保持 F${resolved.keyframeFrame}`
            : undefined;
        return [
          {
            key: `mask-label-${record.cacheKey}`,
            geom: record.geom,
            color: record.color,
            text: buildTrackLabelText(
              {
                className: annotation.class_name,
                trackNumber:
                  annotation.geometry.type === "video_track_mask"
                    ? maskTrackNumbers.get(annotation.id)
                    : undefined,
                stateSuffix,
                attributes:
                  resolved?.attributes ??
                  (annotation as { attributes?: Record<string, unknown> | null }).attributes ??
                  null,
              },
              visual.labelContent.track,
            ),
          },
        ];
      });
    }, [
      annotations,
      committedMaskRecords,
      frameIndex,
      maskTrackNumbers,
      visual.labelContent.track,
      visual.labelVisibility,
    ]);
    const displayedMaskRecords = useMemo(
      () =>
        maskRecords.filter((record) => {
          if (
            !maskCompareCompanionVisible(maskCompareStore?.display, {
              source: record.source === "prediction" ? "ai" : record.source,
              id: record.id,
            })
          )
            return false;
          return (
            !(maskEditor?.active && selectedId) ||
            record.source === "tracker" ||
            record.id !== selectedId
          );
        }),
      [maskCompareStore, maskEditor?.active, maskRecords, selectedId],
    );
    const maskCompareActive = !!maskCompareStore?.display;
    const displayedSamMaskRecords = maskCompareCompanionVisible(maskCompareStore?.display, {
      source: "ai",
    })
      ? samMaskRecords
      : [];

    // v0.21.4 · AI 候选按当前帧过滤(镜像 deriveVideoFrameViews 的视频几何帧过滤)。
    // v0.21.9 WS2 · 检测式轨迹候选(video_track_bbox)也纳入: 用 resolveTrackAtFrame 解出当前帧框,
    //   与逐帧 video_bbox 候选同层渲染(此前只在侧栏可见、画布不画)。
    const frameAiBoxes = useMemo(() => {
      const maskBounds = new Map(
        maskRecords
          .filter((record) => record.source === "prediction")
          .map((record) => [record.id, record.geom]),
      );
      return aiBoxes
        .map((box) => resolveAiBoxAtFrame(box, frameIndex))
        .filter((box): box is (typeof aiBoxes)[number] => box !== null)
        .map((box) => {
          const bounds = maskBounds.get(box.id);
          return bounds ? { ...box, ...bounds } : box;
        });
    }, [aiBoxes, frameIndex, maskRecords]);
    const selectedAiBox = useMemo(
      () => frameAiBoxes.find((b) => b.id === selectedId) ?? null,
      [frameAiBoxes, selectedId],
    );

    // v0.21.11 · 当前帧三类对象(AI 待审 / 人工 video_bbox / 轨迹当前帧视图)分类 + 空间排序,
    // 供 Tab 同类流转 / ` 跨类跳转。人工 vs 轨迹按 annotation.geometry 类型判别; AI 用扁平 x/y。
    // v0.21.12 · 跨网格帧续写待续轨迹(carryOverGhosts)并入「轨迹」类一起循环。
    // fix · 选中轨迹的参考虚影(ghost)也必须并入「轨迹」类, 否则选中它时 Tab 只在两条间弹
    //       (根因见 collectFrameCategories.selectedTrackGhost 注释)。
    const frameCategories = useMemo(() => {
      const ai: FrameObjectRef[] = frameAiBoxes.map((b) => ({ id: b.id, x: b.x, y: b.y }));
      const entries = frameViews.entries.map((entry) => {
        const ann = annotations.find((a) => a.id === entry.id);
        return {
          id: entry.id,
          x: entry.geom.x,
          y: entry.geom.y,
          isTrack: Boolean(ann && isVideoTrack(ann)),
        };
      });
      for (const mask of committedMaskRecords) {
        entries.push({ id: mask.id, x: mask.geom.x, y: mask.geom.y, isTrack: mask.isTrack });
      }
      const carryOverGhosts = frameViews.carryOverGhosts.map((g) => ({
        id: g.id,
        x: g.geom.x,
        y: g.geom.y,
      }));
      const selectedTrackGhost = frameViews.ghost
        ? { id: frameViews.ghost.id, x: frameViews.ghost.geom.x, y: frameViews.ghost.geom.y }
        : null;
      return collectFrameCategories({ ai, entries, carryOverGhosts, selectedTrackGhost });
    }, [
      annotations,
      committedMaskRecords,
      frameAiBoxes,
      frameViews.carryOverGhosts,
      frameViews.entries,
      frameViews.ghost,
    ]);

    const cycleInCategory = useCallback(
      (dir: -1 | 1) => {
        const next = nextInCategory(frameCategories, selectedId, dir);
        if (next) onSelect?.(next);
      },
      [frameCategories, onSelect, selectedId],
    );

    const stepCategory = useCallback(
      (dir: -1 | 1) => {
        const next = nextCategory(frameCategories, selectedId, dir);
        if (next) onSelect?.(next);
      },
      [frameCategories, onSelect, selectedId],
    );

    // v0.21.11 WS2 · 焦点联动: 把对象平移居中(仅出视口/过小才动, 保守不打断已在视口的选中)。
    const focusObject = useCallback(
      (id: string) => {
        if (!viewportSize.w || !viewportSize.h || !size.w || !size.h) return;
        const ai = frameAiBoxes.find((b) => b.id === id);
        const geom = ai
          ? { x: ai.x, y: ai.y, w: ai.w, h: ai.h }
          : (frameViews.entries.find((e) => e.id === id)?.geom ??
            frameViews.carryOverGhosts.find((g) => g.id === id)?.geom ??
            committedMaskRecords.find((mask) => mask.id === id)?.geom ??
            null);
        if (!geom) return;
        const cur = vpRef.current;
        const cx = (geom.x + geom.w / 2) * size.w;
        const cy = (geom.y + geom.h / 2) * size.h;
        const objMaxDimPx = Math.max(geom.w * size.w, geom.h * size.h, 1);
        // 保守缩放: 仅当对象在屏过小才放大到舒适尺寸, 否则保持当前 scale(优先平移居中)。
        let scale = cur.scale;
        if (objMaxDimPx * scale < 48) scale = clampScale(140 / objMaxDimPx);
        const margin = 48;
        const screenCx = cx * scale + cur.tx;
        const screenCy = cy * scale + cur.ty;
        const outOfView =
          screenCx < margin ||
          screenCx > viewportSize.w - margin ||
          screenCy < margin ||
          screenCy > viewportSize.h - margin;
        // 已在视口内且无需变焦 → 不动(避免每次选中都重排, 保留上下文)。
        if (!outOfView && scale === cur.scale) return;
        setViewport({
          scale,
          tx: viewportSize.w / 2 - cx * scale,
          ty: viewportSize.h / 2 - cy * scale,
        });
      },
      [
        committedMaskRecords,
        frameAiBoxes,
        frameViews.carryOverGhosts,
        frameViews.entries,
        setViewport,
        size.h,
        size.w,
        viewportSize.h,
        viewportSize.w,
        vpRef,
      ],
    );

    // QC 质量警告(关键帧间隔过大 / 当前帧极小框 / 同类高重叠)——与旧 SVG 栈 qualityWarnings 逐位一致。
    // 用当前帧 frameViews.entries(带 geom+className),解决控制器内因 frameIndex→entries 循环依赖
    // 而拿不到当前帧框的问题(此处 entries 已在 controller.frameIndex 之后派生)。
    const videoTracks = useMemo(() => annotations.filter(isVideoTrack), [annotations]);
    const qualityWarnings = useMemo(() => {
      const warnings: string[] = [];
      const maxGap = Math.max(30, Math.round(timebase.fps * 2));
      for (const ann of videoTracks) {
        const keyframes = sortedKeyframes(ann.geometry);
        for (let i = 1; i < keyframes.length; i++) {
          const gap = keyframes[i].frame_index - keyframes[i - 1].frame_index;
          if (gap > maxGap) {
            warnings.push(
              `${ann.class_name} ${shortTrackId(ann.geometry.track_id)} 关键帧间隔 ${gap} 帧`,
            );
            break;
          }
        }
      }
      const entries = frameViews.entries;
      for (const entry of entries) {
        if (entry.geom.w < 0.003 || entry.geom.h < 0.003)
          warnings.push(`${entry.className} 当前帧存在极小框`);
      }
      for (let i = 0; i < entries.length; i++) {
        for (let j = i + 1; j < entries.length; j++) {
          const a = entries[i];
          const b = entries[j];
          if (a.className === b.className && shapeIou(a.geom, b.geom) > 0.9) {
            warnings.push(`${a.className} 当前帧存在高度重叠框`);
          }
        }
      }
      return [...new Set(warnings)].slice(0, 3);
    }, [frameViews.entries, timebase.fps, videoTracks]);

    const interaction = useVideoKonvaInteraction({
      containerRef,
      vpRef,
      size,
      annotations,
      entries: frameViews.entries,
      maskEntries: selectableMaskRecords,
      ghost: frameViews.ghost,
      carryOverGhosts: frameViews.carryOverGhosts,
      selectedTrack,
      videoTool,
      creationEnabled:
        (videoTool === "box" || videoTool === "track" || videoTool === "rotated-box") &&
        (!isVideoToolEnabled || isVideoToolEnabled(videoTool)),
      readOnly,
      isPlaybackActive,
      lockedTrackIds,
      frameIndex,
      trackContinueAutoAdvance,
      onSelect: onSelect ?? noopSelect,
      onCreate: onCreate ?? noopCreate,
      onPendingDraw,
      onUpdate: onUpdate ?? noopUpdate,
      onSamPrompt,
      samPolarity,
    });
    const { drag } = interaction;

    // v0.21.20/21 · polygon/polyline 绘制 (点击落点, Enter/双击闭合)。与拖拽 bbox 正交。
    // 四工具: polygon/polyline = 单帧几何; polygon-track/polyline-track = 轨迹关键帧。
    const pointsDraft = useVideoPolygonDraft();
    const pointsOwnerRef = useRef<VideoDrawingDraft | null>(null);
    // 绘制中的光标归一化坐标(橡皮筋预览段 + 首点吸附高亮用),越界/未绘制时 null。
    const [pointsCursor, setPointsCursor] = useState<{ x: number; y: number } | null>(null);
    const isPointsClosedTool = videoTool === "polygon" || videoTool === "polygon-track";
    const isPointsDrawTool =
      isPointsClosedTool || videoTool === "polyline" || videoTool === "polyline-track";
    const pointsDrawEnabled =
      isPointsDrawTool &&
      !readOnly &&
      !isPlaybackActive &&
      (!isVideoToolEnabled || isVideoToolEnabled(videoTool));

    const pointFromClientEvt = useCallback(
      (clientX: number, clientY: number) => {
        const rect = containerRef.current?.getBoundingClientRect();
        if (!rect) return null;
        return clientToVideoNorm(clientX, clientY, rect, vpRef.current, size);
      },
      [size, vpRef],
    );

    const [keypointDraft, setKeypointDraft] = useState<Keypoint[]>([]);
    const keypointOwnerRef = useRef<VideoDrawingDraft | null>(null);
    const keypointDrawEnabled =
      videoTool === "keypoint" &&
      !pendingDrawing &&
      !readOnly &&
      !isPlaybackActive &&
      (keypointSchema?.nodes.length ?? 0) > 0 &&
      (!isVideoToolEnabled || isVideoToolEnabled(videoTool));
    useEffect(() => {
      keypointOwnerRef.current = null;
      setKeypointDraft([]);
    }, [frameIndex, keypointSchema?.nodes.length, videoTool]);

    useEffect(() => {
      if (
        !maskEditor?.active ||
        selectedMaskAnnotation ||
        maskCreationOwnerRef.current?.frameIndex !== frameIndex
      )
        maskCreationOwnerRef.current = null;
    }, [frameIndex, maskEditor?.active, selectedMaskAnnotation]);

    const cancelPointsDraft = useCallback(() => {
      pointsOwnerRef.current = null;
      pointsDraft.cancel();
      setPointsCursor(null);
    }, [pointsDraft]);

    const getDrawingDraft = useCallback((): VideoDrawingDraft | null => {
      if (pointsDraft.draft?.points.length && pointsOwnerRef.current)
        return { ...pointsOwnerRef.current };
      if (keypointDraft.length && keypointOwnerRef.current) return { ...keypointOwnerRef.current };
      return interaction.getDrawingDraft();
    }, [interaction, keypointDraft.length, pointsDraft.draft]);

    const discardDrawingDraft = useCallback(() => {
      cancelPointsDraft();
      keypointOwnerRef.current = null;
      setKeypointDraft([]);
      interaction.discardDrawingDraft();
    }, [cancelPointsDraft, interaction]);

    const commitPointsDraft = useCallback(() => {
      const owner = pointsOwnerRef.current;
      if (!owner) return;
      const pts = pointsDraft.commit();
      pointsOwnerRef.current = null;
      setPointsCursor(null);
      if (!pts) return;
      if (owner.tool === "polygon-track" || owner.tool === "polyline-track") {
        onCreatePointsTrack?.(
          owner.tool === "polyline-track" ? "video_track_polyline" : "video_track_polygon",
          owner.frameIndex,
          pts,
        );
      } else {
        onCreatePoints?.(
          owner.tool === "polyline" ? "video_polyline" : "video_polygon",
          owner.frameIndex,
          pts,
        );
      }
    }, [pointsDraft, onCreatePointsTrack, onCreatePoints]);

    // 落点: polygon/polyline 工具下 Stage pointerdown 累加顶点 (阻断拖拽/选择分流)。
    const handleStagePointerDown = useCallback(
      (e: Parameters<typeof interaction.onStagePointerDown>[0]) => {
        if (issuePinDropArmed || issueNavigationPending) {
          e.cancelBubble = true;
          return;
        }
        if (spacePan || panRef.current || isWorkbenchInteractionBlocked(e.evt)) return;
        if ((e.evt.ctrlKey || e.evt.metaKey) && samMaskRecords.length > 0) {
          const point = pointFromClientEvt(e.evt.clientX, e.evt.clientY);
          const candidate = point ? pickTopRasterMaskAt(samMaskRecords, point) : null;
          if (candidate) {
            e.cancelBubble = true;
            onSelectSamMaskCandidate?.(candidate.id);
            return;
          }
        }
        // v0.23.5 · WS-C · 视频 mask 落点经 canEditMask: 同时检查 task readOnly、选中轨迹 lock、
        // annotation is_locked, 关闭锁定对象经视频 pointer 路径修改的绕过。
        const selectedTrackId = selectedManagedTrack?.geometry.track_id;
        const maskEditable =
          !!maskEditor &&
          canEditMask({
            taskReadOnly: !!readOnly || isPlaybackActive || maskCompareActive,
            annotationLocked: !!selectedMaskAnnotation?.is_locked,
            trackLocked: !!selectedTrackId && lockedTrackIds.has(selectedTrackId),
            segmentLocked: false,
            editorPhase:
              maskEditor.phase ??
              (maskEditor.dirty ? "dirty" : maskEditor.active ? "ready" : "idle"),
          });
        if (maskToolActive && maskEditor && maskEditable) {
          const native = e.evt;
          if (native.button !== 0) return;
          const point = pointFromClientEvt(native.clientX, native.clientY);
          if (!point) return;
          e.cancelBubble = true;
          containerRef.current?.setPointerCapture?.(native.pointerId);
          if (!selectedMaskAnnotation && !maskCreationOwnerRef.current) {
            maskCreationOwnerRef.current = { tool: videoTool, frameIndex };
          }
          if (!maskEditor.active) maskEditor.beginBlank();
          const x = point.x * size.w;
          const y = point.y * size.h;
          if (maskEditor.tool === "fill_add" || maskEditor.tool === "fill_subtract") {
            void maskEditor.runOperation(maskEditor.tool, {
              type: "flood_fill",
              x,
              y,
              value: maskEditor.tool === "fill_add" ? 255 : 0,
              connectivity: maskEditor.connectivity,
            });
            setMaskCursor(point);
            return;
          }
          if (maskEditor.tool === "lasso_add" || maskEditor.tool === "lasso_subtract") {
            maskLassoRef.current = [[x, y]];
            setMaskLassoPoints([[x, y]]);
            setMaskCursor(point);
            return;
          }
          if (maskEditor.tool === "component_keep" || maskEditor.tool === "component_delete") {
            void maskEditor.runOperation(maskEditor.tool, {
              type: "component",
              action: maskEditor.tool === "component_keep" ? "keep" : "delete",
              x,
              y,
              connectivity: maskEditor.connectivity,
            });
            setMaskCursor(point);
            return;
          }
          if (maskEditor.tool === "component_copy") {
            void maskEditor.runInstanceOperation("copy_component", {
              type: "copy_component",
              x,
              y,
              connectivity: maskEditor.connectivity,
            });
            setMaskCursor(point);
            return;
          }
          if (maskEditor.tool === "hole_fill") {
            void maskEditor.runOperation(maskEditor.tool, {
              type: "fill_holes",
              mode: "hit",
              x,
              y,
            });
            setMaskCursor(point);
            return;
          }
          maskEditor.beginStroke();
          maskEditor.paintAt(x, y);
          maskStrokeRef.current = { lastX: x, lastY: y };
          setMaskCursor(point);
          return;
        }
        if (keypointDrawEnabled) {
          const native = e.evt;
          if (native.button !== 0 && native.button !== 2) return;
          const pt = pointFromClientEvt(native.clientX, native.clientY);
          if (!pt) return;
          e.cancelBubble = true;
          if (keypointDraft.length === 0) {
            onSelect?.(null);
            keypointOwnerRef.current = { kind: "keypoint", tool: videoTool, frameIndex };
          }
          const next = [
            ...keypointDraft,
            { x: pt.x, y: pt.y, v: native.button === 2 ? 0 : native.altKey ? 1 : 2 } as Keypoint,
          ];
          if (next.length >= (keypointSchema?.nodes.length ?? 0)) {
            onCreateKeypoints?.(keypointOwnerRef.current?.frameIndex ?? frameIndex, next);
            keypointOwnerRef.current = null;
            setKeypointDraft([]);
          } else {
            setKeypointDraft(next);
          }
          return;
        }
        if (pointsDrawEnabled) {
          const native = e.evt;
          if (native.button !== 0) return; // 右键/中键平移交容器层
          const pt = pointFromClientEvt(native.clientX, native.clientY);
          if (!pt) return;
          // polygon: 点击落在首点吸附半径内 → 闭合提交(需 ≥3 点)。
          const pts = pointsDraft.draft?.points;
          if (isPointsClosedTool && pts && pts.length >= 3) {
            const [fx, fy] = pts[0];
            if (Math.hypot(pt.x - fx, pt.y - fy) <= CLOSE_DISTANCE) {
              commitPointsDraft();
              return;
            }
          }
          if (!pts?.length)
            pointsOwnerRef.current = { kind: "points", tool: videoTool, frameIndex };
          pointsDraft.addPoint(pt, isPointsClosedTool);
          return;
        }
        interaction.onStagePointerDown(e);
      },
      [
        commitPointsDraft,
        frameIndex,
        interaction,
        issuePinDropArmed,
        issueNavigationPending,
        isPlaybackActive,
        isPointsClosedTool,
        lockedTrackIds,
        maskCompareActive,
        maskEditor,
        maskToolActive,
        keypointDraft,
        keypointDrawEnabled,
        keypointSchema?.nodes.length,
        onCreateKeypoints,
        onSelect,
        onSelectSamMaskCandidate,
        pointFromClientEvt,
        pointsDrawEnabled,
        pointsDraft,
        readOnly,
        samMaskRecords,
        selectedManagedTrack,
        selectedMaskAnnotation,
        size.h,
        size.w,
        spacePan,
        videoTool,
      ],
    );

    useEffect(() => {
      if (!maskToolActive || !maskEditor) return;
      const onKey = (event: KeyboardEvent) => {
        if (isMaskHotkeyBlocked(event)) return;
        if (maskCompareActive) return;
        const command = event.ctrlKey || event.metaKey;
        const selectedTrackId = selectedManagedTrack?.geometry.track_id;
        const phase =
          maskEditor.phase ?? (maskEditor.dirty ? "dirty" : maskEditor.active ? "ready" : "idle");
        const editable = canEditMask({
          taskReadOnly: !!readOnly || isPlaybackActive,
          annotationLocked: !!selectedMaskAnnotation?.is_locked,
          trackLocked: !!selectedTrackId && lockedTrackIds.has(selectedTrackId),
          segmentLocked: false,
          editorPhase: phase,
        });
        if (command && (event.key.toLowerCase() === "z" || event.key.toLowerCase() === "y")) {
          const isRedo = event.key.toLowerCase() === "y" || event.shiftKey;
          const canHandle = isRedo ? maskEditor.canRedo : maskEditor.canUndo;
          if (!editable || canHandle) {
            event.preventDefault();
            event.stopImmediatePropagation();
            if (!editable) return;
            if (isRedo) maskEditor.redo();
            else maskEditor.undo();
            return;
          }
        }
        if (event.key === "b" || event.key === "B") {
          event.preventDefault();
          event.stopImmediatePropagation();
          if (!editable) return;
          maskEditor.setMode("brush");
        } else if (event.key === "e" || event.key === "E") {
          event.preventDefault();
          event.stopImmediatePropagation();
          if (!editable) return;
          maskEditor.setMode("erase");
        } else if (event.key === "Enter") {
          event.preventDefault();
          event.stopImmediatePropagation();
          onMaskCommit?.();
        } else if (event.key === "Escape") {
          event.preventDefault();
          event.stopImmediatePropagation();
          onMaskCancel?.();
        }
      };
      window.addEventListener("keydown", onKey, true);
      return () => window.removeEventListener("keydown", onKey, true);
    }, [
      isPlaybackActive,
      lockedTrackIds,
      maskCompareActive,
      maskEditor,
      onMaskCancel,
      onMaskCommit,
      readOnly,
      selectedManagedTrack,
      selectedMaskAnnotation,
      maskToolActive,
    ]);

    // Enter/双击 闭合提交; Esc 取消。切工具/只读 时丢弃草稿。
    useEffect(() => {
      if (!pointsDrawEnabled && pointsDraft.draft) cancelPointsDraft();
    }, [cancelPointsDraft, pointsDrawEnabled, pointsDraft.draft]);
    // 切帧时丢弃未提交的顶点草稿: 顶点是起草帧的像素坐标, 若带到新帧提交会错位落在新帧上。
    // ref 守卫「帧真的变了」才取消 (pointsDraft 身份每渲染变, 不守卫会误伤同帧正常绘制)。
    const draftFrameRef = useRef(frameIndex);
    useEffect(() => {
      if (draftFrameRef.current === frameIndex) return;
      draftFrameRef.current = frameIndex;
      if (pointsDraft.draft) cancelPointsDraft();
    }, [cancelPointsDraft, frameIndex, pointsDraft.draft]);
    useEffect(() => {
      if (!pointsDrawEnabled) return;
      const onKey = (e: KeyboardEvent) => {
        if (isWorkbenchInteractionBlocked(e)) return;
        if (e.key === "Enter") {
          e.preventDefault();
          commitPointsDraft();
        } else if (e.key === "Escape") {
          e.preventDefault();
          cancelPointsDraft();
        } else if (e.key === "Backspace") {
          e.preventDefault();
          pointsDraft.removeLastPoint();
        }
      };
      window.addEventListener("keydown", onKey, true);
      return () => window.removeEventListener("keydown", onKey, true);
    }, [pointsDrawEnabled, cancelPointsDraft, commitPointsDraft, pointsDraft]);

    // 可编辑选中框 → 画 8 向句柄(拖拽中跟随 live geom);live 预览框(画框/移动/缩放)。
    const interactionEditable = !readOnly && !isPlaybackActive;
    const handleBox = useMemo<VideoHandleBox | null>(() => {
      if (!interactionEditable || !selectedId) return null;
      const liveGeom =
        drag && (drag.kind === "move" || drag.kind === "resize") && drag.id === selectedId
          ? drag.current
          : null;
      const entry = frameViews.entries.find((e) => e.id === selectedId);
      if (entry) {
        // 点集几何 (polygon/polyline/OBB) 不画 8 向 resize 句柄; 顶点句柄单独渲染。
        if (entry.points || entry.rotatedBbox || entry.keypoints) return null;
        const ann = annotations.find((a) => a.id === entry.id);
        const trackId = ann && isVideoTrack(ann) ? ann.geometry.track_id : null;
        if (trackId && lockedTrackIds.has(trackId)) return null;
        return { id: entry.id, geom: liveGeom ?? entry.geom, color: entry.color };
      }
      const ghost = frameViews.ghost;
      if (ghost && ghost.id === selectedId) {
        // 点集几何 ghost (polygon/polyline) 不画 8 向 resize 句柄 (与 entry.points 分支一致)。
        if (ghost.points) return null;
        if (selectedTrack && lockedTrackIds.has(selectedTrack.geometry.track_id)) return null;
        return { id: ghost.id, geom: liveGeom ?? ghost.geom, color: ghost.color };
      }
      return null;
    }, [
      annotations,
      drag,
      frameViews.entries,
      frameViews.ghost,
      interactionEditable,
      lockedTrackIds,
      selectedId,
      selectedTrack,
    ]);

    const handleObb = useMemo<VideoHandleObb | null>(() => {
      if (!interactionEditable || !selectedId) return null;
      const entry = frameViews.entries.find((item) => item.id === selectedId);
      if (!entry?.rotatedBbox) return null;
      const live =
        drag &&
        (drag.kind === "obbMove" || drag.kind === "obbResize" || drag.kind === "obbRotate") &&
        drag.id === selectedId
          ? drag.current
          : entry.rotatedBbox;
      return { id: selectedId, geometry: live, color: entry.color };
    }, [drag, frameViews.entries, interactionEditable, selectedId]);

    const handleKeypoints = useMemo<VideoHandleKeypoints | null>(() => {
      if (!interactionEditable || !selectedId) return null;
      const entry = frameViews.entries.find((item) => item.id === selectedId);
      if (!entry?.keypoints) return null;
      const points =
        drag?.kind === "keypointNode" && drag.id === selectedId ? drag.current : entry.keypoints;
      return { id: selectedId, points, color: entry.color };
    }, [drag, frameViews.entries, interactionEditable, selectedId]);

    const preview = useMemo<VideoPreviewBox | null>(() => {
      if (!drag) return null;
      // v0.21.23 · smart-box / exemplar 的提示框预览 (紫色, 与图片侧 SAM 候选同色); point 无框可画。
      // v0.21.26 · exemplar 的 mode==="exemplar" 也画框 (此前只画 "bbox", 导致 exemplar 拖框全程无预览、体感像坏了)。
      if (drag.kind === "samProbe") {
        return drag.mode === "bbox" || drag.mode === "exemplar"
          ? { geom: normalizeGeom(drag.start, drag.current), color: SAM_PROBE_STROKE }
          : null;
      }
      if (drag.kind === "draw") {
        const drawColor =
          videoTool === "track" && selectedTrack
            ? getTrackColor(
                selectedTrack.geometry.track_id,
                selectedTrack.class_name,
                trackColorOverrides,
              )
            : classColor(activeClass);
        return { geom: normalizeGeom(drag.start, drag.current), color: drawColor };
      }
      if (drag.kind === "move" || drag.kind === "resize") {
        const c =
          frameViews.entries.find((e) => e.id === drag.id)?.color ??
          (frameViews.ghost?.id === drag.id ? frameViews.ghost.color : null) ??
          classColor(activeClass);
        return { geom: drag.current, color: c };
      }
      return null;
    }, [
      activeClass,
      drag,
      frameViews.entries,
      frameViews.ghost,
      selectedTrack,
      trackColorOverrides,
      videoTool,
    ]);

    const previewObb = useMemo<VideoPreviewObb | null>(() => {
      if (
        !drag ||
        (drag.kind !== "obbMove" && drag.kind !== "obbResize" && drag.kind !== "obbRotate")
      )
        return null;
      const color =
        frameViews.entries.find((entry) => entry.id === drag.id)?.color ?? classColor(activeClass);
      return { geometry: drag.current, color };
    }, [activeClass, drag, frameViews.entries]);

    const creationScopeHint = (() => {
      const at = (tool: VideoTool, sourceFrame: number, point: { x: number; y: number }) => {
        if (sourceFrame !== frameIndex) return null;
        const isTrack =
          tool === "track" ||
          tool === "polygon-track" ||
          tool === "polyline-track" ||
          tool === "mask-track";
        return {
          text: isTrack ? "新建轨迹，从当前源帧开始" : "仅当前源帧",
          frameIndex: sourceFrame,
          left: Math.max(8, Math.min(vp.tx + point.x * size.w * vp.scale, viewportSize.w - 240)),
          top: Math.max(8, Math.min(vp.ty + point.y * size.h * vp.scale - 30, viewportSize.h - 90)),
        };
      };
      if (pendingDrawing && "frameIndex" in pendingDrawing) {
        const { kind, frameIndex: sourceFrame, geom } = pendingDrawing;
        if (kind === "video_mask") {
          const owner = maskCreationOwnerRef.current;
          return owner && !selectedMaskAnnotation ? at(owner.tool, sourceFrame, geom) : null;
        }
        const tool =
          kind === "video_track_bbox"
            ? "track"
            : kind === "video_track_polygon"
              ? "polygon-track"
              : kind === "video_track_polyline"
                ? "polyline-track"
                : "box";
        return at(tool, sourceFrame, geom);
      }
      const pointsOwner = pointsOwnerRef.current;
      const draftPoints = pointsDraft.draft?.points;
      const lastPoint = draftPoints?.[draftPoints.length - 1];
      if (pointsOwner && lastPoint)
        return at(pointsOwner.tool, pointsOwner.frameIndex, { x: lastPoint[0], y: lastPoint[1] });
      const keypointOwner = keypointOwnerRef.current;
      const lastKeypoint = keypointDraft[keypointDraft.length - 1];
      if (keypointOwner && lastKeypoint)
        return at(keypointOwner.tool, keypointOwner.frameIndex, lastKeypoint);
      const boxOwner = interaction.getDrawingDraft();
      if (boxOwner && drag?.kind === "draw" && !interaction.continuingTrack)
        return at(boxOwner.tool, boxOwner.frameIndex, normalizeGeom(drag.start, drag.current));
      const maskOwner = maskCreationOwnerRef.current;
      if (maskOwner && maskEditor?.dirty && maskCursor && !selectedMaskAnnotation)
        return at(maskOwner.tool, maskOwner.frameIndex, maskCursor);
      return null;
    })();

    // v0.16.4 · 右键上下文菜单
    const selectedAnnotation = useMemo(
      () => annotations.find((ann) => ann.id === selectedId) ?? null,
      [annotations, selectedId],
    );
    const contextMenuAnnotation = useMemo(
      () => annotations.find((ann) => ann.id === contextMenuTargetId) ?? null,
      [annotations, contextMenuTargetId],
    );
    const selectedVideoBboxes = useMemo(
      () => annotations.filter((ann) => isVideoBbox(ann) && selectedIds.includes(ann.id)),
      [annotations, selectedIds],
    );
    const trackActions = useVideoTrackActions({
      selectedTrack: selectedManagedTrack,
      frameIndex,
      readOnly,
      hiddenTrackIds: hiddenTrackIds ?? EMPTY_LOCKED,
      lockedTrackIds,
      onUpdate: onUpdate ?? noopUpdate,
      onToggleHiddenTrack,
      onToggleLockedTrack,
      onPropagateTrack,
    });
    const selectedTrackCurrentKeyframe = useMemo(
      () => selectedTrack?.geometry.keyframes.find((kf) => kf.frame_index === frameIndex) ?? null,
      [frameIndex, selectedTrack],
    );
    const selectedManagedCurrentKeyframe = useMemo(
      () =>
        selectedManagedTrack?.geometry.keyframes.find(
          (keyframe) => keyframe.frame_index === frameIndex,
        ) ?? null,
      [frameIndex, selectedManagedTrack],
    );

    // v0.21.12 · 粘轨迹态提示数据: 轨迹显示编号 + 当前帧是否已有关键帧(切「延展 / 同帧新建」措辞)。
    // 仅轨迹工具 + 有选中轨迹时非空 → 显式化「下一次画框归属选中轨迹」这一隐式模型。
    const stickyTrackHint = useMemo(() => {
      if (videoTool !== "track" || !selectedTrack) return null;
      const num = deriveTrackNumber(videoTracks).get(selectedTrack.id);
      const label =
        num != null
          ? `#${num} ${selectedTrack.class_name}`
          : `${shortTrackId(selectedTrack.geometry.track_id)} ${selectedTrack.class_name}`;
      return { label, hasKeyframeAtFrame: selectedTrackCurrentKeyframe != null };
    }, [videoTool, selectedTrack, videoTracks, selectedTrackCurrentKeyframe]);
    const canDeleteSelectedTrackKeyframe = Boolean(
      selectedManagedTrack &&
      selectedManagedCurrentKeyframe &&
      !readOnly &&
      !selectedManagedTrack.is_locked &&
      !trackActions.selectedTrackLocked &&
      selectedManagedTrack.geometry.keyframes.length > 1 &&
      (selectedManagedTrack.geometry.type !== "video_track_mask" ||
        (maskKeyframeActions && !maskKeyframeActions.busy)),
    );
    const deleteSelectedTrackKeyframe = useCallback(() => {
      if (
        !selectedManagedTrack ||
        !selectedManagedCurrentKeyframe ||
        !canDeleteSelectedTrackKeyframe
      )
        return false;
      if (selectedManagedTrack.geometry.type === "video_track_mask") {
        maskKeyframeActions?.deleteCurrentKeyframe(selectedManagedTrack);
      } else {
        (onUpdate ?? noopUpdate)(selectedManagedTrack, {
          ...selectedManagedTrack.geometry,
          keyframes: sortedKeyframes(selectedManagedTrack.geometry).filter(
            (keyframe) => keyframe.frame_index !== frameIndex,
          ),
        });
      }
      return true;
    }, [
      canDeleteSelectedTrackKeyframe,
      frameIndex,
      maskKeyframeActions,
      noopUpdate,
      onUpdate,
      selectedManagedCurrentKeyframe,
      selectedManagedTrack,
    ]);

    const contextMenuItems = useMemo<DropdownItem[]>(
      () =>
        buildVideoContextMenuItems({
          contextMenuAnnotation,
          selectedAnnotation,
          contextMenuTargetId,
          selectedVideoBboxes,
          readOnly,
          frameIndex,
          trackActions,
          canDeleteSelectedTrackKeyframe,
          deleteSelectedTrackKeyframe,
          onChangeUserBoxClass,
          onComposeTracks,
          onConvertToBboxes,
          onDelete,
          onPropagateTrack,
          onToggleHiddenTrack,
          onToggleLockedTrack,
          hiddenTrackIds,
          lockedTrackIds,
          maskKeyframeActions,
        }),
      [
        canDeleteSelectedTrackKeyframe,
        contextMenuAnnotation,
        contextMenuTargetId,
        deleteSelectedTrackKeyframe,
        frameIndex,
        onChangeUserBoxClass,
        onComposeTracks,
        onConvertToBboxes,
        onDelete,
        onPropagateTrack,
        maskKeyframeActions,
        onToggleHiddenTrack,
        onToggleLockedTrack,
        readOnly,
        selectedAnnotation,
        selectedVideoBboxes,
        trackActions,
        hiddenTrackIds,
        lockedTrackIds,
      ],
    );

    const handleContextMenu = useCallback(
      (evt: ReactMouseEvent<HTMLDivElement>) => {
        evt.preventDefault();
        if (issuePinDropArmed || issueNavigationPending) return;
        if (keypointDrawEnabled) return;
        const down = rightDownRef.current;
        rightDownRef.current = null;
        closeContextMenu();
        if (
          down &&
          Math.hypot(evt.clientX - down.x, evt.clientY - down.y) >= CONTEXT_MENU_DRAG_THRESHOLD_PX
        )
          return;
        if (readOnly) return;
        const rect = containerRef.current?.getBoundingClientRect();
        if (!rect) return;
        const point = clientToVideoNorm(evt.clientX, evt.clientY, rect, vpRef.current, size);
        if (!point) return;
        const pickables = frameViews.ghost
          ? [...frameViews.entries, frameViews.ghost]
          : frameViews.entries;
        const hit =
          pickTopVideoMaskAt(committedMaskRecords, point) ??
          pickTopVideoEntryAt(pickables, point, { size });
        if (!hit) return;
        const hitAnn = annotations.find((a) => a.id === hit.id);
        // v0.21.26 · 命中的不是「可建菜单」的视频几何 → 只选中, 不弹空菜单
        // (此前对 polygon/polyline 无条件 openAt 却给空 [], 表现为「弹一个没条目的菜单」)。
        if (!hitAnn || (!isAnyVideoSingleFrame(hitAnn) && !isAnyVideoTrack(hitAnn))) {
          if (hitAnn) onSelect?.(hit.id);
          return;
        }
        setContextMenuTargetId(hit.id);
        if (isVideoBbox(hitAnn) && selectedIds.includes(hit.id) && selectedVideoBboxes.length > 1) {
          contextMenu.openAt(evt.clientX, evt.clientY);
          return;
        }
        onSelect?.(hit.id);
        contextMenu.openAt(evt.clientX, evt.clientY);
      },
      [
        annotations,
        closeContextMenu,
        committedMaskRecords,
        contextMenu,
        frameViews.entries,
        frameViews.ghost,
        issuePinDropArmed,
        issueNavigationPending,
        keypointDrawEnabled,
        onSelect,
        readOnly,
        selectedIds,
        selectedVideoBboxes.length,
        size,
        vpRef,
      ],
    );

    const fitViewport = useCallback(() => {
      runViewCommand(() => fit(viewportSize.w, viewportSize.h, size.w, size.h));
    }, [fit, runViewCommand, size.h, size.w, viewportSize.h, viewportSize.w]);

    // 实际尺寸(100% 缩放并居中,对齐旧 SVG 栈 setActualSize)。
    const setActualSize = useCallback(() => {
      if (!viewportSize.w || !viewportSize.h) {
        setVp({ scale: 1, tx: 0, ty: 0 });
        return;
      }
      setVp({
        scale: 1,
        tx: (viewportSize.w - size.w) / 2,
        ty: (viewportSize.h - size.h) / 2,
      });
    }, [setVp, size.h, size.w, viewportSize.h, viewportSize.w]);

    const issueView = useVideoIssueView({
      sourceKey,
      taskId: manifest?.task_id ?? null,
      frameIndex,
      selectedId,
      viewport: vp,
      setViewport,
      containerSize: viewportSize,
      mediaSize: size,
      hasRealMediaSize,
      autoFitOnResize,
      focusSelectionEnabled,
      focusObject,
      timelineRef: timelineWindowControlsRef,
    });
    cancelIssueRestoreRef.current = issueView.cancelIssueRestore;
    const issueViewport = captureVideoIssueViewport(vp, viewportSize, size);

    // Mask 直接滚轮调半径；ctrl/⌘+滚轮围绕光标缩放。
    useEffect(() => {
      const onWheel = (e: WheelEvent) => {
        if (isWorkbenchInteractionBlocked(e)) return;
        const el = containerRef.current;
        if (!el) return;
        // 播放组件(时间轴/控制条/概览导航条)叠在 stage 容器内, 它有独立滚轮行为。
        if (
          e.target instanceof Element &&
          e.target.closest('[data-testid="video-playback-overlay"]')
        )
          return;
        const rect = el.getBoundingClientRect();
        if (
          e.clientX < rect.left ||
          e.clientX > rect.right ||
          e.clientY < rect.top ||
          e.clientY > rect.bottom
        )
          return;
        const point = clientToVideoNorm(e.clientX, e.clientY, rect, vpRef.current, size);
        const inFrame = !!point && point.x >= 0 && point.x <= 1 && point.y >= 0 && point.y <= 1;
        if (
          !maskCompareActive &&
          maskToolActive &&
          maskEditor &&
          !(e.ctrlKey || e.metaKey) &&
          inFrame &&
          Math.abs(e.deltaY) > Math.abs(e.deltaX)
        ) {
          e.preventDefault();
          maskEditor.setRadius(maskEditor.radius + (e.deltaY < 0 ? 2 : -2));
          return;
        }
        if (!(e.ctrlKey || e.metaKey)) return;
        e.preventDefault();
        const factor = e.deltaY < 0 ? 1.1 : 1 / 1.1;
        zoomAt(e.clientX - rect.left, e.clientY - rect.top, vpRef.current.scale * factor);
      };
      window.addEventListener("wheel", onWheel, { capture: true, passive: false });
      return () => window.removeEventListener("wheel", onWheel, { capture: true });
    }, [maskCompareActive, maskEditor, maskToolActive, size, vpRef, zoomAt]);

    // 本地视口/导航快捷键(对齐旧 SVG 栈 VideoStage 本地 keydown):
    // Shift+F = fit、0 = 实际尺寸;Home/End = 选中轨迹首/末出现帧。
    useEffect(() => {
      const isInputFocused = (el: EventTarget | null) =>
        el instanceof HTMLElement &&
        (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable);
      const onKeyDown = (e: KeyboardEvent) => {
        if (isWorkbenchInteractionBlocked(e)) return;
        if (isInputFocused(e.target)) return;
        if (
          (e.key === "f" || e.key === "F") &&
          e.shiftKey &&
          !e.ctrlKey &&
          !e.metaKey &&
          !e.altKey
        ) {
          e.preventDefault();
          fitViewport();
          return;
        }
        // v0.24 · 0 让位给第十个类别键；实际尺寸改为 Shift+物理 Digit0
        // （Shift+0 的产出字符随布局变化，故按 e.code 判定，与 Backquote 反向键同一先例）。
        if (e.code === "Digit0" && e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey) {
          e.preventDefault();
          e.stopImmediatePropagation();
          setActualSize();
          return;
        }
        if (e.ctrlKey || e.metaKey || e.altKey) return;
        const track = selectedManagedTrack;
        if (!track) return;
        const frames =
          track.geometry.type === "video_track_mask"
            ? visibleKeyframesForTimeline(track.geometry).map((keyframe) => keyframe.frame_index)
            : track.geometry.keyframes.map((keyframe) => keyframe.frame_index);
        if (e.key === "Home") {
          const frame = frames.length > 0 ? Math.min(...frames) : null;
          if (frame == null) return;
          e.preventDefault();
          seekToFrame(frame, { recordHistory: true });
          return;
        }
        if (e.key === "End") {
          const frame = frames.length > 0 ? Math.max(...frames) : null;
          if (frame == null) return;
          e.preventDefault();
          seekToFrame(frame, { recordHistory: true });
        }
      };
      window.addEventListener("keydown", onKeyDown, true);
      return () => window.removeEventListener("keydown", onKeyDown, true);
    }, [fitViewport, seekToFrame, selectedManagedTrack, setActualSize]);

    const seekManagedKeyframe = useCallback(
      (dir: -1 | 1, options?: { recordHistory?: boolean }) => {
        if (!selectedContextTrack) return;
        const next = nextVisibleKeyframeFrame(selectedContextTrack.geometry, frameIndex, dir);
        if (next != null) seekToFrame(next, options);
      },
      [frameIndex, seekToFrame, selectedContextTrack],
    );

    // useImperativeHandle 委托给 controller.controls,再覆盖 deleteSelectedTrackKeyframe。
    // (captureCurrentFrameJpeg 由 controller.controls 提供, 见 useVideoPlaybackController。)
    // v0.21.23 · 归一化 → 屏幕坐标; 类选择器 popover 的 fixed anchor 用它 (与 onPendingDraw 同式)。
    const normToClient = useCallback(
      (pt: { x: number; y: number }) => {
        const rect = containerRef.current?.getBoundingClientRect();
        if (!rect) return null;
        const p = videoNormToClient(pt, rect, vpRef.current, size);
        return { left: p.x, top: p.y };
      },
      [size, vpRef],
    );

    const focusRegion = useCallback(
      (bbox: { x0: number; y0: number; x1: number; y1: number }) => {
        setVp((current) =>
          fitNormalizedRegion(
            current,
            bbox,
            { width: size.w, height: size.h },
            { width: viewportSize.w, height: viewportSize.h },
          ),
        );
      },
      [setVp, size.h, size.w, viewportSize.h, viewportSize.w],
    );

    const toggleManagedTrackOutside = useCallback(() => {
      if (selectedManagedTrack?.geometry.type === "video_track_mask") {
        if (
          readOnly ||
          selectedManagedTrack.is_locked ||
          trackActions.selectedTrackLocked ||
          maskKeyframeActions?.busy
        )
          return;
        maskKeyframeActions?.toggleCurrentOutside(selectedManagedTrack);
        return;
      }
      trackActions.toggleSelectedTrackOutside();
    }, [maskKeyframeActions, readOnly, selectedManagedTrack, trackActions]);

    const toggleManagedTrackOccluded = useCallback(() => {
      if (selectedManagedTrack?.geometry.type === "video_track_mask") return;
      trackActions.toggleSelectedTrackOccluded();
    }, [selectedManagedTrack, trackActions]);

    const contextTrackLocked = Boolean(
      selectedContextTrack &&
      (selectedContextTrack.is_locked ||
        lockedTrackIds.has(selectedContextTrack.geometry.track_id)),
    );
    const contextWritesBlocked = Boolean(
      readOnly ||
      contextTrackLocked ||
      isPlaybackActive ||
      maskCompareActive ||
      pendingDrawing ||
      maskEditor?.active ||
      (segmentRange &&
        (frameIndex < segmentRange.workStartFrame || frameIndex > segmentRange.workEndFrame)),
    );
    const materializeContextKeyframe = useCallback(() => {
      if (
        !selectedContextTrack ||
        !onUpdate ||
        contextWritesBlocked ||
        trackContext?.state !== "interpolated"
      )
        return;
      const geometry = selectedContextTrack.geometry;
      if (geometry.type === "video_track_bbox") {
        const resolved = resolveTrackAtFrame(geometry, frameIndex);
        if (resolved)
          onUpdate(
            selectedContextTrack,
            upsertKeyframe(geometry, frameIndex, resolved.geom, { source: "manual" }),
          );
      } else if (geometry.type === "video_track_polygon") {
        const resolved = resolveVideoPolygonTrackAtFrame(geometry, frameIndex);
        if (resolved)
          onUpdate(
            selectedContextTrack,
            upsertPointsKeyframe(geometry, frameIndex, resolved.points),
          );
      } else if (geometry.type === "video_track_polyline") {
        const resolved = resolveVideoPolylineTrackAtFrame(geometry, frameIndex);
        if (resolved)
          onUpdate(
            selectedContextTrack,
            upsertPointsKeyframe(geometry, frameIndex, resolved.points),
          );
      }
    }, [contextWritesBlocked, frameIndex, onUpdate, selectedContextTrack, trackContext?.state]);
    const contextActions = useMemo<NonNullable<VideoTrackContextBarProps["actions"]>>(() => {
      if (!selectedContextTrack || !trackContext || contextWritesBlocked) return [];
      const actions: Array<NonNullable<VideoTrackContextBarProps["actions"]>[number]> = [];
      const geometry = selectedContextTrack.geometry;
      if (trackContext.state === "interpolated" && onUpdate) {
        actions.push({ id: "materialize", label: "补关键帧", onClick: materializeContextKeyframe });
      }
      if (geometry.type === "video_track_bbox" && onUpdate) {
        const canRestore =
          trackContext.state === "outside" &&
          visibleKeyframesForTimeline(removeOutsideFrame(geometry, frameIndex)).length > 0;
        if (
          trackContext.state === "keyframe" ||
          trackContext.state === "interpolated" ||
          canRestore
        ) {
          actions.push({
            id: "outside",
            label: trackContext.state === "outside" ? "恢复显示" : "标记 outside",
            shortcut: "O",
            onClick: toggleManagedTrackOutside,
          });
        }
      } else if (
        geometry.type === "video_track_mask" &&
        maskKeyframeActions &&
        !maskKeyframeActions.busy
      ) {
        const canRestore =
          trackContext.state === "outside" &&
          resolveVideoMaskTrackAtFrame(removeOutsideFrame(geometry, frameIndex), frameIndex) !==
            null &&
          geometry.outside?.some(
            (range) =>
              range.source !== "prediction" && range.from <= frameIndex && frameIndex <= range.to,
          );
        if (trackContext.state === "keyframe" || trackContext.state === "held" || canRestore) {
          actions.push({
            id: "outside",
            label: trackContext.state === "outside" ? "恢复显示" : "标记 outside",
            shortcut: "O",
            onClick: toggleManagedTrackOutside,
          });
        }
      }
      if (
        selectedManagedTrack &&
        onPropagateTrack &&
        trackContext.state !== "outside" &&
        trackContext.state !== "unavailable"
      ) {
        actions.push({
          id: "propagate",
          label: "延展轨迹",
          onClick: trackActions.propagateSelectedTrack,
        });
      }
      return actions;
    }, [
      contextWritesBlocked,
      frameIndex,
      maskKeyframeActions,
      materializeContextKeyframe,
      onPropagateTrack,
      onUpdate,
      selectedContextTrack,
      selectedManagedTrack,
      toggleManagedTrackOutside,
      trackActions,
      trackContext,
    ]);
    const seekContextFrame = useCallback(
      (frame: number) => {
        pausePlayback();
        seekToFrame(frame, { recordHistory: true });
      },
      [pausePlayback, seekToFrame],
    );

    useImperativeHandle(
      ref,
      () => ({
        ...controls,
        subscribeIssueNavigationInterrupt,
        captureIssueView: issueView.captureIssueView,
        waitForIssueViewReady: issueView.waitForIssueViewReady,
        beginIssueRestore: issueView.beginIssueRestore,
        getDrawingDraft,
        discardDrawingDraft,
        seekToKeyframe: seekManagedKeyframe,
        toggleSelectedTrackOutside: toggleManagedTrackOutside,
        toggleSelectedTrackOccluded: toggleManagedTrackOccluded,
        toggleSelectedTrackHidden: trackActions.toggleSelectedTrackHidden,
        toggleSelectedTrackLocked: trackActions.toggleSelectedTrackLocked,
        propagateSelectedTrack: trackActions.propagateSelectedTrack,
        normToClient,
        deleteSelectedTrackKeyframe,
        cycleInCategory,
        stepCategory,
        focusObject: (id) => runViewCommand(() => focusObject(id)),
        focusRegion,
      }),
      [
        controls,
        cycleInCategory,
        deleteSelectedTrackKeyframe,
        discardDrawingDraft,
        focusObject,
        focusRegion,
        getDrawingDraft,
        issueView.beginIssueRestore,
        issueView.captureIssueView,
        issueView.waitForIssueViewReady,
        normToClient,
        runViewCommand,
        seekManagedKeyframe,
        stepCategory,
        subscribeIssueNavigationInterrupt,
        toggleManagedTrackOccluded,
        toggleManagedTrackOutside,
        trackActions,
      ],
    );

    const beginPan = useCallback(
      (evt: ReactPointerEvent<HTMLDivElement>) => {
        if (evt.button === 2 && keypointDrawEnabled) return;
        const isSpacePan = evt.button === 0 && spacePan;
        const isPan = evt.button === 2 || isSpacePan;
        if (evt.button === 2) rightDownRef.current = { x: evt.clientX, y: evt.clientY };
        if (!isPan) return;
        evt.preventDefault();
        if (isSpacePan) onSpacePanDragStart?.();
        panRef.current = { x: evt.clientX, y: evt.clientY };
        evt.currentTarget.setPointerCapture?.(evt.pointerId);
        pausePlayback();
        setPanning(true);
      },
      [keypointDrawEnabled, onSpacePanDragStart, pausePlayback, spacePan],
    );

    const onPointerMove = useCallback(
      (evt: ReactPointerEvent<HTMLDivElement>) => {
        // 指针在画布上移动即唤出播放浮层(对齐旧 SVG 栈);离开后由 onPointerLeave 计时收起。
        showPlaybackOverlay();
        // 光标归一化坐标上报(状态栏读出),无论是否在平移;越界(letterbox 区)上报 null。
        if (onCursorMove || pointsDrawEnabled || maskToolActive) {
          const rect = containerRef.current?.getBoundingClientRect();
          const pt = rect
            ? clientToVideoNorm(evt.clientX, evt.clientY, rect, vpRef.current, size)
            : null;
          const inFrame = pt && pt.x >= 0 && pt.x <= 1 && pt.y >= 0 && pt.y <= 1 ? pt : null;
          onCursorMove?.(inFrame);
          // 橡皮筋预览: 仅绘制工具激活时跟踪, 用于「上一点 → 光标」预览段与首点吸附高亮。
          if (pointsDrawEnabled) setPointsCursor(inFrame);
          if (maskToolActive && !maskCompareActive) setMaskCursor(inFrame);
        }
        const maskStroke = maskStrokeRef.current;
        const maskLasso = maskLassoRef.current;
        const selectedTrackId = selectedManagedTrack?.geometry.track_id;
        const maskEditable =
          !!maskEditor &&
          canEditMask({
            taskReadOnly: !!readOnly || isPlaybackActive || maskCompareActive,
            annotationLocked: !!selectedMaskAnnotation?.is_locked,
            trackLocked: !!selectedTrackId && lockedTrackIds.has(selectedTrackId),
            segmentLocked: false,
            editorPhase:
              maskEditor.phase ??
              (maskEditor.dirty ? "dirty" : maskEditor.active ? "ready" : "idle"),
          });
        if (maskLasso && maskEditor && maskEditable) {
          const point = pointFromClientEvt(evt.clientX, evt.clientY);
          if (point) {
            const next: [number, number] = [point.x * size.w, point.y * size.h];
            const previous = maskLasso[maskLasso.length - 1];
            if (!previous || Math.hypot(next[0] - previous[0], next[1] - previous[1]) >= 1) {
              maskLasso.push(next);
              setMaskLassoPoints([...maskLasso]);
            }
          }
        } else if (maskStroke && maskEditor && maskEditable) {
          const point = pointFromClientEvt(evt.clientX, evt.clientY);
          if (point) {
            const x = point.x * size.w;
            const y = point.y * size.h;
            const dx = x - maskStroke.lastX;
            const dy = y - maskStroke.lastY;
            const distance = Math.hypot(dx, dy);
            const count = Math.max(1, Math.floor(distance / Math.max(1, maskEditor.radius / 2)));
            for (let index = 1; index <= count; index += 1) {
              const ratio = index / count;
              maskEditor.paintAt(maskStroke.lastX + dx * ratio, maskStroke.lastY + dy * ratio);
            }
            maskStrokeRef.current = { lastX: x, lastY: y };
          }
        }
        const start = panRef.current;
        if (!start) return;
        const dx = evt.clientX - start.x;
        const dy = evt.clientY - start.y;
        panRef.current = { x: evt.clientX, y: evt.clientY };
        setVp((cur) => ({ ...cur, tx: cur.tx + dx, ty: cur.ty + dy }));
      },
      [
        isPlaybackActive,
        lockedTrackIds,
        maskCompareActive,
        maskEditor,
        onCursorMove,
        pointFromClientEvt,
        pointsDrawEnabled,
        readOnly,
        selectedMaskAnnotation,
        selectedManagedTrack,
        setVp,
        showPlaybackOverlay,
        size,
        maskToolActive,
        vpRef,
      ],
    );

    const endPan = useCallback(() => {
      if (maskStrokeRef.current) {
        maskStrokeRef.current = null;
        maskEditor?.endStroke();
      }
      const lasso = maskLassoRef.current;
      if (lasso) {
        maskLassoRef.current = null;
        setMaskLassoPoints([]);
        if (maskEditor && !maskCompareActive && lasso.length >= 3) {
          void maskEditor.runOperation(maskEditor.tool, {
            type: "polygon",
            points: lasso,
            value: maskEditor.tool === "lasso_subtract" ? 0 : 255,
          });
        }
      }
      panRef.current = null;
      setPanning(false);
    }, [maskCompareActive, maskEditor]);

    useEffect(() => {
      if (!maskCompareActive) return;
      if (maskStrokeRef.current) maskEditor?.endStroke();
      maskStrokeRef.current = null;
      maskLassoRef.current = null;
      setMaskLassoPoints([]);
      setMaskCursor(null);
    }, [maskCompareActive, maskEditor]);

    const onPointerLeave = useCallback(() => {
      onCursorMove?.(null);
      setPointsCursor(null);
      setMaskCursor(null);
      // 指针离开画布 2s 后收起播放浮层,避免其永久遮挡画布。
      schedulePlaybackOverlayHide();
    }, [onCursorMove, schedulePlaybackOverlayHide]);

    // 工具模式光标反馈:平移中 grabbing;按住 Space 可抓;创建工具十字,选择工具普通光标。
    // Konva 容器命中 resize 句柄时由交互层覆盖 stage.container() cursor,未命中则继承此处。
    const creationEnabled =
      (videoTool === "box" || videoTool === "track" || videoTool === "rotated-box") &&
      (!isVideoToolEnabled || isVideoToolEnabled(videoTool));
    // v0.21.23 · 交互式 SAM 工具同样用十字光标 (提示落点即分割位置)。
    // v0.21.26 · 复用交互层同一谓词 isSamProbeTool (含 exemplar / magic-box), 修此前漏登记这两个
    // 工具 → 选中后无十字光标、体感像未进入工具的问题。
    const samProbeTool = isSamProbeTool(videoTool);
    const cursorClass = issueNavigationPending
      ? "cursor-wait"
      : panning
        ? styles.rootPanning
        : spacePan
          ? styles.toolGrab
          : issuePinDropArmed ||
              creationEnabled ||
              pointsDrawEnabled ||
              keypointDrawEnabled ||
              samProbeTool
            ? styles.toolCrosshair
            : "";

    const videoMinimapVisible = viewportSize.w > 0 && viewportSize.h > 0;

    if (isLoading) {
      return (
        <div className={styles.loadingState}>
          <Icon name="loader2" className="spin" /> 加载视频信息...
        </div>
      );
    }
    if (error || !manifest) {
      return (
        <div className={styles.errorState}>
          <Icon name="warning" size={28} /> 视频 manifest 不可用
        </div>
      );
    }

    const annotationCommentBadgesInteractive =
      videoTool === "select" &&
      !pendingDrawing &&
      !isPlaybackActive &&
      !drag &&
      !pointsDraft.draft &&
      keypointDraft.length === 0 &&
      !maskToolActive &&
      !maskCompareActive &&
      !issuePinDropArmed &&
      !issueNavigationPending;

    const canvas = (
      <div
        ref={setContainerNode}
        data-testid="video-konva-stage"
        data-video-frame-source={
          frameSource === "webcodecs"
            ? "webcodecs"
            : frameSource === "video-bitmap"
              ? "native-bitmap"
              : "video"
        }
        data-video-precise-state={preciseSourceState}
        data-video-frame-index={frameIndex}
        data-video-draft-point-count={pointsDraft.draft?.points.length ?? 0}
        data-video-painted-frame-index={precisePaintedFrameIndex ?? -1}
        data-video-view-ready={issueView.viewReady ? "true" : "false"}
        data-video-view-center-x={issueViewport?.center_x}
        data-video-view-center-y={issueViewport?.center_y}
        data-video-view-zoom={issueViewport?.zoom}
        data-media-x={vp.tx}
        data-media-y={vp.ty}
        data-media-width={size.w * vp.scale}
        data-media-height={size.h * vp.scale}
        data-active-class={activeClass}
        className={`${styles.root} ${cursorClass}`}
        onContextMenu={handleContextMenu}
        onPointerDown={issuePinDropArmed || issueNavigationPending ? undefined : beginPan}
        onPointerMove={onPointerMove}
        onPointerUp={endPan}
        onPointerCancel={endPan}
        onPointerLeave={onPointerLeave}
        onDoubleClick={isPointsDrawTool || keypointDrawEnabled ? undefined : fitViewport}
      >
        <video
          ref={setVideoNode}
          data-testid="video-konva-source"
          src={manifest.video_url}
          poster={manifest.poster_url ?? undefined}
          // v0.21.4 · CORS-clean 加载, 否则 createImageBitmap(video) → canvas 会被跨域 MinIO 视频
          // 污染, 单题 AI 抓帧导出 JPEG 抛 SecurityError。storage 已对 presigned GET 返回 ACAO。
          crossOrigin="anonymous"
          playsInline
          className={styles.hiddenVideo}
        />
        <div className={styles.konvaHost}>
          <Stage
            ref={stageRef}
            width={viewportSize.w || 1}
            height={viewportSize.h || 1}
            x={vp.tx}
            y={vp.ty}
            scaleX={vp.scale}
            scaleY={vp.scale}
            onPointerDown={handleStagePointerDown}
            onDblClick={isPointsDrawTool ? commitPointsDraft : undefined}
          >
            <VideoKonvaMediaLayer
              videoEl={videoEl}
              bitmap={displayBitmap?.bitmap ?? null}
              frameIndex={frameIndex}
              preciseFrameIndex={frameSource === "webcodecs" ? frameIndex : null}
              onPreciseFramePainted={markPreciseFramePainted}
              framePresentation={framePresentation}
              onFramePresented={markFramePresented}
              size={size}
              viewport={viewportSize}
              isPlaybackActive={isPlaybackActive}
            />
            <VideoKonvaTracksLayer
              entries={frameViews.entries}
              previews={frameViews.previews}
              ghost={frameViews.ghost}
              carryOverGhosts={frameViews.carryOverGhosts}
              size={size}
              scale={vp.scale}
              visual={visual}
              keypointSchema={keypointSchema}
            />
            <VideoKonvaMaskLayer
              records={displayedMaskRecords}
              size={size}
              scale={vp.scale}
              visual={visual}
            />
            <MaskCompareTileLayer store={maskCompareStore} viewport={maskCompareViewport} />
            {!maskCompareActive &&
              maskToolActive &&
              maskEditor?.active &&
              maskEditor.buffer &&
              !maskOperationPreviewBuffer && (
                <MaskOverlayLayer
                  buffer={maskEditor.buffer}
                  revision={maskEditor.revision}
                  imgW={size.w}
                  imgH={size.h}
                  opacity={visual.fillOpacitySelected}
                  color={maskEditorColor}
                  visible
                />
              )}
            {!maskCompareActive && maskToolActive && maskOperationPreviewBuffer && (
              <MaskOverlayLayer
                buffer={maskOperationPreviewBuffer}
                revision={maskEditor?.operationPreview?.id ?? 0}
                imgW={size.w}
                imgH={size.h}
                opacity={visual.fillOpacitySelected}
                color={MASK_OPERATION_PREVIEW_COLOR}
                visible
              />
            )}
            {!maskCompareActive && maskToolActive && maskInstancePreviewBuffer && (
              <MaskOverlayLayer
                buffer={maskInstancePreviewBuffer}
                revision={maskEditor?.instanceOperationPreview?.id ?? 0}
                imgW={size.w}
                imgH={size.h}
                opacity={visual.fillOpacitySelected}
                color={MASK_INSTANCE_PREVIEW_COLOR}
                visible
              />
            )}
            {!maskCompareActive && maskToolActive && maskLassoPoints.length > 1 && (
              <Layer name="mask-lasso-preview" listening={false}>
                <Line
                  points={maskLassoPoints.flatMap(([x, y]) => [x, y])}
                  stroke={maskEditor?.tool === "lasso_subtract" ? "#f97316" : "#22c55e"}
                  strokeWidth={2 / vp.scale}
                  dash={[5 / vp.scale, 3 / vp.scale]}
                  lineCap="round"
                  lineJoin="round"
                  listening={false}
                />
              </Layer>
            )}
            <VideoKonvaOverlayLayer
              pendingDraft={pendingDraft}
              labels={[...frameViews.labels, ...maskLabels]}
              size={size}
              scale={vp.scale}
              visual={visual}
            />
            {/* v0.21.4 · AI 候选层(当前帧 video_bbox); select 工具下可点选。 */}
            <VideoKonvaAiLayer
              boxes={frameAiBoxes}
              size={size}
              scale={vp.scale}
              selectedId={selectedId}
              listening={videoTool === "select" && !readOnly}
              visual={visual}
              onSelect={(id) => onSelect?.(id)}
            />
            <VideoKonvaInteractionLayer
              size={size}
              scale={vp.scale}
              drag={drag}
              handleBox={handleBox}
              preview={preview}
              handleObb={handleObb}
              previewObb={previewObb}
              handleKeypoints={handleKeypoints}
              onResizeHandlePointerDown={interaction.onResizeHandlePointerDown}
              onObbResizePointerDown={interaction.onObbResizePointerDown}
              onObbRotatePointerDown={interaction.onObbRotatePointerDown}
              onKeypointPointerDown={interaction.onKeypointPointerDown}
            />
            {!maskCompareActive &&
              maskToolActive &&
              maskCursor &&
              maskEditor &&
              (maskEditor.tool === "brush" || maskEditor.tool === "erase") && (
                <Layer name="video-mask-cursor" listening={false}>
                  <Circle
                    x={maskCursor.x * size.w}
                    y={maskCursor.y * size.h}
                    radius={maskEditor.radius}
                    stroke={maskEditor.mode === "erase" ? "#64748b" : "#dc2626"}
                    strokeWidth={1.5 / vp.scale}
                    dash={[4 / vp.scale, 3 / vp.scale]}
                    listening={false}
                  />
                </Layer>
              )}
            {pointsDraft.draft &&
              pointsDraft.draft.points.length > 0 &&
              (() => {
                const hex = colorToHex(classColor(activeClass));
                const isPolyline = !pointsDraft.draft.closed;
                const ps = pointsDraft.draft.points;
                const flat = ps.flatMap(([px, py]) => [px * size.w, py * size.h]);
                // 橡皮筋: 追加「最后一点 → 当前光标」预览段。
                if (pointsCursor) flat.push(pointsCursor.x * size.w, pointsCursor.y * size.h);
                // 首点吸附高亮(仅 polygon, ≥3 点且光标进入闭合半径)。
                const canClose =
                  !isPolyline &&
                  ps.length >= 3 &&
                  !!pointsCursor &&
                  Math.hypot(pointsCursor.x - ps[0][0], pointsCursor.y - ps[0][1]) <=
                    CLOSE_DISTANCE;
                return (
                  <Layer name="points-draft" listening={false}>
                    <Line
                      points={flat}
                      closed={!isPolyline}
                      stroke={hex}
                      strokeWidth={1.5 / vp.scale}
                      dash={[6 / vp.scale, 4 / vp.scale]}
                      lineCap="round"
                      lineJoin="round"
                      fill={isPolyline ? undefined : hexToRgba(hex, visual.fillOpacity)}
                      listening={false}
                    />
                    {ps.map(([px, py], i) => (
                      <Circle
                        key={i}
                        x={px * size.w}
                        y={py * size.h}
                        radius={(i === 0 ? 4.5 : 3) / vp.scale}
                        fill={i === 0 && canClose ? hex : "white"}
                        stroke={hex}
                        strokeWidth={1.5 / vp.scale}
                        listening={false}
                      />
                    ))}
                  </Layer>
                );
              })()}
            {pendingPointsDraft &&
              (() => {
                const hex = colorToHex(classColor(activeClass));
                return (
                  <Layer name="pending-points-draft" listening={false}>
                    <Line
                      points={pendingPointsDraft.points.flatMap(([px, py]) => [
                        px * size.w,
                        py * size.h,
                      ])}
                      closed={pendingPointsDraft.closed}
                      stroke={hex}
                      strokeWidth={2 / vp.scale}
                      dash={[6 / vp.scale, 4 / vp.scale]}
                      lineCap="round"
                      lineJoin="round"
                      fill={
                        pendingPointsDraft.closed ? hexToRgba(hex, visual.fillOpacity) : undefined
                      }
                      listening={false}
                    />
                    {pendingPointsDraft.points.map(([px, py], index) => (
                      <Circle
                        key={`pending-video-point-${index}`}
                        x={px * size.w}
                        y={py * size.h}
                        radius={3 / vp.scale}
                        fill="white"
                        stroke={hex}
                        strokeWidth={1.5 / vp.scale}
                        listening={false}
                      />
                    ))}
                  </Layer>
                );
              })()}
            {(keypointDraft.length > 0 || pendingKeypoints) &&
              (() => {
                const points = pendingKeypoints ?? keypointDraft;
                const pending = !!pendingKeypoints;
                return (
                  <Layer
                    name={pending ? "pending-keypoint-draft" : "keypoint-draft"}
                    listening={false}
                  >
                    {(keypointSchema?.edges ?? []).map(([from, to], index) => {
                      const a = points[from];
                      const b = points[to];
                      if (!a || !b || a.v === 0 || b.v === 0) return null;
                      return (
                        <Line
                          key={`keypoint-draft-edge-${index}`}
                          points={[a.x * size.w, a.y * size.h, b.x * size.w, b.y * size.h]}
                          stroke={colorToHex(classColor(activeClass))}
                          strokeWidth={1.5 / vp.scale}
                          dash={pending ? [6 / vp.scale, 4 / vp.scale] : undefined}
                          opacity={a.v === 1 || b.v === 1 ? 0.6 : 1}
                        />
                      );
                    })}
                    {points.map((point, index) => {
                      const color = keypointColorByIndex(index, keypointSchema);
                      return (
                        <Circle
                          key={`keypoint-draft-${index}`}
                          x={point.x * size.w}
                          y={point.y * size.h}
                          radius={(point.v === 0 ? 2.5 : 4) / vp.scale}
                          fill={
                            point.v === 2 ? color : point.v === 1 ? "white" : hexToRgba(color, 0.25)
                          }
                          stroke={point.v === 0 ? undefined : color}
                          strokeWidth={1.5 / vp.scale}
                        />
                      );
                    })}
                  </Layer>
                );
              })()}
            {/* polygon/polyline (单帧 + 轨迹) 选中 → 顶点句柄 (拖顶点改形); 命中框内拖拽整体平移由 Stage pickTop 处理。
              轨迹在插值帧编辑会物化关键帧; OBB 暂只读 (entry.points 存在但非可编辑点集几何)。 */}
            {!readOnly &&
              !isPlaybackActive &&
              selectedId &&
              (() => {
                const entry = frameViews.entries.find((e) => e.id === selectedId);
                if (!entry?.points) return null;
                const ann = annotations.find((a) => a.id === selectedId);
                const editablePoly =
                  ann &&
                  (isVideoPolygon(ann) ||
                    isVideoPolyline(ann) ||
                    isVideoPolygonTrack(ann) ||
                    isVideoPolylineTrack(ann));
                if (!ann || !editablePoly) return null; // OBB 暂只读
                const editing =
                  drag &&
                  (drag.kind === "polyVertex" || drag.kind === "polyMove") &&
                  drag.id === selectedId;
                const livePoints = editing ? drag.current : entry.points;
                const hex = colorToHex(entry.color);
                const open = isVideoPolyline(ann) || isVideoPolylineTrack(ann);
                const flat = livePoints.flatMap(([px, py]) => [px * size.w, py * size.h]);
                return (
                  <Layer name="poly-edit">
                    {editing && (
                      <Line
                        points={flat}
                        closed={!open}
                        stroke={hex}
                        strokeWidth={1.5 / vp.scale}
                        dash={[6 / vp.scale, 4 / vp.scale]}
                        lineCap="round"
                        lineJoin="round"
                        fill={open ? undefined : hexToRgba(hex, 0.1)}
                        listening={false}
                      />
                    )}
                    {livePoints.map(([px, py], i) => (
                      <Circle
                        key={i}
                        x={px * size.w}
                        y={py * size.h}
                        radius={5 / vp.scale}
                        hitStrokeWidth={10 / vp.scale}
                        fill="white"
                        stroke={hex}
                        strokeWidth={1.5 / vp.scale}
                        onPointerDown={(e) =>
                          interaction.onVertexPointerDown(selectedId, i, livePoints, e)
                        }
                      />
                    ))}
                  </Layer>
                );
              })()}
            {/* v0.21.23 · 交互式 SAM 候选 + 点会话（瞬态，不落库；置顶且不吃事件）。 */}
            {displayedSamMaskRecords.length > 0 && (
              <Layer name="sam-native-mask-candidates" listening={false}>
                {[...displayedSamMaskRecords]
                  .sort((left, right) => left.zOrder - right.zOrder)
                  .map((record) => (
                    <Group key={record.cacheKey} id={record.id} listening={false}>
                      <KonvaImage
                        image={record.image}
                        x={record.bounds.x * size.w}
                        y={record.bounds.y * size.h}
                        width={record.bounds.w * size.w}
                        height={record.bounds.h * size.h}
                        opacity={record.selected ? 0.58 : 0.28}
                        imageSmoothingEnabled={false}
                        listening={false}
                      />
                      {record.selected && (
                        <Rect
                          x={record.bounds.x * size.w}
                          y={record.bounds.y * size.h}
                          width={record.bounds.w * size.w}
                          height={record.bounds.h * size.h}
                          stroke={SAM_PROBE_STROKE}
                          strokeWidth={2 / vp.scale}
                          dash={[6 / vp.scale, 4 / vp.scale]}
                          listening={false}
                        />
                      )}
                    </Group>
                  ))}
              </Layer>
            )}
            {(samCandidates.length > 0 ||
              samSessionPoints.length > 0 ||
              samSessionBoxes.length > 0) && (
              <Layer name="sam-candidates" listening={false}>
                <VideoSamCandidateOverlay
                  candidates={samCandidates}
                  activeIdx={samActiveIdx}
                  previewAsBbox={videoTool === "magic-box"}
                  sessionPoints={samSessionPoints}
                  sessionBoxes={samSessionBoxes}
                  width={size.w}
                  height={size.h}
                  scale={vp.scale}
                />
              </Layer>
            )}
            {((issuePixelFeedbacks?.length ?? 0) > 0 ||
              issuePinDropArmed ||
              issueNavigationPending) && (
              <VideoKonvaIssueLayer
                pixelIssues={(issuePixelFeedbacks ?? []).filter(
                  (f) => f.kind === "issue" && f.anchor_type === "pixel" && !!f.anchor_position,
                )}
                frameIndex={frameIndex}
                size={size}
                scale={vp.scale}
                highlightId={issueHighlightId}
                onPinClick={onIssuePinClick}
                dropArmed={issuePinDropArmed || issueNavigationPending}
                onDrop={issueNavigationPending ? undefined : onIssuePinDrop}
              />
            )}
          </Stage>
          {creationScopeHint && (
            <div
              data-testid="video-creation-scope-hint"
              data-source-frame-index={creationScopeHint.frameIndex}
              className="pointer-events-none absolute left-[var(--creation-scope-left)] top-[var(--creation-scope-top)] z-local-3 max-w-[calc(100%-16px)] rounded border border-border bg-card px-2 py-1 text-xs text-foreground shadow-sm"
              // eslint-disable-next-line no-restricted-syntax -- Position follows the actual creation preview through viewport transforms.
              style={
                {
                  "--creation-scope-left": `${creationScopeHint.left}px`,
                  "--creation-scope-top": `${creationScopeHint.top}px`,
                } as CSSProperties
              }
            >
              {creationScopeHint.text}
            </div>
          )}
          {/* 跟踪当前帧屏幕矩形的不可见标记:改类/批量改类弹窗经 [data-video-overlay] 锚到画布上的框
            (Konva 栈无旧 SVG overlay,此 div 复刻其矩形,随 vp 平移/缩放同步)。 */}
          <div
            data-video-overlay
            className={styles.frameMarker}
            // eslint-disable-next-line no-restricted-syntax -- 帧矩形随 vp 动态变化,经 CSS 变量注入。
            style={
              {
                "--frame-left": `${vp.tx}px`,
                "--frame-top": `${vp.ty}px`,
                "--frame-w": `${size.w * vp.scale}px`,
                "--frame-h": `${size.h * vp.scale}px`,
              } as CSSProperties
            }
          />
          <VideoStageCommentBadges
            entries={frameViews.entries}
            maskRecords={displayedMaskRecords.filter((record) => record.source === "annotation")}
            annotations={annotations}
            counts={drag ? undefined : annotationCommentCounts}
            imgW={size.w}
            imgH={size.h}
            vp={vp}
            viewportSize={viewportSize}
            selectedIds={new Set(selectedIds)}
            onOpenAnnotationComments={onOpenAnnotationComments}
            interactive={annotationCommentBadgesInteractive}
          />
          {/* v0.21.4 · AI 候选贴框快捷条(采纳 / 忽略), 复用图片工作台 SelectionOverlay。 */}
          {selectedAiBox && !readOnly && videoTool === "select" && (
            <SelectionOverlay
              box={selectedAiBox}
              isAi
              imgW={size.w}
              imgH={size.h}
              vp={vp}
              onAccept={() => onAcceptPrediction?.(selectedAiBox)}
              onReject={() => onRejectPrediction?.(selectedAiBox)}
            />
          )}
        </div>
        {playbackError && (
          <div data-testid="video-konva-playback-error" className={styles.playbackError}>
            视频无法播放:{playbackError}
          </div>
        )}
        <VideoQcWarnings warnings={qualityWarnings} />
        <VideoPlaybackOverlay
          sourceKey={sourceKey}
          windowControlsRef={timelineWindowControlsRef}
          onViewInteraction={interruptIssueNavigation}
          frameIndex={frameIndex}
          maxFrame={maxFrame}
          samplingStep={samplingStep}
          largeFrameStep={largeFrameStep}
          timebase={timebase}
          isPlaying={isPlaybackActive}
          playbackRateLabel={
            isJogPlaying ? `${jogPlayback.direction < 0 ? "-" : ""}${jogPlayback.rate}x` : undefined
          }
          selectedTrackTimeline={effectiveSelectedTrackTimeline}
          trackColor={effectiveSelectedTrackColor}
          globalTimelineDensity={globalTimelineDensity}
          predictionDensity={predictionDensity}
          onSeekPredicted={hasPredictedFrames ? seekToAdjacentPredictedFrame : undefined}
          trackColorOverrides={trackColorOverrides}
          loopRegion={loopRegion}
          propagateRange={propagateRange}
          trackerReview={trackerReview}
          onSeekReviewFrame={onSeekReviewFrame}
          segmentRange={segmentRange}
          rangeSelectPurpose={timelineChapterControls?.rangeSelectPurpose ?? "loop"}
          bookmarks={bookmarks}
          chapters={chapters}
          issueFrames={issueFrames}
          onSeekIssueFrame={onSeekIssueFrame}
          hoverPreview={framePreview}
          currentFrameEntryCount={frameViews.entries.length}
          visible={playbackOverlayVisible && !drag}
          interactive
          highlightAction={highlightAction}
          onSeek={(frame) => {
            showPlaybackOverlay();
            // snapToGrid: false —— 点击本身携带明确目标帧，默认 snap 会先发起一次
            // 网格 seek 再被目标 seek 取消，高负载下徒增一次竞态窗口（Issue #114）。
            pausePlayback({ snapToGrid: false });
            seekToFrame(frame, { recordHistory: true });
          }}
          onSeekByFrames={seekOverlayByFrames}
          onTogglePlay={controls.togglePlayback}
          onLoopRegionChange={setNormalizedLoopRegion}
          onClearLoopRegion={clearLoopRegion}
          onSeekBookmark={(targetFrame) => seekToFrame(targetFrame, { recordHistory: true })}
          onSeekChapter={(_, frame) => seekToFrame(frame, { recordHistory: true })}
          onRangeSelect={timelineChapterControls?.onRangeSelect}
          hoveredChapterId={timelineChapterControls?.hoveredChapterId ?? null}
          onHoverChapter={timelineChapterControls?.onHoverChapter}
          onChapterResize={timelineChapterControls?.onResizeChapter}
          onHoverFrameChange={previewFrame}
        />
        <ContextMenu
          open={contextMenu.open && contextMenuItems.length > 0}
          x={contextMenu.x}
          y={contextMenu.y}
          items={contextMenuItems}
          onClose={closeContextMenu}
        />
        <FloatingDock
          scale={vp.scale}
          canUndo={false}
          canRedo={false}
          onUndo={noop}
          onRedo={noop}
          onZoomIn={() => setVp((cur) => ({ ...cur, scale: clampScale(cur.scale * 1.2) }))}
          onZoomOut={() => setVp((cur) => ({ ...cur, scale: clampScale(cur.scale / 1.2) }))}
          onFit={fitViewport}
          showHistory={false}
        />
        {videoMinimapVisible && (
          <Minimap
            imgW={size.w}
            imgH={size.h}
            vpSize={viewportSize}
            vp={vp}
            setVp={setVp}
            thumbnailUrl={manifest.poster_url ?? null}
            fileUrl={null}
            frameSource={
              pickMediaImageSource(isPlaybackActive, videoEl, displayBitmap?.bitmap ?? null) ?? null
            }
            frameVersion={frameIndex}
            isLive={isPlaybackActive}
            currentFrameIndex={frameIndex}
            maxFrame={maxFrame}
            cachedFrameRanges={cachedRanges}
            bottom={64}
          />
        )}
      </div>
    );
    return (
      <>
        <VideoTrackContextBar
          frameIndex={frameIndex}
          trackerReview={trackerReview}
          reviewReference={reviewReference}
          track={
            selectedContextTrack
              ? {
                  className: selectedContextTrack.class_name,
                  shortId: shortTrackId(selectedContextTrack.geometry.track_id),
                  color: effectiveSelectedTrackColor ?? classColor(selectedContextTrack.class_name),
                  locked: contextTrackLocked,
                  hidden: Boolean(hiddenTrackIds?.has(selectedContextTrack.geometry.track_id)),
                  readOnly,
                }
              : null
          }
          context={trackContext}
          onSeekFrame={seekContextFrame}
          actions={contextActions}
          shortcuts={
            selectedManagedTrack
              ? [
                  { key: ", / .", label: "关键帧" },
                  { key: "K", label: "暂停" },
                ]
              : [{ key: "K", label: "暂停" }]
          }
          stickyHint={contextWritesBlocked ? null : stickyTrackHint}
        />
        <div className="relative flex min-h-0 flex-1 flex-col">
          {canvas}
          {overlays}
        </div>
      </>
    );
  },
);

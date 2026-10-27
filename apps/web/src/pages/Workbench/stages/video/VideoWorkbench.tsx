import type { TrackerReviewProjection } from "@/hooks/videoTrackerReviewScope";
import type { VideoTrackContextBarProps } from "../../stage/VideoTrackContextBar";
import { forwardRef, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useAuthStore } from "@/stores/authStore";
import { useOriginalVideoPlayback } from "@/hooks/useOriginalVideoPlayback";
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
import type { VideoStageControls } from "../../stage/videoStageControls";
import { VideoKonvaStage } from "../../stage/VideoKonvaStage";
import type { VideoSamCandidateShape } from "../../stage/VideoSamCandidateOverlay";
import type { AiBox } from "../../state/transforms";
import type { WorkbenchCommonPreferences } from "@/api/auth";
import type { AnnotationFeedback } from "@/api/feedbacks";
import type {
  VideoTimelineChapter,
  VideoTimelineChapterControls,
  VideoSegmentTimelineRange,
} from "../../stage/VideoPlaybackOverlay";
import type { VideoManagedTrackAnnotation, VideoSamPrompt } from "../../stage/videoStageTypes";
import type { VideoMaskCandidate } from "../../stage/videoMaskFrames";
import type { UseMaskEditorReturn } from "../../state/useMaskEditor";
import type { PendingDrawing, VideoTool } from "../../state/useWorkbenchState";
import { useWorkbenchConfig } from "../../state/useWorkbenchConfig";
import { resolveAnnotationVisual } from "../../stage/annotationVisual";
import type { DiffMode } from "../../modes/types";
import type {
  VideoConvertOptions,
  VideoTrackCompositionOptions,
} from "./useVideoAnnotationActions";
import type { RasterMaskRenderRecord } from "../../stage/shared/rasterMaskRender";
import type { VideoMaskKeyframeActionHandlers } from "../../stage/videoMaskKeyframeActions";
import type { MaskCompareTileStore } from "../../stage/shared/maskCompareTileStore";

type Geom = { x: number; y: number; w: number; h: number };
type VideoGeometry =
  | VideoBboxGeometry
  | VideoTrackGeometry
  | VideoTrackMaskGeometry
  | VideoPolygonGeometry
  | VideoPolylineGeometry
  | VideoRotatedBboxGeometry
  | VideoKeypointGeometry
  | VideoTrackPolygonGeometry
  | VideoTrackPolylineGeometry;

export interface VideoWorkbenchProps {
  taskId: string | null;
  overlays?: ReactNode;
  maskCompareStore?: MaskCompareTileStore | null;
  manifest: TaskVideoManifestResponse | undefined;
  frameTimetable?: TaskVideoFrameTimetableResponse;
  isLoading?: boolean;
  error?: unknown;
  annotations: AnnotationResponse[];
  /** v0.21.4 · AI 候选框(全部帧); 舞台内按当前帧过滤渲染 + 采纳/驳回。 */
  aiBoxes?: AiBox[];
  selectedId: string | null;
  activeClass: string;
  keypointSchema?: KeypointSchema | null;
  frameIndex: number;
  selectedIds?: string[];
  reviewDisplayMode?: DiffMode;
  hiddenTrackIds: Set<string>;
  lockedTrackIds: Set<string>;
  trackColorOverrides?: Record<string, string>;
  readOnly: boolean;
  videoTool: VideoTool;
  isVideoToolEnabled?: (t: VideoTool) => boolean;
  spacePan?: boolean;
  onSpacePanDragStart?: () => void;
  pendingDrawing: PendingDrawing;
  chapters?: VideoTimelineChapter[];
  timelineChapterControls?: VideoTimelineChapterControls;
  propagateRange?: { startFrame: number; endFrame: number } | null;
  trackerReview?: TrackerReviewProjection | null;
  reviewReference?: VideoTrackContextBarProps["reviewReference"];
  onSeekReviewFrame?: (frame: number) => void;
  segmentRange?: VideoSegmentTimelineRange | null;
  videoSampling?: VideoSamplingConfig | null;
  performanceTier?: WorkbenchCommonPreferences["performanceTier"];
  onSelect: (id: string | null, opts?: { shift?: boolean }) => void;
  onFrameIndexChange: (frameIndex: number) => void;
  onCreate: (frameIndex: number, geom: Geom) => void;
  /** v0.21.20 · 由绘制顶点新建 polygon/polyline track。 */
  onCreatePointsTrack?: (
    type: "video_track_polygon" | "video_track_polyline",
    frameIndex: number,
    points: [number, number][],
  ) => void;
  /** v0.21.21 · 由绘制顶点新建单帧 polygon/polyline。 */
  /** v0.21.23 · 交互式 SAM: 提示派发 + 瞬态候选 / 点会话 (透传给 VideoKonvaStage)。 */
  onSamPrompt?: (prompt: VideoSamPrompt) => void;
  samCandidates?: VideoSamCandidateShape[];
  samMaskRecords?: readonly RasterMaskRenderRecord<"interactive">[];
  onSelectSamMaskCandidate?: (candidateId: string) => void;
  samActiveIdx?: number;
  samSessionPoints?: { pt: [number, number]; polarity: 1 | 0; obj?: number }[];
  /** v0.21.27 · 框修正 · 当前帧已落的 PVS 框种子 (归一化 xyxy)。 */
  samSessionBoxes?: { bbox: [number, number, number, number]; obj?: number }[];
  maskCandidates?: VideoMaskCandidate[];
  maskEditor?: UseMaskEditorReturn;
  maskKeyframeActions?: VideoMaskKeyframeActionHandlers;
  onMaskCommit?: () => void;
  onMaskCancel?: () => void;
  samPolarity?: "positive" | "negative";
  onCreatePoints?: (
    type: "video_polygon" | "video_polyline",
    frameIndex: number,
    points: [number, number][],
  ) => void;
  onCreateKeypoints?: (frameIndex: number, points: Keypoint[]) => void;
  onPendingDraw: (
    kind: "video_bbox" | "video_track_bbox" | "video_rotated_bbox",
    frameIndex: number,
    geom: Geom,
    anchor: { left: number; top: number },
  ) => void;
  onUpdate: (annotation: AnnotationResponse, geometry: VideoGeometry) => void;
  onRename: (annotation: AnnotationResponse, className: string) => void;
  onChangeUserBoxClass: (id: string) => void;
  onDeleteUserBox: (id: string) => void;
  onConvertToBboxes: (annotation: AnnotationResponse, options: VideoConvertOptions) => void;
  onAcceptPrediction?: (b: AiBox) => void;
  onRejectPrediction?: (b: AiBox) => void;
  onComposeTracks?: (options: VideoTrackCompositionOptions) => void;
  onToggleHiddenTrack?: (trackId: string) => void;
  onToggleLockedTrack?: (trackId: string) => void;
  onPropagateTrack?: (annotation: VideoManagedTrackAnnotation) => void;
  onCursorMove: (pt: { x: number; y: number } | null) => void;
  // v0.11.7 · pixel-anchored issue 图钉 (按当前帧显隐 + 时间轴标记)。
  issuePixelFeedbacks?: AnnotationFeedback[];
  issueHighlightId?: string | null;
  annotationCommentCounts?: Record<string, number>;
  onOpenAnnotationComments?: (annotationId: string) => void;
  onIssuePinClick?: (id: string) => void;
  issuePinDropArmed?: boolean;
  issueNavigationPending?: boolean;
  onIssuePinDrop?: (x: number, y: number, frame?: number) => void;
  onSeekIssueFrame?: (frame: number) => void;
}

const VideoWorkbenchSession = forwardRef<VideoStageControls, VideoWorkbenchProps>(
  function VideoWorkbenchSession(
    {
      maskCompareStore,
      overlays,
      taskId,
      manifest: incomingManifest,
      frameTimetable,
      isLoading,
      error,
      annotations,
      aiBoxes,
      selectedId,
      activeClass,
      keypointSchema,
      frameIndex,
      selectedIds = [],
      reviewDisplayMode,
      hiddenTrackIds,
      lockedTrackIds,
      trackColorOverrides,
      readOnly,
      videoTool,
      isVideoToolEnabled,
      spacePan = false,
      onSpacePanDragStart,
      pendingDrawing,
      chapters,
      timelineChapterControls,
      propagateRange,
      trackerReview,
      reviewReference,
      onSeekReviewFrame,
      segmentRange,
      videoSampling,
      performanceTier,
      onSelect,
      onFrameIndexChange,
      onCreate,
      onCreatePointsTrack,
      onCreatePoints,
      onCreateKeypoints,
      onSamPrompt,
      samCandidates,
      samMaskRecords,
      onSelectSamMaskCandidate,
      samActiveIdx,
      samSessionPoints,
      samSessionBoxes,
      maskCandidates,
      maskEditor,
      maskKeyframeActions,
      onMaskCommit,
      onMaskCancel,
      samPolarity,
      onPendingDraw,
      onUpdate,
      onChangeUserBoxClass,
      onDeleteUserBox,
      onConvertToBboxes,
      onAcceptPrediction,
      onRejectPrediction,
      onComposeTracks,
      onToggleHiddenTrack,
      onToggleLockedTrack,
      onPropagateTrack,
      onCursorMove,
      issuePixelFeedbacks,
      issueHighlightId,
      annotationCommentCounts,
      onOpenAnnotationComments,
      onIssuePinClick,
      issuePinDropArmed,
      issueNavigationPending,
      onIssuePinDrop,
      onSeekIssueFrame,
    },
    ref,
  ) {
    const { config: workbenchConfig } = useWorkbenchConfig();
    const workbenchVideo = workbenchConfig.video;
    // Query cache refreshes can temporarily remove metadata without changing its owner.
    // The keyed session must keep the same stage and draft during that interval.
    const confirmedManifest = useRef<TaskVideoManifestResponse>();
    const currentManifest = incomingManifest?.task_id === taskId ? incomingManifest : undefined;
    useLayoutEffect(() => {
      if (currentManifest) confirmedManifest.current = currentManifest;
    }, [currentManifest]);
    const manifest = currentManifest ?? confirmedManifest.current;
    const [video, setVideo] = useState<HTMLVideoElement | null>(null);
    const original = useOriginalVideoPlayback({
      resource: taskId ? { kind: "task", taskId } : null,
      identityPath: manifest?.original_identity_path,
      video,
    });
    const playbackManifest = useMemo(
      () => manifest && { ...manifest, video_url: original.mediaPath ?? "" },
      [manifest, original.mediaPath],
    );
    // v0.15.27 · 共享标注视觉规格(线宽/填充/字号/标签显隐);与图片工作台共用 common 子集。
    const annotationVisual = useMemo(
      () => resolveAnnotationVisual(workbenchConfig.common),
      [workbenchConfig.common],
    );
    // v0.16.5 · 视频渲染栈统一到 Konva(删旧 SVG 栈,见 ADR-0041):唯一实现,无 flag 分支。
    return (
      <VideoKonvaStage
        key={original.contentKey ?? "pending-original"}
        maskCompareStore={maskCompareStore}
        overlays={overlays}
        ref={ref}
        manifest={playbackManifest}
        onVideoNode={setVideo}
        sourceStatus={original.status}
        sourceContentKey={original.contentKey}
        sourceError={
          original.error ??
          (manifest && !manifest.original_identity_path
            ? "视频原件身份地址不可用，请刷新后重试"
            : null)
        }
        onSourceRetry={original.retry}
        onSourceRecover={original.recover}
        onSourcePrimed={original.onPrimed}
        onPauseIntent={original.cancelResume}
        isSourceReady={original.isSourceReady}
        frameTimetable={frameTimetable}
        isLoading={isLoading && !manifest}
        error={error}
        frameIndex={frameIndex}
        autoFitOnResize={workbenchVideo.autoFitOnResize}
        trackContinueAutoAdvance={workbenchVideo.trackContinueAutoAdvance}
        focusSelectionEnabled={workbenchConfig.common.focusSelectionEnabled}
        performanceTier={performanceTier}
        onFrameIndexChange={onFrameIndexChange}
        annotations={annotations}
        aiBoxes={aiBoxes}
        selectedId={selectedId}
        hiddenTrackIds={hiddenTrackIds}
        reviewDisplayMode={reviewDisplayMode}
        trackColorOverrides={trackColorOverrides}
        activeClass={activeClass}
        keypointSchema={keypointSchema}
        pendingDrawing={pendingDrawing}
        issuePixelFeedbacks={issuePixelFeedbacks}
        issueHighlightId={issueHighlightId}
        annotationCommentCounts={
          workbenchConfig.common.showAnnotationComments ? annotationCommentCounts : undefined
        }
        onOpenAnnotationComments={onOpenAnnotationComments}
        onIssuePinClick={onIssuePinClick}
        issuePinDropArmed={issuePinDropArmed}
        issueNavigationPending={issueNavigationPending}
        onIssuePinDrop={onIssuePinDrop}
        onSeekIssueFrame={onSeekIssueFrame}
        visual={annotationVisual}
        videoTool={videoTool}
        isVideoToolEnabled={isVideoToolEnabled}
        spacePan={spacePan}
        onSpacePanDragStart={onSpacePanDragStart}
        readOnly={readOnly}
        lockedTrackIds={lockedTrackIds}
        selectedIds={selectedIds}
        onSelect={onSelect}
        onCursorMove={onCursorMove}
        onCreate={onCreate}
        onCreatePointsTrack={onCreatePointsTrack}
        onSamPrompt={onSamPrompt}
        samCandidates={samCandidates}
        samMaskRecords={samMaskRecords}
        onSelectSamMaskCandidate={onSelectSamMaskCandidate}
        samActiveIdx={samActiveIdx}
        samSessionPoints={samSessionPoints}
        samSessionBoxes={samSessionBoxes}
        maskCandidates={maskCandidates}
        maskEditor={maskEditor}
        maskKeyframeActions={maskKeyframeActions}
        onMaskCommit={onMaskCommit}
        onMaskCancel={onMaskCancel}
        samPolarity={samPolarity}
        onCreatePoints={onCreatePoints}
        onCreateKeypoints={onCreateKeypoints}
        onPendingDraw={onPendingDraw}
        onUpdate={onUpdate}
        onChangeUserBoxClass={onChangeUserBoxClass}
        onComposeTracks={onComposeTracks}
        onConvertToBboxes={onConvertToBboxes}
        onAcceptPrediction={onAcceptPrediction}
        onRejectPrediction={onRejectPrediction}
        onDelete={(ann) => onDeleteUserBox(ann.id)}
        onPropagateTrack={onPropagateTrack}
        onToggleHiddenTrack={onToggleHiddenTrack}
        onToggleLockedTrack={onToggleLockedTrack}
        chapters={chapters}
        timelineChapterControls={timelineChapterControls}
        propagateRange={propagateRange}
        trackerReview={trackerReview}
        reviewReference={reviewReference}
        onSeekReviewFrame={onSeekReviewFrame}
        segmentRange={segmentRange}
        videoSampling={videoSampling}
        defaultPlaybackRate={workbenchVideo.defaultPlaybackRate}
        largeFrameStep={workbenchVideo.largeFrameStep}
      />
    );
  },
);

export const VideoWorkbench = forwardRef<VideoStageControls, VideoWorkbenchProps>(
  function VideoWorkbench(props, ref) {
    const owner = useAuthStore((state) =>
      JSON.stringify([state.user?.id ?? null, state.sessionId]),
    );
    return (
      <VideoWorkbenchSession key={JSON.stringify([owner, props.taskId])} {...props} ref={ref} />
    );
  },
);

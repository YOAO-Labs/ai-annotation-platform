import type { CommitPolygonSlice } from "../stage/usePolygonSlice";
import type { TrackerReviewProjection } from "@/hooks/videoTrackerReviewScope";
import type { VideoTrackContextBarProps } from "../stage/VideoTrackContextBar";
import { forwardRef, lazy, Suspense, type ReactNode } from "react";
import type {
  Annotation,
  AnnotationResponse,
  Geometry,
  RotatedBboxGeometry,
  TaskVideoFrameTimetableResponse,
  TaskVideoManifestResponse,
  VideoBboxGeometry,
  VideoPolygonGeometry,
  VideoPolylineGeometry,
  VideoRotatedBboxGeometry,
  VideoKeypointGeometry,
  Keypoint,
  VideoSamplingConfig,
  VideoTrackGeometry,
  VideoTrackMaskGeometry,
  VideoTrackPolygonGeometry,
  VideoTrackPolylineGeometry,
} from "@/types";
import type { CommentCanvasDrawing } from "@/api/comments";
import type {
  WorkbenchCommonPreferences,
  WorkbenchLayoutPreferences,
  WorkbenchPointcloudPreferences,
} from "@/api/auth";
import type { ProjectRenderingConfig } from "@/api/projects";
import type { AiBox } from "../state/transforms";
import type {
  PendingDrawing,
  SamPolarity,
  SamSubTool,
  ThreeDTool,
  Tool,
  VideoTool,
} from "../state/useWorkbenchState";
import type { WorkbenchConfigPatch, WorkbenchLayoutPatch } from "../state/useWorkbenchConfig";
import type { Viewport } from "../state/useViewportTransform";
import type { DiffMode } from "../modes/types";
import type { PolygonDraftHandle } from "../stage/tools";
import type { VideoStageControls } from "../stage/videoStageControls";
import type {
  VideoTimelineChapter,
  VideoTimelineChapterControls,
  VideoSegmentTimelineRange,
} from "../stage/VideoPlaybackOverlay";
import type { VideoManagedTrackAnnotation, VideoSamPrompt } from "../stage/videoStageTypes";
import type { VideoMaskCandidate } from "../stage/videoMaskFrames";
import { ImageWorkbench } from "../stages/image/ImageWorkbench";
import type { StageKind } from "../stages/types";
const ThreeDWorkbench = lazy(() => import("../stages/three-d/ThreeDWorkbench"));
import { VideoWorkbench } from "../stages/video/VideoWorkbench";
import type { VideoSamCandidateShape } from "../stage/VideoSamCandidateOverlay";
import type {
  VideoConvertOptions,
  VideoTrackCompositionOptions,
} from "../stages/video/useVideoAnnotationActions";
import type { UseMaskEditorReturn } from "../state/useMaskEditor";
import type { ImageContextMenuClipboardActions } from "../stage/imageStageContextMenu";
import type { RasterMaskRenderRecord } from "../stage/shared/rasterMaskRender";
import type { RasterMaskRecordStatus } from "../stage/shared/useRasterMaskRecords";
import type { VideoMaskKeyframeActionHandlers } from "../stage/videoMaskKeyframeActions";
import type { MaskCompareTileStore } from "../stage/shared/maskCompareTileStore";
import type { WorkbenchImageSource } from "../stage/imagePyramid";
import type { RasterResourceCoordinator } from "../stage/shared/rasterResourceCoordinator";
import type { WorkbenchPetDock } from "./pet/WorkbenchPet";
import type { BboxCreationMode } from "../stage/ImageStage.helpers";

type Geom = { x: number; y: number; w: number; h: number };
type StageGeometry = { imgW: number; imgH: number; vpSize: { w: number; h: number } };
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

interface WorkbenchStageHostCommonProps {
  maskCompareStore?: MaskCompareTileStore | null;
  stageKind: StageKind;
  taskId: string | null;
  overlays: ReactNode;
  readOnly: boolean;
  activeClass: string;
  selectedId: string | null;
  selectedIds: string[];
  annotations: AnnotationResponse[];
  pendingDrawing: PendingDrawing;
  fitTick: number;
  onSelectBox: (id: string | null, opts?: { shift?: boolean }) => void;
  onCursorMove: (pt: { x: number; y: number } | null) => void;
  onDeleteUserBox: (id: string) => void;
  onChangeUserBoxClass: (id: string) => void;
  threeDTool: ThreeDTool;
  onSetThreeDTool: (t: ThreeDTool) => void;
  onCrossFramePropagate: (direction: "next" | "prev") => void;
  onCrossFramePropagateBatch: (direction: "next" | "prev") => void;
  onCrossFramePropagateToTask: (targetTaskId: string, targetFrameIndex: number) => void;
  onCrossFrameInterpolate: (trackId: string, toTaskId: string) => void;
  onNavigateSceneFrame: (targetTaskId: string) => Promise<boolean>;
  scenePlaybackActive?: boolean;
  onScenePlaybackActiveChange?: (active: boolean) => void;
  scenePlaybackBlockedReason?: string | null;
  rightSidebarOpen: boolean;
  rightSidebarWidth: number;
  workbenchLayout: WorkbenchLayoutPreferences;
  onWorkbenchLayoutChange: (patch: WorkbenchLayoutPatch) => void;
  workbenchCommon: WorkbenchCommonPreferences;
  workbenchPointcloud: WorkbenchPointcloudPreferences;
  workbenchConfigLoaded: boolean;
  onWorkbenchConfigChange: (patch: WorkbenchConfigPatch) => void;
  onWorkbenchConfigUpdate: (patch: WorkbenchConfigPatch) => Promise<void>;
  projectRenderingConfig?: ProjectRenderingConfig | null;
}

interface WorkbenchStageHostVideoProps {
  videoManifest: TaskVideoManifestResponse | undefined;
  videoFrameTimetable?: TaskVideoFrameTimetableResponse;
  videoManifestLoading?: boolean;
  videoManifestError?: unknown;
  annotationWritesPending?: boolean;
  videoChapters?: VideoTimelineChapter[];
  videoTimelineChapterControls?: VideoTimelineChapterControls;
  videoPropagateRange?: { startFrame: number; endFrame: number } | null;
  trackerReview?: TrackerReviewProjection | null;
  reviewReference?: VideoTrackContextBarProps["reviewReference"];
  onSeekReviewFrame?: (frame: number) => void;
  videoSegmentRange?: VideoSegmentTimelineRange | null;
  videoSampling?: VideoSamplingConfig | null;
  videoTool: VideoTool;
  keypointSchema?: import("@/types").KeypointSchema | null;
  isVideoToolEnabled?: (t: VideoTool) => boolean;
  onVideoSamPrompt?: (prompt: VideoSamPrompt) => void;
  samCandidates?: VideoSamCandidateShape[];
  samMaskRecords?: readonly RasterMaskRenderRecord<"interactive">[];
  onSelectSamMaskCandidate?: (candidateId: string) => void;
  samActiveIdx?: number;
  samSessionPoints?: { pt: [number, number]; polarity: 1 | 0; obj?: number }[];
  samSessionBoxes?: { bbox: [number, number, number, number]; obj?: number }[];
  videoMaskCandidates?: VideoMaskCandidate[];
  videoMaskEditor?: UseMaskEditorReturn;
  videoMaskKeyframeActions?: VideoMaskKeyframeActionHandlers;
  onVideoMaskCommit?: () => void;
  onVideoMaskCancel?: () => void;
  samPolarity?: "positive" | "negative";
  spacePan: boolean;
  onSpacePanDragStart: () => void;
  videoFrameIndex: number;
  videoReviewDisplayMode?: DiffMode;
  hiddenVideoTrackIds: Set<string>;
  lockedVideoTrackIds: Set<string>;
  trackColorOverrides?: Record<string, string>;
  onVideoFrameIndexChange: (frameIndex: number) => void;
  onVideoCreate: (frameIndex: number, geom: Geom) => void;
  onVideoCreatePointsTrack?: (
    type: "video_track_polygon" | "video_track_polyline",
    frameIndex: number,
    points: [number, number][],
  ) => void;
  onVideoCreatePoints?: (
    type: "video_polygon" | "video_polyline",
    frameIndex: number,
    points: [number, number][],
  ) => void;
  onVideoCreateKeypoints?: (frameIndex: number, points: Keypoint[]) => void;
  onVideoPendingDraw: (
    kind: "video_bbox" | "video_track_bbox" | "video_rotated_bbox",
    frameIndex: number,
    geom: Geom,
    anchor: { left: number; top: number },
  ) => void;
  onVideoUpdate: (annotation: AnnotationResponse, geometry: VideoGeometry) => void;
  onVideoRename: (annotation: AnnotationResponse, className: string) => void;
  onVideoConvertToBboxes: (annotation: AnnotationResponse, options: VideoConvertOptions) => void;
  onVideoComposeTracks?: (options: VideoTrackCompositionOptions) => void;
  onToggleHiddenVideoTrack?: (trackId: string) => void;
  onToggleLockedVideoTrack?: (trackId: string) => void;
  onPropagateVideoTrack?: (annotation: VideoManagedTrackAnnotation) => void;
  aiBoxes?: AiBox[];
  onAcceptPrediction?: (b: AiBox) => void;
  onRejectPrediction?: (b: AiBox) => void;
}

interface WorkbenchStageHostImageProps {
  bboxCreationMode?: BboxCreationMode;
  continuousCreation?: boolean;
  resourceCoordinator?: RasterResourceCoordinator;
  rasterMaskRecords: readonly RasterMaskRenderRecord<"annotation">[];
  predictionRasterMaskRecords?: readonly RasterMaskRenderRecord<"prediction">[];
  rasterMaskStatusById: ReadonlyMap<string, RasterMaskRecordStatus>;
  onRetryRasterMask: (id: string) => void;
  editingRasterMaskId?: string | null;
  maskReadOnly?: boolean;
  fileUrl: string | null;
  imageSource?: WorkbenchImageSource | null;
  onRetryImagePyramid?: () => Promise<void>;
  mediaKey?: string | null;
  blurhash?: string | null;
  imageWidth?: number | null;
  imageHeight?: number | null;
  thumbnailUrl: string | null;
  tool: Tool;
  fadedAiIds: Set<string>;
  nudgeMap: Map<string, Geom>;
  pendingGeomMap: Map<string, import("@/types").Geometry>;
  userBoxes: Annotation[];
  aiBoxes: AiBox[];
  spacePan: boolean;
  vp: Viewport;
  setVp: React.Dispatch<React.SetStateAction<Viewport>>;
  setFitTick: React.Dispatch<React.SetStateAction<number>>;
  onAcceptPrediction: (b: AiBox, attributeOverrides?: Record<string, unknown>) => void;
  onRejectPrediction: (b: AiBox) => void;
  onPatchShapeFlag?: (
    id: string,
    flag: "z_order" | "is_locked" | "is_hidden",
    value: number | boolean,
  ) => void;
  secondaryBarHidden?: boolean;
  onToggleSecondaryBar?: () => void;
  imageClipboardActions?: ImageContextMenuClipboardActions | null;
  onCommitDrawing: (geo: Geom) => void;
  onCommitRotatedBbox: (geo: Geom) => void;
  onCommitRotateBbox: (id: string, before: RotatedBboxGeometry, after: RotatedBboxGeometry) => void;
  onSamPrompt: (
    prompt:
      | { kind: "point"; pt: [number, number]; alt: boolean }
      | { kind: "bbox"; bbox: [number, number, number, number] }
      | { kind: "scribble"; points: [number, number][]; alt: boolean; width: number }
      | { kind: "exemplar"; bbox: [number, number, number, number]; alt: boolean },
  ) => void;
  onCommitMove: (
    id: string,
    before: Geom,
    after: Geom,
    childMoves?: { id: string; before: Geometry; after: Geometry }[],
  ) => void;
  onCommitResize: (id: string, before: Geom, after: Geom) => void;
  onCommitPolygonGeometry: (
    id: string,
    before: [number, number][],
    after: [number, number][],
  ) => void;
  onCommitKeypointGeometry?: (
    id: string,
    before: import("@/types").Keypoint[],
    after: import("@/types").Keypoint[],
  ) => void;
  onJoinSelected: () => void;
  onCropSelected: (baseId: string) => void;
  onCommitPolygonSlice?: CommitPolygonSlice;
  onStageGeometry: (g: StageGeometry) => void;
}

interface WorkbenchStageHostAiProps {
  samCandidates: {
    id: string;
    type: "polygonlabels" | "rectanglelabels";
    points?: [number, number][];
    bbox?: { x: number; y: number; width: number; height: number };
  }[];
  samMaskRecords: readonly RasterMaskRenderRecord<"interactive">[];
  onSelectSamMaskCandidate: (candidateId: string) => void;
  samActiveIdx: number;
  samSessionPoints: { pt: [number, number]; polarity: 1 | 0 }[];
  samSessionScribbles: {
    points: [number, number][];
    polarity: 1 | 0;
    width: number;
  }[];
  samSessionExemplars: { bbox: [number, number, number, number]; polarity: 1 | 0 }[];
  samSubTool: SamSubTool | null;
  samPolarity: SamPolarity;
  onRefineSamCandidate?: (idx: number) => void;
}

interface WorkbenchStageHostEditorProps {
  polygonDraft?: PolygonDraftHandle;
  keypointDraft?: import("../stage/tools").KeypointDraftHandle;
  keypointSchema?: import("@/types").KeypointSchema | null;
  canvasShapes: NonNullable<CommentCanvasDrawing["shapes"]>;
  canvasEditable: boolean;
  canvasStroke: string;
  onCanvasStrokeCommit: (points: number[], stroke: string) => void;
  historicalShapes?: NonNullable<CommentCanvasDrawing["shapes"]>;
  canUndo: boolean;
  canRedo: boolean;
  onUndo: () => void;
  onRedo: () => void;
  onSetCanvasStroke: (stroke: string) => void;
  canvasShapeCount: number;
  onUndoCanvasShape: () => void;
  onClearCanvasShapes: () => void;
  onCancelCanvasDraft: () => void;
  onDoneCanvasDraft: () => void;
  stageGeom: StageGeometry;
  maskEditor?: UseMaskEditorReturn;
  projectRenderingConfig?: import("@/api/projects").ProjectRenderingConfig | null;
  issuePixelFeedbacks?: import("@/api/feedbacks").AnnotationFeedback[];
  highlightIssueId?: string | null;
  onIssuePinClick?: (id: string) => void;
  issuePinDropArmed?: boolean;
  issueNavigationPending?: boolean;
  onIssuePinDrop?: (x: number, y: number, frame?: number) => void;
  onSeekIssueFrame?: (frame: number) => void;
  annotationCommentCounts?: Record<string, number>;
  onOpenAnnotationComments?: (annotationId: string) => void;
}

interface WorkbenchStageHostProps {
  common: WorkbenchStageHostCommonProps;
  video?: WorkbenchStageHostVideoProps;
  image?: WorkbenchStageHostImageProps;
  ai?: WorkbenchStageHostAiProps;
  editors?: WorkbenchStageHostEditorProps;
  petDock?: WorkbenchPetDock;
}

function requireStageGroup<T>(group: T | undefined, groupName: string, stageKind: StageKind): T {
  if (group) return group;
  throw new Error(`WorkbenchStageHost missing ${groupName} props for ${stageKind} stage`);
}

export const WorkbenchStageHost = forwardRef<VideoStageControls, WorkbenchStageHostProps>(
  function WorkbenchStageHost(props, ref) {
    const { common, video, image, ai, editors, petDock } = props;
    const {
      stageKind,
      maskCompareStore,
      taskId,
      overlays,
      readOnly,
      activeClass,
      selectedId,
      selectedIds,
      annotations,
      pendingDrawing,
      fitTick,
      onSelectBox,
      onCursorMove,
      onDeleteUserBox,
      onChangeUserBoxClass,
      threeDTool,
      onSetThreeDTool,
      onCrossFramePropagate,
      onCrossFramePropagateBatch,
      onCrossFramePropagateToTask,
      onCrossFrameInterpolate,
      onNavigateSceneFrame,
      scenePlaybackActive,
      onScenePlaybackActiveChange,
      scenePlaybackBlockedReason,
      rightSidebarOpen,
      rightSidebarWidth,
      workbenchLayout,
      onWorkbenchLayoutChange,
      workbenchCommon,
      workbenchPointcloud,
      workbenchConfigLoaded,
      onWorkbenchConfigChange,
      onWorkbenchConfigUpdate,
      projectRenderingConfig: stageProjectRenderingConfig,
    } = common;
    const videoProps =
      stageKind === "video" ? requireStageGroup(video, "video", stageKind) : undefined;
    const imageProps =
      stageKind === "image" ? requireStageGroup(image, "image", stageKind) : undefined;
    const aiProps = stageKind === "image" ? requireStageGroup(ai, "ai", stageKind) : undefined;
    const editorProps =
      stageKind === "image"
        ? requireStageGroup(editors, "editors", stageKind)
        : stageKind === "video"
          ? editors
          : undefined;
    const {
      videoManifest,
      videoFrameTimetable,
      videoManifestLoading,
      videoManifestError,
      annotationWritesPending,
      videoChapters,
      videoTimelineChapterControls,
      videoPropagateRange,
      trackerReview,
      reviewReference,
      onSeekReviewFrame,
      videoSegmentRange,
      videoSampling,
      videoTool,
      keypointSchema: videoKeypointSchema,
      isVideoToolEnabled,
      onVideoSamPrompt,
      samCandidates: videoSamCandidates,
      samMaskRecords: videoSamMaskRecords,
      onSelectSamMaskCandidate: onSelectVideoSamMaskCandidate,
      samActiveIdx: videoSamActiveIdx,
      samSessionPoints: videoSamSessionPoints,
      samSessionBoxes: videoSamSessionBoxes,
      videoMaskCandidates,
      videoMaskEditor,
      videoMaskKeyframeActions,
      onVideoMaskCommit,
      onVideoMaskCancel,
      samPolarity: videoSamPolarity,
      spacePan: videoSpacePan,
      onSpacePanDragStart: onVideoSpacePanDragStart,
      videoFrameIndex,
      videoReviewDisplayMode,
      hiddenVideoTrackIds,
      lockedVideoTrackIds,
      trackColorOverrides,
      onVideoFrameIndexChange,
      onVideoCreate,
      onVideoCreatePointsTrack,
      onVideoCreatePoints,
      onVideoCreateKeypoints,
      onVideoPendingDraw,
      onVideoUpdate,
      onVideoRename,
      onVideoConvertToBboxes,
      onVideoComposeTracks,
      onToggleHiddenVideoTrack,
      onToggleLockedVideoTrack,
      onPropagateVideoTrack,
      aiBoxes: videoAiBoxes,
      onAcceptPrediction: onVideoAcceptPrediction,
      onRejectPrediction: onVideoRejectPrediction,
    } = videoProps ?? ({} as WorkbenchStageHostVideoProps);
    const {
      continuousCreation,
      bboxCreationMode,
      resourceCoordinator,
      rasterMaskRecords,
      predictionRasterMaskRecords,
      rasterMaskStatusById,
      onRetryRasterMask,
      editingRasterMaskId,
      maskReadOnly,
      fileUrl,
      imageSource,
      onRetryImagePyramid,
      mediaKey,
      blurhash,
      imageWidth,
      imageHeight,
      thumbnailUrl,
      tool,
      fadedAiIds,
      nudgeMap,
      pendingGeomMap,
      userBoxes,
      aiBoxes,
      spacePan,
      vp,
      setVp,
      setFitTick,
      onAcceptPrediction,
      onRejectPrediction,
      onPatchShapeFlag,
      secondaryBarHidden,
      onToggleSecondaryBar,
      imageClipboardActions,
      onCommitDrawing,
      onCommitRotatedBbox,
      onCommitRotateBbox,
      onSamPrompt,
      onCommitMove,
      onCommitResize,
      onCommitPolygonGeometry,
      onCommitKeypointGeometry,
      onJoinSelected,
      onCropSelected,
      onCommitPolygonSlice,
      onStageGeometry,
    } = imageProps ?? ({} as WorkbenchStageHostImageProps);
    const {
      samCandidates,
      samMaskRecords,
      onSelectSamMaskCandidate,
      samActiveIdx,
      samSessionPoints,
      samSessionScribbles,
      samSessionExemplars,
      samSubTool,
      samPolarity,
      onRefineSamCandidate,
    } = aiProps ?? ({} as WorkbenchStageHostAiProps);
    const {
      polygonDraft,
      keypointDraft,
      keypointSchema,
      canvasShapes,
      canvasEditable,
      canvasStroke,
      onCanvasStrokeCommit,
      historicalShapes,
      canUndo,
      canRedo,
      onUndo,
      onRedo,
      onSetCanvasStroke,
      canvasShapeCount,
      onUndoCanvasShape,
      onClearCanvasShapes,
      onCancelCanvasDraft,
      onDoneCanvasDraft,
      stageGeom,
      maskEditor,
      projectRenderingConfig,
      issuePixelFeedbacks,
      highlightIssueId,
      onIssuePinClick,
      issuePinDropArmed,
      issueNavigationPending,
      onIssuePinDrop,
      onSeekIssueFrame,
      annotationCommentCounts,
      onOpenAnnotationComments,
    } = editorProps ?? ({} as WorkbenchStageHostEditorProps);
    return (
      <div className="relative flex min-h-0 flex-1 flex-col" data-workbench-stage>
        {stageKind === "3d" ? (
          <Suspense
            fallback={
              <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
                加载点云查看器…
              </div>
            }
          >
            <ThreeDWorkbench
              taskId={taskId}
              readOnly={readOnly}
              selectedId={selectedId}
              selectedIds={selectedIds}
              onSelectBox={onSelectBox}
              activeClass={activeClass}
              threeDTool={threeDTool}
              onSetThreeDTool={onSetThreeDTool}
              onCrossFramePropagate={onCrossFramePropagate}
              onCrossFramePropagateBatch={onCrossFramePropagateBatch}
              onCrossFramePropagateToTask={onCrossFramePropagateToTask}
              onCrossFrameInterpolate={onCrossFrameInterpolate}
              onNavigateSceneFrame={onNavigateSceneFrame}
              playbackActive={scenePlaybackActive}
              onPlaybackActiveChange={onScenePlaybackActiveChange}
              playbackBlockedReason={scenePlaybackBlockedReason}
              rightSidebarOpen={rightSidebarOpen}
              rightSidebarWidth={rightSidebarWidth}
              triViewFloat={workbenchLayout.triViewFloat}
              cameraPanels={workbenchLayout.cameraPanels}
              pointcloudCamera={workbenchLayout.pointcloudCamera}
              onWorkbenchLayoutChange={onWorkbenchLayoutChange}
              workbenchCommon={workbenchCommon}
              workbenchPointcloud={workbenchPointcloud}
              workbenchConfigLoaded={workbenchConfigLoaded}
              onWorkbenchConfigChange={onWorkbenchConfigChange}
              onWorkbenchConfigUpdate={onWorkbenchConfigUpdate}
              box3dDefaultSize={stageProjectRenderingConfig?.box3dDefaultSize ?? null}
              petDock={petDock}
            />
          </Suspense>
        ) : stageKind === "video" ? (
          <VideoWorkbench
            taskId={taskId}
            overlays={overlays}
            maskCompareStore={maskCompareStore}
            ref={ref}
            manifest={videoManifest}
            frameTimetable={videoFrameTimetable}
            isLoading={videoManifestLoading}
            error={videoManifestError}
            annotations={annotations}
            annotationWritesPending={annotationWritesPending}
            aiBoxes={videoAiBoxes}
            selectedId={selectedId}
            activeClass={activeClass}
            keypointSchema={videoKeypointSchema}
            frameIndex={videoFrameIndex}
            reviewDisplayMode={videoReviewDisplayMode}
            hiddenTrackIds={hiddenVideoTrackIds}
            lockedTrackIds={lockedVideoTrackIds}
            trackColorOverrides={trackColorOverrides}
            selectedIds={selectedIds}
            readOnly={readOnly}
            videoTool={videoTool}
            isVideoToolEnabled={isVideoToolEnabled}
            onSamPrompt={onVideoSamPrompt}
            samCandidates={videoSamCandidates}
            samMaskRecords={videoSamMaskRecords}
            onSelectSamMaskCandidate={onSelectVideoSamMaskCandidate}
            samActiveIdx={videoSamActiveIdx}
            samSessionPoints={videoSamSessionPoints}
            samSessionBoxes={videoSamSessionBoxes}
            maskCandidates={videoMaskCandidates}
            maskEditor={videoMaskEditor}
            maskKeyframeActions={videoMaskKeyframeActions}
            onMaskCommit={onVideoMaskCommit}
            onMaskCancel={onVideoMaskCancel}
            samPolarity={videoSamPolarity}
            spacePan={videoSpacePan}
            onSpacePanDragStart={onVideoSpacePanDragStart}
            pendingDrawing={pendingDrawing}
            chapters={videoChapters}
            timelineChapterControls={videoTimelineChapterControls}
            propagateRange={videoPropagateRange}
            trackerReview={trackerReview}
            reviewReference={reviewReference}
            onSeekReviewFrame={onSeekReviewFrame}
            segmentRange={videoSegmentRange}
            videoSampling={videoSampling}
            performanceTier={workbenchCommon.performanceTier}
            onSelect={onSelectBox}
            onFrameIndexChange={onVideoFrameIndexChange}
            onCreate={onVideoCreate}
            onCreatePointsTrack={onVideoCreatePointsTrack}
            onCreatePoints={onVideoCreatePoints}
            onCreateKeypoints={onVideoCreateKeypoints}
            onPendingDraw={onVideoPendingDraw}
            onUpdate={onVideoUpdate}
            onRename={onVideoRename}
            onChangeUserBoxClass={onChangeUserBoxClass}
            onDeleteUserBox={onDeleteUserBox}
            onConvertToBboxes={onVideoConvertToBboxes}
            onAcceptPrediction={onVideoAcceptPrediction}
            onRejectPrediction={onVideoRejectPrediction}
            onComposeTracks={onVideoComposeTracks}
            onToggleHiddenTrack={onToggleHiddenVideoTrack}
            onToggleLockedTrack={onToggleLockedVideoTrack}
            onPropagateTrack={onPropagateVideoTrack}
            onCursorMove={onCursorMove}
            annotationCommentCounts={annotationCommentCounts}
            onOpenAnnotationComments={onOpenAnnotationComments}
            issuePixelFeedbacks={issuePixelFeedbacks}
            issueHighlightId={highlightIssueId}
            onIssuePinClick={onIssuePinClick}
            issuePinDropArmed={issuePinDropArmed}
            issueNavigationPending={issueNavigationPending}
            onIssuePinDrop={onIssuePinDrop}
            onSeekIssueFrame={onSeekIssueFrame}
          />
        ) : (
          <ImageWorkbench
            continuousCreation={continuousCreation}
            bboxCreationMode={bboxCreationMode}
            resourceCoordinator={resourceCoordinator}
            maskCompareStore={maskCompareStore}
            rasterMaskRecords={rasterMaskRecords}
            predictionRasterMaskRecords={predictionRasterMaskRecords}
            rasterMaskStatusById={rasterMaskStatusById}
            onRetryRasterMask={onRetryRasterMask}
            editingRasterMaskId={editingRasterMaskId}
            maskReadOnly={maskReadOnly}
            readOnly={readOnly}
            fileUrl={fileUrl}
            imageSource={imageSource}
            onRetryImagePyramid={onRetryImagePyramid}
            mediaKey={mediaKey}
            blurhash={blurhash}
            imageWidth={imageWidth}
            imageHeight={imageHeight}
            thumbnailUrl={thumbnailUrl}
            tool={tool}
            activeClass={activeClass}
            selectedId={selectedId}
            selectedIds={selectedIds}
            fadedAiIds={fadedAiIds}
            nudgeMap={nudgeMap}
            pendingGeomMap={pendingGeomMap}
            userBoxes={userBoxes}
            aiBoxes={aiBoxes}
            spacePan={spacePan}
            vp={vp}
            setVp={setVp}
            fitTick={fitTick}
            setFitTick={setFitTick}
            pendingDrawing={pendingDrawing}
            onSelectBox={onSelectBox}
            onAcceptPrediction={onAcceptPrediction}
            onRejectPrediction={onRejectPrediction}
            onDeleteUserBox={onDeleteUserBox}
            onPatchShapeFlag={onPatchShapeFlag}
            secondaryBarHidden={secondaryBarHidden}
            onToggleSecondaryBar={onToggleSecondaryBar}
            clipboardActions={imageClipboardActions}
            onCommitDrawing={onCommitDrawing}
            onCommitRotatedBbox={onCommitRotatedBbox}
            onCommitRotateBbox={onCommitRotateBbox}
            onSamPrompt={onSamPrompt}
            samCandidates={samCandidates}
            samMaskRecords={samMaskRecords}
            onSelectSamMaskCandidate={onSelectSamMaskCandidate}
            samActiveIdx={samActiveIdx}
            samSessionPoints={samSessionPoints}
            samSessionScribbles={samSessionScribbles}
            samSessionExemplars={samSessionExemplars}
            samSubTool={samSubTool}
            samPolarity={samPolarity}
            onCommitMove={onCommitMove}
            onCommitResize={onCommitResize}
            onCommitPolygonGeometry={onCommitPolygonGeometry}
            onCommitKeypointGeometry={onCommitKeypointGeometry}
            onCursorMove={onCursorMove}
            onChangeUserBoxClass={onChangeUserBoxClass}
            onJoinSelected={onJoinSelected}
            onCropSelected={onCropSelected}
            onCommitPolygonSlice={onCommitPolygonSlice}
            onStageGeometry={onStageGeometry}
            polygonDraft={polygonDraft}
            keypointDraft={keypointDraft}
            keypointSchema={keypointSchema}
            canvasShapes={canvasShapes}
            canvasEditable={canvasEditable}
            canvasStroke={canvasStroke}
            onCanvasStrokeCommit={onCanvasStrokeCommit}
            historicalShapes={historicalShapes}
            canUndo={canUndo}
            canRedo={canRedo}
            onUndo={onUndo}
            onRedo={onRedo}
            onSetCanvasStroke={onSetCanvasStroke}
            canvasShapeCount={canvasShapeCount}
            onUndoCanvasShape={onUndoCanvasShape}
            onClearCanvasShapes={onClearCanvasShapes}
            onCancelCanvasDraft={onCancelCanvasDraft}
            onDoneCanvasDraft={onDoneCanvasDraft}
            stageGeom={stageGeom}
            overlays={overlays}
            maskEditor={maskEditor}
            onRefineSamCandidate={onRefineSamCandidate}
            projectRenderingConfig={projectRenderingConfig}
            issuePixelFeedbacks={issuePixelFeedbacks}
            highlightIssueId={highlightIssueId}
            onIssuePinClick={onIssuePinClick}
            issuePinDropArmed={issuePinDropArmed}
            onIssuePinDrop={onIssuePinDrop}
            annotationCommentCounts={annotationCommentCounts}
            onOpenAnnotationComments={onOpenAnnotationComments}
          />
        )}
        {stageKind === "3d" && overlays}
      </div>
    );
  },
);

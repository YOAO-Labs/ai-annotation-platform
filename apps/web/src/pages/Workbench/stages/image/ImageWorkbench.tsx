import type { CommitPolygonSlice } from "../../stage/usePolygonSlice";
import { useEffect, useMemo, useRef, type ReactNode } from "react";
import type { Annotation, Geometry, RotatedBboxGeometry, Keypoint, KeypointSchema } from "@/types";
import type { CommentCanvasDrawing } from "@/api/comments";
import { useWorkbenchConfig } from "../../state/useWorkbenchConfig";
import { clampScale, fitAwareScaleRange } from "../../stage/shared/viewport/zoom";
import { CanvasToolbar } from "../../stage/CanvasToolbar";
import { FloatingDock } from "../../shell/FloatingDock";
import { ImageStage } from "../../stage/ImageStage";
import { Minimap } from "../../stage/Minimap";
import type { AiBox } from "../../state/transforms";
import type { PendingDrawing, SamPolarity, SamSubTool, Tool } from "../../state/useWorkbenchState";
import type { Viewport } from "../../state/useViewportTransform";
import type { KeypointDraftHandle, PolygonDraftHandle } from "../../stage/tools";
import type { UseMaskEditorReturn } from "../../state/useMaskEditor";
import type { ImageContextMenuClipboardActions } from "../../stage/imageStageContextMenu";
import type { RasterMaskRenderRecord } from "../../stage/shared/rasterMaskRender";
import type { RasterMaskRecordStatus } from "../../stage/shared/useRasterMaskRecords";
import type { MaskCompareTileStore } from "../../stage/shared/maskCompareTileStore";
import type { WorkbenchImageSource } from "../../stage/imagePyramid";
import { workbenchImagePreviewUrl } from "../../stage/useWorkbenchImageSource";
import type { RasterResourceCoordinator } from "../../stage/shared/rasterResourceCoordinator";
import type { BboxCreationMode } from "../../stage/ImageStage.helpers";

type Geom = { x: number; y: number; w: number; h: number };
type StageGeometry = { imgW: number; imgH: number; vpSize: { w: number; h: number } };

export interface ImageWorkbenchProps {
  bboxCreationMode?: BboxCreationMode;
  continuousCreation?: boolean;
  resourceCoordinator?: RasterResourceCoordinator;
  maskCompareStore?: MaskCompareTileStore | null;
  rasterMaskRecords: readonly RasterMaskRenderRecord<"annotation">[];
  predictionRasterMaskRecords?: readonly RasterMaskRenderRecord<"prediction">[];
  rasterMaskStatusById: ReadonlyMap<string, RasterMaskRecordStatus>;
  onRetryRasterMask: (id: string) => void;
  editingRasterMaskId?: string | null;
  maskReadOnly?: boolean;
  readOnly: boolean;
  fileUrl: string | null;
  imageSource?: WorkbenchImageSource | null;
  onRetryImagePyramid?: () => Promise<void>;
  mediaKey?: string | null;
  blurhash?: string | null;
  imageWidth?: number | null;
  imageHeight?: number | null;
  thumbnailUrl: string | null;
  tool: Tool;
  activeClass: string;
  selectedId: string | null;
  selectedIds: string[];
  fadedAiIds: Set<string>;
  nudgeMap: Map<string, Geom>;
  /** v0.20.22 · 提交在途几何 override, 见 usePendingGeom (防松手闪回原尺寸)。 */
  pendingGeomMap: Map<string, Geometry>;
  userBoxes: Annotation[];
  aiBoxes: AiBox[];
  spacePan: boolean;
  vp: Viewport;
  setVp: React.Dispatch<React.SetStateAction<Viewport>>;
  fitTick: number;
  setFitTick: React.Dispatch<React.SetStateAction<number>>;
  pendingDrawing: PendingDrawing;
  onSelectBox: (id: string | null, opts?: { shift?: boolean }) => void;
  onAcceptPrediction: (b: AiBox) => void;
  onRejectPrediction: (b: AiBox) => void;
  onDeleteUserBox: (id: string) => void;
  onPatchShapeFlag?: (
    id: string,
    flag: "z_order" | "is_locked" | "is_hidden",
    value: number | boolean,
  ) => void;
  secondaryBarHidden?: boolean;
  onToggleSecondaryBar?: () => void;
  clipboardActions?: ImageContextMenuClipboardActions | null;
  onCommitDrawing: (geo: Geom) => void;
  /** v0.10.28 · 旋转框: 拖出矩形 → 提交 angle=0 的 rotated_bbox。 */
  onCommitRotatedBbox: (geo: Geom) => void;
  /** v0.10.28 · 旋转框: 旋转手柄落定 → 更新 angle。 */
  onCommitRotateBbox: (id: string, before: RotatedBboxGeometry, after: RotatedBboxGeometry) => void;
  onSamPrompt: (
    prompt:
      | { kind: "point"; pt: [number, number]; alt: boolean }
      | { kind: "bbox"; bbox: [number, number, number, number] }
      | { kind: "scribble"; points: [number, number][]; alt: boolean; width: number }
      | { kind: "exemplar"; bbox: [number, number, number, number]; alt: boolean },
  ) => void;
  samCandidates: {
    id: string;
    type: "polygonlabels" | "rectanglelabels";
    points?: [number, number][];
    bbox?: { x: number; y: number; width: number; height: number };
  }[];
  samMaskRecords: readonly RasterMaskRenderRecord<"interactive">[];
  onSelectSamMaskCandidate: (candidateId: string) => void;
  samActiveIdx: number;
  /** v0.18.18 · §5.5 当前点会话已落的正/负点, 透传画布 overlay。 */
  samSessionPoints: { pt: [number, number]; polarity: 1 | 0 }[];
  samSessionScribbles: {
    points: [number, number][];
    polarity: 1 | 0;
    width: number;
  }[];
  /** v0.18.19 · exemplar refine 会话已落的正/负框, 透传画布 overlay。 */
  samSessionExemplars: { bbox: [number, number, number, number]; polarity: 1 | 0 }[];
  /** v0.10.2 · 派生自 tool, 非 AI 工具时为 null. */
  samSubTool: SamSubTool | null;
  samPolarity: SamPolarity;
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
  onCommitKeypointGeometry?: (id: string, before: Keypoint[], after: Keypoint[]) => void;
  onCursorMove: (pt: { x: number; y: number } | null) => void;
  onChangeUserBoxClass: (id: string) => void;
  onJoinSelected: () => void;
  onCropSelected: (baseId: string) => void;
  onCommitPolygonSlice?: CommitPolygonSlice;
  onStageGeometry: (g: StageGeometry) => void;
  polygonDraft?: PolygonDraftHandle;
  keypointDraft?: KeypointDraftHandle;
  keypointSchema?: KeypointSchema | null;
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
  overlays?: ReactNode;
  /** v0.10.8 · I11 · Mask 编辑器状态；传给 ImageStage 用于 Konva overlay 与 paintAt。 */
  maskEditor?: UseMaskEditorReturn;
  /** v0.10.9 · SAM 候选「精修」入口（画布浮按钮 + R 键）。 */
  onRefineSamCandidate?: (idx: number) => void;
  /** v0.10.10 · I17.3 · 项目级 rendering_config 覆盖；透传给 ImageStage 内的 useWorkbenchConfig。 */
  projectRenderingConfig?: import("@/api/projects").ProjectRenderingConfig | null;
  /** v0.10.20 · I18 · pixel-anchored issue feedback 数据源 (Shell 通过 useFeedbacks 提供). */
  issuePixelFeedbacks?: import("@/api/feedbacks").AnnotationFeedback[];
  highlightIssueId?: string | null;
  onIssuePinClick?: (id: string) => void;
  issuePinDropArmed?: boolean;
  onIssuePinDrop?: (x: number, y: number) => void;
  /** Per-annotation saved comment totals for the image canvas. */
  annotationCommentCounts?: Record<string, number>;
  /** Open an annotation's comment list through the guarded Workbench shell. */
  onOpenAnnotationComments?: (annotationId: string) => void;
}

export function ImageWorkbench({
  continuousCreation,
  bboxCreationMode,
  resourceCoordinator,
  maskCompareStore,
  rasterMaskRecords,
  predictionRasterMaskRecords,
  rasterMaskStatusById,
  onRetryRasterMask,
  editingRasterMaskId,
  maskReadOnly,
  readOnly,
  fileUrl,
  imageSource,
  onRetryImagePyramid,
  mediaKey,
  blurhash,
  imageWidth,
  imageHeight,
  thumbnailUrl,
  tool,
  activeClass,
  selectedId,
  selectedIds,
  fadedAiIds,
  nudgeMap,
  pendingGeomMap,
  userBoxes,
  aiBoxes,
  spacePan,
  vp,
  setVp,
  fitTick,
  setFitTick,
  pendingDrawing,
  onSelectBox,
  onAcceptPrediction,
  onRejectPrediction,
  onDeleteUserBox,
  onPatchShapeFlag,
  secondaryBarHidden,
  onToggleSecondaryBar,
  clipboardActions,
  onCommitDrawing,
  onCommitRotatedBbox,
  onCommitRotateBbox,
  onSamPrompt,
  samCandidates,
  samMaskRecords,
  onSelectSamMaskCandidate,
  samActiveIdx,
  samSessionPoints,
  samSessionScribbles,
  samSessionExemplars,
  samSubTool,
  samPolarity,
  onCommitMove,
  onCommitResize,
  onCommitPolygonGeometry,
  onCommitKeypointGeometry,
  onCursorMove,
  onChangeUserBoxClass,
  onJoinSelected,
  onCropSelected,
  onCommitPolygonSlice,
  onStageGeometry,
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
  overlays,
  maskEditor,
  onRefineSamCandidate,
  projectRenderingConfig,
  issuePixelFeedbacks,
  highlightIssueId,
  onIssuePinClick,
  issuePinDropArmed,
  onIssuePinDrop,
  annotationCommentCounts,
  onOpenAnnotationComments,
}: ImageWorkbenchProps) {
  const imageScaleRange = useMemo(
    () =>
      fitAwareScaleRange(stageGeom.vpSize.w, stageGeom.vpSize.h, stageGeom.imgW, stageGeom.imgH),
    [stageGeom.imgH, stageGeom.imgW, stageGeom.vpSize.h, stageGeom.vpSize.w],
  );
  const displayedRasterMaskRecords = useMemo(() => {
    const hiddenIds = new Set(
      userBoxes.filter((annotation) => annotation.is_hidden).map((annotation) => annotation.id),
    );
    return rasterMaskRecords
      .filter((record) => !hiddenIds.has(record.id) && record.id !== editingRasterMaskId)
      .sort((left, right) => left.zOrder - right.zOrder);
  }, [editingRasterMaskId, rasterMaskRecords, userBoxes]);
  const editingRasterMaskRecord = useMemo(
    () =>
      editingRasterMaskId
        ? (rasterMaskRecords.find((record) => record.id === editingRasterMaskId) ?? null)
        : null,
    [editingRasterMaskId, rasterMaskRecords],
  );
  // v0.21.11 · 图片焦点联动: 选中对象(键盘两级循环 / 点选)若出视口或过小则平移居中 + 适度放大。
  // 与视频同构, 由 common.focusSelectionEnabled gate; 默认关。用 ref 读最新盒集/几何,
  // effect 只在 selectedId 变化时跑(避免盒集逐次变身份触发重排)。
  const { config: focusConfig } = useWorkbenchConfig();
  const focusSelectionEnabled = focusConfig.common.focusSelectionEnabled;
  const focusStateRef = useRef({
    aiBoxes,
    userBoxes,
    stageGeom,
    setVp,
    rasterMaskStatusById,
  });
  focusStateRef.current = {
    aiBoxes,
    userBoxes,
    stageGeom,
    setVp,
    rasterMaskStatusById,
  };
  const selectedRasterMaskStatus = selectedId ? rasterMaskStatusById.get(selectedId) : undefined;
  const selectedRasterMaskFocusRevision =
    selectedRasterMaskStatus?.state === "ready"
      ? selectedRasterMaskStatus.cacheKey
      : (selectedRasterMaskStatus?.state ?? null);
  useEffect(() => {
    if (!focusSelectionEnabled || !selectedId) return;
    const {
      aiBoxes: ai,
      userBoxes: users,
      stageGeom: geom,
      setVp: setViewport,
      rasterMaskStatusById,
    } = focusStateRef.current;
    const { imgW, imgH, vpSize } = geom;
    if (!imgW || !imgH || !vpSize.w || !vpSize.h) return;
    const box = ai.find((b) => b.id === selectedId) ?? users.find((b) => b.id === selectedId);
    if (!box) return;
    const maskStatus = rasterMaskStatusById.get(box.id);
    if (box.geometry?.type === "raster_mask" && maskStatus?.state !== "ready") return;
    const bounds = maskStatus?.state === "ready" ? maskStatus.bounds : box;
    const cx = (bounds.x + bounds.w / 2) * imgW;
    const cy = (bounds.y + bounds.h / 2) * imgH;
    const objMaxDimPx = Math.max(bounds.w * imgW, bounds.h * imgH, 1);
    setViewport((cur) => {
      let scale = cur.scale;
      if (objMaxDimPx * scale < 48) scale = clampScale(140 / objMaxDimPx);
      const margin = 48;
      const screenCx = cx * scale + cur.tx;
      const screenCy = cy * scale + cur.ty;
      const outOfView =
        screenCx < margin ||
        screenCx > vpSize.w - margin ||
        screenCy < margin ||
        screenCy > vpSize.h - margin;
      if (!outOfView && scale === cur.scale) return cur;
      return { scale, tx: vpSize.w / 2 - cx * scale, ty: vpSize.h / 2 - cy * scale };
    });
  }, [focusSelectionEnabled, selectedId, selectedRasterMaskFocusRevision]);

  return (
    <ImageStage
      continuousCreation={continuousCreation}
      bboxCreationMode={bboxCreationMode}
      key={mediaKey ?? fileUrl ?? "image-stage"}
      resourceCoordinator={resourceCoordinator}
      maskCompareStore={maskCompareStore}
      rasterMaskRecords={displayedRasterMaskRecords}
      predictionRasterMaskRecords={predictionRasterMaskRecords}
      tiledMaskOverviewRecord={editingRasterMaskRecord}
      rasterMaskStatusById={rasterMaskStatusById}
      onRetryRasterMask={onRetryRasterMask}
      maskReadOnly={maskReadOnly || !!maskEditor?.tiledReadOnly}
      readOnly={readOnly}
      fileUrl={fileUrl}
      imageSource={imageSource}
      onRetryImagePyramid={onRetryImagePyramid}
      mediaKey={mediaKey}
      blurhash={blurhash}
      imageWidth={imageWidth}
      imageHeight={imageHeight}
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
      pendingDrawing={pendingDrawing}
      onSelectBox={onSelectBox}
      onAcceptPrediction={onAcceptPrediction}
      onRejectPrediction={onRejectPrediction}
      onDeleteUserBox={onDeleteUserBox}
      secondaryBarHidden={secondaryBarHidden}
      onToggleSecondaryBar={onToggleSecondaryBar}
      onCommitDrawing={onCommitDrawing}
      onPatchShapeFlag={onPatchShapeFlag}
      clipboardActions={clipboardActions}
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
      overlay={
        <>
          <FloatingDock
            scale={vp.scale}
            canUndo={canUndo}
            canRedo={canRedo}
            onUndo={onUndo}
            onRedo={onRedo}
            onZoomIn={() =>
              setVp((cur) => ({
                ...cur,
                scale: clampScale(cur.scale * 1.2, imageScaleRange),
              }))
            }
            onZoomOut={() =>
              setVp((cur) => ({
                ...cur,
                scale: clampScale(cur.scale / 1.2, imageScaleRange),
              }))
            }
            onFit={() => setFitTick((n) => n + 1)}
          />
          {canvasEditable && (
            <CanvasToolbar
              stroke={canvasStroke}
              onSetStroke={onSetCanvasStroke}
              shapeCount={canvasShapeCount}
              onUndo={onUndoCanvasShape}
              onClear={onClearCanvasShapes}
              onCancel={onCancelCanvasDraft}
              onDone={onDoneCanvasDraft}
            />
          )}
          {stageGeom.imgW > 0 && stageGeom.vpSize.w > 0 && (
            <Minimap
              imgW={stageGeom.imgW}
              imgH={stageGeom.imgH}
              vpSize={stageGeom.vpSize}
              vp={vp}
              setVp={setVp}
              thumbnailUrl={workbenchImagePreviewUrl(imageSource ?? null) ?? thumbnailUrl}
              fileUrl={
                imageSource?.kind === "single" ? imageSource.url : imageSource ? null : fileUrl
              }
              bottom={64}
            />
          )}
          {overlays}
        </>
      }
    />
  );
}

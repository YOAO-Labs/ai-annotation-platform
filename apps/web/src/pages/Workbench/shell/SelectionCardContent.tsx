import type { ComponentProps, ReactNode } from "react";
import type { Annotation, AnnotationResponse } from "@/types";
import { confirmDialog } from "@/components/ui/decisionDialog";
import {
  isAnyVideoSingleFrame,
  isVideoMask,
  isVideoMaskTrack,
  isVideoPointsTrack,
  isVideoTrack,
} from "../stage/videoStageGeometry";
import { trackRangesOverlap } from "../stage/VideoTrackSidebar";
import type { VideoTrackGapMode } from "../stage/VideoTrackComposeDialog";
import type { StageKind } from "../stages/types";
import { AIPredictionCardContent } from "./selectionCard/AIPredictionCardContent";
import { ImageBatchCardContent } from "./ImageBatchCardContent";
import { ImageSelectionCardContent } from "./ImageSelectionCardContent";
import { VideoBoxBatchCardContent } from "./VideoBoxBatchCardContent";
import { VideoTrackBatchCardContent } from "./VideoTrackBatchCardContent";
import { SelectionCardPlaceholder } from "./SelectedAnnotationCard";
import { VideoFrameBoxCardContent } from "./selectionCard/VideoFrameBoxCardContent";
import { VideoPointsTrackCardContent } from "./selectionCard/VideoPointsTrackCardContent";
import type { VideoMaskKeyframeActionHandlers } from "../stage/videoMaskKeyframeActions";
import type { VideoTrackAnnotation } from "../stage/videoStageTypes";
import { ConversionBatchCardContent } from "./selectionCard/ConversionBatchCardContent";

type AiBoxProps = ComponentProps<typeof AIPredictionCardContent>;
type ImageBatchProps = ComponentProps<typeof ImageBatchCardContent>;
type ImageSelectionProps = ComponentProps<typeof ImageSelectionCardContent>;
type VideoTrackBatchProps = ComponentProps<typeof VideoTrackBatchCardContent>;

export interface SelectionCardContentProps {
  stageKind: StageKind;
  isLocked: boolean;
  multi: boolean;
  count: number;
  ann: AnnotationResponse | null;
  selectedAiBox: AiBoxProps["box"] | null;
  selectedIds: string[];
  imageWidth: number | null;
  imageHeight: number | null;
  videoFps: number | null;
  videoFrameIndex: number;
  setVideoFrameIndex: (frame: number) => void;
  attributeSchema?: ImageSelectionProps["attributeSchema"];
  /** 图片原生 Mask 持久化模式；native 才开放编辑/区域转 Mask 入口。 */
  imageMaskPersistenceMode: "native" | "legacy" | "blocked";
  userBoxes: Annotation[];
  visibleAnnotations: AnnotationResponse[];
  videoBatchTracks: VideoTrackAnnotation[];
  /** 活跃标注引用（装配层持有）；删除确认 await 之后必须迟到读取，过滤并发已删轨迹。 */
  annotationsSnapshotRef: { readonly current: readonly AnnotationResponse[] };
  classes: VideoTrackBatchProps["classes"];
  hiddenVideoTrackIds: ReadonlySet<string>;
  lockedVideoTrackIds: ReadonlySet<string>;
  rasterMaskStatus: ImageSelectionProps["rasterMaskStatus"];
  rasterMaskRetry: ImageSelectionProps["onRetryRasterMask"];
  videoMaskKeyframeActions?: VideoMaskKeyframeActionHandlers;
  renderTrackCard: ReactNode;
  // ---- 命令（全部来自既有领域 owner / 装配层） ----
  setSelectedId: (id: string | null) => void;
  handleSelectBox: (id: string | null) => void;
  requestVideoTool: (tool: "mask" | "mask-track") => void;
  onStartBatchChangeClass: ImageBatchProps["onChangeClass"];
  onJoinSelectedPolygons: ImageBatchProps["onJoin"];
  onBatchPatchFlag: (flag: "is_locked" | "is_hidden") => void;
  /** 选中集批量删除（组件零参调用；选区归装配层）。 */
  onBatchDelete: () => void;
  /** 轨迹批量删除（显式传入轨迹列表）。 */
  onVideoBatchDelete: (tracks: VideoTrackAnnotation[]) => void;
  onBatchTrack: () => void;
  onComposeTracks: (
    op:
      | { operation: "aggregate_bboxes"; annotationIds: string[]; deleteSources: boolean }
      | { operation: "merge_tracks"; annotationIds: string[] }
      | { operation: "join_tracks"; annotationIds: string[]; gapMode: VideoTrackGapMode },
  ) => void;
  onVideoBatchRename: (tracks: AnnotationResponse[], className: string) => void;
  onStartChangeClass: (annotationId: string) => void;
  onDeleteBox: (id: string) => void;
  onUpdateAttributes: (annotationId: string, attributes: Record<string, unknown>) => void;
  onPatchShapeFlag: (
    annotationId: string,
    flag: "z_order" | "is_locked" | "is_hidden",
    value: number | boolean,
  ) => void;
  acceptPrediction: NonNullable<AiBoxProps["onAccept"]>;
  rejectPrediction: NonNullable<AiBoxProps["onReject"]>;
  refinePrediction: NonNullable<AiBoxProps["onRefine"]>;
  openAnnotationConversion: (ids: string | string[]) => void;
  enterImageRasterMaskEdit: (annotationId: string) => void;
  toggleHiddenVideoTrack: (trackId: string) => void;
  toggleLockedVideoTrack: (trackId: string) => void;
  openPropagateDialog: (source: AnnotationResponse | AnnotationResponse[]) => void;
}

/**
 * 选中浮卡的内容分派（§4.4(4)）：按 stage / 多选 / 几何族渲染对应内容组件，
 * 并持有内容层的本地判定——批量转换资格（原生 Mask 持久化权限）、视频轨迹
 * 合并/拼接资格、批量显隐锁定的「全部选中才翻转」语义、轨迹删除前确认。
 * 浮卡窗口位置与折叠偏好仍归装配层（SelectedAnnotationCardProps 外壳）。
 */
export function SelectionCardContent(props: SelectionCardContentProps): ReactNode {
  const {
    stageKind,
    isLocked,
    multi,
    count,
    ann,
    selectedAiBox,
    selectedIds,
    imageWidth,
    imageHeight,
    videoFps,
    videoFrameIndex,
    setVideoFrameIndex,
    attributeSchema,
    imageMaskPersistenceMode,
    userBoxes,
    visibleAnnotations,
    videoBatchTracks,
    annotationsSnapshotRef,
    classes,
    hiddenVideoTrackIds,
    lockedVideoTrackIds,
    rasterMaskStatus,
    rasterMaskRetry,
    videoMaskKeyframeActions,
    renderTrackCard,
    setSelectedId,
    handleSelectBox,
    requestVideoTool,
    onStartBatchChangeClass,
    onJoinSelectedPolygons,
    onBatchPatchFlag,
    onBatchDelete,
    onComposeTracks,
    onVideoBatchRename,
    onStartChangeClass,
    onDeleteBox,
    onUpdateAttributes,
    onPatchShapeFlag,
    acceptPrediction,
    rejectPrediction,
    refinePrediction,
    openAnnotationConversion,
    enterImageRasterMaskEdit,
    toggleHiddenVideoTrack,
    toggleLockedVideoTrack,
    openPropagateDialog,
  } = props;

  if (multi && stageKind === "image") {
    // 图片多选:批量操作(改类 / 合并 / 锁定 / 隐藏 / 删除)收进浮卡,取代退役的贴框浮条。
    const selectedAnns = userBoxes.filter((b) => selectedIds.includes(b.id));
    const allLocked = selectedAnns.length > 0 && selectedAnns.every((a) => a.is_locked);
    const allHidden = selectedAnns.length > 0 && selectedAnns.every((a) => a.is_hidden);
    const conversionSourceType = selectedAnns[0]?.geometry?.type;
    const canBatchConvert = Boolean(
      conversionSourceType &&
      selectedAnns.every((item) => item.geometry?.type === conversionSourceType) &&
      ["polygon", "multi_polygon", "raster_mask"].includes(conversionSourceType) &&
      (conversionSourceType === "raster_mask" || imageMaskPersistenceMode === "native") &&
      !selectedAnns.some((item) => item.is_locked),
    );
    return (
      <ImageBatchCardContent
        count={count}
        readOnly={isLocked}
        allLocked={allLocked}
        allHidden={allHidden}
        onChangeClass={onStartBatchChangeClass}
        onJoin={onJoinSelectedPolygons}
        onToggleLock={() => onBatchPatchFlag("is_locked")}
        onToggleHidden={() => onBatchPatchFlag("is_hidden")}
        onDelete={onBatchDelete}
        onClear={() => setSelectedId(null)}
        onConvert={canBatchConvert ? () => openAnnotationConversion(selectedIds) : undefined}
      />
    );
  }
  if (multi && stageKind === "video") {
    // 视频多选:单帧框(video_bbox)走 selectedIds,给批量卡(改类 / 锁 / 隐藏 / 删除 + 聚合为轨迹);
    // 轨迹多选走右栏 roster 的 selectedTrackIds,不进 selectedIds,浮卡保持精简占位。
    const selectedAnns = visibleAnnotations.filter((a) => selectedIds.includes(a.id));
    const allVideoBbox =
      selectedAnns.length > 0 && selectedAnns.every((a) => a.geometry.type === "video_bbox");
    if (allVideoBbox) {
      const allLocked = selectedAnns.every((a) => a.is_locked);
      const allHidden = selectedAnns.every((a) => a.is_hidden);
      return (
        <VideoBoxBatchCardContent
          count={count}
          readOnly={isLocked}
          allLocked={allLocked}
          allHidden={allHidden}
          onChangeClass={onStartBatchChangeClass}
          onToggleLock={() => onBatchPatchFlag("is_locked")}
          onToggleHidden={() => onBatchPatchFlag("is_hidden")}
          onDelete={props.onBatchDelete}
          onAggregate={() =>
            onComposeTracks({
              operation: "aggregate_bboxes",
              annotationIds: selectedIds,
              deleteSources: true,
            })
          }
          onClear={() => setSelectedId(null)}
        />
      );
    }
    const sourceType = selectedAnns[0]?.geometry.type;
    const sameConvertibleSource = Boolean(
      sourceType &&
      ["video_polygon", "video_track_polygon", "video_track_mask"].includes(sourceType) &&
      selectedAnns.every((item) => item.geometry.type === sourceType) &&
      !selectedAnns.some((item) => item.is_locked),
    );
    if (sameConvertibleSource) {
      return (
        <ConversionBatchCardContent
          count={count}
          sourceType={sourceType}
          readOnly={isLocked}
          onConvert={() => openAnnotationConversion(selectedIds)}
          onClear={() => setSelectedId(null)}
        />
      );
    }
    return <SelectionCardPlaceholder summary={`已选中 ${count} 个标注。`} />;
  }
  if (multi) {
    return <SelectionCardPlaceholder summary={`已选中 ${count} 个标注。`} />;
  }
  if (selectedAiBox) {
    // AI 预测分支(图片端专属):置信度条 + 来源/候选序号 + 采纳/精修/忽略,直连模型既有 handler。
    return (
      <AIPredictionCardContent
        box={selectedAiBox}
        imageWidth={imageWidth}
        imageHeight={imageHeight}
        attributeSchema={attributeSchema}
        readOnly={isLocked}
        onAccept={acceptPrediction}
        onReject={rejectPrediction}
        onRefine={refinePrediction}
      />
    );
  }
  if (stageKind === "video") {
    if (ann && isAnyVideoSingleFrame(ann)) {
      // 视频单帧标注 (bbox / polygon / polyline / rotated_bbox):不属任何轨迹、会被轨迹面板
      // 过滤掉,改用专属单帧卡(帧定位 + 指标 + 属性)。
      return (
        <VideoFrameBoxCardContent
          annotation={ann}
          imageWidth={imageWidth}
          imageHeight={imageHeight}
          fps={videoFps}
          attributeSchema={attributeSchema}
          readOnly={isLocked}
          onSeekFrame={setVideoFrameIndex}
          onChangeClass={onStartChangeClass}
          onDelete={onDeleteBox}
          onUpdateAttributes={onUpdateAttributes}
          onConvert={ann.geometry.type === "video_polygon" ? openAnnotationConversion : undefined}
          onEditMask={isVideoMask(ann) ? () => requestVideoTool("mask") : undefined}
        />
      );
    }
    if (ann && (isVideoPointsTrack(ann) || isVideoMaskTrack(ann))) {
      // 点集轨迹 (polygon / polyline track):简化卡(指标 + 改类 / 显隐 / 锁 / 删整条)。
      return (
        <VideoPointsTrackCardContent
          annotation={ann}
          frameIndex={videoFrameIndex}
          imageWidth={imageWidth}
          imageHeight={imageHeight}
          fps={videoFps}
          readOnly={isLocked}
          hidden={hiddenVideoTrackIds.has(ann.geometry.track_id)}
          locked={lockedVideoTrackIds.has(ann.geometry.track_id)}
          onSeekFrame={setVideoFrameIndex}
          onChangeClass={onStartChangeClass}
          onDelete={onDeleteBox}
          onToggleHidden={toggleHiddenVideoTrack}
          onToggleLock={toggleLockedVideoTrack}
          onEditMask={isVideoMaskTrack(ann) ? () => requestVideoTool("mask-track") : undefined}
          onPropagate={isVideoMaskTrack(ann) ? () => openPropagateDialog(ann) : undefined}
          onConvert={
            ann.geometry.type === "video_track_polygon" || isVideoMaskTrack(ann)
              ? openAnnotationConversion
              : undefined
          }
          maskActions={isVideoMaskTrack(ann) ? videoMaskKeyframeActions : undefined}
        />
      );
    }
    if (videoBatchTracks.length >= 2) {
      // 多选 ≥2 条轨迹 → 浮卡渲染批量卡 (与右栏 roster 批量条对等)。
      const ids = videoBatchTracks.map((t) => t.id);
      const sameClass =
        videoBatchTracks.length === 2 &&
        videoBatchTracks[0].class_name === videoBatchTracks[1].class_name;
      const canMerge = sameClass;
      const canJoin = sameClass && !trackRangesOverlap(videoBatchTracks[0], videoBatchTracks[1]);
      const countHint = `需恰好选中 2 条轨迹（当前 ${videoBatchTracks.length} 条）`;
      const mergeReason = canMerge
        ? null
        : videoBatchTracks.length !== 2
          ? countHint
          : "两条轨迹需同类";
      const joinReason = canJoin
        ? null
        : videoBatchTracks.length !== 2
          ? countHint
          : !sameClass
            ? "两条轨迹需同类"
            : "两条轨迹的可见帧区间不能重叠";
      const setBatchHidden = (hidden: boolean) =>
        videoBatchTracks.forEach((t) => {
          if (hiddenVideoTrackIds.has(t.geometry.track_id) !== hidden)
            toggleHiddenVideoTrack(t.geometry.track_id);
        });
      const setBatchLocked = (locked: boolean) =>
        videoBatchTracks.forEach((t) => {
          if (lockedVideoTrackIds.has(t.geometry.track_id) !== locked)
            toggleLockedVideoTrack(t.geometry.track_id);
        });
      // 全选中才算「已隐藏 / 已锁定」→ 切换按钮翻转为反向动作; 部分选中时仍显示正向动作(与图片侧一致)。
      const allTracksHidden = videoBatchTracks.every((t) =>
        hiddenVideoTrackIds.has(t.geometry.track_id),
      );
      const allTracksLocked = videoBatchTracks.every((t) =>
        lockedVideoTrackIds.has(t.geometry.track_id),
      );
      return (
        <VideoTrackBatchCardContent
          count={videoBatchTracks.length}
          readOnly={isLocked}
          classes={classes}
          canMerge={canMerge}
          canJoin={canJoin}
          mergeDisabledReason={mergeReason}
          joinDisabledReason={joinReason}
          allHidden={allTracksHidden}
          allLocked={allTracksLocked}
          onChangeClass={(cls) => onVideoBatchRename(videoBatchTracks, cls)}
          onBatchTrack={isLocked ? undefined : props.onBatchTrack}
          onToggleHidden={() => setBatchHidden(!allTracksHidden)}
          onToggleLock={() => setBatchLocked(!allTracksLocked)}
          onMerge={() => onComposeTracks({ operation: "merge_tracks", annotationIds: ids })}
          onJoin={(gapMode: VideoTrackGapMode) =>
            onComposeTracks({ operation: "join_tracks", annotationIds: ids, gapMode })
          }
          onDelete={() => {
            void (async () => {
              const confirmed = await confirmDialog({
                tone: "danger",
                title: `删除 ${videoBatchTracks.length} 条轨迹？`,
                confirmLabel: "删除",
              });
              if (!confirmed) return;
              // 等待决定期间部分轨迹可能已被删除;确认后迟到读取活跃标注，
              // 过滤后仍非空才提交 (删除已消失项只会报错)。
              const stillPresent = videoBatchTracks.filter((t) =>
                annotationsSnapshotRef.current.some((item) => item.id === t.id),
              );
              if (stillPresent.length > 0) props.onVideoBatchDelete(stillPresent);
            })();
          }}
          onClear={() => handleSelectBox(null)}
        />
      );
    }
    if (ann && isVideoTrack(ann)) {
      // 视频 bbox 轨迹:单轨迹两层信息卡由轨迹清单构建器渲染。
      return renderTrackCard;
    }
    // 兜底:未被上面任何分支覆盖的视频几何也给占位摘要 (类别 + type)。
    return (
      <SelectionCardPlaceholder
        summary={ann ? `类别 ${ann.class_name} · ${ann.geometry.type}` : "已选中 1 个标注。"}
      />
    );
  }
  if (ann && stageKind === "image") {
    return (
      <ImageSelectionCardContent
        annotation={ann}
        imageWidth={imageWidth}
        imageHeight={imageHeight}
        attributeSchema={attributeSchema}
        readOnly={isLocked}
        onChangeClass={onStartChangeClass}
        onToggleFlag={onPatchShapeFlag}
        onDelete={onDeleteBox}
        onUpdateAttributes={onUpdateAttributes}
        rasterMaskStatus={rasterMaskStatus}
        onRetryRasterMask={rasterMaskRetry}
        onEditRasterMask={
          imageMaskPersistenceMode === "native" ? () => enterImageRasterMaskEdit(ann.id) : undefined
        }
        onConvertRegionToRaster={
          imageMaskPersistenceMode === "native" ? openAnnotationConversion : undefined
        }
        onConvertRasterToRegion={openAnnotationConversion}
      />
    );
  }
  return (
    <SelectionCardPlaceholder
      summary={ann ? `类别 ${ann.class_name} · ${ann.geometry.type}` : "已选中 1 个标注。"}
    />
  );
}

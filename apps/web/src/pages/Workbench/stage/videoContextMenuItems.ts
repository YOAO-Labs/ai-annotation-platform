import type { AnnotationResponse } from "@/types";
import type { DropdownItem } from "@/components/ui/DropdownMenu";
import {
  isVideoBbox,
  isVideoMask,
  isVideoMaskTrack,
  isVideoPointsSingleFrame,
  isVideoPointsTrack,
  isVideoTrack,
} from "./videoStageGeometry";
import type { VideoTrackActions } from "./useVideoTrackActions";
import type { VideoMaskKeyframeActionHandlers } from "./videoMaskKeyframeActions";
import { isFrameOutside } from "./videoTrackOutside";
import type {
  VideoTrackAnnotation,
  VideoTrackCompositionOptions,
  VideoTrackConversionOptions,
} from "./videoStageTypes";

export interface VideoContextMenuCtx {
  contextMenuAnnotation: AnnotationResponse | null;
  selectedAnnotation: AnnotationResponse | null;
  contextMenuTargetId: string | null;
  selectedVideoBboxes: AnnotationResponse[];
  readOnly: boolean;
  frameIndex: number;
  trackActions: VideoTrackActions;
  canDeleteSelectedTrackKeyframe: boolean;
  deleteSelectedTrackKeyframe: () => boolean;
  onChangeUserBoxClass?: (id: string) => void;
  onComposeTracks?: (options: VideoTrackCompositionOptions) => void;
  onConvertToBboxes?: (
    annotation: AnnotationResponse,
    options: VideoTrackConversionOptions,
  ) => void;
  onDelete?: (annotation: AnnotationResponse) => void;
  onPropagateTrack?: (annotation: VideoTrackAnnotation) => void;
  onToggleHiddenTrack?: (trackId: string) => void;
  onToggleLockedTrack?: (trackId: string) => void;
  hiddenTrackIds?: Set<string>;
  lockedTrackIds?: Set<string>;
  maskKeyframeActions?: VideoMaskKeyframeActionHandlers;
}

export function buildVideoContextMenuItems(ctx: VideoContextMenuCtx): DropdownItem[] {
  const {
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
  } = ctx;

  if (contextMenuAnnotation && isVideoBbox(contextMenuAnnotation)) {
    const aggregateTargets = selectedVideoBboxes.some((ann) => ann.id === contextMenuAnnotation.id)
      ? selectedVideoBboxes
      : [];
    const sameClass = new Set(aggregateTargets.map((ann) => ann.class_name)).size <= 1;
    const uniqueFrames =
      new Set(aggregateTargets.map((ann) => (isVideoBbox(ann) ? ann.geometry.frame_index : -1)))
        .size === aggregateTargets.length;
    const canAggregate = aggregateTargets.length > 1 && sameClass && uniqueFrames;
    return [
      {
        id: "bbox-class",
        label: "改类别",
        icon: "tag",
        disabled: readOnly || !onChangeUserBoxClass,
        onSelect: () => onChangeUserBoxClass?.(contextMenuAnnotation.id),
      },
      ...(aggregateTargets.length > 1
        ? [
            { id: "bbox-divider", divider: true, label: "" } as DropdownItem,
            {
              id: "bbox-aggregate",
              label: "聚合为轨迹",
              disabled: readOnly || !onComposeTracks || !canAggregate,
              onSelect: () =>
                onComposeTracks?.({
                  operation: "aggregate_bboxes",
                  annotationIds: aggregateTargets.map((ann) => ann.id),
                  deleteSources: true,
                }),
            } satisfies DropdownItem,
          ]
        : []),
      { id: "bbox-delete-divider", divider: true, label: "" },
      {
        id: "bbox-delete",
        label: "删除",
        icon: "trash",
        kbd: "Del",
        disabled: readOnly || !onDelete,
        onSelect: () => onDelete?.(contextMenuAnnotation),
      },
    ];
  }

  if (
    contextMenuAnnotation &&
    (isVideoPointsSingleFrame(contextMenuAnnotation) || isVideoMask(contextMenuAnnotation))
  ) {
    return [
      {
        id: "points-class",
        label: "改类别",
        icon: "tag",
        disabled: readOnly || !onChangeUserBoxClass,
        onSelect: () => onChangeUserBoxClass?.(contextMenuAnnotation.id),
      },
      { id: "points-delete-divider", divider: true, label: "" },
      {
        id: "points-delete",
        label: "删除",
        icon: "trash",
        kbd: "Del",
        disabled: readOnly || !onDelete,
        onSelect: () => onDelete?.(contextMenuAnnotation),
      },
    ];
  }

  if (contextMenuAnnotation && isVideoMaskTrack(contextMenuAnnotation)) {
    const trackId = contextMenuAnnotation.geometry.track_id;
    const locked = lockedTrackIds?.has(trackId) ?? false;
    const frameOutside = isFrameOutside(contextMenuAnnotation.geometry, frameIndex);
    const frameManualOutside = (contextMenuAnnotation.geometry.outside ?? []).some(
      (range) =>
        range.source !== "prediction" && range.from <= frameIndex && frameIndex <= range.to,
    );
    const exact = contextMenuAnnotation.geometry.keyframes.some(
      (keyframe) => keyframe.frame_index === frameIndex,
    );
    const frameMutationDisabled =
      readOnly ||
      locked ||
      contextMenuAnnotation.is_locked ||
      maskKeyframeActions?.busy ||
      !maskKeyframeActions;
    return [
      {
        id: "mask-copy-current",
        label: "复制当前 Mask",
        icon: "copy",
        disabled: !maskKeyframeActions || maskKeyframeActions.busy || frameOutside,
        onSelect: () => maskKeyframeActions?.copyCurrent(contextMenuAnnotation),
      },
      {
        id: "mask-paste-current",
        label: "粘贴到当前轨迹",
        icon: "clipboardPaste",
        disabled: frameMutationDisabled || !maskKeyframeActions?.hasClipboard,
        onSelect: () => maskKeyframeActions?.pasteSameTrack(contextMenuAnnotation),
      },
      {
        id: "mask-paste-new",
        label: "粘贴为新轨迹",
        icon: "plus",
        disabled: frameMutationDisabled || !maskKeyframeActions?.hasClipboard,
        onSelect: () => maskKeyframeActions?.pasteNewTrack(contextMenuAnnotation),
      },
      { id: "mask-frame-divider", divider: true, label: "" },
      {
        id: "mask-outside",
        label: frameOutside ? (frameManualOutside ? "恢复保持" : "预测消失") : "标记消失",
        icon: "eyeOff",
        kbd: "O",
        disabled: frameMutationDisabled || (frameOutside && !frameManualOutside),
        onSelect: () => maskKeyframeActions?.toggleCurrentOutside(contextMenuAnnotation),
      },
      {
        id: "mask-delete-keyframe",
        label: "删除当前关键帧",
        icon: "trash",
        kbd: "Del",
        disabled:
          frameMutationDisabled ||
          !exact ||
          frameOutside ||
          contextMenuAnnotation.geometry.keyframes.length <= 1,
        onSelect: () => maskKeyframeActions?.deleteCurrentKeyframe(contextMenuAnnotation),
      },
      {
        id: "mask-split-components",
        label: "组件拆分为轨迹",
        icon: "scissors",
        disabled: frameMutationDisabled || frameOutside,
        onSelect: () => maskKeyframeActions?.splitCurrentComponents(contextMenuAnnotation),
      },
      { id: "mask-track-divider", divider: true, label: "" },
      {
        id: "mask-track-class",
        label: "改类别",
        icon: "tag",
        disabled: frameMutationDisabled || !onChangeUserBoxClass,
        onSelect: () => onChangeUserBoxClass?.(contextMenuAnnotation.id),
      },
      {
        id: "mask-delete-track",
        label: "删除整条轨迹",
        icon: "trash",
        kbd: "Ctrl+Del",
        disabled: frameMutationDisabled || !onDelete,
        onSelect: () => onDelete?.(contextMenuAnnotation),
      },
    ];
  }

  if (contextMenuAnnotation && isVideoPointsTrack(contextMenuAnnotation)) {
    const trackId = contextMenuAnnotation.geometry.track_id;
    const locked = lockedTrackIds?.has(trackId) ?? false;
    const hidden = hiddenTrackIds?.has(trackId) ?? false;
    return [
      {
        id: "points-track-locked",
        label: locked ? "解锁轨迹" : "锁定轨迹",
        icon: locked ? "unlock" : "lock",
        kbd: "L",
        disabled: readOnly || !onToggleLockedTrack,
        onSelect: () => onToggleLockedTrack?.(trackId),
      },
      {
        id: "points-track-hidden",
        label: hidden ? "显示轨迹" : "隐藏轨迹",
        icon: hidden ? "eyeOff" : "eye",
        kbd: "H",
        disabled: readOnly || !onToggleHiddenTrack,
        onSelect: () => onToggleHiddenTrack?.(trackId),
      },
      { id: "points-track-edit-divider", divider: true, label: "" },
      {
        id: "points-track-class",
        label: "改类别",
        icon: "tag",
        disabled: readOnly || locked || !onChangeUserBoxClass,
        onSelect: () => onChangeUserBoxClass?.(contextMenuAnnotation.id),
      },
      {
        id: "points-track-delete",
        label: "删除整条轨迹",
        icon: "trash",
        kbd: "Del",
        disabled: readOnly || locked || !onDelete,
        onSelect: () => onDelete?.(contextMenuAnnotation),
      },
    ];
  }

  if (
    !selectedAnnotation ||
    selectedAnnotation.id !== contextMenuTargetId ||
    !isVideoTrack(selectedAnnotation)
  )
    return [];
  const frameEditDisabled = !trackActions.canEditSelectedTrack;
  const trackMutationDisabled = readOnly || trackActions.selectedTrackLocked;
  return [
    {
      id: "outside",
      label: trackActions.currentFrameOutside ? "恢复显示" : "标记消失",
      icon: "eyeOff",
      kbd: "O",
      disabled: frameEditDisabled,
      onSelect: trackActions.toggleSelectedTrackOutside,
    },
    {
      id: "occluded",
      label: trackActions.currentFrameOccluded ? "取消遮挡" : "标记遮挡",
      icon: "rect",
      kbd: "Q",
      disabled: frameEditDisabled,
      onSelect: trackActions.toggleSelectedTrackOccluded,
    },
    { id: "state-divider", divider: true, label: "" },
    {
      id: "locked",
      label: trackActions.selectedTrackLocked ? "解锁轨迹" : "锁定轨迹",
      icon: trackActions.selectedTrackLocked ? "unlock" : "lock",
      kbd: "L",
      disabled: readOnly || !onToggleLockedTrack,
      onSelect: trackActions.toggleSelectedTrackLocked,
    },
    {
      id: "hidden",
      label: trackActions.selectedTrackHidden ? "显示轨迹" : "隐藏轨迹",
      icon: trackActions.selectedTrackHidden ? "eyeOff" : "eye",
      kbd: "H",
      disabled: readOnly || !onToggleHiddenTrack,
      onSelect: trackActions.toggleSelectedTrackHidden,
    },
    {
      id: "propagate",
      label: "AI 延展此轨迹",
      icon: "bot",
      kbd: "Ctrl+B",
      disabled: frameEditDisabled || !onPropagateTrack,
      onSelect: trackActions.propagateSelectedTrack,
    },
    { id: "edit-divider", divider: true, label: "" },
    {
      id: "class",
      label: "改类别",
      icon: "tag",
      disabled: trackMutationDisabled || !onChangeUserBoxClass,
      onSelect: () => onChangeUserBoxClass?.(selectedAnnotation.id),
    },
    {
      id: "split-frame",
      label: "当前帧转独立框",
      icon: "box",
      disabled: trackMutationDisabled || !onConvertToBboxes,
      onSelect: () =>
        onConvertToBboxes?.(selectedAnnotation, {
          operation: "split",
          scope: "frame",
          frameIndex,
        }),
    },
    {
      id: "delete-keyframe",
      label: "删除当前关键帧",
      icon: "trash",
      kbd: "Del",
      disabled: !canDeleteSelectedTrackKeyframe,
      onSelect: deleteSelectedTrackKeyframe,
    },
    {
      id: "delete-track",
      label: "删除整条轨迹",
      icon: "trash",
      kbd: "Ctrl+Del",
      disabled: trackMutationDisabled || !onDelete,
      onSelect: () => onDelete?.(selectedAnnotation),
    },
  ];
}

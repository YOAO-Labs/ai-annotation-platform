import type { PendingDrawing, EditingClass, Geom } from "../state/useWorkbenchState";
import type { Viewport } from "../state/useViewportTransform";
import {
  ClassPickerPopover,
  type ClassPickerCancelReason,
  type ClassPickerAttrEditing,
} from "./ClassPickerPopover";

type StageGeometry = {
  imgW: number;
  imgH: number;
};

type BatchChangeTarget = {
  geom: Geom;
  className: string;
  count: number;
  anchor?: { left: number; top: number };
} | null;

interface WorkbenchOverlaysProps {
  pendingDrawing: PendingDrawing;
  editingClass: EditingClass;
  samPendingGeom: Geom | null;
  samPendingAnchor?: { left: number; top: number } | null;
  samDefaultClass: string;
  batchChanging: boolean;
  batchChangeTarget: BatchChangeTarget;
  imageOverlayEnabled: boolean;
  stageGeom: StageGeometry;
  vp: Viewport;
  classes: string[];
  editingClassClasses: string[];
  batchChangeClasses?: string[];
  recentClasses: string[];
  activeClass: string;
  onPickPendingClass: (cls: string) => void;
  onCancelPending: (reason: ClassPickerCancelReason) => void;
  onCommitChangeClass: (cls: string) => void;
  onChangeClassKeepOpen?: (cls: string) => void;
  changeClassAttrEditing?: ClassPickerAttrEditing;
  onCancelChangeClass: () => void;
  onSamCommitClass: (cls: string) => void;
  onSamCancelClass: () => void;
  onCommitBatchChangeClass: (cls: string) => void;
  onCancelBatchChange: () => void;
}

function isAnchoredPending(
  pending: PendingDrawing,
): pending is NonNullable<PendingDrawing> & { anchor: { left: number; top: number } } {
  return !!pending && "anchor" in pending;
}

function hasFixedAnchor<T extends { anchor?: { left: number; top: number } } | null>(
  value: T,
): value is NonNullable<T> & { anchor: { left: number; top: number } } {
  return !!value?.anchor;
}

export function WorkbenchOverlays({
  pendingDrawing,
  editingClass,
  samPendingGeom,
  samPendingAnchor,
  samDefaultClass,
  batchChanging,
  batchChangeTarget,
  imageOverlayEnabled,
  stageGeom,
  vp,
  classes,
  editingClassClasses,
  batchChangeClasses = classes,
  recentClasses,
  activeClass,
  onPickPendingClass,
  onCancelPending,
  onCommitChangeClass,
  onChangeClassKeepOpen,
  changeClassAttrEditing,
  onCancelChangeClass,
  onSamCommitClass,
  onSamCancelClass,
  onCommitBatchChangeClass,
  onCancelBatchChange,
}: WorkbenchOverlaysProps) {
  const canUseImagePosition = imageOverlayEnabled && stageGeom.imgW > 0 && stageGeom.imgH > 0;

  return (
    <>
      {pendingDrawing && isAnchoredPending(pendingDrawing) && (
        <ClassPickerPopover
          position="fixed"
          anchor={pendingDrawing.anchor}
          classes={classes}
          recent={recentClasses}
          defaultClass={activeClass}
          onPick={onPickPendingClass}
          onCancel={onCancelPending}
        />
      )}
      {pendingDrawing && !isAnchoredPending(pendingDrawing) && canUseImagePosition && (
        <ClassPickerPopover
          geom={pendingDrawing.geom}
          imgW={stageGeom.imgW}
          imgH={stageGeom.imgH}
          vp={vp}
          classes={classes}
          recent={recentClasses}
          defaultClass={activeClass}
          onPick={onPickPendingClass}
          onCancel={onCancelPending}
        />
      )}
      {editingClass && hasFixedAnchor(editingClass) && !pendingDrawing && (
        <ClassPickerPopover
          position="fixed"
          anchor={editingClass.anchor}
          classes={editingClassClasses}
          recent={recentClasses}
          defaultClass={editingClass.currentClass}
          title={
            editingClass.accept
              ? `采纳 → 选项目标签 (模型类别: ${editingClass.currentClass})`
              : `改类别 (当前: ${editingClass.currentClass})`
          }
          onPick={
            changeClassAttrEditing && onChangeClassKeepOpen
              ? onChangeClassKeepOpen
              : onCommitChangeClass
          }
          onCancel={onCancelChangeClass}
          attrEditing={changeClassAttrEditing}
        />
      )}
      {editingClass && !hasFixedAnchor(editingClass) && canUseImagePosition && !pendingDrawing && (
        <ClassPickerPopover
          geom={editingClass.geom}
          imgW={stageGeom.imgW}
          imgH={stageGeom.imgH}
          vp={vp}
          classes={editingClassClasses}
          recent={recentClasses}
          defaultClass={editingClass.currentClass}
          title={
            editingClass.accept
              ? `采纳 → 选项目标签 (模型类别: ${editingClass.currentClass})`
              : `改类别 (当前: ${editingClass.currentClass})`
          }
          onPick={
            changeClassAttrEditing && onChangeClassKeepOpen
              ? onChangeClassKeepOpen
              : onCommitChangeClass
          }
          onCancel={onCancelChangeClass}
          attrEditing={changeClassAttrEditing}
        />
      )}
      {samPendingGeom && samPendingAnchor && !pendingDrawing && !editingClass && (
        <ClassPickerPopover
          position="fixed"
          anchor={samPendingAnchor}
          classes={classes}
          recent={recentClasses}
          defaultClass={samDefaultClass}
          title="接受 SAM 候选 → 选类别"
          onPick={onSamCommitClass}
          onCancel={onSamCancelClass}
        />
      )}
      {samPendingGeom &&
        !samPendingAnchor &&
        canUseImagePosition &&
        !pendingDrawing &&
        !editingClass && (
          <ClassPickerPopover
            geom={samPendingGeom}
            imgW={stageGeom.imgW}
            imgH={stageGeom.imgH}
            vp={vp}
            classes={classes}
            recent={recentClasses}
            defaultClass={samDefaultClass}
            title="接受 SAM 候选 → 选类别"
            onPick={onSamCommitClass}
            onCancel={onSamCancelClass}
          />
        )}
      {batchChanging &&
        batchChangeTarget &&
        hasFixedAnchor(batchChangeTarget) &&
        !pendingDrawing &&
        !editingClass && (
          <ClassPickerPopover
            position="fixed"
            anchor={batchChangeTarget.anchor}
            classes={batchChangeClasses}
            recent={recentClasses}
            defaultClass={batchChangeTarget.className}
            title={`批量改类别 (${batchChangeTarget.count} 个)`}
            onPick={onCommitBatchChangeClass}
            onCancel={onCancelBatchChange}
          />
        )}
      {batchChanging &&
        batchChangeTarget &&
        !hasFixedAnchor(batchChangeTarget) &&
        canUseImagePosition &&
        !pendingDrawing &&
        !editingClass && (
          <ClassPickerPopover
            geom={batchChangeTarget.geom}
            imgW={stageGeom.imgW}
            imgH={stageGeom.imgH}
            vp={vp}
            classes={batchChangeClasses}
            recent={recentClasses}
            defaultClass={batchChangeTarget.className}
            title={`批量改类别 (${batchChangeTarget.count} 个)`}
            onPick={onCommitBatchChangeClass}
            onCancel={onCancelBatchChange}
          />
        )}
    </>
  );
}

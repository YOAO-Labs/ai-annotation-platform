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
  /** 视频几何用固定屏幕锚点(无 image 定位);图片留空走 geom + vp。 */
  anchor?: { left: number; top: number };
} | null;

interface WorkbenchOverlaysProps {
  pendingDrawing: PendingDrawing;
  editingClass: EditingClass;
  samPendingGeom: Geom | null;
  /**
   * 视频侧 SAM 候选的类选择器锚点 (屏幕坐标)。
   * 图片侧用 geom + vp 换算; 视频画布的 vp 不在这层, 故由画布换算好再传下来 —— 与视频
   * pendingDrawing 的 anchor 同式。二者互斥: 有 anchor 走 fixed 定位。
   */
  samPendingAnchor?: { left: number; top: number } | null;
  samDefaultClass: string;
  batchChanging: boolean;
  batchChangeTarget: BatchChangeTarget;
  imageOverlayEnabled: boolean;
  stageGeom: StageGeometry;
  vp: Viewport;
  classes: string[];
  /** B-57 · 采纳预测选类时按预测自身工具单位 (如 region) 列出的类别; 非采纳态等于 classes。 */
  editingClassClasses: string[];
  /** 批量改类按选中对象的工具单位取类别；混合工具单位会在打开弹层前阻止。 */
  batchChangeClasses?: string[];
  recentClasses: string[];
  activeClass: string;
  onPickPendingClass: (cls: string) => void;
  onCancelPending: (reason: ClassPickerCancelReason) => void;
  onCommitChangeClass: (cls: string) => void;
  /** 改类悬浮框含属性时用此回调——提交改类但不关闭悬浮框，便于接着编辑属性。 */
  onChangeClassKeepOpen?: (cls: string) => void;
  /** 改类悬浮框内联属性编辑（按当前选中标注派生；缺省时退化为纯改类、点选即关）。 */
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

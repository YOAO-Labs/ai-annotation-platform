// Mask 编辑器的 UI 集成。
//
// 工具职责仅声明：用户按下指针时，若 mask 编辑器未激活则以空白 buffer 入场，并向
// ImageStage 返回 maskBrush DragInit；其余（连续 paintAt / commit / hotkey）由
// ImageStage / WorkbenchShell / useMaskEditor 协同处理。
//
// 注意：实际 buffer 状态在 useMaskEditor (state/useMaskEditor.ts)；本工具与之耦合
// 走 ToolPointerContext 的 ctx.maskEditor。

import type { CanvasTool as CanvasToolMeta, DragInit } from "./index";
// 单一 mask 编辑准入闸门 (同时检查 readOnly + is_locked)。
import { canEditMask } from "../../state/canEditMask";

export const MaskTool: CanvasToolMeta = {
  id: "mask",
  hotkey: "M",
  label: "Mask 笔刷",
  icon: "brush",
  cursor: "crosshair",
  onPointerDown: ({ pt, readOnly, imgW, imgH, maskEditor, annotationLocked }) => {
    // pointer 入口经 canEditMask: task readOnly 或 annotation is_locked
    // 任一为 true → 拒绝落笔 (关闭锁定对象经 pointer 路径修改的绕过)。editorPhase 此处
    // 取 ready (落笔瞬间 buffer 已就绪或即将 beginBlank); loading/saving 由上层 session 守。
    if (
      !canEditMask({
        taskReadOnly: readOnly,
        annotationLocked: !!annotationLocked,
        trackLocked: false,
        segmentLocked: false,
        // 裸 useMaskEditor 没有 phase，保留旧的 pointerdown→beginBlank 行为；
        // session 包装器会明确给出 loading/saving 并在这里被拒绝。
        editorPhase: maskEditor?.phase ?? "ready",
      })
    ) {
      return null;
    }
    if (!maskEditor) return null;
    if (maskEditor.tool === "slice_mask") {
      if (!maskEditor.active || maskEditor.dirty || maskEditor.instanceOperationPreview)
        return null;
      return { kind: "maskSlice", start: [pt.x, pt.y], end: [pt.x, pt.y] };
    }
    // 像素坐标（pixel-space）— 与 maskBuffer / paintAt 单位一致。
    const px = pt.x * imgW;
    const py = pt.y * imgH;
    if (!maskEditor.active) {
      maskEditor.beginBlank();
    }
    if (maskEditor.tool === "fill_add" || maskEditor.tool === "fill_subtract") {
      void maskEditor.runOperation(maskEditor.tool, {
        type: "flood_fill",
        x: px,
        y: py,
        value: maskEditor.tool === "fill_add" ? 255 : 0,
        connectivity: maskEditor.connectivity,
      });
      return null;
    }
    if (maskEditor.tool === "lasso_add" || maskEditor.tool === "lasso_subtract") {
      return { kind: "maskLasso", points: [[px, py]] } as DragInit;
    }
    if (maskEditor.tool === "component_keep" || maskEditor.tool === "component_delete") {
      void maskEditor.runOperation(maskEditor.tool, {
        type: "component",
        action: maskEditor.tool === "component_keep" ? "keep" : "delete",
        x: px,
        y: py,
        connectivity: maskEditor.connectivity,
      });
      return null;
    }
    if (maskEditor.tool === "component_copy") {
      void maskEditor.runInstanceOperation("copy_component", {
        type: "copy_component",
        x: px,
        y: py,
        connectivity: maskEditor.connectivity,
      });
      return null;
    }
    if (maskEditor.tool === "hole_fill") {
      void maskEditor.runOperation(maskEditor.tool, {
        type: "fill_holes",
        mode: "hit",
        x: px,
        y: py,
      });
      return null;
    }
    // WS-B/A3 · 开启一笔的历史边界: 配合 ImageStage pointerup 的 endStroke,
    // 让图片 mask 每笔可 undo (与视频路径 VideoKonvaStage 一致)。
    maskEditor.beginStroke();
    // 立即落第一笔；连续 paintAt 由 ImageStage 在 pointermove 中线段插值。
    maskEditor.paintAt(px, py);
    return { kind: "maskBrush", lastX: px, lastY: py } as DragInit;
  },
};

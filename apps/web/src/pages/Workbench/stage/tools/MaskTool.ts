import type { CanvasTool as CanvasToolMeta, DragInit } from "./index";
import { canEditMask } from "../../state/canEditMask";

export const MaskTool: CanvasToolMeta = {
  id: "mask",
  hotkey: "M",
  label: "Mask 笔刷",
  icon: "brush",
  cursor: "crosshair",
  onPointerDown: ({ pt, readOnly, imgW, imgH, maskEditor, annotationLocked }) => {
    if (
      !canEditMask({
        taskReadOnly: readOnly,
        annotationLocked: !!annotationLocked,
        trackLocked: false,
        segmentLocked: false,
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
    maskEditor.beginStroke();
    maskEditor.paintAt(px, py);
    return { kind: "maskBrush", lastX: px, lastY: py } as DragInit;
  },
};

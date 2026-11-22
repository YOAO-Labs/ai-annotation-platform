import type { CanvasTool, DragInit, ToolPointerContext } from "./index";

export const PolylineTool: CanvasTool = {
  id: "polyline",
  hotkey: "L",
  label: "折线",
  icon: "annotationPolyline",
  cursor: "crosshair",
  onPointerDown: ({
    pt,
    evt,
    readOnly,
    pendingDrawing,
    polygonDraft,
  }: ToolPointerContext): DragInit | null => {
    if (readOnly || pendingDrawing || !polygonDraft) return null;
    if (evt.button !== 0) return null;
    polygonDraft.addPoint([pt.x, pt.y]);
    return null;
  },
};

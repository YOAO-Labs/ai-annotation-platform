import type { CanvasTool as CanvasToolMeta, DragInit } from "./index";

export const CanvasTool: CanvasToolMeta = {
  id: "canvas",
  hotkey: "C",
  label: "画布批注",
  icon: "edit",
  cursor: "crosshair",
  onPointerDown: ({ pt, readOnly }) => {
    if (readOnly) return null;
    return { kind: "canvasStroke", points: [pt.x, pt.y] } as DragInit;
  },
};

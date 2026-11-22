import type { CanvasTool, DragInit, ToolPointerContext } from "./index";

const CLOSE_DISTANCE = 0.008;

export const PolygonTool: CanvasTool = {
  id: "polygon",
  hotkey: "P",
  label: "多边形",
  icon: "annotationPolygon",
  cursor: "crosshair",
  onPointerDown: ({
    pt,
    evt,
    readOnly,
    pendingDrawing,
    spacePan,
    polygonDraft,
    snapPoint,
    startPolygonAutoPoints,
  }: ToolPointerContext): DragInit | null => {
    if (readOnly || pendingDrawing || spacePan || !polygonDraft) return null;
    if (evt.button !== 0) return null;
    const target = snapPoint?.(pt, evt) ?? pt;
    if (evt.shiftKey && startPolygonAutoPoints) {
      startPolygonAutoPoints([target.x, target.y], evt);
      return null;
    }
    const points = polygonDraft.points;
    if (points.length >= 3) {
      const [fx, fy] = points[0];
      const dx = target.x - fx;
      const dy = target.y - fy;
      if (Math.hypot(dx, dy) <= CLOSE_DISTANCE) {
        polygonDraft.close();
        return null;
      }
    }
    polygonDraft.addPoint([target.x, target.y]);
    return null;
  },
};

export { CLOSE_DISTANCE };

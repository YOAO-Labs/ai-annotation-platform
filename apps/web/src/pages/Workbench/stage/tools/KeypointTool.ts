import type { CanvasTool, DragInit, ToolPointerContext } from "./index";

export const KeypointTool: CanvasTool = {
  id: "keypoint",
  hotkey: "F",
  label: "关键点",
  icon: "keypoints",
  cursor: "crosshair",
  onPointerDown: ({
    pt,
    evt,
    readOnly,
    pendingDrawing,
    keypointDraft,
  }: ToolPointerContext): DragInit | null => {
    if (readOnly || pendingDrawing || !keypointDraft) return null;
    if (keypointDraft.nodeCount === 0) return null;
    if (keypointDraft.points.length >= keypointDraft.nodeCount) return null;
    if (evt.button === 2) {
      keypointDraft.addPoint({ x: pt.x, y: pt.y, v: 0 });
      return null;
    }
    if (evt.button !== 0) return null;
    keypointDraft.addPoint({ x: pt.x, y: pt.y, v: evt.altKey ? 1 : 2 });
    return null;
  },
};

import type { CanvasTool, DragInit, ToolPointerContext } from "./index";

// 旋转框 (OBB): 交互复用 BboxTool 的轴对齐拖框 (kind "draw"); ImageStage 在
// tool === "rotated-box" 时把松手得到的矩形提交为 angle=0 的 rotated_bbox。提交后选中
// 该框, 顶部出现旋转手柄 (KonvaRotatedBox), 拖手柄走 kind "rotateBox" 改 angle。
export const RotatedBboxTool: CanvasTool = {
  id: "rotated-box",
  hotkey: "W",
  label: "旋转框",
  icon: "rotatedBox",
  cursor: "crosshair",
  onPointerDown: ({
    pt,
    evt,
    spacePan,
    readOnly,
    pendingDrawing,
    onClearSelection,
  }: ToolPointerContext): DragInit | null => {
    if (pendingDrawing) return null;
    if (spacePan || readOnly) {
      if (readOnly) onClearSelection();
      return { kind: "pan", sx: pt.x, sy: pt.y };
    }
    if (!evt.shiftKey) onClearSelection();
    return { kind: "draw", sx: pt.x, sy: pt.y, cx: pt.x, cy: pt.y };
  },
};

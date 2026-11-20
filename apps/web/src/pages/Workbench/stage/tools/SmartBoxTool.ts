import type { CanvasTool, DragInit, ToolPointerContext } from "./index";

/**
 * 拆自旧 SamTool / sub-tool="bbox".
 * 拖框作为 SAM 单框单 mask prompt; 小拖动 (< 0.5%) ImageStage 自然忽略.
 * requiredPrompt = "interactive_box" (旧 "bbox" 退役, 统一双 backend 命名).
 */
export const SmartBoxTool: CanvasTool = {
  id: "smart-box",
  hotkey: "S",
  label: "智能框",
  icon: "scan",
  cursor: "crosshair",
  requiredPrompt: "interactive_box",
  onPointerDown: ({
    pt,
    spacePan,
    readOnly,
    pendingDrawing,
    onClearSelection,
    preserveSelectionForPrompt,
  }: ToolPointerContext): DragInit | null => {
    if (pendingDrawing) return null;
    if (spacePan || readOnly) {
      if (readOnly) onClearSelection();
      return { kind: "pan", sx: pt.x, sy: pt.y };
    }
    if (!preserveSelectionForPrompt) onClearSelection();
    return {
      kind: "samProbe",
      mode: "bbox",
      sx: pt.x,
      sy: pt.y,
      cx: pt.x,
      cy: pt.y,
      alt: false,
    };
  },
};

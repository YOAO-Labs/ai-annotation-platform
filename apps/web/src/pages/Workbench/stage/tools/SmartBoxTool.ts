import type { CanvasTool, DragInit, ToolPointerContext } from "./index";

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

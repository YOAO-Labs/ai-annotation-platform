import type { CanvasTool, DragInit, ToolPointerContext } from "./index";

export const SmartPointTool: CanvasTool = {
  id: "smart-point",
  hotkey: "S",
  label: "智能点",
  icon: "target",
  cursor: "crosshair",
  requiredPrompt: "point",
  onPointerDown: ({
    pt,
    evt,
    spacePan,
    readOnly,
    pendingDrawing,
    onClearSelection,
    preserveSelectionForPrompt,
    samPolarity = "positive",
  }: ToolPointerContext): DragInit | null => {
    if (pendingDrawing) return null;
    if (spacePan || readOnly) {
      if (readOnly) onClearSelection();
      return { kind: "pan", sx: pt.x, sy: pt.y };
    }
    if (!preserveSelectionForPrompt) onClearSelection();
    const negative = !!evt.altKey || samPolarity === "negative";
    return {
      kind: "samProbe",
      mode: "point",
      sx: pt.x,
      sy: pt.y,
      cx: pt.x,
      cy: pt.y,
      alt: negative,
    };
  },
};

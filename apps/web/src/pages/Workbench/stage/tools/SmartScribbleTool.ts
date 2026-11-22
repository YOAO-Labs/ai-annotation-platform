import type { CanvasTool, DragInit, ToolPointerContext } from "./index";

export const SmartScribbleTool: CanvasTool = {
  id: "smart-scribble",
  hotkey: "",
  label: "智能笔迹",
  icon: "smartScribble",
  cursor: "crosshair",
  requiredPrompt: "scribble",
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
    if (!preserveSelectionForPrompt) return null;
    const negative = !!evt.altKey || samPolarity === "negative";
    return { kind: "samScribble", points: [[pt.x, pt.y]], alt: negative };
  },
};

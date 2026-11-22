import type { CanvasTool, DragInit, ToolPointerContext } from "./index";

export const ExemplarTool: CanvasTool = {
  id: "exemplar",
  hotkey: "S",
  label: "示例 (Exemplar)",
  icon: "copy",
  cursor: "crosshair",
  requiredPrompt: "exemplar",
  onPointerDown: ({
    pt,
    evt,
    spacePan,
    readOnly,
    pendingDrawing,
    onClearSelection,
    samPolarity = "positive",
  }: ToolPointerContext): DragInit | null => {
    if (pendingDrawing) return null;
    if (spacePan || readOnly) {
      if (readOnly) onClearSelection();
      return { kind: "pan", sx: pt.x, sy: pt.y };
    }
    onClearSelection();
    const negative = !!evt.altKey || samPolarity === "negative";
    return {
      kind: "samProbe",
      mode: "exemplar",
      sx: pt.x,
      sy: pt.y,
      cx: pt.x,
      cy: pt.y,
      alt: negative,
    };
  },
};

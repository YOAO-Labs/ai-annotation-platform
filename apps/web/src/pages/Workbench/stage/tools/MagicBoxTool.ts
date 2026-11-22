import type { CanvasTool, DragInit, ToolPointerContext } from "./index";

export const MagicBoxTool: CanvasTool = {
  id: "magic-box",
  hotkey: "G",
  label: "魔法收紧",
  icon: "wandSparkles",
  cursor: "crosshair",
  requiredPrompt: "interactive_box",
  onPointerDown: ({
    pt,
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
    onClearSelection();
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

import type { CanvasTool, DragInit, ToolPointerContext } from "./index";

export const TextPromptTool: CanvasTool = {
  id: "text-prompt",
  hotkey: "S",
  label: "文本提示",
  icon: "messageSquareText",
  cursor: "default",
  requiredPrompt: "text",
  onPointerDown: ({
    pt,
    spacePan,
    readOnly,
    onClearSelection,
  }: ToolPointerContext): DragInit | null => {
    if (spacePan || readOnly) {
      if (readOnly) onClearSelection();
      return { kind: "pan", sx: pt.x, sy: pt.y };
    }
    return null;
  },
};

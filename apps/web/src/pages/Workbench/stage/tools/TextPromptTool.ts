import type { CanvasTool, DragInit, ToolPointerContext } from "./index";

/**
 * 拆自旧 SamTool / sub-tool="text".
 * 画布事件不响应 (返回 null), 交互在 InteractiveToolBar 内的文本输入完成.
 * requiredPrompt = "text".
 */
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

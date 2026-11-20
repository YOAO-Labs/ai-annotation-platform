import type { CanvasTool, DragInit, ToolPointerContext } from "./index";

/**
 * Magic Box.
 *
 * 行为: 用户拖出一个粗略 bbox → SAM bbox prompt → 返回 polygon → 取该 polygon 的
 * 紧凑外接矩形写回 bbox 标注 (不进 AI 候选层). 复用 SmartBoxTool 的拖框语义.
 * requiredPrompt = "interactive_box" (依赖 ML backend 支持 interactive_box prompt).
 */
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

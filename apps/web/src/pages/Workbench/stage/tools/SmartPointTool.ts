import type { CanvasTool, DragInit, ToolPointerContext } from "./index";

/**
 * 拆自旧 SamTool / sub-tool="point".
 * 单击产生 positive point prompt; Alt+点击 / samPolarity=negative 产生 negative.
 * requiredPrompt = "point" → 后端 /setup.supported_prompts 含 "point" 才可用.
 */
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

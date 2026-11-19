import type { CanvasTool, DragInit, ToolPointerContext } from "./index";

/**
 * exemplar 工具仅 SAM 3 PCS 支持。
 * 用户拖框圈出图中某个实例作为视觉示例, 后端找全图相似实例返回多个 mask.
 * 行为与 SmartBox 相同 (拖框), 但 ImageStage 在松手时以 mode="exemplar" 派发 → routes to runExemplar.
 * requiredPrompt = "exemplar"; 仅 backend 声明支持时才可用 (grounded-sam2 → 灰).
 *
 * 升级为 PCS refine 会话: 正框扩召回 / 负框排误检, 每次重发全量。负框手势同 smart-point:
 * Alt+拖框 或 samPolarity=negative → 负框 (alt 字段透传, 父层据此定 polarity)。
 */
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

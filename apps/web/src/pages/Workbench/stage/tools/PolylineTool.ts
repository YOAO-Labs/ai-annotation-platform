import type { CanvasTool, DragInit, ToolPointerContext } from "./index";

/**
 * 折线工具。开放、不闭合。
 * 交互：左键逐点落点累积顶点；Enter / 双击 → 结束 (≥2 点即可，不自动闭合)；
 *        Esc → 取消；Backspace → 撤销最后一点（由 Shell hotkey 派生）。
 *
 * 复用 PolygonTool 的草稿机制：ctx.polygonDraft（closed:false 语义）。落点通过
 * polygonDraft.addPoint 直接 mutate Shell 维护的草稿状态。与 polygon 不同：无首点
 * 闭合判定。
 */
export const PolylineTool: CanvasTool = {
  id: "polyline",
  hotkey: "L",
  label: "折线",
  icon: "annotationPolyline",
  cursor: "crosshair",
  onPointerDown: ({
    pt,
    evt,
    readOnly,
    pendingDrawing,
    polygonDraft,
  }: ToolPointerContext): DragInit | null => {
    if (readOnly || pendingDrawing || !polygonDraft) return null;
    if (evt.button !== 0) return null; // 仅左键落点
    polygonDraft.addPoint([pt.x, pt.y]);
    return null;
  },
};

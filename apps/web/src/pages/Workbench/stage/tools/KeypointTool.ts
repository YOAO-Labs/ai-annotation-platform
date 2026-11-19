import type { CanvasTool, DragInit, ToolPointerContext } from "./index";

/**
 * 关键点工具 (COCO 范式)。
 * 交互：按当前类别 keypoint_schema.nodes **依次** 落点：
 *   - 左键           → 放下一个可见点 (v=2)；
 *   - Alt + 左键      → 当前节点标记为「被遮挡」(v=1，位置仍记录)；
 *   - 右键           → 当前节点「跳过 / 不可见」(v=0，位置占位)；
 * 放满 nodes 数量即由 Shell 的 keypointDraft.commit 自动提交一个实例。
 *
 * 注意：与 PolygonTool 同构，本工具不返回 DragInit；落点直接 mutate Shell 维护的草稿
 *       (ctx.keypointDraft)。空白处按下不触发其它工具的 setDrag 路径。
 */
export const KeypointTool: CanvasTool = {
  id: "keypoint",
  hotkey: "F",
  label: "关键点",
  icon: "keypoints",
  cursor: "crosshair",
  onPointerDown: ({
    pt,
    evt,
    readOnly,
    pendingDrawing,
    keypointDraft,
  }: ToolPointerContext): DragInit | null => {
    if (readOnly || pendingDrawing || !keypointDraft) return null;
    // 未配置 schema (nodeCount=0) → 无可放节点; 已放满 → 不再吃事件
    if (keypointDraft.nodeCount === 0) return null;
    if (keypointDraft.points.length >= keypointDraft.nodeCount) return null;
    if (evt.button === 2) {
      // 右键：跳过当前节点 (v=0)
      keypointDraft.addPoint({ x: pt.x, y: pt.y, v: 0 });
      return null;
    }
    if (evt.button !== 0) return null;
    // Alt+左键 → 遮挡 (v=1)，普通左键 → 可见 (v=2)
    keypointDraft.addPoint({ x: pt.x, y: pt.y, v: evt.altKey ? 1 : 2 });
    return null;
  },
};

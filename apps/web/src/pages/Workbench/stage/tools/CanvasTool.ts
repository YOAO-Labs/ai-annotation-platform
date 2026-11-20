// CanvasTool（C 键）：reviewer/annotator 在原图上画批注（红圈/箭头），
// 序列化为归一化 [0,1] 坐标，与 ImageStage 的 vp 共享坐标系，缩放/平移自动跟随。
//
// 注意：实际的 stroke 状态由 useWorkbenchState.canvasDraft 持有；本 tool 只
// 启动 DragInit；ImageStage 的 onPointerMove/Up 分支负责把 [pt.x,pt.y] 序列累加
// 到 canvasDraft.shapes。提交由浮动 CanvasToolbar 触发（见 CanvasToolbar.tsx）。

import type { CanvasTool as CanvasToolMeta, DragInit } from "./index";

export const CanvasTool: CanvasToolMeta = {
  id: "canvas",
  hotkey: "C",
  label: "画布批注",
  icon: "edit",
  cursor: "crosshair",
  onPointerDown: ({ pt, readOnly }) => {
    if (readOnly) return null;
    // 启动一段新的 stroke（ImageStage onMove 会持续 append）
    return { kind: "canvasStroke", points: [pt.x, pt.y] } as DragInit;
  },
};

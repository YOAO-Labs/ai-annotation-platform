// ImageStage 内的 5th Konva Layer：渲染（+ 编辑）评论画布批注。
//
// 坐标系：shapes 永远是归一化 [0,1]，渲染时乘 imgW/imgH；strokeWidth = 2/scale
// 保持屏幕粗细恒定。Layer 是 ImageStage Stage 的子节点，自动跟随 vp.tx/ty/scale。
//
// 模式：
//   - editable=false（read-only）：listening 关闭，纯展示。当前未广泛挂载，
//     主要由 canvas-edit 模式启用。
//   - editable=true：listening 开启，但仅在空白点击时启动新 stroke
//     （hit-test 由 ImageStage onMouseDown 完成；本 Layer 自身的 Line shapes
//      也设 listening=false 防止"点已有线"误触发新画）。
//
// 编辑模式下的实时草稿（drag.kind === "canvasStroke"）由父 ImageStage 渲染为额外
// 的 Line（不在本 Layer 持有），见 ImageStage.tsx 中 overlay layer 内的 polygon
// 草稿处理类比。

import { Layer, Line } from "react-konva";

import type { CommentCanvasDrawing } from "@/api/comments";

interface Props {
  shapes: NonNullable<CommentCanvasDrawing["shapes"]>;
  /** 正在画的笔触（实时预览），shape 为归一化 [x1,y1,x2,y2,...] 列表。*/
  draftStroke?: number[] | null;
  /** 当前选中颜色（用于 draft 渲染）。*/
  draftColor?: string;
  imgW: number;
  imgH: number;
  scale: number;
  editable?: boolean;
}

export function CanvasDrawingLayer({
  shapes,
  draftStroke,
  draftColor = "#ef4444",
  imgW,
  imgH,
  scale,
  editable = false,
}: Props) {
  return (
    <Layer name="canvas-drawing" listening={editable}>
      {shapes.map((s, i) => (
        <Line
          key={i}
          points={denormalize(s.points, imgW, imgH)}
          stroke={s.stroke ?? "#ef4444"}
          strokeWidth={2 / scale}
          lineCap="round"
          lineJoin="round"
          tension={0}
          listening={false}
        />
      ))}
      {draftStroke && draftStroke.length >= 4 && (
        <Line
          points={denormalize(draftStroke, imgW, imgH)}
          stroke={draftColor}
          strokeWidth={2 / scale}
          lineCap="round"
          lineJoin="round"
          tension={0}
          listening={false}
        />
      )}
    </Layer>
  );
}

function denormalize(pts: number[], imgW: number, imgH: number): number[] {
  const out = new Array<number>(pts.length);
  for (let i = 0; i < pts.length; i += 2) {
    out[i] = pts[i] * imgW;
    out[i + 1] = pts[i + 1] * imgH;
  }
  return out;
}

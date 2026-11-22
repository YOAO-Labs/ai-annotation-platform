import { Layer, Line } from "react-konva";

import type { CommentCanvasDrawing } from "@/api/comments";

interface Props {
  shapes: NonNullable<CommentCanvasDrawing["shapes"]>;
  draftStroke?: number[] | null;
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

import { Fragment, useEffect, useState } from "react";
import { Circle, Line, Rect, Text } from "react-konva";
import { tightenBboxFromPolygon } from "./shared/geometry/bbox";

const SAM_STROKE = "#a855f7";
const POSITIVE_POINT = "#22c55e";
const NEGATIVE_POINT = "#ef4444";
const OBJ_PALETTE = ["#a855f7", "#06b6d4", "#f59e0b", "#ec4899", "#84cc16", "#6366f1"];
function objRingColor(obj: number): string {
  return OBJ_PALETTE[(obj - 1) % OBJ_PALETTE.length];
}
const SAM_DASH: [number, number] = [6, 4];
const SAM_DASH_PERIOD = SAM_DASH[0] + SAM_DASH[1];
const SAM_FLOW_SPEED = 22;

function hexToRgba(hex: string, alpha: number): string {
  const v = hex.replace("#", "");
  const r = parseInt(v.slice(0, 2), 16);
  const g = parseInt(v.slice(2, 4), 16);
  const b = parseInt(v.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

export type VideoSamCandidateShape = {
  id: string;
  type: string;
  points?: [number, number][];
  bbox?: { x: number; y: number; width: number; height: number };
};

export interface VideoSamCandidateOverlayProps {
  candidates: VideoSamCandidateShape[];
  activeIdx: number;
  previewAsBbox?: boolean;
  sessionPoints: { pt: [number, number]; polarity: 1 | 0; obj?: number }[];
  sessionBoxes?: { bbox: [number, number, number, number]; obj?: number }[];
  width: number;
  height: number;
  scale: number;
}

export function VideoSamCandidateOverlay({
  candidates,
  activeIdx,
  previewAsBbox = false,
  sessionPoints,
  sessionBoxes,
  width,
  height,
  scale,
}: VideoSamCandidateOverlayProps) {
  const hasCandidates = candidates.length > 0;
  const boxes = sessionBoxes ?? [];
  const [dashOffset, setDashOffset] = useState(0);

  useEffect(() => {
    if (!hasCandidates) return;
    let raf = 0;
    let start: number | null = null;
    const loop = (t: number) => {
      if (start === null) start = t;
      setDashOffset(-(((t - start) / 1000) * SAM_FLOW_SPEED) % SAM_DASH_PERIOD);
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, [hasCandidates]);

  if (!hasCandidates && sessionPoints.length === 0 && boxes.length === 0) return null;

  const dash = [SAM_DASH[0] / scale, SAM_DASH[1] / scale];
  const offset = dashOffset / scale;

  return (
    <>
      {candidates.map((c, idx) => {
        const isActive = idx === activeIdx;
        const strokeWidth = (isActive ? 3.5 : 2.5) / scale;
        const common = {
          stroke: SAM_STROKE,
          strokeWidth,
          dash,
          dashOffset: offset,
          fill: hexToRgba(SAM_STROKE, isActive ? 0.18 : 0.08),
          opacity: isActive ? 1 : 0.7,
          listening: false as const,
        };

        if (c.type === "rectanglelabels" && c.bbox) {
          return (
            <Rect
              key={c.id}
              x={c.bbox.x * width}
              y={c.bbox.y * height}
              width={c.bbox.width * width}
              height={c.bbox.height * height}
              {...common}
            />
          );
        }
        if (!c.points || c.points.length < 3) return null;
        if (previewAsBbox) {
          const tight = tightenBboxFromPolygon(c.points);
          if (!tight) return null;
          return (
            <Rect
              key={c.id}
              x={tight.x * width}
              y={tight.y * height}
              width={tight.w * width}
              height={tight.h * height}
              {...common}
            />
          );
        }
        const flat: number[] = [];
        for (const [x, y] of c.points) flat.push(x * width, y * height);
        return <Line key={c.id} points={flat} closed {...common} />;
      })}

      {sessionPoints.map((sp, i) => {
        const ring = sp.obj != null ? objRingColor(sp.obj) : "#ffffff";
        const positive = sp.polarity === 1;
        const x = sp.pt[0] * width;
        const y = sp.pt[1] * height;
        const radius = (positive ? 4 : 6) / scale;
        return (
          <Fragment key={`sp-${i}`}>
            <Circle
              x={x}
              y={y}
              radius={radius}
              fill={positive ? POSITIVE_POINT : NEGATIVE_POINT}
              stroke={ring}
              strokeWidth={(sp.obj != null ? 2 : 1.5) / scale}
              listening={false}
            />
            {!positive && (
              <>
                <Line
                  points={[
                    x - radius * 0.55,
                    y - radius * 0.55,
                    x + radius * 0.55,
                    y + radius * 0.55,
                  ]}
                  stroke="#ffffff"
                  strokeWidth={1.5 / scale}
                  lineCap="round"
                  listening={false}
                />
                <Line
                  points={[
                    x - radius * 0.55,
                    y + radius * 0.55,
                    x + radius * 0.55,
                    y - radius * 0.55,
                  ]}
                  stroke="#ffffff"
                  strokeWidth={1.5 / scale}
                  lineCap="round"
                  listening={false}
                />
              </>
            )}
            {sp.obj != null && (
              <Text
                x={x + (positive ? 6 : 8) / scale}
                y={y - (positive ? 11 : 13) / scale}
                text={String(sp.obj)}
                fontSize={11 / scale}
                fontStyle="bold"
                fill={ring}
                listening={false}
              />
            )}
          </Fragment>
        );
      })}

      {boxes.map((sb, i) => {
        const [x1, y1, x2, y2] = sb.bbox;
        const ring = sb.obj != null ? objRingColor(sb.obj) : SAM_STROKE;
        const bx = Math.min(x1, x2) * width;
        const by = Math.min(y1, y2) * height;
        const bw = Math.abs(x2 - x1) * width;
        const bh = Math.abs(y2 - y1) * height;
        return (
          <Fragment key={`sb-${i}`}>
            <Rect
              x={bx}
              y={by}
              width={bw}
              height={bh}
              stroke={ring}
              strokeWidth={2 / scale}
              fill={hexToRgba(ring, 0.12)}
              listening={false}
            />
            {sb.obj != null && (
              <Text
                x={bx + 3 / scale}
                y={by + 2 / scale}
                text={String(sb.obj)}
                fontSize={11 / scale}
                fontStyle="bold"
                fill={ring}
                listening={false}
              />
            )}
          </Fragment>
        );
      })}
    </>
  );
}

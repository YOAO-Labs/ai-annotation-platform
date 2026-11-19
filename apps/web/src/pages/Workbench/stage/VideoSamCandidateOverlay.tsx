/**
 * 视频交互式 SAM 候选浮层（smart-point / smart-box）。
 *
 * **不复用 `VideoKonvaAiLayer`**：那一层是**批量候选**（落库 `Prediction`）的渲染，其上游
 * `aiBoxFrames.resolveAiBoxAtFrame` 是帧作用域去重的 SSOT 且只认 bbox 类几何（其余类型会
 * fallthrough 成轴对齐 Rect 画错）。交互式候选是**瞬态、不落库、只属当前帧**，混进去会污染
 * 那个 SSOT。图片侧同样把 `SamCandidateOverlay` 作为独立组件，此处与之对齐。
 *
 * 坐标一律归一化 [0,1]，渲染时乘 stage 尺寸（memory「交互候选坐标系」：百分比会飞出画布）。
 */
import { Fragment, useEffect, useState } from "react";
import { Circle, Line, Rect, Text } from "react-konva";
import { tightenBboxFromPolygon } from "./shared/geometry/bbox";

/** 与图片侧 SAM_CANDIDATE_STROKE 同值（canvas 数据域颜色，非 Tailwind token）。 */
const SAM_STROKE = "#a855f7";
const POSITIVE_POINT = "#22c55e";
const NEGATIVE_POINT = "#ef4444";
// 逐目标配色（canvas 数据域色，非 token）：多目标 PVS 种子时按 obj_id
// 给种子点的描边环上色 + 标号，区分不同目标各自的轨迹；填充仍按极性（绿正/红负）保留心智。
const OBJ_PALETTE = ["#a855f7", "#06b6d4", "#f59e0b", "#ec4899", "#84cc16", "#6366f1"];
function objRingColor(obj: number): string {
  return OBJ_PALETTE[(obj - 1) % OBJ_PALETTE.length];
}
// 屏幕像素：虚线段 + 间隔（一周期 10px）；流速 px/秒（marching ants）。
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
  /**
   * Magic Box: 候选多边形只作几何源, 采纳时收紧成外接矩形 —— 预览也画矩形, 否则用户看到
   * 一个贴合轮廓的多边形、落库却是个框。与图片侧 SamCandidateOverlay 的同款特判对齐。
   */
  previewAsBbox?: boolean;
  /**
   * 当前点会话已落的正/负点，供多点精修时可视化。
   * `obj`（PVS 多目标时才有）用于按 obj_id 给描边环上色 + 标号，单目标为 undefined（白边、无标号）。
   */
  sessionPoints: { pt: [number, number]; polarity: 1 | 0; obj?: number }[];
  /**
   * 框修正 · 当前帧已落的 PVS 框种子（归一化 xyxy）。实线 + 按 obj 配色描边，
   * 与虚线 AI 候选框区分；`obj` 多目标时标号。
   */
  sessionBoxes?: { bbox: [number, number, number, number]; obj?: number }[];
  /** stage 尺寸（像素）。 */
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
      // 负向偏移 → 虚线沿边缘向前流动；取模避免数值无限增大。
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
        // Magic Box: 多边形候选以其紧凑外接矩形预览 (采纳时也是这么收紧的)。
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

      {/* 点会话可视化：填充按极性（正点绿、负点红，Alt 落负点精修）；多目标 PVS 时
          描边环按 obj_id 配色 + 右上标号，单目标为白边、无标号。 */}
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

      {/* 框修正 · PVS 框种子: 实线 + 按 obj 配色 (区别虚线 AI 候选框)。 */}
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

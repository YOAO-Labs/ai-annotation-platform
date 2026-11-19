import { memo, Fragment } from "react";
import { Rect, Line } from "react-konva";
import { colorToHex, hexToRgba } from "./colors";
import { fillAlpha, strokeWidthFor, type AnnotationVisualConfig } from "./annotationVisual";
import { screenToWorld } from "./shared/viewport/scaleCancel";
import type { VideoPixelSize } from "./videoKonvaCoordinates";
import type { VideoStageGeom } from "./videoStageTypes";

/** AI 追出关键帧的角标色 (violet, 对齐时间轴 trackKeyframePrediction / 预测密度轨 / roster)。 */
const PREDICTED_BADGE_HEX = "#8b5cf6";

interface VideoKonvaTrackShapeProps {
  /** 归一化 [0,1] bbox (polygon track 时为多边形外接盒, 供角标定位)。 */
  geom: VideoStageGeom;
  /** polygon/polyline track 当前帧归一化顶点; 存在时画 <Line> 而非 <Rect>。 */
  points?: [number, number][];
  /** true = polyline 开路径 (Line 不闭合、不填充); 缺省 = polygon 闭合。 */
  open?: boolean;
  /** 原始 CSS 色(轨迹色 / 类别色);内部转 hex。 */
  color: string;
  /** 插值帧或遮挡 → 虚线(对齐旧 SVG VideoTrackShape 的 "6 4")。 */
  dashed: boolean;
  /** 当前帧是 AI 追出的关键帧 → 左上角 violet 角标 (区别于插值虚线、人工实线)。 */
  predicted?: boolean;
  selected: boolean;
  size: VideoPixelSize;
  scale: number;
  visual: AnnotationVisualConfig;
}

/**
 * 单个视频 track 框(Konva,抄图片 KonvaBox 范式)。
 *
 * 像素空间(归一化 × size,决策 B);线宽/虚线走 `/scale` 抵消(替代旧 SVG 的
 * `non-scaling-stroke`,屏幕恒定);线宽/填充 alpha 复用 annotationVisual 纯函数
 * (与图片同源)。轨迹层 listening=false，不处理交互。
 */
function VideoKonvaTrackShapeComponent({
  geom,
  points,
  open = false,
  color,
  dashed,
  predicted = false,
  selected,
  size,
  scale,
  visual,
}: VideoKonvaTrackShapeProps) {
  const hex = colorToHex(color);
  const sw = screenToWorld(strokeWidthFor(selected, visual), scale);
  const x = geom.x * size.w;
  const y = geom.y * size.h;
  const badge = 6 / scale; // 屏幕恒定的角标边长
  const dash = dashed ? [6 / scale, 4 / scale] : undefined;
  const fill = hexToRgba(hex, fillAlpha(selected, visual));
  // polyline (open) 至少 2 点、不闭合不填充; polygon 至少 3 点、闭合带填充。
  const minPts = open ? 2 : 3;
  return (
    <Fragment>
      {points && points.length >= minPts ? (
        <Line
          name="video-track-shape"
          points={points.flatMap(([px, py]) => [px * size.w, py * size.h])}
          closed={!open}
          stroke={hex}
          strokeWidth={sw}
          dash={dash}
          fill={open ? undefined : fill}
          lineCap="round"
          lineJoin="round"
          listening={false}
        />
      ) : (
        <Rect
          name="video-track-shape"
          x={x}
          y={y}
          width={geom.w * size.w}
          height={geom.h * size.h}
          stroke={hex}
          strokeWidth={sw}
          dash={dash}
          fill={fill}
          listening={false}
        />
      )}
      {predicted && (
        <Rect
          name="video-track-predicted-badge"
          x={x}
          y={y}
          width={badge}
          height={badge}
          fill={PREDICTED_BADGE_HEX}
          listening={false}
        />
      )}
    </Fragment>
  );
}

export const VideoKonvaTrackShape = memo(VideoKonvaTrackShapeComponent);

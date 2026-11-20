import { useMemo } from "react";
import { Layer, Line, Circle, Rect, Ellipse, Group, Text } from "react-konva";
import type { KeypointSchema } from "@/types";
import { useTheme } from "@/hooks/useTheme";
import { colorToHex, cssVarToHex, hexToRgba } from "./colors";
import { strokeWidthFor, type AnnotationVisualConfig } from "./annotationVisual";
import { screenToWorld } from "./shared/viewport/scaleCancel";
import { VideoKonvaTrackShape } from "./VideoKonvaTrackShape";
import type { VideoPixelSize } from "./videoKonvaCoordinates";
import type { VideoEntryView, VideoGhostView, VideoTrackPreviewView } from "./videoFrameViews";
import { keypointColorByIndex } from "./ImageStageShapes";

// 关键帧圆点半径(世界单位,= 旧 SVG viewBox 单位 0.0038/0.0052,随画布缩放;
// 描边走 /scale 屏幕恒定,与旧 non-scaling-stroke 一致)。
const TRACK_KEYFRAME_DOT_RADIUS = 0.0038;
const TRACK_OCCLUDED_DOT_RADIUS = 0.0052;

interface VideoKonvaTracksLayerProps {
  entries: VideoEntryView[];
  previews: VideoTrackPreviewView[];
  ghost: VideoGhostView | null;
  /** 跨网格帧续写参考框(非选中待续轨迹);比选中 ghost 更淡, 提示「仅参考、点选后可续」。 */
  carryOverGhosts?: VideoGhostView[];
  size: VideoPixelSize;
  scale: number;
  visual: AnnotationVisualConfig;
  keypointSchema?: KeypointSchema | null;
}

/**
 * 视频 track 可视层(Konva Layer "tracks")。
 *
 * 渲染:轨迹预览线(Line)+ 选中态关键帧圆点(Circle)+ 当前帧 track 框
 * (VideoKonvaTrackShape)+ 选中轨迹 ghost 参考框。坐标像素空间(归一化 × size);
 * 线宽/虚线 `/scale` 屏幕恒定(替代旧 SVG non-scaling-stroke),圆点半径世界单位
 * (随画布缩放)。轨迹层 listening=false，不处理交互。
 */
export function VideoKonvaTracksLayer({
  entries,
  previews,
  ghost,
  carryOverGhosts,
  size,
  scale,
  visual,
  keypointSchema,
}: VideoKonvaTracksLayerProps) {
  const { resolved: theme } = useTheme();
  const occludedDotFill = useMemo(() => cssVarToHex("--sc-card", theme), [theme]);

  return (
    <Layer name="tracks" listening={false}>
      {previews.map((preview) => {
        if (preview.points.length === 0) return null;
        if (preview.points.length === 1 && !preview.selected) return null;
        const hex = colorToHex(preview.color);
        return (
          <Line
            key={`preview-line-${preview.key}`}
            name="video-track-path-preview"
            points={preview.points.flatMap((p) => [p.x * size.w, p.y * size.h])}
            stroke={hex}
            strokeWidth={(preview.selected ? 2.5 : 1.5) / scale}
            dash={preview.selected ? undefined : [4 / scale, 4 / scale]}
            lineCap="round"
            lineJoin="round"
            opacity={preview.selected ? 0.82 : 0.42}
            listening={false}
          />
        );
      })}
      {previews.flatMap((preview) =>
        preview.selected
          ? preview.points.map((p) => {
              const hex = colorToHex(preview.color);
              return (
                <Circle
                  key={`kf-${preview.key}-${p.frame}`}
                  name="video-track-keyframe-dot"
                  x={p.x * size.w}
                  y={p.y * size.h}
                  radius={
                    (p.occluded ? TRACK_OCCLUDED_DOT_RADIUS : TRACK_KEYFRAME_DOT_RADIUS) * size.w
                  }
                  fill={p.occluded ? occludedDotFill : hex}
                  stroke={hex}
                  strokeWidth={1.5 / scale}
                  opacity={0.82}
                  listening={false}
                />
              );
            })
          : [],
      )}
      {entries.map((entry) => {
        if (entry.rotatedBbox) {
          const geometry = entry.rotatedBbox;
          const hex = colorToHex(entry.color);
          return (
            <Group
              key={entry.key}
              name="video-rotated-box"
              x={geometry.cx * size.w}
              y={geometry.cy * size.h}
              rotation={geometry.angle}
            >
              <Rect
                x={-(geometry.w * size.w) / 2}
                y={-(geometry.h * size.h) / 2}
                width={geometry.w * size.w}
                height={geometry.h * size.h}
                stroke={hex}
                strokeWidth={screenToWorld(strokeWidthFor(entry.selected, visual), scale)}
                fill={hexToRgba(
                  hex,
                  entry.selected ? visual.fillOpacitySelected : visual.fillOpacity,
                )}
                shadowEnabled={entry.selected}
                shadowColor={hex}
                shadowBlur={8 / scale}
                listening={false}
              />
            </Group>
          );
        }
        if (entry.keypoints) {
          const edges = keypointSchema?.edges ?? [];
          return (
            <Group key={entry.key} name="video-keypoints" listening={false}>
              {edges.map(([from, to], index) => {
                const a = entry.keypoints?.[from];
                const b = entry.keypoints?.[to];
                if (!a || !b || a.v === 0 || b.v === 0) return null;
                return (
                  <Line
                    key={`edge-${index}`}
                    points={[a.x * size.w, a.y * size.h, b.x * size.w, b.y * size.h]}
                    stroke={colorToHex(entry.color)}
                    strokeWidth={screenToWorld(strokeWidthFor(entry.selected, visual), scale)}
                    opacity={a.v === 1 || b.v === 1 ? 0.6 : 1}
                  />
                );
              })}
              {entry.keypoints.map((point, index) => {
                const color = keypointColorByIndex(index, keypointSchema);
                const radius = (entry.selected ? 5 : 4) / scale;
                return (
                  <Group key={`point-${index}`}>
                    <Circle
                      x={point.x * size.w}
                      y={point.y * size.h}
                      radius={point.v === 0 ? radius * 0.6 : radius}
                      fill={
                        point.v === 2 ? color : point.v === 1 ? "white" : hexToRgba(color, 0.25)
                      }
                      stroke={point.v === 0 ? undefined : color}
                      strokeWidth={1.5 / scale}
                      shadowEnabled={entry.selected && point.v > 0}
                      shadowColor={color}
                      shadowBlur={6 / scale}
                    />
                    {entry.selected && point.v > 0 && keypointSchema?.nodes[index]?.name && (
                      <Text
                        x={point.x * size.w + radius + 2 / scale}
                        y={point.y * size.h - visual.labelFontSize / (2 * scale)}
                        text={keypointSchema.nodes[index].name}
                        fill={color}
                        fontSize={(visual.labelFontSize - 1) / scale}
                      />
                    )}
                  </Group>
                );
              })}
            </Group>
          );
        }
        return (
          <VideoKonvaTrackShape
            key={entry.key}
            geom={entry.geom}
            points={entry.points}
            open={entry.open}
            color={entry.color}
            dashed={entry.dashed}
            predicted={entry.predicted}
            selected={entry.selected}
            size={size}
            scale={scale}
            visual={visual}
          />
        );
      })}
      {ghost && ghost.points && (
        <Line
          name="video-track-ghost"
          points={ghost.points.flatMap(([px, py]) => [px * size.w, py * size.h])}
          closed={!ghost.open}
          stroke={colorToHex(ghost.color)}
          strokeWidth={screenToWorld(strokeWidthFor(false, visual), scale)}
          dash={[6 / scale, 4 / scale]}
          lineCap="round"
          lineJoin="round"
          fill={ghost.open ? undefined : hexToRgba(colorToHex(ghost.color), 0.05)}
          opacity={0.6}
          listening={false}
        />
      )}
      {ghost &&
        !ghost.points &&
        (() => {
          const hex = colorToHex(ghost.color);
          const stroke = screenToWorld(strokeWidthFor(false, visual), scale);
          // kalman 模式:在参考框外画一圈淡色误差椭圆——半轴 = 框半宽高 + 2σ(≈95% 置信),
          // σ 随外推距离/关键帧稀疏度增长,膨胀的椭圆即「预测越不确定、椭圆越大」的视觉提示。
          const cx = (ghost.geom.x + ghost.geom.w / 2) * size.w;
          const cy = (ghost.geom.y + ghost.geom.h / 2) * size.h;
          const ellipse = ghost.uncertainty ? (
            <Ellipse
              name="video-track-ghost-uncertainty"
              x={cx}
              y={cy}
              radiusX={(ghost.geom.w / 2) * size.w + 2 * ghost.uncertainty.sx * size.w}
              radiusY={(ghost.geom.h / 2) * size.h + 2 * ghost.uncertainty.sy * size.h}
              stroke={hex}
              strokeWidth={stroke}
              dash={[3 / scale, 5 / scale]}
              opacity={0.3}
              listening={false}
            />
          ) : null;
          return (
            <>
              {ellipse}
              <Rect
                name="video-track-ghost"
                x={ghost.geom.x * size.w}
                y={ghost.geom.y * size.h}
                width={ghost.geom.w * size.w}
                height={ghost.geom.h * size.h}
                stroke={hex}
                strokeWidth={stroke}
                dash={[6 / scale, 4 / scale]}
                fill={hexToRgba(hex, 0.05)}
                opacity={0.6}
                listening={false}
              />
            </>
          );
        })()}
      {(carryOverGhosts ?? []).map((g) => {
        const hex = colorToHex(g.color);
        const stroke = screenToWorld(strokeWidthFor(false, visual), scale);
        // 点集轨迹的续写虚影按各自几何画轮廓/折线; bbox 轨迹画外接框(现状)。
        if (g.points) {
          return (
            <Line
              key={`carryover-${g.id}`}
              name="video-track-carryover-ghost"
              points={g.points.flatMap(([px, py]) => [px * size.w, py * size.h])}
              closed={!g.open}
              stroke={hex}
              strokeWidth={stroke}
              dash={[3 / scale, 5 / scale]}
              lineCap="round"
              lineJoin="round"
              opacity={0.34}
              listening={false}
            />
          );
        }
        return (
          <Rect
            key={`carryover-${g.id}`}
            name="video-track-carryover-ghost"
            x={g.geom.x * size.w}
            y={g.geom.y * size.h}
            width={g.geom.w * size.w}
            height={g.geom.h * size.h}
            stroke={hex}
            strokeWidth={stroke}
            dash={[3 / scale, 5 / scale]}
            opacity={0.34}
            listening={false}
          />
        );
      })}
    </Layer>
  );
}

import { Layer, Rect, Label, Line, Tag, Text } from "react-konva";
import type { KonvaEventObject } from "konva/lib/Node";
import { classColorForCanvas, colorToHex, displayClassName, hexToRgba } from "./colors";
import {
  buildLabelText,
  shouldShowLabel,
  DEFAULT_ANNOTATION_VISUAL,
  type AnnotationVisualConfig,
} from "./annotationVisual";
import { BOX_LABEL_FONT_FAMILY, BOX_LABEL_PAD_PX, labelOffsetWorld } from "./boxVisual";
import type { VideoPixelSize } from "./videoKonvaCoordinates";
import { predictionSourceLabel, type AiBox } from "../state/transforms";

interface VideoKonvaAiLayerProps {
  /** 已按当前帧过滤的 AI 候选框(video_bbox, 归一化 0-1 坐标)。 */
  boxes: AiBox[];
  size: VideoPixelSize;
  scale: number;
  selectedId: string | null;
  /** 仅 select 工具下可点选(镜像图片舞台的 ai 层 listening=selectActive)。 */
  listening: boolean;
  fadedIds?: Set<string>;
  /** 视觉配置:标签内容(ai 段)/ 显隐 / 字号,与图片工作台 AI 框标签同源。 */
  visual?: AnnotationVisualConfig;
  onSelect: (id: string) => void;
}

/** AI 框标签文本:类别名恒显 + ai 段(✦来源 / 置信度 / 分组号 / 属性)。与图片栈同一套。 */
function aiLabelText(b: AiBox, content: AnnotationVisualConfig["labelContent"]["ai"]): string {
  return buildLabelText(
    {
      className: displayClassName(b.cls),
      instanceId: null,
      confidence: b.conf,
      attributes: b.attributes ?? null,
      sourcePrefix: `✦ ${predictionSourceLabel(b.predictionSource)} `,
    },
    content,
  );
}

/**
 * 视频 AI 候选可视 + 可选层(Konva Layer "ai")。
 *
 * 渲染当前帧的 AI 候选框(虚线 + 类色 + 淡填充), 点击选中(cancelBubble 阻止冒泡到
 * Stage 级 picking, 否则会被空击逻辑立刻取消选)。采纳/驳回按钮由 SelectionOverlay
 * 在 DOM 层贴框呈现(见 VideoKonvaStage)。样式对齐图片工作台的候选框。
 *
 * 框顶标签复用 common.labelContent 的 ai 段与 labelVisibility 门控,与图片工作台 AI 框
 * 标签完全同源(buildLabelText,含 ✦来源前缀)。标签置于框顶上方、字号屏幕恒定,
 * 抄 VideoKonvaOverlayLayer 的 Label/Tag/Text 范式。
 */
export function VideoKonvaAiLayer({
  boxes,
  size,
  scale,
  selectedId,
  listening,
  fadedIds,
  visual = DEFAULT_ANNOTATION_VISUAL,
  onSelect,
}: VideoKonvaAiLayerProps) {
  const aiContent = visual.labelContent.ai;
  const labelFontSize = visual.labelFontSize / scale;
  return (
    <Layer name="ai" listening={listening}>
      {boxes.map((b) => {
        const color = classColorForCanvas(b.cls);
        const selected = selectedId === b.id;
        const faded = fadedIds?.has(b.id) ?? false;
        const points = b.polygon ?? b.polyline;
        if (points) {
          return (
            <Line
              key={b.id}
              name="video-ai-candidate"
              points={points.flatMap(([x, y]) => [x * size.w, y * size.h])}
              closed={Boolean(b.polygon)}
              stroke={color}
              strokeWidth={(selected ? 2.5 : 1.5) / scale}
              dash={[4 / scale, 3 / scale]}
              fill={b.polygon ? hexToRgba(color, selected ? 0.12 : 0.05) : undefined}
              opacity={faded ? 0.35 : 1}
              lineCap="round"
              lineJoin="round"
              onPointerDown={(e: KonvaEventObject<PointerEvent>) => {
                e.cancelBubble = true;
                onSelect(b.id);
              }}
            />
          );
        }
        if (b.geometry?.type === "video_track_mask") return null;
        return (
          <Rect
            key={b.id}
            name="video-ai-candidate"
            x={b.x * size.w}
            y={b.y * size.h}
            width={b.w * size.w}
            height={b.h * size.h}
            stroke={color}
            strokeWidth={(selected ? 2.5 : 1.5) / scale}
            dash={[4 / scale, 3 / scale]}
            fill={hexToRgba(color, selected ? 0.12 : 0.05)}
            opacity={faded ? 0.35 : 1}
            onPointerDown={(e: KonvaEventObject<PointerEvent>) => {
              e.cancelBubble = true;
              onSelect(b.id);
            }}
          />
        );
      })}
      {boxes.map((b) => {
        const selected = selectedId === b.id;
        if (!shouldShowLabel(selected, visual.labelVisibility)) return null;
        const text = aiLabelText(b, aiContent);
        if (!text) return null;
        const faded = fadedIds?.has(b.id) ?? false;
        const hex = colorToHex(classColorForCanvas(b.cls));
        return (
          <Label
            key={`label-${b.id}`}
            name="video-ai-label"
            x={b.x * size.w}
            y={b.y * size.h - labelOffsetWorld(visual.labelFontSize, scale)}
            opacity={faded ? 0.35 : 1}
            listening={false}
          >
            <Tag fill={hex} cornerRadius={3 / scale} />
            <Text
              text={text}
              fill="white"
              fontSize={labelFontSize}
              padding={BOX_LABEL_PAD_PX / scale}
              fontFamily={BOX_LABEL_FONT_FAMILY}
            />
          </Label>
        );
      })}
    </Layer>
  );
}

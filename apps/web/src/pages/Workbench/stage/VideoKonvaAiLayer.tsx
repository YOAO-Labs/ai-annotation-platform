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
  boxes: AiBox[];
  size: VideoPixelSize;
  scale: number;
  selectedId: string | null;
  listening: boolean;
  fadedIds?: Set<string>;
  visual?: AnnotationVisualConfig;
  onSelect: (id: string) => void;
}

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

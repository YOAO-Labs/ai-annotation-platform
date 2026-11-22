import { Layer, Rect, Label, Tag, Text } from "react-konva";
import { classColor, colorToHex, hexToRgba } from "./colors";
import { fillAlpha, strokeWidthFor, type AnnotationVisualConfig } from "./annotationVisual";
import { screenToWorld } from "./shared/viewport/scaleCancel";
import { BOX_LABEL_FONT_FAMILY, BOX_LABEL_PAD_PX, labelOffsetWorld } from "./boxVisual";
import type { VideoPixelSize } from "./videoKonvaCoordinates";
import type { VideoLabelView } from "./videoFrameViews";
import type { VideoStageGeom } from "./videoStageTypes";

interface VideoKonvaOverlayLayerProps {
  pendingDraft?: { geom: VideoStageGeom; className: string } | null;
  labels: VideoLabelView[];
  size: VideoPixelSize;
  scale: number;
  visual: AnnotationVisualConfig;
}

export function VideoKonvaOverlayLayer({
  pendingDraft,
  labels,
  size,
  scale,
  visual,
}: VideoKonvaOverlayLayerProps) {
  const labelFontSize = visual.labelFontSize / scale;
  return (
    <Layer name="overlay" listening={false}>
      {pendingDraft &&
        (() => {
          const hex = colorToHex(classColor(pendingDraft.className));
          const g = pendingDraft.geom;
          return (
            <Rect
              name="video-pending-draft"
              x={g.x * size.w}
              y={g.y * size.h}
              width={g.w * size.w}
              height={g.h * size.h}
              stroke={hex}
              strokeWidth={screenToWorld(strokeWidthFor(false, visual), scale)}
              dash={[6 / scale, 4 / scale]}
              fill={hexToRgba(hex, fillAlpha(false, visual))}
              opacity={0.9}
              listening={false}
            />
          );
        })()}
      {labels.map((label) => {
        const hex = colorToHex(label.color);
        return (
          <Label
            key={label.key}
            name="video-label"
            x={label.geom.x * size.w}
            y={label.geom.y * size.h - labelOffsetWorld(visual.labelFontSize, scale)}
            opacity={label.opacity ?? 1}
            listening={false}
          >
            <Tag fill={hex} cornerRadius={3 / scale} />
            <Text
              text={label.text}
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

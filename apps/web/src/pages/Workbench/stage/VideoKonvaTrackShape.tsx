import { memo, Fragment } from "react";
import { Rect, Line } from "react-konva";
import type { VideoPolygonGeometry } from "@/types";
import { colorToHex, hexToRgba } from "./colors";
import { fillAlpha, strokeWidthFor, type AnnotationVisualConfig } from "./annotationVisual";
import { screenToWorld } from "./shared/viewport/scaleCancel";
import { drawEvenOddShape } from "./shared/geometry/evenOddFill";
import type { VideoPixelSize } from "./videoKonvaCoordinates";
import type { VideoStageGeom } from "./videoStageTypes";

const PREDICTED_BADGE_HEX = "#8b5cf6";

interface VideoKonvaTrackShapeProps {
  geom: VideoStageGeom;
  points?: [number, number][];
  holes?: VideoPolygonGeometry["holes"];
  open?: boolean;
  color: string;
  dashed: boolean;
  predicted?: boolean;
  selected: boolean;
  size: VideoPixelSize;
  scale: number;
  visual: AnnotationVisualConfig;
}

function VideoKonvaTrackShapeComponent({
  geom,
  points,
  holes,
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
  const badge = 6 / scale;
  const dash = dashed ? [6 / scale, 4 / scale] : undefined;
  const fill = hexToRgba(hex, fillAlpha(selected, visual));
  const minPts = open ? 2 : 3;
  const rings = points && !open && holes?.length ? [{ points, holes }] : undefined;
  return (
    <Fragment>
      {points && points.length >= minPts ? (
        <Line
          name="video-track-shape"
          points={points.flatMap(([px, py]) => [px * size.w, py * size.h])}
          closed={!open}
          fillRule={rings ? "evenodd" : undefined}
          sceneFunc={
            rings
              ? (context, shape) => {
                  drawEvenOddShape(context, shape, rings, size.w, size.h);
                }
              : undefined
          }
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

import { Layer, Rect, Group, Circle, Line } from "react-konva";
import type Konva from "konva";
import type { Keypoint, VideoRotatedBboxGeometry } from "@/types";
import { colorToHex, hexToRgba } from "./colors";
import { BOX_HANDLE_SCREEN_PX } from "./boxVisual";
import { screenToWorld } from "./shared/viewport/scaleCancel";
import type { VideoPixelSize } from "./videoKonvaCoordinates";
import type { ResizeDirection } from "./ResizeHandles";
import type { VideoDragState, VideoStageGeom } from "./videoStageTypes";

const HANDLE_DIRECTIONS: { dir: ResizeDirection; cx: number; cy: number; cursor: string }[] = [
  { dir: "nw", cx: 0, cy: 0, cursor: "nwse-resize" },
  { dir: "n", cx: 0.5, cy: 0, cursor: "ns-resize" },
  { dir: "ne", cx: 1, cy: 0, cursor: "nesw-resize" },
  { dir: "e", cx: 1, cy: 0.5, cursor: "ew-resize" },
  { dir: "se", cx: 1, cy: 1, cursor: "nwse-resize" },
  { dir: "s", cx: 0.5, cy: 1, cursor: "ns-resize" },
  { dir: "sw", cx: 0, cy: 1, cursor: "nesw-resize" },
  { dir: "w", cx: 0, cy: 0.5, cursor: "ew-resize" },
];

export type VideoHandleBox = { id: string; geom: VideoStageGeom; color: string };
export type VideoPreviewBox = { geom: VideoStageGeom; color: string };
export type VideoHandleObb = { id: string; geometry: VideoRotatedBboxGeometry; color: string };
export type VideoPreviewObb = { geometry: VideoRotatedBboxGeometry; color: string };
export type VideoHandleKeypoints = { id: string; points: Keypoint[]; color: string };

interface VideoKonvaInteractionLayerProps {
  size: VideoPixelSize;
  scale: number;
  drag: VideoDragState;
  handleBox: VideoHandleBox | null;
  preview: VideoPreviewBox | null;
  handleObb?: VideoHandleObb | null;
  previewObb?: VideoPreviewObb | null;
  handleKeypoints?: VideoHandleKeypoints | null;
  onResizeHandlePointerDown: (
    dir: ResizeDirection,
    entryId: string,
    geom: VideoStageGeom,
    e: Konva.KonvaEventObject<PointerEvent>,
  ) => void;
  onObbResizePointerDown?: (
    dir: ResizeDirection,
    entryId: string,
    geometry: VideoRotatedBboxGeometry,
    e: Konva.KonvaEventObject<PointerEvent>,
  ) => void;
  onObbRotatePointerDown?: (
    entryId: string,
    geometry: VideoRotatedBboxGeometry,
    e: Konva.KonvaEventObject<PointerEvent>,
  ) => void;
  onKeypointPointerDown?: (
    entryId: string,
    nodeIdx: number,
    points: Keypoint[],
    e: Konva.KonvaEventObject<PointerEvent>,
  ) => void;
}

export function VideoKonvaInteractionLayer({
  size,
  scale,
  drag,
  handleBox,
  preview,
  handleObb,
  previewObb,
  handleKeypoints,
  onResizeHandlePointerDown,
  onObbResizePointerDown,
  onObbRotatePointerDown,
  onKeypointPointerDown,
}: VideoKonvaInteractionLayerProps) {
  const handleSize = screenToWorld(BOX_HANDLE_SCREEN_PX, scale);
  const previewHex = preview ? colorToHex(preview.color) : "";

  return (
    <Layer name="interaction">
      {preview && (
        <Rect
          name={drag?.kind === "draw" ? "video-pending-draft" : "video-drag-preview"}
          x={preview.geom.x * size.w}
          y={preview.geom.y * size.h}
          width={preview.geom.w * size.w}
          height={preview.geom.h * size.h}
          stroke={previewHex}
          strokeWidth={screenToWorld(2, scale)}
          dash={[screenToWorld(6, scale), screenToWorld(4, scale)]}
          fill={hexToRgba(previewHex, 0.06)}
          listening={false}
        />
      )}
      {previewObb && (
        <Group
          name="video-obb-drag-preview"
          x={previewObb.geometry.cx * size.w}
          y={previewObb.geometry.cy * size.h}
          rotation={previewObb.geometry.angle}
          listening={false}
        >
          <Rect
            x={-(previewObb.geometry.w * size.w) / 2}
            y={-(previewObb.geometry.h * size.h) / 2}
            width={previewObb.geometry.w * size.w}
            height={previewObb.geometry.h * size.h}
            stroke={colorToHex(previewObb.color)}
            strokeWidth={screenToWorld(2, scale)}
            dash={[screenToWorld(6, scale), screenToWorld(4, scale)]}
            fill={hexToRgba(colorToHex(previewObb.color), 0.06)}
          />
        </Group>
      )}
      {handleBox &&
        HANDLE_DIRECTIONS.map(({ dir, cx, cy, cursor }) => (
          <Rect
            key={dir}
            name="video-resize-handle"
            x={(handleBox.geom.x + handleBox.geom.w * cx) * size.w - handleSize / 2}
            y={(handleBox.geom.y + handleBox.geom.h * cy) * size.h - handleSize / 2}
            width={handleSize}
            height={handleSize}
            fill="white"
            stroke={colorToHex(handleBox.color)}
            strokeWidth={screenToWorld(1.5, scale)}
            cornerRadius={screenToWorld(2, scale)}
            hitStrokeWidth={screenToWorld(6, scale)}
            onPointerDown={(e: Konva.KonvaEventObject<PointerEvent>) =>
              onResizeHandlePointerDown(dir, handleBox.id, handleBox.geom, e)
            }
            onMouseEnter={(e: Konva.KonvaEventObject<MouseEvent>) => {
              const stage = e.target.getStage();
              if (stage) stage.container().style.cursor = cursor;
            }}
            onMouseLeave={(e: Konva.KonvaEventObject<MouseEvent>) => {
              const stage = e.target.getStage();
              if (stage) stage.container().style.cursor = "";
            }}
          />
        ))}
      {handleObb &&
        (() => {
          const geometry = handleObb.geometry;
          const width = geometry.w * size.w;
          const height = geometry.h * size.h;
          const halfWidth = width / 2;
          const halfHeight = height / 2;
          const color = colorToHex(handleObb.color);
          const rotationY = -halfHeight - screenToWorld(18, scale);
          return (
            <Group
              name="video-obb-handles"
              x={geometry.cx * size.w}
              y={geometry.cy * size.h}
              rotation={geometry.angle}
            >
              {HANDLE_DIRECTIONS.map(({ dir, cx, cy, cursor }) => (
                <Rect
                  key={dir}
                  x={-halfWidth + width * cx - handleSize / 2}
                  y={-halfHeight + height * cy - handleSize / 2}
                  width={handleSize}
                  height={handleSize}
                  fill="white"
                  stroke={color}
                  strokeWidth={screenToWorld(1.5, scale)}
                  cornerRadius={screenToWorld(2, scale)}
                  hitStrokeWidth={screenToWorld(6, scale)}
                  onPointerDown={(e) => onObbResizePointerDown?.(dir, handleObb.id, geometry, e)}
                  onMouseEnter={(e) => {
                    const stage = e.target.getStage();
                    if (stage) stage.container().style.cursor = cursor;
                  }}
                  onMouseLeave={(e) => {
                    const stage = e.target.getStage();
                    if (stage) stage.container().style.cursor = "";
                  }}
                />
              ))}
              <Line
                points={[0, -halfHeight, 0, rotationY]}
                stroke={color}
                strokeWidth={screenToWorld(1.5, scale)}
                listening={false}
              />
              <Circle
                x={0}
                y={rotationY}
                radius={handleSize / 2}
                hitStrokeWidth={handleSize}
                fill="white"
                stroke={color}
                strokeWidth={screenToWorld(1.5, scale)}
                onPointerDown={(e) => onObbRotatePointerDown?.(handleObb.id, geometry, e)}
                onMouseEnter={(e) => {
                  const stage = e.target.getStage();
                  if (stage) stage.container().style.cursor = "grab";
                }}
                onMouseLeave={(e) => {
                  const stage = e.target.getStage();
                  if (stage) stage.container().style.cursor = "";
                }}
              />
            </Group>
          );
        })()}
      {handleKeypoints?.points.map((point, index) =>
        point.v === 0 ? null : (
          <Circle
            key={`keypoint-handle-${index}`}
            name="video-keypoint-handle"
            x={point.x * size.w}
            y={point.y * size.h}
            radius={5 / scale}
            hitStrokeWidth={10 / scale}
            fill={point.v === 2 ? colorToHex(handleKeypoints.color) : "white"}
            stroke={colorToHex(handleKeypoints.color)}
            strokeWidth={1.5 / scale}
            onPointerDown={(e) =>
              onKeypointPointerDown?.(handleKeypoints.id, index, handleKeypoints.points, e)
            }
            onMouseEnter={(e) => {
              const stage = e.target.getStage();
              if (stage) stage.container().style.cursor = e.evt.altKey ? "pointer" : "grab";
            }}
            onMouseLeave={(e) => {
              const stage = e.target.getStage();
              if (stage) stage.container().style.cursor = "";
            }}
          />
        ),
      )}
    </Layer>
  );
}

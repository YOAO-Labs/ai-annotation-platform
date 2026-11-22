import type Konva from "konva";
import { Circle, Group, Label, Line, Rect, Tag, Text } from "react-konva";
import type { Annotation, RotatedBboxGeometry, Keypoint, KeypointSchema } from "@/types";
import { useMemo } from "react";
import { predictionSourceLabel, type AiBox } from "../state/transforms";
import type { ResizeDirection } from "./ResizeHandles";
import type { ClassesConfig } from "@/api/projects";
import { classColorForCanvas, displayClassName, hexToRgba } from "./colors";
import { buildVertexIndex } from "./iou-index";
import type { Pt } from "./polygonGeom";
import { simplifyPolygon } from "./shared/geometry/simplify";
import {
  BOX_HANDLE_SCREEN_PX,
  BOX_LABEL_FONT_FAMILY,
  BOX_LABEL_PAD_PX,
  labelOffsetWorld,
} from "./boxVisual";
import { screenToWorld } from "./shared/viewport/scaleCancel";
import {
  buildLabelText,
  fillAlpha,
  shouldShowLabel,
  strokeWidthFor,
  type AnnotationVisualConfig,
} from "./annotationVisual";
import {
  collectOuterRings,
  drawEvenOddShape,
  type PolygonRing,
} from "./shared/geometry/evenOddFill";

const LOD_VERTEX_THRESHOLD = 60;
const VERTEX_CULL_THRESHOLD = 60;
const DEFAULT_FADED_OPACITY = 0.35;

function shapeLabelText(
  b: Annotation,
  isAi: boolean,
  content: AnnotationVisualConfig["labelContent"],
): string {
  const predictionSource = (b as Partial<AiBox>).predictionSource ?? null;
  return buildLabelText(
    {
      className: displayClassName(b.cls),
      instanceId: null,
      confidence: b.conf,
      attributes: (b as { attributes?: Record<string, unknown> }).attributes ?? null,
      sourcePrefix: isAi ? `✦ ${predictionSourceLabel(predictionSource)} ` : undefined,
    },
    isAi ? content.ai : content.single,
  );
}

export interface ViewportBBox {
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
}

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
interface KonvaBoxProps {
  b: Annotation;
  annotationId?: string;
  isAi: boolean;
  selected: boolean;
  editable: boolean;
  faded: boolean;
  fadedOpacity?: number;
  visual: AnnotationVisualConfig;
  colorConfig?: ClassesConfig;
  occluded?: boolean;
  imgW: number;
  imgH: number;
  scale: number;
  onClick: (e?: Konva.KonvaEventObject<MouseEvent>) => void;
  onMoveStart: ((e: Konva.KonvaEventObject<MouseEvent>) => void) | null;
  onResizeStart: ((dir: ResizeDirection, e: Konva.KonvaEventObject<MouseEvent>) => void) | null;
}

export const SIBLING_HIGHLIGHT_COLOR = "#38bdf8";

export function KonvaBox({
  b,
  annotationId,
  isAi,
  selected,
  editable,
  faded,
  occluded = false,
  fadedOpacity = DEFAULT_FADED_OPACITY,
  visual,
  colorConfig,
  imgW,
  imgH,
  scale,
  onClick,
  onMoveStart,
  onResizeStart,
}: KonvaBoxProps) {
  const color = classColorForCanvas(b.cls, colorConfig);
  const sw = screenToWorld(strokeWidthFor(selected, visual), scale);
  const handleSize = screenToWorld(BOX_HANDLE_SCREEN_PX, scale);
  const labelFontSize = visual.labelFontSize / scale;
  const isUserSelected = selected && !isAi && editable;
  const labelText = shapeLabelText(b, isAi, visual.labelContent);
  const opacity = faded ? fadedOpacity : occluded ? 0.5 : 1;
  const needsOpacityBuffer = opacity !== 1;

  return (
    <Group id={annotationId}>
      <Rect
        x={b.x * imgW}
        y={b.y * imgH}
        width={b.w * imgW}
        height={b.h * imgH}
        stroke={color}
        strokeWidth={sw}
        dash={isAi || occluded ? [4 / scale, 3 / scale] : undefined}
        fill={hexToRgba(color, fillAlpha(selected, visual))}
        opacity={opacity}
        perfectDrawEnabled={needsOpacityBuffer}
        shadowEnabled={selected && !faded}
        shadowColor={color}
        shadowBlur={8 / scale}
        shadowOpacity={0.4}
        onClick={(e) => {
          e.cancelBubble = true;
          onClick(e);
        }}
        onMouseDown={(e) => {
          if (!isUserSelected || e.evt.button !== 0 || !onMoveStart) return;
          e.cancelBubble = true;
          onMoveStart(e);
        }}
        onMouseEnter={(e) => {
          const stage = e.target.getStage();
          if (stage && isUserSelected) stage.container().style.cursor = "move";
        }}
        onMouseLeave={(e) => {
          const stage = e.target.getStage();
          if (stage) stage.container().style.cursor = "";
        }}
      />

      {shouldShowLabel(selected, visual.labelVisibility) && !(isAi && faded) && (
        <Label
          x={b.x * imgW}
          y={b.y * imgH - labelOffsetWorld(visual.labelFontSize, scale)}
          listening={false}
        >
          <Tag fill={color} cornerRadius={3 / scale} />
          <Text
            text={labelText}
            fill="white"
            fontSize={labelFontSize}
            padding={BOX_LABEL_PAD_PX / scale}
            fontFamily={BOX_LABEL_FONT_FAMILY}
          />
        </Label>
      )}

      {isUserSelected &&
        onResizeStart &&
        HANDLE_DIRECTIONS.map(({ dir, cx, cy, cursor }) => (
          <Rect
            key={dir}
            x={(b.x + b.w * cx) * imgW - handleSize / 2}
            y={(b.y + b.h * cy) * imgH - handleSize / 2}
            width={handleSize}
            height={handleSize}
            fill="white"
            stroke={color}
            strokeWidth={1.5 / scale}
            cornerRadius={2 / scale}
            onMouseDown={(e) => {
              e.cancelBubble = true;
              onResizeStart(dir, e);
            }}
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
    </Group>
  );
}

interface KonvaPolygonProps {
  b: Annotation;
  annotationId?: string;
  isAi: boolean;
  selected: boolean;
  faded: boolean;
  fadedOpacity?: number;
  visual: AnnotationVisualConfig;
  colorConfig?: ClassesConfig;
  occluded?: boolean;
  imgW: number;
  imgH: number;
  scale: number;
  onClick: (e?: Konva.KonvaEventObject<MouseEvent>) => void;
  points?: Pt[];
  selfIntersect?: boolean;
  editable?: boolean;
  onVertexMouseDown?: (vidx: number, e: Konva.KonvaEventObject<MouseEvent>) => void;
  onEdgeMouseDown?: (edgeIdx: number, e: Konva.KonvaEventObject<MouseEvent>) => void;
  onBodyMouseDown?: ((e: Konva.KonvaEventObject<MouseEvent>) => void) | null;
  viewportBBox?: ViewportBBox;
  holes?: Pt[][];
  multiPolygon?: PolygonRing[];
}

export function KonvaPolygon({
  b,
  annotationId,
  isAi,
  selected,
  faded,
  occluded = false,
  imgW,
  imgH,
  scale,
  onClick,
  fadedOpacity = DEFAULT_FADED_OPACITY,
  visual,
  colorConfig,
  points,
  selfIntersect,
  editable,
  onVertexMouseDown,
  onEdgeMouseDown,
  onBodyMouseDown,
  viewportBBox,
  holes,
  multiPolygon,
}: KonvaPolygonProps) {
  const color = classColorForCanvas(b.cls, colorConfig);
  const sw = screenToWorld(strokeWidthFor(selected, visual), scale);
  const labelFontSize = visual.labelFontSize / scale;
  const labelText = shapeLabelText(b, isAi, visual.labelContent);
  const ps = useMemo<Pt[]>(
    () => (points && points.length >= 3 ? points : (b.polygon ?? [])),
    [b.polygon, points],
  );
  const renderPs = useMemo<Pt[]>(() => {
    if (editable || selected) return ps;
    if (ps.length < LOD_VERTEX_THRESHOLD) return ps;
    if (scale <= 0 || imgW <= 0) return ps;
    const epsilon = 1 / (scale * Math.max(imgW, imgH));
    return simplifyPolygon(ps, epsilon);
  }, [ps, editable, selected, scale, imgW, imgH]);
  const flat: number[] = [];
  for (const [px, py] of renderPs) flat.push(px * imgW, py * imgH);
  const strokeColor = selfIntersect ? "oklch(0.55 0.22 25)" : color;

  const visibleVertexIdx = useMemo<Set<number> | null>(() => {
    if (!editable || !viewportBBox || ps.length < VERTEX_CULL_THRESHOLD) return null;
    const idx = buildVertexIndex(ps);
    return new Set(idx.verticesInBBox(viewportBBox));
  }, [editable, viewportBBox, ps]);

  const holesArr = holes ?? b.holes;
  const multiArr = multiPolygon ?? b.multiPolygon;
  const hasHolesOrMulti = !!(
    (multiArr && multiArr.length > 0) ||
    (holesArr && holesArr.some((ring) => ring.length >= 2))
  );
  const fillRings = useMemo<PolygonRing[]>(() => {
    if (!hasHolesOrMulti) return [];
    return collectOuterRings({
      primaryPoints: ps,
      holes: holesArr,
      multiPolygon: multiArr,
    });
  }, [hasHolesOrMulti, ps, holesArr, multiArr]);
  const drawEvenOdd = useMemo(
    () => (ctx: Konva.Context, shape: Konva.Shape) => {
      if (fillRings.length === 0) {
        ctx.beginPath();
        return;
      }
      drawEvenOddShape(ctx, shape, fillRings, imgW, imgH);
    },
    [fillRings, imgW, imgH],
  );

  return (
    <Group id={annotationId}>
      <Line
        points={flat}
        closed
        sceneFunc={hasHolesOrMulti ? drawEvenOdd : undefined}
        fillRule={hasHolesOrMulti ? "evenodd" : undefined}
        stroke={strokeColor}
        strokeWidth={sw}
        dash={isAi || selfIntersect || occluded ? [4 / scale, 3 / scale] : undefined}
        fill={hexToRgba(color, fillAlpha(selected, visual))}
        opacity={faded ? fadedOpacity : occluded ? 0.5 : 1}
        shadowEnabled={selected && !faded}
        shadowColor={selfIntersect ? "oklch(0.55 0.22 25)" : color}
        shadowBlur={8 / scale}
        shadowOpacity={0.4}
        onClick={(e) => {
          e.cancelBubble = true;
          onClick(e);
        }}
        onMouseDown={(e) => {
          if (!editable || !onBodyMouseDown || e.evt.button !== 0) return;
          e.cancelBubble = true;
          onBodyMouseDown(e);
        }}
        onMouseEnter={(e) => {
          const stage = e.target.getStage();
          if (stage && editable && onBodyMouseDown) stage.container().style.cursor = "move";
        }}
        onMouseLeave={(e) => {
          const stage = e.target.getStage();
          if (stage) stage.container().style.cursor = "";
        }}
      />
      {shouldShowLabel(selected, visual.labelVisibility) &&
        flat.length >= 2 &&
        !(isAi && faded) && (
          <Label
            x={flat[0]}
            y={flat[1] - labelOffsetWorld(visual.labelFontSize, scale)}
            listening={false}
          >
            <Tag fill={strokeColor} cornerRadius={3 / scale} />
            <Text
              text={labelText + (selfIntersect ? " ⚠" : "")}
              fill="white"
              fontSize={labelFontSize}
              padding={BOX_LABEL_PAD_PX / scale}
              fontFamily={BOX_LABEL_FONT_FAMILY}
            />
          </Label>
        )}

      {editable &&
        onEdgeMouseDown &&
        ps.map((_, i) => {
          if (
            visibleVertexIdx &&
            !visibleVertexIdx.has(i) &&
            !visibleVertexIdx.has((i + 1) % ps.length)
          ) {
            return null;
          }
          const a = ps[i];
          const c = ps[(i + 1) % ps.length];
          return (
            <Line
              key={`edge-${i}`}
              points={[a[0] * imgW, a[1] * imgH, c[0] * imgW, c[1] * imgH]}
              stroke="rgba(0,0,0,0)"
              strokeWidth={10 / scale}
              hitStrokeWidth={10 / scale}
              onMouseDown={(e) => {
                if (!e.evt.altKey) return;
                e.cancelBubble = true;
                onEdgeMouseDown(i, e);
              }}
              onMouseEnter={(e) => {
                if (!e.evt.altKey) return;
                const stage = e.target.getStage();
                if (stage) stage.container().style.cursor = "copy";
              }}
              onMouseLeave={(e) => {
                const stage = e.target.getStage();
                if (stage) stage.container().style.cursor = "";
              }}
            />
          );
        })}

      {editable &&
        onVertexMouseDown &&
        ps.map(([px, py], i) => {
          if (visibleVertexIdx && !visibleVertexIdx.has(i)) return null;
          return (
            <Circle
              key={`v-${i}`}
              x={px * imgW}
              y={py * imgH}
              radius={6 / scale}
              hitStrokeWidth={9 / scale}
              fill="white"
              stroke={color}
              strokeWidth={1.5 / scale}
              onMouseDown={(e) => {
                e.cancelBubble = true;
                onVertexMouseDown(i, e);
              }}
              onMouseEnter={(e) => {
                const stage = e.target.getStage();
                if (stage) stage.container().style.cursor = e.evt.shiftKey ? "not-allowed" : "grab";
              }}
              onMouseLeave={(e) => {
                const stage = e.target.getStage();
                if (stage) stage.container().style.cursor = "";
              }}
            />
          );
        })}
    </Group>
  );
}

interface KonvaRotatedBoxProps {
  b: Annotation;
  annotationId?: string;
  geometry: RotatedBboxGeometry;
  angle: number;
  isAi: boolean;
  selected: boolean;
  editable: boolean;
  faded: boolean;
  fadedOpacity?: number;
  visual: AnnotationVisualConfig;
  colorConfig?: ClassesConfig;
  occluded?: boolean;
  imgW: number;
  imgH: number;
  scale: number;
  onClick: (e?: Konva.KonvaEventObject<MouseEvent>) => void;
  onMoveStart?: ((e: Konva.KonvaEventObject<MouseEvent>) => void) | null;
  onRotateStart: ((e: Konva.KonvaEventObject<MouseEvent>) => void) | null;
  onResizeStart: ((dir: ResizeDirection, e: Konva.KonvaEventObject<MouseEvent>) => void) | null;
}

export function KonvaRotatedBox({
  b,
  annotationId,
  geometry,
  angle,
  isAi,
  selected,
  editable,
  faded,
  occluded = false,
  fadedOpacity = DEFAULT_FADED_OPACITY,
  visual,
  colorConfig,
  imgW,
  imgH,
  scale,
  onClick,
  onMoveStart,
  onRotateStart,
  onResizeStart,
}: KonvaRotatedBoxProps) {
  const color = classColorForCanvas(b.cls, colorConfig);
  const sw = screenToWorld(strokeWidthFor(selected, visual), scale);
  const handleSize = screenToWorld(BOX_HANDLE_SCREEN_PX, scale);
  const labelFontSize = visual.labelFontSize / scale;
  const isUserSelected = selected && !isAi && editable;
  const labelText = shapeLabelText(b, isAi, visual.labelContent);

  const cx = geometry.cx * imgW;
  const cy = geometry.cy * imgH;
  const wPx = geometry.w * imgW;
  const hPx = geometry.h * imgH;
  const hw = wPx / 2;
  const hh = hPx / 2;
  const handleOffset = 18 / scale;
  const handleY = -hh - handleOffset;

  return (
    <Group id={annotationId} x={cx} y={cy} rotation={angle}>
      <Rect
        x={0}
        y={0}
        offsetX={hw}
        offsetY={hh}
        width={wPx}
        height={hPx}
        stroke={color}
        strokeWidth={sw}
        dash={isAi || occluded ? [4 / scale, 3 / scale] : undefined}
        fill={hexToRgba(color, fillAlpha(selected, visual))}
        opacity={faded ? fadedOpacity : occluded ? 0.5 : 1}
        shadowEnabled={selected && !faded}
        shadowColor={color}
        shadowBlur={8 / scale}
        shadowOpacity={0.4}
        onClick={(e) => {
          e.cancelBubble = true;
          onClick(e);
        }}
        onMouseDown={(e) => {
          if (!isUserSelected || !onMoveStart || e.evt.button !== 0) return;
          e.cancelBubble = true;
          onMoveStart(e);
        }}
        onMouseEnter={(e) => {
          const stage = e.target.getStage();
          if (stage && isUserSelected && onMoveStart) stage.container().style.cursor = "move";
        }}
        onMouseLeave={(e) => {
          const stage = e.target.getStage();
          if (stage) stage.container().style.cursor = "";
        }}
      />

      {shouldShowLabel(selected, visual.labelVisibility) && !(isAi && faded) && (
        <Label x={-hw} y={-hh - labelOffsetWorld(visual.labelFontSize, scale)} listening={false}>
          <Tag fill={color} cornerRadius={3 / scale} />
          <Text
            text={labelText}
            fill="white"
            fontSize={labelFontSize}
            padding={BOX_LABEL_PAD_PX / scale}
            fontFamily={BOX_LABEL_FONT_FAMILY}
          />
        </Label>
      )}

      {isUserSelected &&
        onResizeStart &&
        HANDLE_DIRECTIONS.map(({ dir, cx: hx, cy: hy, cursor }) => (
          <Rect
            key={dir}
            x={-hw + wPx * hx - handleSize / 2}
            y={-hh + hPx * hy - handleSize / 2}
            width={handleSize}
            height={handleSize}
            fill="white"
            stroke={color}
            strokeWidth={1.5 / scale}
            cornerRadius={2 / scale}
            onMouseDown={(e) => {
              if (e.evt.button !== 0) return;
              e.cancelBubble = true;
              onResizeStart(dir, e);
            }}
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

      {isUserSelected && onRotateStart && (
        <>
          <Line
            points={[0, -hh, 0, handleY]}
            stroke={color}
            strokeWidth={1.5 / scale}
            listening={false}
          />
          <Circle
            x={0}
            y={handleY}
            radius={handleSize / 2}
            hitStrokeWidth={handleSize}
            fill="white"
            stroke={color}
            strokeWidth={1.5 / scale}
            onMouseDown={(e) => {
              if (e.evt.button !== 0) return;
              e.cancelBubble = true;
              onRotateStart(e);
            }}
            onMouseEnter={(e) => {
              const stage = e.target.getStage();
              if (stage) stage.container().style.cursor = "grab";
            }}
            onMouseLeave={(e) => {
              const stage = e.target.getStage();
              if (stage) stage.container().style.cursor = "";
            }}
          />
        </>
      )}
    </Group>
  );
}

interface KonvaPolylineProps {
  b: Annotation;
  annotationId?: string;
  isAi: boolean;
  selected: boolean;
  faded: boolean;
  fadedOpacity?: number;
  visual: AnnotationVisualConfig;
  colorConfig?: ClassesConfig;
  occluded?: boolean;
  imgW: number;
  imgH: number;
  scale: number;
  onClick: (e?: Konva.KonvaEventObject<MouseEvent>) => void;
  points: Pt[];
  editable?: boolean;
  onVertexMouseDown?: (vidx: number, e: Konva.KonvaEventObject<MouseEvent>) => void;
  onEdgeMouseDown?: (edgeIdx: number, e: Konva.KonvaEventObject<MouseEvent>) => void;
  onBodyMouseDown?: ((e: Konva.KonvaEventObject<MouseEvent>) => void) | null;
}

export function KonvaPolyline({
  b,
  annotationId,
  isAi,
  selected,
  faded,
  occluded = false,
  imgW,
  imgH,
  scale,
  onClick,
  fadedOpacity = DEFAULT_FADED_OPACITY,
  visual,
  colorConfig,
  points,
  editable,
  onVertexMouseDown,
  onEdgeMouseDown,
  onBodyMouseDown,
}: KonvaPolylineProps) {
  const color = classColorForCanvas(b.cls, colorConfig);
  const sw = screenToWorld(strokeWidthFor(selected, visual), scale);
  const labelFontSize = visual.labelFontSize / scale;
  const labelText = shapeLabelText(b, isAi, visual.labelContent);
  const ps: Pt[] = points;
  const flat: number[] = [];
  for (const [px, py] of ps) flat.push(px * imgW, py * imgH);

  return (
    <Group id={annotationId}>
      <Line
        points={flat}
        closed={false}
        stroke={color}
        strokeWidth={sw}
        lineCap="round"
        lineJoin="round"
        dash={isAi || occluded ? [4 / scale, 3 / scale] : undefined}
        opacity={faded ? fadedOpacity : occluded ? 0.5 : 1}
        hitStrokeWidth={10 / scale}
        shadowEnabled={selected && !faded}
        shadowColor={color}
        shadowBlur={8 / scale}
        shadowOpacity={0.4}
        onClick={(e) => {
          e.cancelBubble = true;
          onClick(e);
        }}
        onMouseDown={(e) => {
          if (!editable || !onBodyMouseDown || e.evt.button !== 0) return;
          e.cancelBubble = true;
          onBodyMouseDown(e);
        }}
        onMouseEnter={(e) => {
          const stage = e.target.getStage();
          if (stage && editable && onBodyMouseDown) stage.container().style.cursor = "move";
        }}
        onMouseLeave={(e) => {
          const stage = e.target.getStage();
          if (stage) stage.container().style.cursor = "";
        }}
      />
      {shouldShowLabel(selected, visual.labelVisibility) &&
        flat.length >= 2 &&
        !(isAi && faded) && (
          <Label
            x={flat[0]}
            y={flat[1] - labelOffsetWorld(visual.labelFontSize, scale)}
            listening={false}
          >
            <Tag fill={color} cornerRadius={3 / scale} />
            <Text
              text={labelText}
              fill="white"
              fontSize={labelFontSize}
              padding={BOX_LABEL_PAD_PX / scale}
              fontFamily={BOX_LABEL_FONT_FAMILY}
            />
          </Label>
        )}

      {editable &&
        onEdgeMouseDown &&
        ps.slice(0, -1).map((a, i) => {
          const c = ps[i + 1];
          return (
            <Line
              key={`edge-${i}`}
              points={[a[0] * imgW, a[1] * imgH, c[0] * imgW, c[1] * imgH]}
              stroke="rgba(0,0,0,0)"
              strokeWidth={10 / scale}
              hitStrokeWidth={10 / scale}
              onMouseDown={(e) => {
                if (!e.evt.altKey) return;
                e.cancelBubble = true;
                onEdgeMouseDown(i, e);
              }}
              onMouseEnter={(e) => {
                if (!e.evt.altKey) return;
                const stage = e.target.getStage();
                if (stage) stage.container().style.cursor = "copy";
              }}
              onMouseLeave={(e) => {
                const stage = e.target.getStage();
                if (stage) stage.container().style.cursor = "";
              }}
            />
          );
        })}

      {editable &&
        onVertexMouseDown &&
        ps.map(([px, py], i) => (
          <Circle
            key={`v-${i}`}
            x={px * imgW}
            y={py * imgH}
            radius={6 / scale}
            hitStrokeWidth={9 / scale}
            fill="white"
            stroke={color}
            strokeWidth={1.5 / scale}
            onMouseDown={(e) => {
              e.cancelBubble = true;
              onVertexMouseDown(i, e);
            }}
            onMouseEnter={(e) => {
              const stage = e.target.getStage();
              if (stage) stage.container().style.cursor = e.evt.shiftKey ? "not-allowed" : "grab";
            }}
            onMouseLeave={(e) => {
              const stage = e.target.getStage();
              if (stage) stage.container().style.cursor = "";
            }}
          />
        ))}
    </Group>
  );
}

const KEYPOINT_PALETTE = [
  "#f59e0b",
  "#10b981",
  "#ec4899",
  "#8b5cf6",
  "#06b6d4",
  "#ef4444",
  "#84cc16",
  "#6366f1",
];

export function keypointColorByIndex(idx: number, schema?: KeypointSchema | null): string {
  const c = schema?.nodes[idx]?.color;
  return c ?? KEYPOINT_PALETTE[Math.abs(idx) % KEYPOINT_PALETTE.length];
}

interface KonvaKeypointProps {
  b: Annotation;
  annotationId?: string;
  isAi: boolean;
  selected: boolean;
  faded: boolean;
  fadedOpacity?: number;
  visual: AnnotationVisualConfig;
  colorConfig?: ClassesConfig;
  imgW: number;
  imgH: number;
  scale: number;
  schema?: KeypointSchema | null;
  onClick: (e?: Konva.KonvaEventObject<MouseEvent>) => void;
  editable?: boolean;
  onNodeMouseDown?: (nodeIdx: number, e: Konva.KonvaEventObject<MouseEvent>) => void;
  onToggleVisibility?: (nodeIdx: number) => void;
}

export function KonvaKeypoint({
  b,
  annotationId,
  isAi,
  selected,
  faded,
  fadedOpacity = DEFAULT_FADED_OPACITY,
  visual,
  colorConfig,
  imgW,
  imgH,
  scale,
  schema,
  onClick,
  editable = false,
  onNodeMouseDown,
  onToggleVisibility,
}: KonvaKeypointProps) {
  const color = classColorForCanvas(b.cls, colorConfig);
  const kps: Keypoint[] = b.keypoints ?? [];
  const r = (selected ? 5 : 4) / scale;
  const sw = screenToWorld(strokeWidthFor(selected, visual), scale);
  const labelFontSize = visual.labelFontSize / scale;
  const labelText = shapeLabelText(b, isAi, visual.labelContent);
  const edges = schema?.edges ?? [];

  const anchor = kps.find((p) => p.v > 0);
  const anchorX = (anchor?.x ?? b.x) * imgW;
  const anchorY = (anchor?.y ?? b.y) * imgH;

  return (
    <Group id={annotationId} opacity={faded ? fadedOpacity : 1}>
      {edges.map(([i, j], idx) => {
        const a = kps[i];
        const c = kps[j];
        if (!a || !c || a.v === 0 || c.v === 0) return null;
        return (
          <Line
            key={`edge-${idx}`}
            points={[a.x * imgW, a.y * imgH, c.x * imgW, c.y * imgH]}
            stroke={color}
            strokeWidth={sw}
            opacity={a.v === 1 || c.v === 1 ? 0.6 : 1}
            listening={false}
          />
        );
      })}

      {kps.map((p, i) => {
        const nodeColor = keypointColorByIndex(i, schema);
        const cx = p.x * imgW;
        const cy = p.y * imgH;
        if (p.v === 0) {
          return (
            <Circle
              key={`kp-${i}`}
              x={cx}
              y={cy}
              radius={r * 0.6}
              fill={hexToRgba(nodeColor, 0.25)}
              listening={false}
            />
          );
        }
        return (
          <Circle
            key={`kp-${i}`}
            x={cx}
            y={cy}
            radius={r}
            hitStrokeWidth={9 / scale}
            fill={p.v === 2 ? nodeColor : "white"}
            stroke={nodeColor}
            strokeWidth={sw}
            shadowEnabled={selected}
            shadowColor={nodeColor}
            shadowBlur={6 / scale}
            shadowOpacity={0.5}
            onClick={(e) => {
              e.cancelBubble = true;
              if (editable && onToggleVisibility && e.evt.altKey) {
                onToggleVisibility(i);
                return;
              }
              onClick(e);
            }}
            onMouseDown={(e) => {
              if (!editable || !onNodeMouseDown || e.evt.button !== 0 || e.evt.altKey) return;
              e.cancelBubble = true;
              onNodeMouseDown(i, e);
            }}
            onMouseEnter={(e) => {
              const stage = e.target.getStage();
              if (stage && editable)
                stage.container().style.cursor = e.evt.altKey ? "pointer" : "grab";
            }}
            onMouseLeave={(e) => {
              const stage = e.target.getStage();
              if (stage) stage.container().style.cursor = "";
            }}
          />
        );
      })}

      {(selected || editable) &&
        schema?.nodes &&
        kps.map((p, i) => {
          if (p.v === 0) return null;
          const node = schema.nodes[i];
          const name = node?.name;
          if (!name) return null;
          const nodeLabel = node?.sublabel ? `${name}·${node.sublabel}` : name;
          return (
            <Text
              key={`kp-label-${i}`}
              x={p.x * imgW + r + 2 / scale}
              y={p.y * imgH - labelFontSize / 2}
              text={nodeLabel}
              fill={keypointColorByIndex(i, schema)}
              fontSize={(visual.labelFontSize - 1) / scale}
              fontFamily={BOX_LABEL_FONT_FAMILY}
              listening={false}
            />
          );
        })}

      {shouldShowLabel(selected, visual.labelVisibility) && !(isAi && faded) && (
        <Label
          x={anchorX}
          y={anchorY - labelOffsetWorld(visual.labelFontSize, scale)}
          listening={false}
        >
          <Tag fill={color} cornerRadius={3 / scale} />
          <Text
            text={labelText}
            fill="white"
            fontSize={labelFontSize}
            padding={BOX_LABEL_PAD_PX / scale}
            fontFamily={BOX_LABEL_FONT_FAMILY}
          />
        </Label>
      )}
    </Group>
  );
}

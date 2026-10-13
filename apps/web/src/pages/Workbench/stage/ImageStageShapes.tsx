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
// v0.23.5 WS-E · holes / multi_polygon even-odd 渲染地基 (ADR-0052 D5 / D8)。
import {
  collectOuterRings,
  drawEvenOddShape,
  type PolygonRing,
} from "./shared/geometry/evenOddFill";

// v0.10.4 I2.1 · Douglas-Peucker LOD: 仅在 polygon 不可编辑 / 未选中时启用；
// 简化阈值 ≈ 1 屏幕像素，避免视觉差异。
const LOD_VERTEX_THRESHOLD = 60; // ≤60 顶点不简化（O(n²) 渲染开销低于 RDP 设置成本）。
// v0.10.4 I2.3 · 编辑态顶点视口粗筛门限：>60 顶点才走 rbush 粗筛，避免小 polygon 开销。
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
      // v0.21.3 · 编组下线后图片框无实例序号 (原读 group_id); 跨帧实例身份走 track_id, 不入此标签。
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
  /** v0.15.27 · 共享视觉规格(线宽/填充/字号/标签显隐+内容)。 */
  visual: AnnotationVisualConfig;
  /**
   * 显式传入的类别配色 (按项目)。缺省时回落到 Workbench 设置的模块级全局配置;
   * 独立渲染面 (如 Data Manager 只读预览) 必须显式传入, 避免读到上一个项目的全局色。
   */
  colorConfig?: ClassesConfig;
  /** v0.10.5 M4-β · I15 occluded：渲染为虚线 + 半透。 */
  occluded?: boolean;
  imgW: number;
  imgH: number;
  scale: number;
  onClick: (e?: Konva.KonvaEventObject<MouseEvent>) => void;
  onMoveStart: ((e: Konva.KonvaEventObject<MouseEvent>) => void) | null;
  onResizeStart: ((dir: ResizeDirection, e: Konva.KonvaEventObject<MouseEvent>) => void) | null;
}

// v0.20.14 · 父子标注同胞高亮环色。选中父框时其子框描边此固定色 + 细点线,
// 刻意区别于 AI 框的类色虚线; 属画布数据域固定色。
export const SIBLING_HIGHLIGHT_COLOR = "#38bdf8"; // sky-400

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
  /** v0.15.27 · 共享视觉规格(线宽/填充/字号/标签显隐+内容)。 */
  visual: AnnotationVisualConfig;
  /**
   * 显式传入的类别配色 (按项目)。缺省时回落到 Workbench 设置的模块级全局配置;
   * 独立渲染面 (如 Data Manager 只读预览) 必须显式传入, 避免读到上一个项目的全局色。
   */
  colorConfig?: ClassesConfig;
  /** v0.10.5 M4-β · I15 occluded：渲染为虚线 + 半透（与 selfIntersect 红色互斥）。 */
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
  /** v0.10.4 I2.3 · 归一化 [0,1] viewport bbox。提供后启用顶点视口粗筛。 */
  viewportBBox?: ViewportBBox;
  /**
   * v0.23.5 WS-E · 单 polygon 的内环 (holes), 仅参与 even-odd 填充, 不参与顶点编辑。
   * 与 b.holes 二选一透传; 显式 prop 优先 (供 ImageStage override 几何时使用)。
   */
  holes?: Pt[][];
  /**
   * v0.23.5 WS-E · 多连通域: 每个 {points, holes?} 是一个独立外环。提供后外环用
   * even-odd 合并填充 (分量按定义互不相交, evenodd 不会 XOR 抵消)。与 b.multiPolygon
   * 二选一; 显式 prop 优先。
   */
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
  // I2.1 渲染层 LOD：编辑态 / 选中态用原顶点（保证手感）；其它态按 viewport scale 简化。
  const renderPs = useMemo<Pt[]>(() => {
    if (editable || selected) return ps;
    if (ps.length < LOD_VERTEX_THRESHOLD) return ps;
    if (scale <= 0 || imgW <= 0) return ps;
    // 视觉等价阈值 = 1 像素；归一化空间换算 ≈ 1 / (scale * imgW)。
    const epsilon = 1 / (scale * Math.max(imgW, imgH));
    return simplifyPolygon(ps, epsilon);
  }, [ps, editable, selected, scale, imgW, imgH]);
  const flat: number[] = [];
  for (const [px, py] of renderPs) flat.push(px * imgW, py * imgH);
  const strokeColor = selfIntersect ? "oklch(0.55 0.22 25)" : color;

  // v0.10.4 I2.3 · 编辑态下大 polygon 用 rbush 视口粗筛，省 Konva 节点数与 hit-test。
  // 只在 editable && ps.length>阈值 && 提供了 viewportBBox 时启用；否则全量渲染保持现有行为。
  const visibleVertexIdx = useMemo<Set<number> | null>(() => {
    if (!editable || !viewportBBox || ps.length < VERTEX_CULL_THRESHOLD) return null;
    const idx = buildVertexIndex(ps);
    return new Set(idx.verticesInBBox(viewportBBox));
  }, [editable, viewportBBox, ps]);

  // v0.23.5 WS-E · holes / multi_polygon even-odd 渲染 (ADR-0052 D5)。
  // 显式 prop 优先, 缺省回落 b.holes / b.multiPolygon (transforms.geometryToShape 透传)。
  // 仅当存在非空 holes 或 multi_polygon 时走 sceneFunc 分支; 否则保留单环 <Line> 快路径
  // (常见 bbox/单 polygon 不回归 LOD simplify / vertex cull 行为)。
  const holesArr = holes ?? b.holes;
  const multiArr = multiPolygon ?? b.multiPolygon;
  const hasHolesOrMulti = !!(
    (multiArr && multiArr.length > 0) ||
    (holesArr && holesArr.some((ring) => ring.length >= 2))
  );
  // 收集 even-odd 子路径列表 (纯函数, 便于单测); 仅在分支启用时计算。
  const fillRings = useMemo<PolygonRing[]>(() => {
    if (!hasHolesOrMulti) return [];
    return collectOuterRings({
      primaryPoints: ps,
      holes: holesArr,
      multiPolygon: multiArr,
    });
  }, [hasHolesOrMulti, ps, holesArr, multiArr]);
  // sceneFunc 闭包依赖 imgW/imgH + rings; 用 useMemo 稳定引用减少 Konva 重绘。
  const drawEvenOdd = useMemo(
    () => (ctx: Konva.Context, shape: Konva.Shape) => {
      if (fillRings.length === 0) {
        // 无可画 → 清空 path, 让 Konva 退化为不填充 (与 Line 空 points 行为一致)。
        ctx.beginPath();
        return;
      }
      // 填充与描边都覆盖外环 + holes；hole 边缘是真实边界，不能在描边阶段剥离。
      drawEvenOddShape(ctx, shape, fillRings, imgW, imgH);
    },
    [fillRings, imgW, imgH],
  );

  return (
    <Group id={annotationId}>
      <Line
        points={flat}
        closed
        // v0.23.5 WS-E · 有 holes/multi 时走 sceneFunc + fillRule=evenodd;
        // Konva Context.fill(...args) 直接转发到原生 canvas.fill, fillRule 透传无损。
        // 无 holes/multi 时 sceneFunc=undefined, Konva 用内置 Line._sceneFunc (快路径)。
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
          // I2.3 · 边视口粗筛：边的两个端点都不在视口内 → 跳过（边完全在屏外）。
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
          // I2.3 · 顶点视口粗筛：屏外顶点不渲染 Circle 手柄。
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

// v0.10.28 · 旋转框 (OBB)。把整框放进一个绕中心 (cx,cy) 旋转 angle 度的 Group:
//   - 主体 Rect 用 offsetX/offsetY = 半宽高, 让本地原点落在框中心。
//   - 标签贴在框左上角 (本地坐标 -hw,-hh)。
//   - 选中可编辑时, 顶部边外侧画一个旋转手柄圆点 + 连接线。
// 样式 (色 / 选中 / faded / occluded) 比照 KonvaBox。
interface KonvaRotatedBoxProps {
  b: Annotation;
  annotationId?: string;
  geometry: RotatedBboxGeometry;
  /** 实时角度 (拖拽中由 ImageStage override; 否则 = geometry.angle)。度数 [0,360) 顺时针。 */
  angle: number;
  isAi: boolean;
  selected: boolean;
  editable: boolean;
  faded: boolean;
  fadedOpacity?: number;
  /** v0.15.27 · 共享视觉规格(线宽/填充/字号/标签显隐+内容)。 */
  visual: AnnotationVisualConfig;
  /**
   * 显式传入的类别配色 (按项目)。缺省时回落到 Workbench 设置的模块级全局配置;
   * 独立渲染面 (如 Data Manager 只读预览) 必须显式传入, 避免读到上一个项目的全局色。
   */
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
  // 旋转手柄: 顶边中点外侧 (本地坐标 y < -hh)。
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
  /** v0.15.27 · 共享视觉规格(线宽/填充/字号/标签显隐+内容)。 */
  visual: AnnotationVisualConfig;
  /**
   * 显式传入的类别配色 (按项目)。缺省时回落到 Workbench 设置的模块级全局配置;
   * 独立渲染面 (如 Data Manager 只读预览) 必须显式传入, 避免读到上一个项目的全局色。
   */
  colorConfig?: ClassesConfig;
  /** v0.10.5 M4-β · I15 occluded：渲染为虚线 + 半透。 */
  occluded?: boolean;
  imgW: number;
  imgH: number;
  scale: number;
  onClick: (e?: Konva.KonvaEventObject<MouseEvent>) => void;
  /** 折线顶点（归一化，≥2 点，不闭合）。 */
  points: Pt[];
  editable?: boolean;
  onVertexMouseDown?: (vidx: number, e: Konva.KonvaEventObject<MouseEvent>) => void;
  onEdgeMouseDown?: (edgeIdx: number, e: Konva.KonvaEventObject<MouseEvent>) => void;
  onBodyMouseDown?: ((e: Konva.KonvaEventObject<MouseEvent>) => void) | null;
}

/**
 * v0.10.28 · 折线 (polyline) 渲染：Konva <Line closed={false}>，无填充、无首尾连边、无自交检测。
 * 顶点拖拽 / Alt 边插点 / Shift 删点的交互与 KonvaPolygon 一致，但边集合不环绕 (0..n-2)。
 */
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

      {/* 折线边：0..n-2（不环绕）。Alt+点击在该段插入顶点。 */}
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
  /** v0.15.27 · 共享视觉规格(线宽/填充/字号/标签显隐+内容)。 */
  visual: AnnotationVisualConfig;
  /**
   * 显式传入的类别配色 (按项目)。缺省时回落到 Workbench 设置的模块级全局配置;
   * 独立渲染面 (如 Data Manager 只读预览) 必须显式传入, 避免读到上一个项目的全局色。
   */
  colorConfig?: ClassesConfig;
  imgW: number;
  imgH: number;
  scale: number;
  /** 当前类别的骨骼模板 (命名节点 + 连线)。缺省时按 index 取色、无连线。 */
  schema?: KeypointSchema | null;
  onClick: (e?: Konva.KonvaEventObject<MouseEvent>) => void;
  editable?: boolean;
  /** 单点拖拽起始 (移动该 keypoint)。 */
  onNodeMouseDown?: (nodeIdx: number, e: Konva.KonvaEventObject<MouseEvent>) => void;
  /** Alt+点击节点切换可见性 (2 → 1 → 0 → 2 循环)。 */
  onToggleVisibility?: (nodeIdx: number) => void;
}

/**
 * v0.10.28 · 关键点渲染：
 *   - 按 schema.edges 在对应两点间画骨骼连线 (两端 v>0 时才连)；
 *   - 每个节点画圆点：v=2 实心 / v=1 空心 / v=0 淡显小点；颜色取 schema.nodes[i].color；
 *   - 节点标签 = schema.nodes[i].name（有 sublabel 时显示 name·sublabel）；选中态加阴影 + 类别标签。
 */
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

  // 类别标签锚点：第一个已标注点；都没有则用 bbox 左上。
  const anchor = kps.find((p) => p.v > 0);
  const anchorX = (anchor?.x ?? b.x) * imgW;
  const anchorY = (anchor?.y ?? b.y) * imgH;

  return (
    <Group id={annotationId} opacity={faded ? fadedOpacity : 1}>
      {/* 骨骼连线 (两端可见 / 遮挡才连) */}
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

      {/* 节点圆点 */}
      {kps.map((p, i) => {
        const nodeColor = keypointColorByIndex(i, schema);
        const cx = p.x * imgW;
        const cy = p.y * imgH;
        if (p.v === 0) {
          // 未标注：淡显小点 (不可拖)
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
            // v=2 实心 / v=1 空心
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

      {/* 节点名标签 (选中或编辑态时显示, 避免拥挤) */}
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

      {/* 类别标签 */}
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

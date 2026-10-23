import type { AttributeField, ToolBindings } from "@/api/projects";
import type { ToolUnitId } from "@/constants/toolUnits";
import type {
  Annotation,
  AnnotationResponse,
  BboxGeometry,
  Geometry,
  Keypoint,
  MultiPolygonGeometry,
  PolygonGeometry,
  PolylineGeometry,
  PredictionResponse,
  PredictionSourceValue,
} from "@/types";

/** 把 {x,y,w,h} 包装为 bbox geometry。常用于 commit 几何变更时。 */
export function bboxGeom(g: { x: number; y: number; w: number; h: number }): BboxGeometry {
  return { type: "bbox", x: g.x, y: g.y, w: g.w, h: g.h };
}

export function polygonGeom(points: [number, number][]): PolygonGeometry {
  return { type: "polygon", points };
}

/** v0.10.28 · 把顶点序列包装为 polyline geometry（不闭合）。 */
export function polylineGeom(points: [number, number][]): PolylineGeometry {
  return { type: "polyline", points };
}

/** v0.10.28 · 把 keypoint 列表包装为 keypoint geometry。 */
export function keypointGeom(points: Keypoint[]): { type: "keypoint"; points: Keypoint[] } {
  return { type: "keypoint", points };
}

/** 计算 polygon 顶点的轴对齐包围盒（归一化）。 */
export function polygonBounds(points: [number, number][]): {
  x: number;
  y: number;
  w: number;
  h: number;
} {
  if (points.length === 0) return { x: 0, y: 0, w: 0, h: 0 };
  let minX = points[0][0],
    maxX = points[0][0];
  let minY = points[0][1],
    maxY = points[0][1];
  for (let i = 1; i < points.length; i++) {
    const [px, py] = points[i];
    if (px < minX) minX = px;
    if (px > maxX) maxX = px;
    if (py < minY) minY = py;
    if (py > maxY) maxY = py;
  }
  return { x: minX, y: minY, w: Math.max(0, maxX - minX), h: Math.max(0, maxY - minY) };
}

/**
 * v0.9.14 · 多连通域 polygons 的合并 bounding rect（取所有 polygon 外环顶点的 union AABB）。
 */
function multiPolygonBounds(polygons: PolygonGeometry[]): {
  x: number;
  y: number;
  w: number;
  h: number;
} {
  if (polygons.length === 0) return { x: 0, y: 0, w: 0, h: 0 };
  let minX = Infinity,
    maxX = -Infinity,
    minY = Infinity,
    maxY = -Infinity;
  for (const p of polygons) {
    for (const [px, py] of p.points) {
      if (px < minX) minX = px;
      if (px > maxX) maxX = px;
      if (py < minY) minY = py;
      if (py > maxY) maxY = py;
    }
  }
  if (minX === Infinity) return { x: 0, y: 0, w: 0, h: 0 };
  return { x: minX, y: minY, w: Math.max(0, maxX - minX), h: Math.max(0, maxY - minY) };
}

/** v0.9.14 · 多 polygon 中按外环顶点数取最大者作为渲染主环（与单 polygon 编辑路径兼容）。 */
function pickPrimaryPolygon(g: MultiPolygonGeometry): PolygonGeometry {
  let best = g.polygons[0];
  for (let i = 1; i < g.polygons.length; i++) {
    if (g.polygons[i].points.length > best.points.length) best = g.polygons[i];
  }
  return best;
}

/** Geometry → 用于 UI 渲染的 bounding rect + 可选 polygon 顶点 + holes / multiPolygon 透传。
 *
 * v0.9.14 · 多连通域降级策略: ImageStage 编辑路径仅识别单环 polygon, 多连通时取顶点数最多的
 * 主外环作为 `polygon` 字段, 完整 polygons 数组同时挂在 `multiPolygon` 上供 v0.10.x 镂空
 * 渲染升级使用. holes 字段也从 PolygonGeometry 透传, 暂不参与渲染 (v0.10.x 引入 sceneFunc
 * + evenodd 时启用).
 */
export function geometryToShape(g: Geometry): {
  x: number;
  y: number;
  w: number;
  h: number;
  polygon?: [number, number][];
  polyline?: [number, number][];
  holes?: [number, number][][];
  multiPolygon?: { points: [number, number][]; holes?: [number, number][][] }[];
  keypoints?: Keypoint[];
} {
  // v0.10.28 · keypoint: bounding rect 取所有已标注 (v>0) 点的 AABB; keypoints 透传供画布渲染。
  if (g.type === "keypoint") {
    const visible = g.points.filter((p) => p.v > 0);
    if (visible.length === 0) return { x: 0, y: 0, w: 0, h: 0, keypoints: g.points };
    let minX = Infinity,
      minY = Infinity,
      maxX = -Infinity,
      maxY = -Infinity;
    for (const p of visible) {
      if (p.x < minX) minX = p.x;
      if (p.y < minY) minY = p.y;
      if (p.x > maxX) maxX = p.x;
      if (p.y > maxY) maxY = p.y;
    }
    return { x: minX, y: minY, w: maxX - minX, h: maxY - minY, keypoints: g.points };
  }
  if (g.type === "polygon") {
    const b = polygonBounds(g.points);
    return { ...b, polygon: g.points, holes: g.holes };
  }
  if (g.type === "polyline") {
    // 复用 polygonBounds 计算顶点 AABB（与闭合无关，仅取 min/max）。
    const b = polygonBounds(g.points);
    return { ...b, polyline: g.points };
  }
  if (g.type === "multi_polygon") {
    const primary = pickPrimaryPolygon(g);
    const b = multiPolygonBounds(g.polygons);
    return {
      ...b,
      polygon: primary.points,
      holes: primary.holes,
      multiPolygon: g.polygons.map((p) => ({ points: p.points, holes: p.holes })),
    };
  }
  if (g.type === "video_bbox") {
    return { x: g.x, y: g.y, w: g.w, h: g.h };
  }
  if (g.type === "video_polygon") {
    // v0.21.21 · 单帧 polygon: 与图片 polygon 同样返回外接盒 + polygon 顶点 (供列表 / Minimap / 选中锚点)。
    const b = polygonBounds(g.points);
    return { ...b, polygon: g.points, holes: g.holes };
  }
  if (g.type === "video_polyline") {
    // v0.21.21 · 单帧 polyline: 顶点 AABB + polyline 顶点。
    const b = polygonBounds(g.points);
    return { ...b, polyline: g.points };
  }
  if (g.type === "video_rotated_bbox") {
    // v0.21.22 · 单帧 OBB: 四角旋转后 AABB + polygon 顶点 (供列表/Minimap/选中锚点)。
    const rad = (g.angle * Math.PI) / 180;
    const cos = Math.cos(rad),
      sin = Math.sin(rad);
    const hw = g.w / 2,
      hh = g.h / 2;
    const corners = (
      [
        [-hw, -hh],
        [hw, -hh],
        [hw, hh],
        [-hw, hh],
      ] as [number, number][]
    ).map(
      ([dx, dy]) => [g.cx + dx * cos - dy * sin, g.cy + dx * sin + dy * cos] as [number, number],
    );
    return { ...polygonBounds(corners), polygon: corners };
  }
  if (g.type === "video_keypoint") {
    const points = g.points
      .filter((point) => point.v > 0)
      .map((point) => [point.x, point.y] as [number, number]);
    return points.length > 0 ? polygonBounds(points) : { x: 0, y: 0, w: 0, h: 0 };
  }
  if (g.type === "video_track_bbox") {
    const outside = g.outside ?? [];
    const isOutside = (frame: number) => outside.some((r) => frame >= r.from && frame <= r.to);
    const keyframe = g.keyframes.find((kf) => !isOutside(kf.frame_index)) ?? g.keyframes[0];
    return keyframe?.bbox ?? { x: 0, y: 0, w: 0, h: 0 };
  }
  if (g.type === "video_track_polygon" || g.type === "video_track_polyline") {
    // v0.21.20 · polygon/polyline track: 取首个可见关键帧的顶点外接盒 (供列表 / Minimap / 选中锚点)。
    const outside = g.outside ?? [];
    const isOutside = (frame: number) => outside.some((r) => frame >= r.from && frame <= r.to);
    const keyframe = g.keyframes.find((kf) => !isOutside(kf.frame_index)) ?? g.keyframes[0];
    const pts = keyframe?.points ?? [];
    return pts.length >= 2 ? polygonBounds(pts) : { x: 0, y: 0, w: 0, h: 0 };
  }
  if (g.type === "rotated_bbox") {
    // 旋转矩形四角旋转后的轴对齐包围盒（供列表 / Minimap / 选中浮条锚点）。
    const rad = (g.angle * Math.PI) / 180;
    const cos = Math.cos(rad),
      sin = Math.sin(rad);
    const hw = g.w / 2,
      hh = g.h / 2;
    const corners = (
      [
        [-hw, -hh],
        [hw, -hh],
        [hw, hh],
        [-hw, hh],
      ] as [number, number][]
    ).map(
      ([dx, dy]) => [g.cx + dx * cos - dy * sin, g.cy + dx * sin + dy * cos] as [number, number],
    );
    return polygonBounds(corners);
  }
  if (g.type === "box_3d" || g.type === "point_mask_3d") {
    // v0.13.3 · 3D 几何无 2D 投影(投影联动是 v0.13.4),退化为空 shape;
    // 2D 画布消费方不画,3D 渲染走 three-d 模块(PointCloudScene)。
    return { x: 0, y: 0, w: 0, h: 0 };
  }
  if (g.type === "video_mask" || g.type === "video_track_mask" || g.type === "raster_mask") {
    // RLE AABB 需要异步读取 mask content；同步列表适配层不猜外接框。
    // v0.23.6 · raster_mask 同样需要异步加载，返回空外接框。
    return { x: 0, y: 0, w: 0, h: 0 };
  }
  return { x: g.x, y: g.y, w: g.w, h: g.h };
}

/**
 * v0.11.27 · 收集属性 schema 中标了 `style_occluded` 的 boolean 字段 key。
 * 这些属性为 true 时，对应标注框渲染为虚线+半透（遮挡样式）。
 */
export const collectOccludedKeys = (fields: AttributeField[]): Set<string> =>
  new Set(fields.filter((f) => f.type === "boolean" && f.style_occluded).map((f) => f.key));

export function annotationToBox(a: AnnotationResponse, occludedKeys?: Set<string>): Annotation {
  const shape = geometryToShape(a.geometry);
  return {
    id: a.id,
    annotation_type: a.annotation_type,
    geometry: a.geometry,
    ...shape,
    cls: a.class_name,
    conf: a.confidence ?? 1,
    source: a.source as Annotation["source"],
    parent_prediction_id: a.parent_prediction_id,
    lead_time: a.lead_time,
    render_key: a.render_key,
    // v0.10.5 M4-β · shape 状态位（I15）。
    z_order: a.z_order ?? 0,
    is_locked: a.is_locked ?? false,
    is_hidden: a.is_hidden ?? false,
    version: a.version ?? 1,
    // v0.11.27 · 遮挡为渲染派生：任一 style_occluded 属性为 true 即触发。
    occluded: occludedKeys ? [...occludedKeys].some((k) => a.attributes?.[k] === true) : false,
    // v0.20.9 · 父子标注; 透传供侧栏缩进呈现。null/undefined 为顶层框。
    parent_annotation_id: a.parent_annotation_id ?? null,
    // v0.18.0 · 透传属性,供画布标签「标签内容·属性(单帧段)」渲染 (此前漏拷,
    // 导致单帧标签的属性开关无内容可显; AI 候选侧由 predictionsToBoxes 已拷)。
    attributes: a.attributes ?? null,
    // v0.20.10 · 属性级溯源, 供属性面板渲染 AI 来源 chip。
    attributes_meta: a.attributes_meta ?? null,
  };
}

export const PREDICTION_SOURCE_FILTERS = ["ml_backend", "external_import"] as const;
export type PredictionSourceFilter = (typeof PREDICTION_SOURCE_FILTERS)[number];
export type PredictionSourceVisibility = Record<PredictionSourceFilter, boolean>;
export type PredictionSourceCounts = Record<PredictionSourceFilter, number>;

export function defaultPredictionSourceVisibility(): PredictionSourceVisibility {
  return { ml_backend: true, external_import: true };
}

export function emptyPredictionSourceCounts(): PredictionSourceCounts {
  return { ml_backend: 0, external_import: 0 };
}

export function normalizePredictionSource(
  source: PredictionSourceValue | undefined,
): PredictionSourceFilter | null {
  if (source === "ml_backend" || source === "external_import")
    return source as PredictionSourceFilter;
  return null;
}

export function predictionSourceLabel(source: PredictionSourceValue | undefined): string {
  if (source === "ml_backend") return "模型";
  if (source === "external_import") return "导入";
  if (source) return "其他";
  return "未知";
}

export type AiBox = Annotation & {
  predictionId: string;
  shapeIndex: number;
  // B-57 · 预测自身的工具单位 (如 polygon→region); 采纳选类时据此取该单位的类别集合。
  // Annotation 基类不含此字段 (仅 AnnotationResponse 有), 故在 AiBox 上显式补。
  tool_unit_id?: string | null;
  predictionSource: PredictionSourceValue;
  // v0.14.9 · OCR / doc_layout 候选携带的属性 (如 OCR 的 {text, language?, orientation?}).
  // 后端 PredictionShape 协议外字段, 透传供 AIInspectorPanel 渲染文本摘要并在 accept 时带入新建标注.
  attributes?: Record<string, unknown> | null;
};

/**
 * DINO 写入的 class_name 是项目类别的英文 alias; 反查对应 tool_unit 的 classes
 * 把 alias 映射回原类别名 (镜像后端 accept_prediction 的归一, 强隔离: 仅查本 unit)。
 * 命中不到则原样返回。返回大小写敏感的原始名。
 */
function aliasResolverForUnit(
  toolBindings: ToolBindings | undefined,
  unit: ToolUnitId,
): (raw: string) => string {
  const map = new Map<string, string>();
  for (const c of toolBindings?.[unit]?.classes ?? []) {
    if (c.alias && c.alias.trim() && c.name) {
      map.set(c.alias.trim().toLowerCase(), c.name);
    }
  }
  if (map.size === 0) return (raw) => raw;
  return (raw) => map.get(raw.trim().toLowerCase()) ?? raw;
}

export function predictionsToBoxes(
  predictions: PredictionResponse[],
  toolBindings?: ToolBindings,
): AiBox[] {
  const resolverCache = new Map<ToolUnitId, (raw: string) => string>();
  return predictions.flatMap((p) => {
    return p.result.map((shape, i) => {
      const unit = (shape.tool_unit_id ?? p.tool_unit_id ?? "bbox") as ToolUnitId;
      let resolve = resolverCache.get(unit);
      if (!resolve) {
        resolve = aliasResolverForUnit(toolBindings, unit);
        resolverCache.set(unit, resolve);
      }
      const nativeBoundClass =
        shape.geometry.type === "raster_mask" &&
        toolBindings?.[unit]?.classes?.some((entry) => entry.name === shape.class_name);
      const s = geometryToShape(shape.geometry);
      const shapeIndex = typeof shape.shape_index === "number" ? shape.shape_index : i;
      // v0.14.9 · OCR / doc_layout 的 attributes (text / language / orientation 等) 是协议外字段,
      // 后端 PredictionShape codegen 类型未声明, 这里防御式读取透传.
      const attributes = (shape as { attributes?: Record<string, unknown> }).attributes;
      return {
        id: `pred-${p.id}-${shapeIndex}`,
        annotation_type: shape.geometry.type,
        geometry: shape.geometry,
        // B-57 · 采纳时让选类 popover 能按预测自身的工具单位 (而非当前激活工具) 取类别集合。
        tool_unit_id: unit,
        predictionId: p.id,
        shapeIndex,
        ...s,
        cls: nativeBoundClass ? shape.class_name : resolve(shape.class_name),
        conf: shape.confidence,
        source: "prediction_based" as const,
        predictionSource: p.source ?? null,
        ...(attributes && typeof attributes === "object" ? { attributes } : {}),
      };
    });
  });
}

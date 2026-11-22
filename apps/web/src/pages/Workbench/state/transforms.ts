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

export function bboxGeom(g: { x: number; y: number; w: number; h: number }): BboxGeometry {
  return { type: "bbox", x: g.x, y: g.y, w: g.w, h: g.h };
}

export function polygonGeom(points: [number, number][]): PolygonGeometry {
  return { type: "polygon", points };
}

export function polylineGeom(points: [number, number][]): PolylineGeometry {
  return { type: "polyline", points };
}

export function keypointGeom(points: Keypoint[]): { type: "keypoint"; points: Keypoint[] } {
  return { type: "keypoint", points };
}

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

function pickPrimaryPolygon(g: MultiPolygonGeometry): PolygonGeometry {
  let best = g.polygons[0];
  for (let i = 1; i < g.polygons.length; i++) {
    if (g.polygons[i].points.length > best.points.length) best = g.polygons[i];
  }
  return best;
}

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
    const b = polygonBounds(g.points);
    return { ...b, polygon: g.points, holes: g.holes };
  }
  if (g.type === "video_polyline") {
    const b = polygonBounds(g.points);
    return { ...b, polyline: g.points };
  }
  if (g.type === "video_rotated_bbox") {
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
    const outside = g.outside ?? [];
    const isOutside = (frame: number) => outside.some((r) => frame >= r.from && frame <= r.to);
    const keyframe = g.keyframes.find((kf) => !isOutside(kf.frame_index)) ?? g.keyframes[0];
    const pts = keyframe?.points ?? [];
    return pts.length >= 2 ? polygonBounds(pts) : { x: 0, y: 0, w: 0, h: 0 };
  }
  if (g.type === "rotated_bbox") {
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
    return { x: 0, y: 0, w: 0, h: 0 };
  }
  if (g.type === "video_mask" || g.type === "video_track_mask" || g.type === "raster_mask") {
    return { x: 0, y: 0, w: 0, h: 0 };
  }
  return { x: g.x, y: g.y, w: g.w, h: g.h };
}

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
    z_order: a.z_order ?? 0,
    is_locked: a.is_locked ?? false,
    is_hidden: a.is_hidden ?? false,
    version: a.version ?? 1,
    occluded: occludedKeys ? [...occludedKeys].some((k) => a.attributes?.[k] === true) : false,
    parent_annotation_id: a.parent_annotation_id ?? null,
    attributes: a.attributes ?? null,
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
  tool_unit_id?: string | null;
  predictionSource: PredictionSourceValue;
  attributes?: Record<string, unknown> | null;
};

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
      const attributes = (shape as { attributes?: Record<string, unknown> }).attributes;
      return {
        id: `pred-${p.id}-${shapeIndex}`,
        annotation_type: shape.geometry.type,
        geometry: shape.geometry,
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

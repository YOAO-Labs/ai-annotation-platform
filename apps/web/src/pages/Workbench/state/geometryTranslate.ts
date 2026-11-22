import type { Annotation, Geometry } from "@/types";

export function canTranslateAnnotationGeometry(annotation: Annotation): boolean {
  return annotation.geometry?.type !== "raster_mask";
}

export function clamp01(value: number): number {
  return Math.max(0, Math.min(1, value));
}

function computeClampedShift(
  points: readonly [number, number][],
  offX: number,
  offY: number,
): [number, number] {
  if (!points.length) return [offX, offY];
  let minX = Infinity;
  let maxX = -Infinity;
  let minY = Infinity;
  let maxY = -Infinity;
  for (const [x, y] of points) {
    if (x < minX) minX = x;
    if (x > maxX) maxX = x;
    if (y < minY) minY = y;
    if (y > maxY) maxY = y;
  }
  const dx = Math.max(-minX, Math.min(1 - maxX, offX));
  const dy = Math.max(-minY, Math.min(1 - maxY, offY));
  return [dx, dy];
}

export function translatePoints(
  points: [number, number][],
  offX: number,
  offY: number,
): [number, number][] {
  const [dx, dy] = computeClampedShift(points, offX, offY);
  return points.map(([x, y]) => [x + dx, y + dy]);
}

function applyShift(points: [number, number][], dx: number, dy: number): [number, number][] {
  return points.map(([x, y]) => [x + dx, y + dy]);
}

export function translateGeometry(
  annotation: Annotation,
  offX: number,
  offY: number,
): { geometry: Geometry; annotationType: string } {
  if (!canTranslateAnnotationGeometry(annotation)) {
    throw new Error("raster_mask does not support geometric translation");
  }
  const geometry = annotation.geometry;
  if (geometry?.type === "polygon") {
    const [dx, dy] = computeClampedShift(geometry.points, offX, offY);
    return {
      annotationType: "polygon",
      geometry: {
        type: "polygon",
        points: applyShift(geometry.points, dx, dy),
        holes: geometry.holes ? geometry.holes.map((ring) => applyShift(ring, dx, dy)) : undefined,
      },
    };
  }
  if (geometry?.type === "multi_polygon") {
    const allOuter = geometry.polygons.flatMap((p) => p.points);
    const [dx, dy] = computeClampedShift(allOuter, offX, offY);
    return {
      annotationType: "polygon",
      geometry: {
        type: "multi_polygon",
        polygons: geometry.polygons.map((polygon) => ({
          type: "polygon",
          points: applyShift(polygon.points, dx, dy),
          holes: polygon.holes ? polygon.holes.map((ring) => applyShift(ring, dx, dy)) : undefined,
        })),
      },
    };
  }
  if (geometry?.type === "polyline") {
    return {
      annotationType: "polyline",
      geometry: {
        type: "polyline",
        points: translatePoints(geometry.points, offX, offY),
      },
    };
  }
  if (geometry?.type === "rotated_bbox") {
    return {
      annotationType: "rotated_bbox",
      geometry: {
        ...geometry,
        cx: clamp01(geometry.cx + offX),
        cy: clamp01(geometry.cy + offY),
      },
    };
  }
  if (geometry?.type === "keypoint") {
    const kpXY: [number, number][] = geometry.points.map((p) => [p.x, p.y]);
    const [dx, dy] = computeClampedShift(kpXY, offX, offY);
    return {
      annotationType: "keypoint",
      geometry: {
        type: "keypoint",
        points: geometry.points.map((point) => ({
          ...point,
          x: point.x + dx,
          y: point.y + dy,
        })),
      },
    };
  }
  if (geometry?.type === "bbox") {
    return {
      annotationType: "bbox",
      geometry: {
        type: "bbox",
        x: Math.max(0, Math.min(1 - geometry.w, geometry.x + offX)),
        y: Math.max(0, Math.min(1 - geometry.h, geometry.y + offY)),
        w: geometry.w,
        h: geometry.h,
      },
    };
  }
  if (annotation.polygon && annotation.polygon.length >= 3) {
    return {
      annotationType: "polygon",
      geometry: {
        type: "polygon",
        points: translatePoints(annotation.polygon, offX, offY),
      },
    };
  }
  return {
    annotationType: "bbox",
    geometry: {
      type: "bbox",
      x: Math.max(0, Math.min(1 - annotation.w, annotation.x + offX)),
      y: Math.max(0, Math.min(1 - annotation.h, annotation.y + offY)),
      w: annotation.w,
      h: annotation.h,
    },
  };
}

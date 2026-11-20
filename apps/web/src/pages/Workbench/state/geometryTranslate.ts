// 归一化 [0,1] 坐标系下按 (offX, offY) 平移一个标注几何。各几何类型逐一处理, 保持形状,
// 只在贴边时截去多余位移 (与 bbox 平移语义一致)。
// 粘贴偏移与 ImageStage 父子 Alt 拖动共用同一套平移逻辑。
import type { Annotation, Geometry } from "@/types";

/**
 * raster_mask 没有可同步平移的矢量几何；原生画布编辑开启前，剪贴板和
 * 父子联动必须显式跳过，不得落入末尾的 bbox 兼容分支。
 */
export function canTranslateAnnotationGeometry(annotation: Annotation): boolean {
  return annotation.geometry?.type !== "raster_mask";
}

export function clamp01(value: number): number {
  return Math.max(0, Math.min(1, value));
}

/** 求点集当前 x/y 方向上的公共可平移区间, 把 (offX, offY) 截到该区间内 → 保持形状不被拉扁。
 * (对比：逐点 clamp01 → 只要一个顶点越界, 其它顶点继续平移, 形状被抻扁。) */
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

/** 按 (dx, dy) 施加已算好的位移 (不再重新求 clamp), 用于 multi_polygon / keypoint / polygon+holes
 * 之类需要多组顶点共享同一平移量的场景, 避免各组独立 clamp 导致相对位置解耦。 */
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
    // outer + holes 用同一 shift (holes 内嵌于 outer, 边界由 outer 决定)。
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
    // 跨多个 polygon 用合并 bbox 求共享 shift, 各 polygon 用同一 (dx, dy) 平移 →
    // 保持 polygon 之间相对位置不漂移 (贴边时统一被截, 不解耦)。
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
    // keypoint 各节点用同一 shift, 保持骨骼结构不被拉扁 (含隐藏/被遮挡的可见性节点)。
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

import type { Annotation } from "@/types";
import type { Viewport } from "../state/useViewportTransform";
import type { Pt } from "./polygonGeom";
import {
  snapPointToCandidates,
  snapPointToSegments,
  type SnapMatch,
  type SnapPointCandidate,
  type SnapSegmentCandidate,
  type SnapViewportTransform,
} from "./shared/geometry/snap";

export type BboxCreationMode = "corner" | "center";

export function imageBoxFromDrag(drag: {
  sx: number;
  sy: number;
  cx: number;
  cy: number;
  fromCenter?: boolean;
}) {
  if (drag.fromCenter) {
    const rx = Math.max(0, Math.min(Math.abs(drag.cx - drag.sx), drag.sx, 1 - drag.sx));
    const ry = Math.max(0, Math.min(Math.abs(drag.cy - drag.sy), drag.sy, 1 - drag.sy));
    return { x: drag.sx - rx, y: drag.sy - ry, w: 2 * rx, h: 2 * ry };
  }
  return {
    x: Math.min(drag.sx, drag.cx),
    y: Math.min(drag.sy, drag.cy),
    w: Math.abs(drag.cx - drag.sx),
    h: Math.abs(drag.cy - drag.sy),
  };
}

export function normalizeImageCoordinate(
  clientX: number,
  clientY: number,
  rect: { left: number; top: number },
  vp: Viewport,
  imgW: number,
  imgH: number,
): { x: number; y: number } {
  return {
    x: (clientX - rect.left - vp.tx) / vp.scale / imgW,
    y: (clientY - rect.top - vp.ty) / vp.scale / imgH,
  };
}

export function isNormalizedImagePoint(
  point: { x: number; y: number } | null,
): point is { x: number; y: number } {
  return !!point && point.x >= 0 && point.x <= 1 && point.y >= 0 && point.y <= 1;
}

export function siblingHighlightChildren<T extends { parent_annotation_id?: string | null }>(
  boxes: T[],
  selectedId: string | null,
  selectionSize: number,
): T[] {
  const parentId = selectionSize === 1 ? selectedId : null;
  if (!parentId) return [];
  return boxes.filter((b) => b.parent_annotation_id === parentId);
}

export function shouldRenderImageAnnotationShape(
  annotation: Pick<Annotation, "geometry">,
): boolean {
  return annotation.geometry?.type !== "raster_mask";
}

export function resolveSnapMatch(
  point: Pt,
  candidates: {
    points: readonly SnapPointCandidate[];
    segments: readonly SnapSegmentCandidate[];
  },
  thresholdPx: number,
  transform: SnapViewportTransform,
): SnapMatch | null {
  const pointMatch = snapPointToCandidates(point, candidates.points, thresholdPx, transform);
  const segmentMatch = snapPointToSegments(point, candidates.segments, thresholdPx, transform);
  if (!pointMatch) return segmentMatch;
  if (!segmentMatch) return pointMatch;
  return pointMatch.distancePx <= segmentMatch.distancePx ? pointMatch : segmentMatch;
}

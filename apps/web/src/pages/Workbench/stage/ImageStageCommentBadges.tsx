import type { Annotation, Geometry } from "@/types";
import { Icon } from "@/components/ui/Icon";
import type { Viewport } from "../state/useViewportTransform";
import type { RasterMaskNormalizedBounds } from "./shared/rasterMaskRender";
import styles from "./ImageStage.module.css";

export interface AnnotationCommentBadgeModel {
  id: string;
  count: number;
  label: string;
  left: number;
  top: number;
  priority: number;
}

export interface AnnotationCommentBadgeAnchor {
  id: string;
  count: number;
  bounds: RasterMaskNormalizedBounds;
}

export interface BuildAnnotationCommentBadgeModelsOptions {
  anchors: readonly AnnotationCommentBadgeAnchor[];
  imgW: number;
  imgH: number;
  vp: Pick<Viewport, "scale" | "tx" | "ty">;
  viewportSize?: { w: number; h: number };
  selectedIds?: ReadonlySet<string>;
}

type RasterMaskBoundsRecord = {
  id: string;
  bounds?: RasterMaskNormalizedBounds | null;
};

export interface BuildAnnotationCommentBadgesOptions {
  annotations: readonly Annotation[];
  rasterMaskRecords?: readonly RasterMaskBoundsRecord[];
  counts?: Readonly<Record<string, number>>;
  imgW: number;
  imgH: number;
  vp: Pick<Viewport, "scale" | "tx" | "ty">;
  viewportSize?: { w: number; h: number };
  selectedIds?: ReadonlySet<string>;
}

function boundsFromPoints(points: readonly [number, number][]): RasterMaskNormalizedBounds | null {
  if (points.length === 0) return null;
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const [x, y] of points) {
    if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
    minX = Math.min(minX, x);
    minY = Math.min(minY, y);
    maxX = Math.max(maxX, x);
    maxY = Math.max(maxY, y);
  }
  return Number.isFinite(minX) &&
    Number.isFinite(minY) &&
    Number.isFinite(maxX) &&
    Number.isFinite(maxY)
    ? { x: minX, y: minY, w: maxX - minX, h: maxY - minY }
    : null;
}

export function annotationCommentBadgeBoundsFromGeometry(
  annotation: Annotation,
  geometry: Geometry | undefined = annotation.geometry,
  imgW = 1,
  imgH = 1,
): RasterMaskNormalizedBounds | null {
  if (!geometry) return { x: annotation.x, y: annotation.y, w: annotation.w, h: annotation.h };
  if (geometry.type === "raster_mask") return null;
  if (geometry.type === "bbox") {
    return { x: geometry.x, y: geometry.y, w: geometry.w, h: geometry.h };
  }
  if (geometry.type === "rotated_bbox") {
    const angle = (geometry.angle * Math.PI) / 180;
    const halfW = geometry.w * imgW * 0.5;
    const halfH = geometry.h * imgH * 0.5;
    const extentX = Math.abs(Math.cos(angle)) * halfW + Math.abs(Math.sin(angle)) * halfH;
    const extentY = Math.abs(Math.sin(angle)) * halfW + Math.abs(Math.cos(angle)) * halfH;
    return {
      x: geometry.cx - extentX / imgW,
      y: geometry.cy - extentY / imgH,
      w: (extentX * 2) / imgW,
      h: (extentY * 2) / imgH,
    };
  }
  if (geometry.type === "polygon" || geometry.type === "polyline") {
    return boundsFromPoints(geometry.points);
  }
  if (geometry.type === "multi_polygon") {
    return boundsFromPoints(geometry.polygons.flatMap((polygon) => polygon.points));
  }
  if (geometry.type === "keypoint") {
    return boundsFromPoints(
      geometry.points.filter((point) => point.v > 0).map((point) => [point.x, point.y]),
    );
  }
  return { x: annotation.x, y: annotation.y, w: annotation.w, h: annotation.h };
}

function validBounds(
  bounds: RasterMaskNormalizedBounds | undefined | null,
): bounds is RasterMaskNormalizedBounds {
  return (
    !!bounds &&
    Number.isFinite(bounds.x) &&
    Number.isFinite(bounds.y) &&
    Number.isFinite(bounds.w) &&
    Number.isFinite(bounds.h) &&
    bounds.w >= 0 &&
    bounds.h >= 0
  );
}

function validRasterBounds(
  bounds: RasterMaskNormalizedBounds | undefined | null,
): bounds is RasterMaskNormalizedBounds {
  return validBounds(bounds) && bounds.w > 0 && bounds.h > 0;
}

export function annotationCommentBadgeBounds(
  annotation: Annotation,
  rasterMaskBoundsById: ReadonlyMap<
    string,
    RasterMaskNormalizedBounds | null | undefined
  > = new Map(),
  imgW = 1,
  imgH = 1,
): RasterMaskNormalizedBounds | null {
  if (annotation.geometry?.type === "raster_mask") {
    const bounds = rasterMaskBoundsById.get(annotation.id);
    return validRasterBounds(bounds) ? bounds : null;
  }
  const bounds = annotationCommentBadgeBoundsFromGeometry(
    annotation,
    annotation.geometry,
    imgW,
    imgH,
  );
  return validBounds(bounds) ? bounds : null;
}

export function buildAnnotationCommentBadges({
  annotations,
  rasterMaskRecords = [],
  counts,
  imgW,
  imgH,
  vp,
  viewportSize,
  selectedIds,
}: BuildAnnotationCommentBadgesOptions): AnnotationCommentBadgeModel[] {
  if (!counts || vp.scale <= 0 || !Number.isFinite(imgW) || !Number.isFinite(imgH)) return [];
  const rasterMaskBoundsById = new Map(
    rasterMaskRecords.map((record) => [record.id, record.bounds] as const),
  );

  const anchors = annotations.flatMap((annotation) => {
    if (annotation.is_hidden) return [];
    const rawCount = counts[annotation.id];
    if (!Number.isFinite(rawCount) || Math.trunc(rawCount) <= 0) return [];
    const bounds = annotationCommentBadgeBounds(annotation, rasterMaskBoundsById, imgW, imgH);
    if (!bounds) return [];
    const count = Math.trunc(rawCount);
    if (count <= 0) return [];
    return [{ id: annotation.id, count, bounds }];
  });
  return buildAnnotationCommentBadgeModels({
    anchors,
    imgW,
    imgH,
    vp,
    viewportSize,
    selectedIds,
  });
}

export function buildAnnotationCommentBadgeModels({
  anchors,
  imgW,
  imgH,
  vp,
  viewportSize,
  selectedIds,
}: BuildAnnotationCommentBadgeModelsOptions): AnnotationCommentBadgeModel[] {
  if (vp.scale <= 0 || !Number.isFinite(imgW) || !Number.isFinite(imgH)) return [];
  return anchors.flatMap(({ id, count, bounds }) => {
    if (!Number.isFinite(count) || Math.trunc(count) <= 0 || !validBounds(bounds)) return [];
    const safeCount = Math.trunc(count);
    const right = vp.tx + (bounds.x + bounds.w) * imgW * vp.scale;
    const topEdge = vp.ty + bounds.y * imgH * vp.scale;
    const { left, top } = placeAnnotationCommentBadge(right, topEdge, viewportSize);
    return [
      {
        id,
        count: safeCount,
        label: safeCount > 9 ? "9+" : String(safeCount),
        left,
        top,
        priority: selectedIds?.has(id) ? 2 : 1,
      },
    ];
  });
}

const BADGE_HITBOX_PX = 40;
const BADGE_HALF_PX = BADGE_HITBOX_PX / 2;
const BADGE_HANDLE_GAP_PX = 8;
const BADGE_OFFSET_PX = BADGE_HALF_PX + BADGE_HANDLE_GAP_PX;

function placeAnnotationCommentBadge(
  right: number,
  topEdge: number,
  viewportSize?: { w: number; h: number },
): { left: number; top: number } {
  const preferred = { left: right + BADGE_OFFSET_PX, top: topEdge - BADGE_OFFSET_PX };
  if (!viewportSize || viewportSize.w <= 0 || viewportSize.h <= 0) return preferred;

  const candidates = [
    preferred,
    { left: right + BADGE_OFFSET_PX, top: topEdge + BADGE_OFFSET_PX },
    { left: right - BADGE_OFFSET_PX, top: topEdge - BADGE_OFFSET_PX },
    { left: right - BADGE_OFFSET_PX, top: topEdge + BADGE_OFFSET_PX },
  ];
  const minLeft = BADGE_HALF_PX;
  const maxLeft = Math.max(minLeft, viewportSize.w - BADGE_HALF_PX);
  const minTop = BADGE_HALF_PX;
  const maxTop = Math.max(minTop, viewportSize.h - BADGE_HALF_PX);
  const visibleCandidate = candidates.find(
    (candidate) =>
      candidate.left >= minLeft &&
      candidate.left <= maxLeft &&
      candidate.top >= minTop &&
      candidate.top <= maxTop,
  );
  if (visibleCandidate) return visibleCandidate;

  return {
    left: Math.min(Math.max(preferred.left, minLeft), maxLeft),
    top: Math.min(Math.max(preferred.top, minTop), maxTop),
  };
}

export interface ImageStageCommentBadgesProps extends BuildAnnotationCommentBadgesOptions {
  onOpenAnnotationComments?: (annotationId: string) => void;
  interactive?: boolean;
}

export interface AnnotationCommentBadgeTargetsProps {
  badges: readonly AnnotationCommentBadgeModel[];
  onOpenAnnotationComments?: (annotationId: string) => void;
  interactive?: boolean;
}

export function AnnotationCommentBadgeTargets({
  badges,
  onOpenAnnotationComments,
  interactive = true,
}: AnnotationCommentBadgeTargetsProps) {
  if (badges.length === 0) return null;

  return (
    <div
      className={`${styles.annotationCommentBadges}${interactive ? "" : ` ${styles.annotationCommentBadgesDisabled}`}`}
      data-testid="annotation-comment-badges"
    >
      {badges.map((badge) => {
        const label = `标注 ${badge.id} 有 ${badge.count} 条评论`;
        return (
          <button
            key={badge.id}
            type="button"
            className={styles.annotationCommentBadge}
            ref={(element) => {
              element?.style.setProperty("--annotation-comment-left", `${badge.left}px`);
              element?.style.setProperty("--annotation-comment-top", `${badge.top}px`);
            }}
            data-priority={badge.priority}
            data-testid="annotation-comment-badge"
            data-annotation-id={badge.id}
            aria-label={label}
            title={label}
            disabled={!interactive}
            tabIndex={interactive ? 0 : -1}
            onPointerDown={(event) => event.stopPropagation()}
            onPointerUp={(event) => event.stopPropagation()}
            onClick={(event) => {
              event.stopPropagation();
              if (!interactive) return;
              onOpenAnnotationComments?.(badge.id);
            }}
            onDoubleClick={(event) => event.stopPropagation()}
            onKeyDown={(event) => {
              event.stopPropagation();
              if (!interactive && (event.key === "Enter" || event.key === " ")) {
                event.preventDefault();
              }
            }}
            onKeyUp={(event) => event.stopPropagation()}
          >
            <span className={styles.annotationCommentBadgeBubble}>
              <Icon name="messageCircle" size={12} />
              <span aria-hidden="true">{badge.label}</span>
            </span>
          </button>
        );
      })}
    </div>
  );
}

export function ImageStageCommentBadges({
  annotations,
  rasterMaskRecords,
  counts,
  imgW,
  imgH,
  vp,
  viewportSize,
  selectedIds,
  onOpenAnnotationComments,
  interactive = true,
}: ImageStageCommentBadgesProps) {
  const badges = buildAnnotationCommentBadges({
    annotations,
    rasterMaskRecords,
    counts,
    imgW,
    imgH,
    vp,
    viewportSize,
    selectedIds,
  });
  return (
    <AnnotationCommentBadgeTargets
      badges={badges}
      onOpenAnnotationComments={onOpenAnnotationComments}
      interactive={interactive}
    />
  );
}

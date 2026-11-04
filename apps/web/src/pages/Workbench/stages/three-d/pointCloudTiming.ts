export const POINT_CLOUD_TIMING_EVENT = "pointcloud:render-timing";

export type PointCloudTimingPhase =
  | "pcd-frame-ready"
  | "camera-bitmaps-ready"
  | "camera-depth-ready"
  | "camera-textures-ready"
  | "geometry-ready"
  | "camera-color-ready";

export interface PointCloudTimingDetail {
  phase: PointCloudTimingPhase;
  pointCloudUrl: string;
  at: number;
  durationMs?: number;
  cacheHit?: boolean;
}

function emitPointCloudTiming(detail: PointCloudTimingDetail) {
  if (typeof window === "undefined") return;
  performance.mark(`pointcloud:${detail.phase}`, { detail });
  window.dispatchEvent(
    new CustomEvent<PointCloudTimingDetail>(POINT_CLOUD_TIMING_EVENT, { detail }),
  );
}

/** Emit a non-paint resource stage into the same trace stream as the paint boundaries. */
export function markPointCloudStage(
  phase: Exclude<PointCloudTimingPhase, "geometry-ready" | "camera-color-ready">,
  pointCloudUrl: string,
  startedAt: number,
  cacheHit?: boolean,
) {
  if (typeof window === "undefined") return;
  const at = performance.now();
  emitPointCloudTiming({
    phase,
    pointCloudUrl,
    at,
    durationMs: at - startedAt,
    cacheHit,
  });
}

/**
 * Emit after the preceding scene/material update has crossed a browser paint boundary.
 * The matching Performance mark is visible in DevTools traces; the event drives the
 * repeatable local A/B benchmark without polling transient UI text.
 */
export function markPointCloudPaint(
  phase: PointCloudTimingPhase,
  pointCloudUrl: string,
  shouldCommit: () => boolean = () => true,
) {
  window.requestAnimationFrame(() => {
    window.requestAnimationFrame(() => {
      if (!shouldCommit()) return;
      const detail: PointCloudTimingDetail = {
        phase,
        pointCloudUrl,
        at: performance.now(),
      };
      emitPointCloudTiming(detail);
    });
  });
}

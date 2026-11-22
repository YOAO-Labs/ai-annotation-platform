import type { Pt } from "./polygonGeom";

export const POLYGON_AUTO_SPACING_PX = 8;
export const POLYGON_AUTO_POINT_LIMIT = 20_000;

export function samePolygonPoint(a: Pt | undefined, b: Pt): boolean {
  return !!a && Math.hypot(a[0] - b[0], a[1] - b[1]) < 1e-10;
}

export function createPolygonSampler(start: Pt, width: number, height: number) {
  let previous = start;
  let remainder = 0;
  return {
    sample(point: Pt, emit: (point: Pt) => boolean) {
      const dx = point[0] - previous[0];
      const dy = point[1] - previous[1];
      const distance = Math.hypot(dx * width, dy * height);
      if (!Number.isFinite(distance) || distance < 1e-10) return;
      let travelled = POLYGON_AUTO_SPACING_PX - remainder;
      while (travelled <= distance + 1e-8) {
        const t = Math.min(1, travelled / distance);
        if (!emit([previous[0] + dx * t, previous[1] + dy * t])) break;
        travelled += POLYGON_AUTO_SPACING_PX;
      }
      remainder = (remainder + distance) % POLYGON_AUTO_SPACING_PX;
      if (remainder < 1e-8 || POLYGON_AUTO_SPACING_PX - remainder < 1e-8) remainder = 0;
      previous = point;
    },
  };
}

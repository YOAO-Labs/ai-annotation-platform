import type { Viewport } from "../useViewportTransform";
import { clampScale } from "./zoom";

export interface NormalizedRegion {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

export function fitNormalizedRegion(
  current: Viewport,
  region: NormalizedRegion,
  content: { width: number; height: number },
  viewport: { width: number; height: number },
  margin = 48,
): Viewport {
  const { x0, y0, x1, y1 } = region;
  if (
    !Number.isFinite(x0) ||
    !Number.isFinite(y0) ||
    !Number.isFinite(x1) ||
    !Number.isFinite(y1) ||
    x0 < 0 ||
    y0 < 0 ||
    x1 > 1 ||
    y1 > 1 ||
    x0 >= x1 ||
    y0 >= y1 ||
    content.width <= 0 ||
    content.height <= 0 ||
    viewport.width <= 0 ||
    viewport.height <= 0
  )
    return current;
  const regionWidth = (x1 - x0) * content.width;
  const regionHeight = (y1 - y0) * content.height;
  const availableWidth = Math.max(1, viewport.width - margin * 2);
  const availableHeight = Math.max(1, viewport.height - margin * 2);
  const scale = clampScale(Math.min(availableWidth / regionWidth, availableHeight / regionHeight));
  const centerX = ((x0 + x1) / 2) * content.width;
  const centerY = ((y0 + y1) / 2) * content.height;
  return {
    scale,
    tx: viewport.width / 2 - centerX * scale,
    ty: viewport.height / 2 - centerY * scale,
  };
}

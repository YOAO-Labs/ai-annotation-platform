import type { Viewport } from "../useViewportTransform";

export function fitToCanvas(
  viewportW: number,
  viewportH: number,
  contentW: number,
  contentH: number,
): Viewport | null {
  if (!viewportW || !viewportH || !contentW || !contentH) return null;
  const scale = Math.min(viewportW / contentW, viewportH / contentH);
  return {
    scale,
    tx: (viewportW - contentW * scale) / 2,
    ty: (viewportH - contentH * scale) / 2,
  };
}

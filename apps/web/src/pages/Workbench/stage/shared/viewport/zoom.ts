import type { Viewport } from "../useViewportTransform";
import { fitToCanvas } from "./fit";

export const SCALE_RANGE = { min: 0.2, max: 8 } as const;

export interface ScaleRange {
  min: number;
  max: number;
}

export function fitAwareScaleRange(
  viewportW: number,
  viewportH: number,
  contentW: number,
  contentH: number,
): ScaleRange {
  const fitScale = fitToCanvas(viewportW, viewportH, contentW, contentH)?.scale;
  return {
    min: fitScale && fitScale > 0 ? Math.min(SCALE_RANGE.min, fitScale) : SCALE_RANGE.min,
    max: SCALE_RANGE.max,
  };
}

export function clampScale(scale: number, range: ScaleRange = SCALE_RANGE): number {
  return Math.min(range.max, Math.max(range.min, scale));
}

export function zoomAtPoint(
  vp: Viewport,
  cx: number,
  cy: number,
  nextScale: number,
  range: ScaleRange = SCALE_RANGE,
): Viewport {
  const scale = clampScale(nextScale, range);
  if (scale === vp.scale) return vp;
  const ratio = scale / vp.scale;
  return {
    scale,
    tx: cx - (cx - vp.tx) * ratio,
    ty: cy - (cy - vp.ty) * ratio,
  };
}

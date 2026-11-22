import type { Viewport } from "../state/useViewportTransform";

export type VideoPixelSize = { w: number; h: number };
export type Point = { x: number; y: number };

export function videoIntrinsicSize(width?: number | null, height?: number | null): VideoPixelSize {
  const aspect = width && height ? width / height : 16 / 9;
  const w = width || 1280;
  const h = height || Math.round(w / aspect);
  return { w, h };
}

export function normToPixel(pt: Point, size: VideoPixelSize): Point {
  return { x: pt.x * size.w, y: pt.y * size.h };
}

export function pixelToNorm(pt: Point, size: VideoPixelSize): Point {
  return {
    x: size.w > 0 ? pt.x / size.w : 0,
    y: size.h > 0 ? pt.y / size.h : 0,
  };
}

export function clientToVideoNorm(
  clientX: number,
  clientY: number,
  rect: { left: number; top: number },
  vp: Viewport,
  size: VideoPixelSize,
): Point | null {
  if (!size.w || !size.h) return null;
  return {
    x: (clientX - rect.left - vp.tx) / vp.scale / size.w,
    y: (clientY - rect.top - vp.ty) / vp.scale / size.h,
  };
}

export function videoNormToClient(
  pt: Point,
  rect: { left: number; top: number },
  vp: Viewport,
  size: VideoPixelSize,
): Point {
  return {
    x: rect.left + vp.tx + pt.x * size.w * vp.scale,
    y: rect.top + vp.ty + pt.y * size.h * vp.scale,
  };
}

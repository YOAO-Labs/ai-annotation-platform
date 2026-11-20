import type { Viewport } from "../state/useViewportTransform";

/**
 * 视频 Konva 栈坐标模型(决策 B:像素空间 + Konva transform)。
 *
 * 历史 SVG 栈曾用「归一化 [0,1] + SVG CTM + viewBoxHeight=1/aspect」两套 client↔svg
 * fallback(已随 SVG 栈删除)。本栈废弃 CTM 路径,改用图片侧同款范式:
 *   - 存储仍是归一化 [0,1]×[0,1](数据零迁移,epic 红线①);
 *   - 渲染/命中边界用视频固有宽高(等价图片 imgW/imgH)换算成像素「世界」坐标;
 *   - Konva Stage 负责 scale/pan,client↔world 用与图片 `toImg()` 同构的逆变换。
 *
 * 纯函数,栈无关,便于单测。本版(底图-only)主要由 normToPixel 喂 Konva.Image 尺寸;
 * 标注层命中测试复用 client↔norm 转换；测试覆盖坐标往返一致性。
 */

export type VideoPixelSize = { w: number; h: number };
export type Point = { x: number; y: number };

/** 视频元数据宽高 → 固有像素尺寸(等价图片 imgW/imgH)。缺省回退 1280×720(16:9)。
 *  与历史归一化推导逐位一致(向后兼容)。 */
export function videoIntrinsicSize(width?: number | null, height?: number | null): VideoPixelSize {
  const aspect = width && height ? width / height : 16 / 9;
  const w = width || 1280;
  const h = height || Math.round(w / aspect);
  return { w, h };
}

/** 归一化 [0,1] → 像素世界坐标。 */
export function normToPixel(pt: Point, size: VideoPixelSize): Point {
  return { x: pt.x * size.w, y: pt.y * size.h };
}

/** 像素世界坐标 → 归一化 [0,1]。尺寸为 0 时该轴返回 0(避免除零)。 */
export function pixelToNorm(pt: Point, size: VideoPixelSize): Point {
  return {
    x: size.w > 0 ? pt.x / size.w : 0,
    y: size.h > 0 ? pt.y / size.h : 0,
  };
}

/**
 * 屏幕(client)坐标 → 归一化视频坐标。与图片 `toImg()` 同构:
 * 先减容器左上 + 视口平移、除以缩放得像素世界坐标,再除以固有尺寸归一化。
 * 容器矩形 / 尺寸未就绪时返回 null,调用方早退。
 */
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

/** 归一化视频坐标 → 屏幕(client)坐标。clientToVideoNorm 的逆变换(往返一致)。 */
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

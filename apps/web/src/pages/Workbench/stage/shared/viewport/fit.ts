import type { Viewport } from "../useViewportTransform";

/**
 * 公共 viewport 原语 · fit-to-canvas(纯计算)。
 *
 * 画布栈统一地基:把「内容居中铺进视口」的缩放/平移计算从 ImageStage(`fitNow`)
 * 与 useViewportTransform(`fit`)各自的内联实现里抽成单一纯函数,供图片现在、视频
 * 复用——消除「同一段 fit 数学两份维护、悄悄漂移」的长期税。
 *
 * 取较小缩放比让内容完整可见(contain 语义),并在两轴居中留边。
 * 入参任一为 0 / 负(视口或内容尺寸未就绪)时返回 null,调用方保留当前 vp。
 */
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

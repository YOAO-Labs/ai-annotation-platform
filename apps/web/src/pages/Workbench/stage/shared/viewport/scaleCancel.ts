/**
 * 公共 viewport 原语 · scale 抵消(纯计算)。
 *
 * Konva 形状节点活在「已被 Stage scale 放大的世界坐标系」里,要让线宽 / 字号 / 控制点 /
 * dash 等视觉量在**屏幕**上恒定(不随缩放变粗变大),就得把屏幕像素量除以当前 scale 换算成
 * 世界量再喂给节点——即图片侧遍布的 `px / scale`。
 *
 * 图片和视频画布共用此缩放原语。语义等价于
 * 内联 `px / scale`，供图片和视频共用。
 */

/** 屏幕像素量 → 世界量(scale 抵消)。等价旧内联 `px / scale`。 */
export function screenToWorld(px: number, scale: number): number {
  return px / scale;
}

/** dash 数组的逐项 scale 抵消(等价 `[a/scale, b/scale, …]`)。 */
export function dashToWorld(dash: number[], scale: number): number[] {
  return dash.map((d) => d / scale);
}

// 时间轴帧 ↔ 百分比坐标换算的单一来源 (single source of truth)。
//
// 从 VideoPlaybackOverlay 的 frameLeft / frameFromPointer / rangeStyle 三处内联
// 公式抽出, 先以固定窗口 {from:0, to:maxFrame} 注入 —— 此步纯重构, 零行为变更。WS2 起窗口可收窄为
// 可见帧子区间 (横向 zoom), 届时点位 / 区间刷选 / 密度条全部经此换算, 保证同一坐标基准。

/** 可见帧窗口 [from, to] (含端点)。全窗口即 {from:0, to:maxFrame}。 */
export interface TimelineWindow {
  from: number;
  to: number;
}

/**
 * 帧 → 沿可见窗口的百分比 [0..100]。窗口跨度 <=0 时返回 0
 * (与旧 `maxFrame > 0 ? … : 0` 分支等价, 避免除零)。
 */
export function frameToPct(frame: number, win: TimelineWindow): number {
  const span = win.to - win.from;
  return span > 0 ? ((frame - win.from) / span) * 100 : 0;
}

/**
 * 指针在轨道内的比例 [0..1] → 帧 (四舍五入, clamp 到窗口 [from, to])。
 * ratio 由调用方以 `(pointerX - rect.left) / rect.width` 算得, 允许越界 (超出即被 clamp)。
 */
export function pctToFrame(ratio: number, win: TimelineWindow): number {
  const span = win.to - win.from;
  const frame = win.from + ratio * span;
  return Math.max(win.from, Math.min(win.to, Math.round(frame)));
}

// —— 横向缩放/平移 ——
// 缩放/平移的窗口可为分数帧 (避免反复缩放的整数取整漂移); 渲染与反解不受影响 (pctToFrame 末端取整)。

/** 最小可见跨度 (帧): 放大到此即停, 避免聚合密度桶在过小窗口里被误读成精确单帧。 */
export const MIN_VISIBLE_SPAN = 48;
/** 每次滚轮的缩放系数指数 k: factor = exp(deltaY * k)。deltaY<0 (上滚) → factor<1 放大。 */
export const ZOOM_WHEEL_K = 0.0015;

/** 是否全窗口 (未缩放)。maxFrame<=0 视为全窗口 (空视频不缩放)。 */
export function isFullWindow(win: TimelineWindow, maxFrame: number): boolean {
  return win.from <= 0 && win.to >= maxFrame;
}

/** 把窗口约束进 [0, maxFrame], 跨度夹到 [min(minSpan,maxFrame), maxFrame], 越界时整体平移保跨度。 */
export function clampWindow(
  win: TimelineWindow,
  maxFrame: number,
  minSpan: number,
): TimelineWindow {
  if (maxFrame <= 0) return { from: 0, to: 0 };
  const span = Math.min(Math.max(win.to - win.from, Math.min(minSpan, maxFrame)), maxFrame);
  let from = win.from;
  if (from + span > maxFrame) from = maxFrame - span;
  if (from < 0) from = 0;
  return { from, to: from + span };
}

/** 以锚点比例 anchorRatio∈[0,1] 为不动点缩放窗口。factor<1 放大, >1 缩小。 */
export function zoomWindow(
  win: TimelineWindow,
  maxFrame: number,
  anchorRatio: number,
  factor: number,
  minSpan: number,
): TimelineWindow {
  const span = win.to - win.from;
  const anchor = win.from + anchorRatio * span;
  const nextSpan = Math.min(Math.max(span * factor, Math.min(minSpan, maxFrame)), maxFrame);
  const from = anchor - anchorRatio * nextSpan;
  return clampWindow({ from, to: from + nextSpan }, maxFrame, minSpan);
}

/** 平移窗口 (deltaFrames>0 向后/右移), 保跨度并 clamp 进 [0, maxFrame]。 */
export function panWindow(
  win: TimelineWindow,
  maxFrame: number,
  deltaFrames: number,
  minSpan: number,
): TimelineWindow {
  return clampWindow({ from: win.from + deltaFrames, to: win.to + deltaFrames }, maxFrame, minSpan);
}

// 尺子刻度步长: 选一个"整齐"帧步长 (1/2/2.5/5×10ⁿ), 使可见窗口内约 6 个标签。
// 随缩放窗口跨度变化自动切档, 长视频缩放后不会挤成一坨或稀到没参照。
export function niceFrameStep(span: number): number {
  const target = Math.max(1, span / 6);
  const pow = Math.pow(10, Math.floor(Math.log10(target)));
  const candidate = [1, 2, 2.5, 5, 10].map((m) => m * pow).find((c) => c >= target);
  return Math.max(1, Math.round(candidate ?? 10 * pow));
}

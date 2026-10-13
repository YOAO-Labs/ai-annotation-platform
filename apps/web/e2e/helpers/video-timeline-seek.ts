/**
 * 视频时间轴定位辅助（`default-two` shard 2/4 偶发失败修复）。
 *
 * 背景：持久化用例曾用「裸坐标点击时间轴 + 固定十次键盘步进补偿」建立前置帧。
 * 播放浮层在指针离开画布 2s 后进入 `pointer-events: none`，裸 `mouse.click`
 * 绕过 actionability 检查被静默吞掉，只剩 k 键网格吸附帧和有限补偿（失败 trace
 * 中点击后停留 F95、逐步补偿到 F105、最终断言 F120 失败的来源）。
 *
 * 现在的定位契约：先建立「可见窗口包含目标」，再经应用既有的 range
 * `onChange → onSeek` 通路写入能精确表达目标帧的归一化值。换算必须与组件的
 * `timelineCoords.pctToFrame` 一致：value/10000 → ratio → round(clamp(from + ratio*span))，
 * 反向映射不能精确还原目标帧时显式失败，绝不静默接受近似帧。
 */

/** 可见帧窗口 [from, to]（含端点，可为缩放产生的分数值）。 */
export interface TimelineWindowRange {
  from: number;
  to: number;
}

export interface RangeValueMapping {
  /** 写入 range 输入的归一化值（min=0, max=10000）。 */
  value: number;
  /** 组件按 pctToFrame 反解出的帧号。 */
  representedFrame: number;
  /** 反解是否精确等于目标帧；false 时调用方必须显式失败或缩窗重试。 */
  expressible: boolean;
}

/** 非法 seek 目标的描述；合法目标返回 null。 */
export function invalidSeekTarget(frame: number): string | null {
  if (!Number.isFinite(frame) || !Number.isInteger(frame) || frame < 0)
    return `seek 目标必须是有限的非负整数帧，收到 ${frame}`;
  return null;
}

/**
 * 目标帧 → range 归一化值（0..10000）的反向映射，并按组件同款公式反解验证。
 * 窗口退化（span<=0）时返回不可表达；调用方应先检查窗口再调用以获得更准确的报错。
 */
export function rangeValueForFrame(frame: number, win: TimelineWindowRange): RangeValueMapping {
  const span = win.to - win.from;
  if (span <= 0) return { value: 0, representedFrame: Number.NaN, expressible: false };
  const value = Math.round(((frame - win.from) / span) * 10000);
  const representedFrame = Math.max(
    win.from,
    Math.min(win.to, Math.round(win.from + (value / 10000) * span)),
  );
  return {
    value,
    representedFrame,
    expressible:
      Number.isInteger(frame) &&
      frame >= 0 &&
      value >= 0 &&
      value <= 10000 &&
      representedFrame === frame,
  };
}

/**
 * 真实指针测试：目标帧在 shell 上的点击横坐标（像素，相对 shell 左缘）。
 * 端点各内缩 2px 避开边界像素、圆角与子像素舍入；中间帧使用精确比例位置
 * （浏览器输入的整数像素量化引起的比例漂移远小于半帧）。几何与组件
 * `frameFromPointer`（(clientX - rect.left) / rect.width → pctToFrame）一致。
 */
export function timelineClickX(frame: number, win: TimelineWindowRange, width: number): number {
  const span = win.to - win.from;
  if (!Number.isFinite(width) || width <= 0) throw new Error(`时间轴 shell 宽度不可用：${width}`);
  if (span <= 0) throw new Error(`时间轴窗口 [${win.from}, ${win.to}] 退化，无法换算点击位置`);
  const ratio = (frame - win.from) / span;
  if (ratio === 0) return 2;
  if (ratio === 1) return width - 2;
  return ratio * width;
}

/**
 * 组件 `frameFromPointer` 的镜像：对实际落点像素预测应用将导航到的帧。
 * 浏览器输入量化到整数像素，端点附近的比例漂移可能改变取整结果；断言必须
 * 使用本函数对实际 x 的预测，而不是想当然的目标帧。
 */
export function frameAtClickX(x: number, win: TimelineWindowRange, width: number): number {
  const span = win.to - win.from;
  if (!Number.isFinite(width) || width <= 0) throw new Error(`时间轴 shell 宽度不可用：${width}`);
  if (span <= 0) throw new Error(`时间轴窗口 [${win.from}, ${win.to}] 退化，无法换算点击位置`);
  return Math.max(win.from, Math.min(win.to, Math.round(win.from + (x / width) * span)));
}

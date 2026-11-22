export interface TimelineWindowRange {
  from: number;
  to: number;
}

export interface RangeValueMapping {
  value: number;
  representedFrame: number;
  expressible: boolean;
}

export function invalidSeekTarget(frame: number): string | null {
  if (!Number.isFinite(frame) || !Number.isInteger(frame) || frame < 0)
    return `seek 目标必须是有限的非负整数帧，收到 ${frame}`;
  return null;
}

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

export function timelineClickX(frame: number, win: TimelineWindowRange, width: number): number {
  const span = win.to - win.from;
  if (!Number.isFinite(width) || width <= 0) throw new Error(`时间轴 shell 宽度不可用：${width}`);
  if (span <= 0) throw new Error(`时间轴窗口 [${win.from}, ${win.to}] 退化，无法换算点击位置`);
  const ratio = (frame - win.from) / span;
  if (ratio === 0) return 2;
  if (ratio === 1) return width - 2;
  return ratio * width;
}

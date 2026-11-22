export interface TimelineWindow {
  from: number;
  to: number;
}

export function frameToPct(frame: number, win: TimelineWindow): number {
  const span = win.to - win.from;
  return span > 0 ? ((frame - win.from) / span) * 100 : 0;
}

export function pctToFrame(ratio: number, win: TimelineWindow): number {
  const span = win.to - win.from;
  const frame = win.from + ratio * span;
  return Math.max(win.from, Math.min(win.to, Math.round(frame)));
}

export const MIN_VISIBLE_SPAN = 48;
export const ZOOM_WHEEL_K = 0.0015;

export function isFullWindow(win: TimelineWindow, maxFrame: number): boolean {
  return win.from <= 0 && win.to >= maxFrame;
}

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

export function panWindow(
  win: TimelineWindow,
  maxFrame: number,
  deltaFrames: number,
  minSpan: number,
): TimelineWindow {
  return clampWindow({ from: win.from + deltaFrames, to: win.to + deltaFrames }, maxFrame, minSpan);
}

export function niceFrameStep(span: number): number {
  const target = Math.max(1, span / 6);
  const pow = Math.pow(10, Math.floor(Math.log10(target)));
  const candidate = [1, 2, 2.5, 5, 10].map((m) => m * pow).find((c) => c >= target);
  return Math.max(1, Math.round(candidate ?? 10 * pow));
}

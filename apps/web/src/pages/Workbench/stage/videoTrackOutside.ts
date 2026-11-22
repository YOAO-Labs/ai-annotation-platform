import type { VideoTrackGeometry, VideoTrackOutsideRange } from "@/types";

type OutsideInput = Partial<VideoTrackOutsideRange> | null | undefined;

function normalizeFrame(value: unknown) {
  const frame = Number(value);
  if (!Number.isFinite(frame)) return null;
  return Math.max(0, Math.floor(frame));
}

function cleanRange(range: OutsideInput): VideoTrackOutsideRange | null {
  if (!range) return null;
  const from = normalizeFrame(range.from);
  const to = normalizeFrame(range.to);
  if (from === null || to === null) return null;
  const start = Math.min(from, to);
  const end = Math.max(from, to);
  return {
    from: start,
    to: end,
    source: range.source === "prediction" ? "prediction" : "manual",
  };
}

export function normalizeOutsideRanges(
  ranges: readonly OutsideInput[] | undefined,
): VideoTrackOutsideRange[] {
  const cleaned = (ranges ?? [])
    .map(cleanRange)
    .filter((range): range is VideoTrackOutsideRange => Boolean(range))
    .sort(
      (a, b) =>
        a.from - b.from ||
        a.to - b.to ||
        (a.source ?? "manual").localeCompare(b.source ?? "manual"),
    );

  const merged: VideoTrackOutsideRange[] = [];
  for (const range of cleaned) {
    const prev = merged[merged.length - 1];
    if (prev && prev.source === range.source && range.from <= prev.to + 1) {
      prev.to = Math.max(prev.to, range.to);
      continue;
    }
    merged.push({ ...range });
  }
  return merged;
}

export type OutsideRangeCarrier = { outside?: readonly VideoTrackOutsideRange[] | null };

export function effectiveOutsideRanges(track: OutsideRangeCarrier): VideoTrackOutsideRange[] {
  return normalizeOutsideRanges(track.outside ?? []);
}

export function isFrameInOutsideRanges(
  ranges: readonly VideoTrackOutsideRange[],
  frameIndex: number,
) {
  return ranges.some((range) => frameIndex >= range.from && frameIndex <= range.to);
}

export function isFrameOutside(track: OutsideRangeCarrier, frameIndex: number) {
  return isFrameInOutsideRanges(effectiveOutsideRanges(track), frameIndex);
}

export function outsideRangesIntersect(
  ranges: readonly VideoTrackOutsideRange[],
  fromFrame: number,
  toFrame: number,
) {
  const from = Math.min(fromFrame, toFrame);
  const to = Math.max(fromFrame, toFrame);
  return ranges.some((range) => range.from <= to && range.to >= from);
}

export function addOutsideRange<
  T extends VideoTrackGeometry | { outside?: VideoTrackOutsideRange[] },
>(track: T, range: VideoTrackOutsideRange): T {
  return {
    ...track,
    outside: normalizeOutsideRanges([...(track.outside ?? []), range]),
  } as T;
}

export function removeOutsideFrame<T extends { outside?: VideoTrackOutsideRange[] }>(
  track: T,
  frameIndex: number,
): T {
  const frame = Math.max(0, Math.floor(frameIndex));
  const next: VideoTrackOutsideRange[] = [];

  for (const range of normalizeOutsideRanges(track.outside ?? [])) {
    if (frame < range.from || frame > range.to) {
      next.push(range);
      continue;
    }
    if (range.from < frame) next.push({ ...range, to: frame - 1 });
    if (frame < range.to) next.push({ ...range, from: frame + 1 });
  }

  return {
    ...track,
    outside: normalizeOutsideRanges(next),
  } as T;
}

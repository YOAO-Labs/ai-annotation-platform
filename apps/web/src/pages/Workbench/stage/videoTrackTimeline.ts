import type { VideoTrackOutsideRange } from "@/types";
import type { VideoContextTrackGeometry } from "./videoTrackContext";
import {
  effectiveOutsideRanges,
  isFrameInOutsideRanges,
  outsideRangesIntersect,
} from "./videoTrackOutside";

export interface VideoTrackTimelineKeyframe {
  frame: number;
  source: "manual" | "prediction";
  occluded: boolean;
}

export interface VideoTrackTimelineSegment {
  from: number;
  to: number;
  hasPrediction: boolean;
  kind?: "interpolated" | "held";
}

export interface VideoTrackTimelineOutsideSegment {
  from: number;
  to: number;
  source: VideoTrackOutsideRange["source"];
}

export interface VideoTrackTimeline {
  trackId: string;
  keyframes: VideoTrackTimelineKeyframe[];
  outside: VideoTrackTimelineOutsideSegment[];
  interpolated: VideoTrackTimelineSegment[];
}

export interface VideoTimelineDensityBin {
  index: number;
  from: number;
  to: number;
  density: number;
  tracks: { trackId: string; count: number }[];
}

type TimelineTrack = VideoContextTrackGeometry;
type TimelineKeyframe = TimelineTrack["keyframes"][number];

function sortedLatestKeyframes(track: TimelineTrack): TimelineKeyframe[] {
  const latestByFrame = new Map<number, TimelineKeyframe>();
  for (const keyframe of track.keyframes) {
    latestByFrame.set(keyframe.frame_index, keyframe);
  }
  return [...latestByFrame.values()].sort((a, b) => a.frame_index - b.frame_index);
}

export function visibleKeyframesForTimeline(track: TimelineTrack): TimelineKeyframe[] {
  const outsideRanges = effectiveOutsideRanges(track);
  return sortedLatestKeyframes(track).filter(
    (keyframe) => !isFrameInOutsideRanges(outsideRanges, keyframe.frame_index),
  );
}

export function buildSelectedTrackTimeline(
  track: TimelineTrack,
  segmentKind: "interpolated" | "held" = "interpolated",
): VideoTrackTimeline {
  const outsideRanges = effectiveOutsideRanges(track);
  const visibleKeyframes = visibleKeyframesForTimeline(track);
  const interpolated: VideoTrackTimelineSegment[] = [];

  for (let i = 1; i < visibleKeyframes.length; i++) {
    const before = visibleKeyframes[i - 1];
    const after = visibleKeyframes[i];
    if (after.frame_index <= before.frame_index + 1) continue;
    if (outsideRangesIntersect(outsideRanges, before.frame_index + 1, after.frame_index - 1))
      continue;
    interpolated.push({
      from: before.frame_index,
      to: after.frame_index,
      hasPrediction: before.source === "prediction" || after.source === "prediction",
      ...(segmentKind === "held" ? { kind: "held" as const } : {}),
    });
  }

  return {
    trackId: track.track_id,
    keyframes: visibleKeyframes.map((keyframe) => ({
      frame: keyframe.frame_index,
      source: keyframe.source === "prediction" ? "prediction" : "manual",
      occluded: Boolean(keyframe.occluded),
    })),
    outside: outsideRanges.map((range) => ({
      from: range.from,
      to: range.to,
      source: range.source ?? "manual",
    })),
    interpolated,
  };
}

export function buildGlobalTimelineDensity(
  tracks: readonly TimelineTrack[],
  maxFrame: number,
  bins = 80,
  manualBboxFrames: readonly number[] = [],
): VideoTimelineDensityBin[] {
  const safeMaxFrame = Math.max(0, Math.floor(maxFrame));
  const binCount = Math.max(1, Math.min(Math.floor(bins), safeMaxFrame + 1 || 1));
  const counts = Array.from({ length: binCount }, () => 0);
  const trackCounts: Map<string, number>[] = Array.from({ length: binCount }, () => new Map());

  const binOf = (frameIndex: number) => {
    const frame = Math.max(0, Math.min(safeMaxFrame, Math.floor(frameIndex)));
    return safeMaxFrame > 0
      ? Math.min(binCount - 1, Math.floor((frame / (safeMaxFrame + 1)) * binCount))
      : 0;
  };

  for (const track of tracks) {
    for (const keyframe of sortedLatestKeyframes(track)) {
      const index = binOf(keyframe.frame_index);
      counts[index] += 1;
      const perTrack = trackCounts[index];
      perTrack.set(track.track_id, (perTrack.get(track.track_id) ?? 0) + 1);
    }
  }

  for (const frameIndex of manualBboxFrames) {
    counts[binOf(frameIndex)] += 1;
  }

  return counts.map((density, index) => {
    const from = Math.floor((index / binCount) * (safeMaxFrame + 1));
    const to = Math.max(from, Math.floor(((index + 1) / binCount) * (safeMaxFrame + 1)) - 1);
    const trackShares = [...trackCounts[index]]
      .map(([trackId, count]) => ({ trackId, count }))
      .sort((a, b) => b.count - a.count);
    return { index, from, to, density, tracks: trackShares };
  });
}

export interface PredictionDensityBin {
  index: number;
  from: number;
  to: number;
  count: number;
}

export function buildPredictionDensity(
  predictedFrames: readonly number[],
  maxFrame: number,
  bins = 80,
): PredictionDensityBin[] {
  const safeMaxFrame = Math.max(0, Math.floor(maxFrame));
  const binCount = Math.max(1, Math.min(Math.floor(bins), safeMaxFrame + 1 || 1));
  const counts = Array.from({ length: binCount }, () => 0);
  const binOf = (frameIndex: number) => {
    const frame = Math.max(0, Math.min(safeMaxFrame, Math.floor(frameIndex)));
    return safeMaxFrame > 0
      ? Math.min(binCount - 1, Math.floor((frame / (safeMaxFrame + 1)) * binCount))
      : 0;
  };
  for (const frameIndex of predictedFrames) counts[binOf(frameIndex)] += 1;
  return counts.map((count, index) => {
    const from = Math.floor((index / binCount) * (safeMaxFrame + 1));
    const to = Math.max(from, Math.floor(((index + 1) / binCount) * (safeMaxFrame + 1)) - 1);
    return { index, from, to, count };
  });
}

export function nextVisibleKeyframeFrame(
  track: TimelineTrack,
  frameIndex: number,
  dir: -1 | 1,
): number | null {
  const frames = visibleKeyframesForTimeline(track).map((keyframe) => keyframe.frame_index);
  if (frames.length === 0) return null;
  if (dir > 0) return frames.find((frame) => frame > frameIndex) ?? null;
  for (let i = frames.length - 1; i >= 0; i--) {
    if (frames[i] < frameIndex) return frames[i];
  }
  return null;
}

export function prevKeyframeFrame(track: TimelineTrack, frameIndex: number): number | null {
  return nextVisibleKeyframeFrame(track, frameIndex, -1);
}

export function nextKeyframeFrame(track: TimelineTrack, frameIndex: number): number | null {
  return nextVisibleKeyframeFrame(track, frameIndex, 1);
}

export function firstAppearFrame(track: TimelineTrack): number | null {
  const frames = visibleKeyframesForTimeline(track);
  return frames.length > 0 ? frames[0].frame_index : null;
}

export function lastAppearFrame(track: TimelineTrack): number | null {
  const frames = visibleKeyframesForTimeline(track);
  return frames.length > 0 ? frames[frames.length - 1].frame_index : null;
}

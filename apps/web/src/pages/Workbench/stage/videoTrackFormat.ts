import type { VideoTrackKeyframe } from "@/types";
import { sortedKeyframes } from "./videoStageGeometry";
import { isFrameOutside } from "./videoTrackOutside";
import type { VideoFrameEntry, VideoTrackAnnotation } from "./videoStageTypes";

export function frameRange(frames: number[]): string {
  if (frames.length === 0) return "无帧";
  const min = Math.min(...frames);
  const max = Math.max(...frames);
  return min === max ? `F${min}` : `F${min}-F${max}`;
}

export function keyframeStatus(kf: VideoTrackKeyframe, outside: boolean): string {
  if (outside) return "消失";
  if (kf.occluded) return "遮挡";
  return "正常";
}

export function firstVisibleTrackFrame(track: VideoTrackAnnotation["geometry"]): number | null {
  if (track.keyframes.length === 0) return null;
  const visible = track.keyframes.filter((kf) => !isFrameOutside(track, kf.frame_index));
  const frames = (visible.length > 0 ? visible : track.keyframes).map((kf) => kf.frame_index);
  return Math.min(...frames);
}

export function exactFrameLabel(
  selectedTrack: VideoTrackAnnotation | null,
  frameIndex: number,
  outside: boolean,
): string {
  if (!selectedTrack) return `F${frameIndex}`;
  if (outside) return `F${frameIndex} · 消失`;
  const exact = selectedTrack.geometry.keyframes.find((kf) => kf.frame_index === frameIndex);
  if (exact?.occluded) return `F${frameIndex} · 遮挡`;
  return `F${frameIndex} · ${exact ? "关键帧" : "非关键帧"}`;
}

export function statusChipText(kf: VideoTrackKeyframe | undefined, outside = false): string {
  if (outside) return "当前消失";
  if (kf?.occluded) return "当前遮挡";
  return kf ? "关键帧" : "非关键帧";
}

export function sourceChipText(source: VideoFrameEntry["source"] | null): string {
  if (source === "prediction") return "prediction";
  if (source === "interpolated") return "interpolated";
  if (source === "legacy") return "legacy bbox";
  if (source === "manual") return "manual";
  return "无当前帧";
}

export function nextPredictionFrame(
  track: VideoTrackAnnotation["geometry"],
  frameIndex: number,
): number | null {
  const predictionFrames = sortedKeyframes(track)
    .filter((kf) => kf.source === "prediction" && !isFrameOutside(track, kf.frame_index))
    .map((kf) => kf.frame_index);
  return predictionFrames.find((frame) => frame > frameIndex) ?? predictionFrames[0] ?? null;
}

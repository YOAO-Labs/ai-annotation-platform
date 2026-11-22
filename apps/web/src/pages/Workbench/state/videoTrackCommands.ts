import type { VideoTrackGeometry, VideoTrackKeyframe, VideoTrackOutsideRange } from "@/types";
import type { Command } from "./useAnnotationHistory";

type KeyframeAttributes = Record<string, unknown> | null | undefined;
export type VideoTrackKeyframeWithAttrs = VideoTrackKeyframe & { attributes?: KeyframeAttributes };

function keyframeAttributes(kf: VideoTrackKeyframe): KeyframeAttributes {
  return (kf as VideoTrackKeyframeWithAttrs).attributes;
}

function sameAttributes(a: KeyframeAttributes, b: KeyframeAttributes): boolean {
  const left = a ?? null;
  const right = b ?? null;
  if (left === null && right === null) return true;
  return JSON.stringify(left) === JSON.stringify(right);
}

function cloneKeyframe(kf: VideoTrackKeyframe): VideoTrackKeyframe {
  const attrs = keyframeAttributes(kf);
  const cloned: VideoTrackKeyframeWithAttrs = {
    frame_index: kf.frame_index,
    bbox: { ...kf.bbox },
    source: kf.source,
    occluded: kf.occluded,
  };
  if (attrs != null) cloned.attributes = { ...attrs };
  return cloned;
}

function sameKeyframe(
  a: VideoTrackKeyframe | undefined,
  b: VideoTrackKeyframe | undefined,
): boolean {
  if (!a || !b) return a === b;
  return (
    a.frame_index === b.frame_index &&
    a.source === b.source &&
    (a.occluded ?? false) === (b.occluded ?? false) &&
    a.bbox.x === b.bbox.x &&
    a.bbox.y === b.bbox.y &&
    a.bbox.w === b.bbox.w &&
    a.bbox.h === b.bbox.h &&
    sameAttributes(keyframeAttributes(a), keyframeAttributes(b))
  );
}

function sameOutsideRanges(
  a: VideoTrackOutsideRange[] | undefined,
  b: VideoTrackOutsideRange[] | undefined,
): boolean {
  const left = a ?? [];
  const right = b ?? [];
  if (left.length !== right.length) return false;
  for (let i = 0; i < left.length; i += 1) {
    const l = left[i];
    const r = right[i];
    if (l.from !== r.from || l.to !== r.to || (l.source ?? null) !== (r.source ?? null)) {
      return false;
    }
  }
  return true;
}

export function buildVideoKeyframeCommand(
  annotationId: string,
  before: VideoTrackGeometry,
  after: VideoTrackGeometry,
): Extract<Command, { kind: "videoKeyframe" }> | null {
  if (before.type !== "video_track_bbox" || after.type !== "video_track_bbox") return null;
  if (before.track_id !== after.track_id) return null;
  if (!sameOutsideRanges(before.outside, after.outside)) return null;

  const frames = new Set<number>();
  before.keyframes.forEach((kf) => frames.add(kf.frame_index));
  after.keyframes.forEach((kf) => frames.add(kf.frame_index));

  const changed: Array<{
    frameIndex: number;
    before: VideoTrackKeyframe | null;
    after: VideoTrackKeyframe | null;
  }> = [];

  for (const frameIndex of frames) {
    const beforeKf = before.keyframes.find((kf) => kf.frame_index === frameIndex);
    const afterKf = after.keyframes.find((kf) => kf.frame_index === frameIndex);
    if (!sameKeyframe(beforeKf, afterKf)) {
      changed.push({
        frameIndex,
        before: beforeKf ? cloneKeyframe(beforeKf) : null,
        after: afterKf ? cloneKeyframe(afterKf) : null,
      });
    }
  }

  if (changed.length !== 1) return null;
  const only = changed[0];
  return {
    kind: "videoKeyframe",
    annotationId,
    frameIndex: only.frameIndex,
    before: only.before,
    after: only.after,
  };
}

export function applyVideoKeyframeToGeometry(
  geometry: VideoTrackGeometry,
  frameIndex: number,
  keyframe: VideoTrackKeyframe | null,
): VideoTrackGeometry {
  const keyframes = geometry.keyframes.filter((kf) => kf.frame_index !== frameIndex);
  if (keyframe) keyframes.push(cloneKeyframe(keyframe));
  keyframes.sort((a, b) => a.frame_index - b.frame_index);
  return { ...geometry, keyframes };
}

export type VideoPropagateDirection = "forward" | "backward";

export interface VideoPropagateOptions {
  direction: VideoPropagateDirection;
  count: number;
  overwrite: boolean;
}

export function propagateKeyframes(
  geometry: VideoTrackGeometry,
  fromFrame: number,
  fromBbox: VideoTrackKeyframe["bbox"],
  options: VideoPropagateOptions,
): VideoTrackGeometry | null {
  const count = Math.max(0, Math.floor(options.count));
  if (count <= 0) return null;

  const existing = new Map(geometry.keyframes.map((kf) => [kf.frame_index, kf]));
  const step = options.direction === "backward" ? -1 : 1;
  const targets: number[] = [];
  for (let i = 1; i <= count; i += 1) {
    const frame = fromFrame + step * i;
    if (frame < 0) break;
    targets.push(frame);
  }

  let mutated = false;
  for (const frame of targets) {
    if (!options.overwrite && existing.has(frame)) continue;
    existing.set(frame, {
      frame_index: frame,
      bbox: { ...fromBbox },
      source: "manual",
      occluded: false,
    });
    mutated = true;
  }
  if (!mutated) return null;

  const keyframes = [...existing.values()].sort((a, b) => a.frame_index - b.frame_index);
  return { ...geometry, keyframes };
}

export function buildVideoPropagateCommand(
  annotationId: string,
  before: VideoTrackGeometry,
  after: VideoTrackGeometry,
): Extract<Command, { kind: "update" }> {
  return {
    kind: "update",
    annotationId,
    before: { geometry: before },
    after: { geometry: after },
  };
}

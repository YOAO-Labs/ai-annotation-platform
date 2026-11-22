import type {
  AnnotationResponse,
  VideoTrackGeometry,
  VideoTrackMaskGeometry,
  VideoTrackPolygonGeometry,
  VideoTrackPolylineGeometry,
} from "@/types";
import {
  resolveTrackAtFrame,
  resolveVideoMaskTrackAtFrame,
  resolveVideoPolygonTrackAtFrame,
  resolveVideoPolylineTrackAtFrame,
} from "./videoStageGeometry";
import { isFrameOutside } from "./videoTrackOutside";
import { nextKeyframeFrame, prevKeyframeFrame } from "./videoTrackTimeline";

export type VideoContextTrackGeometry =
  | VideoTrackGeometry
  | VideoTrackPolygonGeometry
  | VideoTrackPolylineGeometry
  | VideoTrackMaskGeometry;

export type VideoContextTrackAnnotation = AnnotationResponse & {
  geometry: VideoContextTrackGeometry;
};

export interface VideoTrackContext {
  state: "keyframe" | "interpolated" | "held" | "outside" | "unavailable";
  source: "manual" | "prediction" | "interpolated" | "unknown";
  sourceFrame: number | null;
  occluded: boolean;
  previousFrame: number | null;
  nextFrame: number | null;
}

function recordedSource(source: unknown): VideoTrackContext["source"] {
  return source === "manual" || source === "prediction" || source === "interpolated"
    ? source
    : "unknown";
}

export function deriveVideoTrackContext(
  track: VideoContextTrackGeometry,
  frameIndex: number,
): VideoTrackContext {
  const context: VideoTrackContext = {
    state: "unavailable",
    source: "unknown",
    sourceFrame: null,
    occluded: false,
    previousFrame: prevKeyframeFrame(track, frameIndex),
    nextFrame: nextKeyframeFrame(track, frameIndex),
  };
  const exact = track.keyframes.find((keyframe) => keyframe.frame_index === frameIndex);

  if (isFrameOutside(track, frameIndex)) {
    return {
      ...context,
      state: "outside",
      source: recordedSource(exact?.source),
      sourceFrame: exact?.frame_index ?? null,
    };
  }

  if (track.type === "video_track_mask") {
    const resolved = resolveVideoMaskTrackAtFrame(track, frameIndex);
    if (!resolved) return context;
    const anchor = track.keyframes.find(
      (keyframe) => keyframe.frame_index === resolved.keyframeFrame,
    );
    return {
      ...context,
      state: resolved.keyframeFrame === frameIndex ? "keyframe" : "held",
      source: recordedSource(anchor?.source),
      sourceFrame: resolved.keyframeFrame,
      occluded: Boolean(anchor?.occluded),
    };
  }

  if (exact) {
    return {
      ...context,
      state: "keyframe",
      source: recordedSource(exact.source),
      sourceFrame: exact.frame_index,
      occluded: Boolean(exact.occluded),
    };
  }

  const resolved =
    track.type === "video_track_bbox"
      ? resolveTrackAtFrame(track, frameIndex)
      : track.type === "video_track_polygon"
        ? resolveVideoPolygonTrackAtFrame(track, frameIndex)
        : resolveVideoPolylineTrackAtFrame(track, frameIndex);

  return resolved ? { ...context, state: "interpolated" } : context;
}

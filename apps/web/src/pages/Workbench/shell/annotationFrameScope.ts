import type { Annotation } from "@/types";
import {
  resolveTrackAtFrame,
  resolveVideoMaskTrackAtFrame,
  resolveVideoPolygonTrackAtFrame,
  resolveVideoPolylineTrackAtFrame,
} from "../stage/videoStageGeometry";
import { isFrameOutside } from "../stage/videoTrackOutside";
import type { AiBox } from "../state/transforms";

export type FrameFilter = "all" | "current";

export function boxIsOnFrame(box: Annotation | AiBox, frameIndex: number): boolean {
  const geometry = box.geometry;
  if (!geometry) return true;
  if (
    geometry.type === "video_bbox" ||
    geometry.type === "video_polygon" ||
    geometry.type === "video_polyline" ||
    geometry.type === "video_rotated_bbox" ||
    geometry.type === "video_keypoint" ||
    geometry.type === "video_mask"
  ) {
    return geometry.frame_index === frameIndex;
  }
  if (geometry.type === "video_track_bbox")
    return resolveTrackAtFrame(geometry, frameIndex) !== null;
  if (geometry.type === "video_track_polygon")
    return resolveVideoPolygonTrackAtFrame(geometry, frameIndex) !== null;
  if (geometry.type === "video_track_polyline")
    return resolveVideoPolylineTrackAtFrame(geometry, frameIndex) !== null;
  if (geometry.type === "video_track_mask")
    return resolveVideoMaskTrackAtFrame(geometry, frameIndex) !== null;
  return true;
}

export function firstTrackFrame(box: Annotation | AiBox): number | null {
  const geometry = box.geometry;
  if (!geometry) return null;
  if (
    geometry.type === "video_bbox" ||
    geometry.type === "video_polygon" ||
    geometry.type === "video_polyline" ||
    geometry.type === "video_rotated_bbox" ||
    geometry.type === "video_keypoint" ||
    geometry.type === "video_mask"
  ) {
    return geometry.frame_index;
  }
  if (
    geometry.type !== "video_track_bbox" &&
    geometry.type !== "video_track_polygon" &&
    geometry.type !== "video_track_polyline" &&
    geometry.type !== "video_track_mask"
  )
    return null;
  if (geometry.keyframes.length === 0) return null;
  const visible = geometry.keyframes.filter((kf) => !isFrameOutside(geometry, kf.frame_index));
  const frames = (visible.length > 0 ? visible : geometry.keyframes).map((kf) => kf.frame_index);
  return Math.min(...frames);
}

export function filterBoxesByFrame<T extends Annotation | AiBox>(
  boxes: T[],
  frameIndex: number | undefined,
  filter: FrameFilter,
): T[] {
  if (filter !== "current" || typeof frameIndex !== "number") return boxes;
  return boxes.filter((box) => boxIsOnFrame(box, frameIndex));
}

import type {
  VideoTrackGeometry,
  VideoTrackMaskGeometry,
  VideoTrackPolygonGeometry,
  VideoTrackPolylineGeometry,
} from "@/types";
import { polygonBounds, type AiBox } from "../state/transforms";
import {
  resolveTrackAtFrame,
  resolveVideoMaskTrackAtFrame,
  resolveVideoPolygonTrackAtFrame,
  resolveVideoPolylineTrackAtFrame,
} from "./videoStageGeometry";

export function aiBoxOnFrame(box: AiBox, frameIndex: number): boolean {
  const g = box.geometry;
  if (!g) return false;
  if (
    g.type === "video_bbox" ||
    g.type === "video_polygon" ||
    g.type === "video_polyline" ||
    g.type === "video_rotated_bbox" ||
    g.type === "video_keypoint" ||
    g.type === "video_mask"
  )
    return g.frame_index === frameIndex;
  if (g.type === "video_track_bbox") {
    return resolveTrackAtFrame(g as VideoTrackGeometry, frameIndex) !== null;
  }
  if (g.type === "video_track_polygon") {
    return resolveVideoPolygonTrackAtFrame(g as VideoTrackPolygonGeometry, frameIndex) !== null;
  }
  if (g.type === "video_track_polyline") {
    return resolveVideoPolylineTrackAtFrame(g as VideoTrackPolylineGeometry, frameIndex) !== null;
  }
  if (g.type === "video_track_mask") {
    return resolveVideoMaskTrackAtFrame(g as VideoTrackMaskGeometry, frameIndex) !== null;
  }
  return true;
}

export function resolveAiBoxAtFrame(box: AiBox, frameIndex: number): AiBox | null {
  const g = box.geometry;
  if (!g) return null;
  if (
    g.type === "video_bbox" ||
    g.type === "video_polygon" ||
    g.type === "video_polyline" ||
    g.type === "video_rotated_bbox" ||
    g.type === "video_keypoint" ||
    g.type === "video_mask"
  )
    return g.frame_index === frameIndex ? box : null;
  if (g.type === "video_track_bbox") {
    const resolved = resolveTrackAtFrame(g as VideoTrackGeometry, frameIndex);
    if (!resolved) return null;
    const { x, y, w, h } = resolved.geom;
    return { ...box, x, y, w, h };
  }
  if (g.type === "video_track_polygon" || g.type === "video_track_polyline") {
    const resolved =
      g.type === "video_track_polygon"
        ? resolveVideoPolygonTrackAtFrame(g as VideoTrackPolygonGeometry, frameIndex)
        : resolveVideoPolylineTrackAtFrame(g as VideoTrackPolylineGeometry, frameIndex);
    if (!resolved) return null;
    const bounds = polygonBounds(resolved.points);
    return g.type === "video_track_polygon"
      ? { ...box, ...bounds, polygon: resolved.points, polyline: undefined }
      : { ...box, ...bounds, polyline: resolved.points, polygon: undefined };
  }
  if (g.type === "video_track_mask") {
    return resolveVideoMaskTrackAtFrame(g as VideoTrackMaskGeometry, frameIndex) ? box : null;
  }
  return box;
}

export function dedupeAiBoxesById(boxes: readonly AiBox[]): AiBox[] {
  const seen = new Set<string>();
  const out: AiBox[] = [];
  for (const b of boxes) {
    if (seen.has(b.id)) continue;
    seen.add(b.id);
    out.push(b);
  }
  return out;
}

export function collectPredictedFrames(boxes: readonly AiBox[]): number[] {
  const frames = new Set<number>();
  for (const b of dedupeAiBoxesById(boxes)) {
    const g = b.geometry;
    if (!g) continue;
    if (
      g.type === "video_bbox" ||
      g.type === "video_polygon" ||
      g.type === "video_polyline" ||
      g.type === "video_rotated_bbox" ||
      g.type === "video_keypoint" ||
      g.type === "video_mask"
    ) {
      frames.add(g.frame_index);
    } else if (
      g.type === "video_track_bbox" ||
      g.type === "video_track_polygon" ||
      g.type === "video_track_polyline" ||
      g.type === "video_track_mask"
    ) {
      for (const keyframe of g.keyframes) frames.add(keyframe.frame_index);
    }
  }
  return [...frames].sort((a, b) => a - b);
}

export function adjacentPredictedFrame(
  frames: readonly number[],
  current: number,
  dir: -1 | 1,
): number | null {
  if (dir > 0) return frames.find((f) => f > current) ?? null;
  for (let i = frames.length - 1; i >= 0; i--) {
    if (frames[i] < current) return frames[i];
  }
  return null;
}

// AI 候选 (预测) 的「帧作用域 + 去重」SSOT。
// 被三处共用, 避免各写一份口径漂移:
//  - 侧栏 AI 待审计数和列表按当前帧过滤。
//  - 画布 AI 层当前帧渲染 (video_bbox + video_track_bbox)
//  - 时间轴预测密度轨及跳到下一/上一预测帧。
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

/**
 * AI 候选在给定帧是否可见:
 *  - 视频单帧几何: 比对 frame_index
 *  - 视频轨迹: 该帧能否从 keyframes 解出形状 (resolve*AtFrame 非 null)
 *  - 其它 (图片候选): 不做帧过滤, 恒可见
 */
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

/**
 * 解出 AI 候选在给定帧的可渲染框 (归一化 x/y/w/h); 不在该帧则 null。
 *  - video_bbox: 帧号命中则原样返回
 *  - video_track_bbox: resolveTrackAtFrame 解出当前帧框, 覆盖顶层 x/y/w/h
 *    (顶层坐标本是代表关键帧、非当前帧, 直接渲染会画错位置)
 * 画布 AI 层用它把逐帧候选与检测式轨迹候选统一渲染为当前帧的框。
 */
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

/**
 * 按 id 去重 (predictionsToBoxes 的 id = `pred-{predictionId}-{shapeIndex}`)。
 * offset 分页重取期间，相邻页可能包含相同 shape；去重避免重复计数。
 */
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

/**
 * 汇「有预测的帧集合」: 视频单帧几何取 frame_index; 视频轨迹取其各关键帧帧号。
 * 升序去重, 供时间轴预测密度轨 + 跳到下一/上一预测帧。
 */
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

/** 从升序帧集合里找 current 之后 (dir>0) / 之前 (dir<0) 最近的预测帧; 无则 null。 */
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

import type { VideoTrackGeometry, VideoTrackKeyframe, VideoTrackOutsideRange } from "@/types";
import type { Command } from "./useAnnotationHistory";

// keyframe.attributes 是「逐帧覆盖」(仅承载 schema 中 mutable=true 的键)。
// 手写 @/types 的 VideoTrackKeyframe 暂未补 attributes 字段 (Wave 0 仅同步了 OpenAPI 生成类型),
// 故此处用局部扩展类型读写, 等收尾时把 attributes 回填到 src/types/index.ts。
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
  /** 复制到的后续/向前帧数 (不含 fromFrame 本身)。 */
  count: number;
  /** 是否覆盖目标帧上已有的关键帧。false 时跳过已存在关键帧。 */
  overwrite: boolean;
}

/**
 * Propagate: 把 fromFrame 处解析出的 bbox 复制到后续 / 向前 N 帧。
 * 纯几何变换, 不触后端; 返回铺帧后的新 geometry, 与 from 帧无变化时返回 null。
 *
 * - 复制源 bbox = fromFrame 处的关键帧框 (调用方负责传入已解析的 fromBbox)。
 * - overwrite=false 时跳过目标帧上已有关键帧, 不覆盖。
 * - 铺出的帧 source 固定为 "manual" (人工传播), occluded=false。
 */
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

/**
 * Propagate 合成单条 undo command。沿用既有 `update` 命令 (记录 before/after geometry),
 * 无需新增 Command kind; undo/redo 直接整体回放 geometry。
 */
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

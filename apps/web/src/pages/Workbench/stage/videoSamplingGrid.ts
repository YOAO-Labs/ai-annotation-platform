// 视频「软网格导航」核心纯逻辑（v0.10.29 Phase 1）。
// 网格 = 绝对网格，锚定 0：step=N → {0, N, 2N, ...}。frame_index 永远是源视频帧号；
// 采样只是视图层。step=1 时所有源帧都是网格点，导航行为退化为现状（向后兼容）。
// 规格见 docs/plans/archive/2026-05-21-v0.10.29-video-frame-sampling.md §1。

import type { VideoSamplingConfig } from "@/types";

/** 由采样配置 + 源 fps 派生网格步长（源帧空间）。缺省 / mode=none → 1。 */
export function deriveSamplingStep(
  sampling: VideoSamplingConfig | null | undefined,
  sourceFps: number,
): number {
  if (!sampling) return 1;
  if (sampling.mode === "step") {
    const step = sampling.frame_step;
    if (step != null && Number.isFinite(step) && step >= 1) {
      return Math.max(1, Math.round(step));
    }
    return 1;
  }
  if (sampling.mode === "fps") {
    const target = sampling.target_fps;
    if (
      target != null &&
      Number.isFinite(target) &&
      target > 0 &&
      Number.isFinite(sourceFps) &&
      sourceFps > 0
    ) {
      const ratio = sourceFps / target;
      const floor = Math.floor(ratio);
      const rounded = ratio - floor === 0.5 ? floor + (floor % 2) : Math.round(ratio);
      return Math.max(1, rounded);
    }
    return 1;
  }
  return 1;
}

function clamp(value: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, value));
}

/**
 * 严格大于 f 的最近网格点。尾部不足一格（下一网格点已越过 maxFrame）时停在当前位置，
 * **不落到 off-grid 的 maxFrame** —— 否则 step=30/maxFrame=299 时会跳到 299 再卡死
 * （gridNext(299)=299 反复同值）。最末几帧（末网格点~maxFrame）靠 Shift 逃生口微调到达。
 */
export function gridNext(f: number, step: number, maxFrame: number): number {
  const N = Math.max(1, step);
  const next = (Math.floor(f / N) + 1) * N;
  return next <= maxFrame ? next : f;
}

/** 严格小于 f 的最近网格点；到头部钳到 0。 */
export function gridPrev(f: number, step: number, maxFrame: number): number {
  const N = Math.max(1, step);
  return clamp(Math.floor((f - 1) / N) * N, 0, maxFrame);
}

/** 就近网格点（用于暂停吸附）。 */
export function snapToGrid(f: number, step: number, maxFrame: number): number {
  const N = Math.max(1, step);
  return clamp(Math.round(f / N) * N, 0, maxFrame);
}

/** 逃生口：±1 源帧。 */
export function microStep(f: number, dir: -1 | 1, maxFrame: number): number {
  return clamp(f + dir, 0, maxFrame);
}

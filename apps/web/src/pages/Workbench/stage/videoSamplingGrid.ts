import type { VideoSamplingConfig } from "@/types";

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

export function gridNext(f: number, step: number, maxFrame: number): number {
  const N = Math.max(1, step);
  const next = (Math.floor(f / N) + 1) * N;
  return next <= maxFrame ? next : f;
}

export function gridPrev(f: number, step: number, maxFrame: number): number {
  const N = Math.max(1, step);
  return clamp(Math.floor((f - 1) / N) * N, 0, maxFrame);
}

export function snapToGrid(f: number, step: number, maxFrame: number): number {
  const N = Math.max(1, step);
  return clamp(Math.round(f / N) * N, 0, maxFrame);
}

export function microStep(f: number, dir: -1 | 1, maxFrame: number): number {
  return clamp(f + dir, 0, maxFrame);
}

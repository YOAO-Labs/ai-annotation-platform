import type { TaskVideoFrameTimetableResponse, VideoMetadata } from "@/types";

export interface FrameTimebase {
  fps: number;
  frameCount: number;
  source: "ffprobe" | "estimated";
  ptsMs: number[] | null;
  durationMs?: number | null;
}

const DEFAULT_FPS = 30;

function clampFrame(frameIndex: number, frameCount: number) {
  const maxFrame = Math.max(0, frameCount - 1);
  if (!Number.isFinite(frameIndex)) return 0;
  return Math.max(0, Math.min(maxFrame, Math.round(frameIndex)));
}

function findClosestFrame(ptsMs: number[], targetMs: number) {
  let lo = 0;
  let hi = ptsMs.length - 1;
  while (lo <= hi) {
    const mid = Math.floor((lo + hi) / 2);
    if (ptsMs[mid] < targetMs) lo = mid + 1;
    else hi = mid - 1;
  }
  if (lo <= 0) return 0;
  if (lo >= ptsMs.length) return ptsMs.length - 1;
  const prev = lo - 1;
  return Math.abs(ptsMs[prev] - targetMs) <= Math.abs(ptsMs[lo] - targetMs) ? prev : lo;
}

function roundTiesToEven(value: number) {
  const lower = Math.floor(value);
  const fraction = value - lower;
  if (fraction < 0.5) return lower;
  if (fraction > 0.5) return lower + 1;
  return lower % 2 === 0 ? lower : lower + 1;
}

function interpolateTimetable(
  frameCount: number,
  fps: number,
  frames: TaskVideoFrameTimetableResponse["frames"],
): number[] | null {
  const byFrame = new Map<number, number>();
  for (const frame of frames) {
    if (
      Number.isInteger(frame.frame_index) &&
      frame.frame_index >= 0 &&
      frame.frame_index < frameCount &&
      Number.isFinite(frame.pts_ms)
    ) {
      byFrame.set(frame.frame_index, frame.pts_ms);
    }
  }
  const anchors = [...byFrame.entries()].sort(([left], [right]) => left - right);
  if (anchors.length === 0) return null;

  const ptsMs = new Array<number>(frameCount);
  const [firstFrame, firstPts] = anchors[0];
  for (let frame = 0; frame < firstFrame; frame++) {
    ptsMs[frame] = roundTiesToEven(firstPts + ((frame - firstFrame) / fps) * 1000);
  }

  for (let index = 0; index < anchors.length; index++) {
    const [frame, pts] = anchors[index];
    ptsMs[frame] = pts;
    const next = anchors[index + 1];
    if (!next) {
      for (let trailing = frame + 1; trailing < frameCount; trailing++) {
        ptsMs[trailing] = roundTiesToEven(pts + ((trailing - frame) / fps) * 1000);
      }
      continue;
    }

    const [nextFrame, nextPts] = next;
    for (let missing = frame + 1; missing < nextFrame; missing++) {
      const ratio = (missing - frame) / (nextFrame - frame);
      ptsMs[missing] = roundTiesToEven(pts + ratio * (nextPts - pts));
    }
  }
  return ptsMs;
}

export function buildFrameTimebase(
  metadata: VideoMetadata | undefined,
  timetable?: TaskVideoFrameTimetableResponse,
): FrameTimebase {
  const fps =
    metadata?.fps && metadata.fps > 0
      ? metadata.fps
      : timetable?.fps && timetable.fps > 0
        ? timetable.fps
        : DEFAULT_FPS;
  const lastTimetableFrame = timetable?.frames[timetable.frames.length - 1];
  const timetableFrameCount = lastTimetableFrame ? lastTimetableFrame.frame_index + 1 : undefined;
  const frameCount = Math.max(
    1,
    metadata?.frame_count ?? timetable?.frame_count ?? timetableFrameCount ?? 1,
  );
  const durationMs =
    typeof metadata?.duration_ms === "number" &&
    Number.isFinite(metadata.duration_ms) &&
    metadata.duration_ms > 0
      ? metadata.duration_ms
      : null;
  if (timetable?.source === "ffprobe" && timetable.frames.length > 0) {
    const ptsMs = interpolateTimetable(frameCount, fps, timetable.frames);
    if (ptsMs) return { fps, frameCount, source: "ffprobe", ptsMs, durationMs };
  }
  return { fps, frameCount, source: "estimated", ptsMs: null, durationMs };
}

export function frameTimebaseDuration(timebase: FrameTimebase): number {
  if (timebase.durationMs && Number.isFinite(timebase.durationMs)) {
    return timebase.durationMs / 1000;
  }
  return timebase.frameCount / timebase.fps;
}

export function frameToTime(frameIndex: number, timebase: FrameTimebase): number {
  const frame = clampFrame(frameIndex, timebase.frameCount);
  const pts = timebase.ptsMs?.[frame];
  if (pts !== undefined && Number.isFinite(pts)) return pts / 1000;
  return frame / timebase.fps;
}

export function frameToSeekTime(frameIndex: number, timebase: FrameTimebase): number {
  const frame = clampFrame(frameIndex, timebase.frameCount);
  const pts = timebase.ptsMs?.[frame];
  if (pts !== undefined && Number.isFinite(pts)) {
    const nextPts = timebase.ptsMs?.[frame + 1];
    if (nextPts !== undefined && Number.isFinite(nextPts) && nextPts > pts) {
      return (pts + (nextPts - pts) * 0.25) / 1000;
    }
    const prevPts = timebase.ptsMs?.[frame - 1];
    if (prevPts !== undefined && Number.isFinite(prevPts) && pts > prevPts) {
      return (pts + (pts - prevPts) * 0.25) / 1000;
    }
    return pts / 1000;
  }
  return (frame + 0.25) / timebase.fps;
}

export function timeToFrame(mediaTime: number, timebase: FrameTimebase): number {
  if (!Number.isFinite(mediaTime)) return 0;
  const targetMs = Math.max(0, mediaTime * 1000);
  if (timebase.ptsMs?.length) {
    return clampFrame(findClosestFrame(timebase.ptsMs, targetMs), timebase.frameCount);
  }
  return clampFrame(mediaTime * timebase.fps, timebase.frameCount);
}

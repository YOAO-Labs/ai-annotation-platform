export const SCENE_TIMELINE_CELL_WIDTH = 40;
export const SCENE_TIMELINE_ZOOM_LEVELS = [24, 40, 64, 80] as const;
export const SCENE_TIMELINE_OVERSCAN = 12;
export const SCENE_TIMELINE_INITIAL_WINDOW = 80;
export const SCENE_TIMELINE_MAX_WINDOW = 200;

interface TimelineRangeInput {
  sceneStart: number;
  sceneEnd: number;
  firstVirtualIndex: number;
  lastVirtualIndex: number;
}

export interface TimelineFrameRange {
  startFrame: number;
  endFrame: number;
}

export function timelineQueryRange({
  sceneStart,
  sceneEnd,
  firstVirtualIndex,
  lastVirtualIndex,
}: TimelineRangeInput): TimelineFrameRange {
  const requestedStart = Math.max(
    sceneStart,
    sceneStart + firstVirtualIndex - SCENE_TIMELINE_OVERSCAN,
  );
  const requestedEnd = Math.min(sceneEnd, sceneStart + lastVirtualIndex + SCENE_TIMELINE_OVERSCAN);
  const endFrame = Math.min(requestedEnd, requestedStart + SCENE_TIMELINE_MAX_WINDOW - 1);
  return { startFrame: requestedStart, endFrame };
}

export function timelineInitialRange(): TimelineFrameRange {
  return { startFrame: 0, endFrame: SCENE_TIMELINE_INITIAL_WINDOW - 1 };
}

export function densityRatio(count: number, maxCount: number): number {
  if (count <= 0 || maxCount <= 0) return 0;
  return Math.min(1, count / maxCount);
}

export function timelineZoomOffset({
  scrollLeft,
  viewportWidth,
  oldWidth,
  newWidth,
  currentIndex,
  followCurrent,
}: {
  scrollLeft: number;
  viewportWidth: number;
  oldWidth: number;
  newWidth: number;
  currentIndex: number;
  followCurrent: boolean;
}): number {
  const anchor = followCurrent ? currentIndex + 0.5 : (scrollLeft + viewportWidth / 2) / oldWidth;
  return Math.max(0, scrollLeft + anchor * (newWidth - oldWidth));
}

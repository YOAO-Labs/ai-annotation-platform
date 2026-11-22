import type { VideoTool } from "../state/useWorkbenchState";
import type { TimelineWindow } from "./timelineCoords";

export interface VideoIssueViewport {
  center_x: number;
  center_y: number;
  zoom: number;
}

export interface VideoIssueView {
  viewport?: VideoIssueViewport | null;
  timeline_window?: TimelineWindow | null;
}

export interface VideoIssueViewCapture {
  taskId: string;
  frameIndex: number;
  viewport: VideoIssueViewport;
  timeline_window: TimelineWindow;
}

export interface VideoIssueViewRestoreResult {
  status: "restored" | "cancelled" | "unavailable";
  clamped: boolean;
}

export interface VideoIssueRestoreLease {
  restore: (
    view: VideoIssueView,
    selectedId: string | null,
  ) => Promise<VideoIssueViewRestoreResult>;
  release: () => void;
}

export interface VideoTimelineWindowControls {
  capture: () => TimelineWindow | null;
  restore: (window: TimelineWindow) => { window: TimelineWindow; clamped: boolean } | null;
}

export interface VideoFrameSeekResult {
  status: "ready" | "cancelled" | "timeout" | "unavailable";
  frameIndex: number;
  source: "webcodecs" | "video-bitmap" | "video-element" | null;
}

export interface VideoFramePresentation {
  requestId: number;
  frameIndex: number;
  source: NonNullable<VideoFrameSeekResult["source"]>;
  image: ImageBitmap | HTMLVideoElement;
  isCurrent: () => boolean;
}

export type VideoDrawingDraft = {
  kind: "points" | "keypoint" | "box";
  tool: VideoTool;
  frameIndex: number;
  continuingTrack?: true;
};

export interface VideoStageControls {
  isSourceReady?: () => boolean;
  subscribeIssueNavigationInterrupt?: (listener: () => void) => () => void;
  captureIssueView?: () => VideoIssueViewCapture | null;
  waitForIssueViewReady?: (signal: AbortSignal) => Promise<boolean>;
  beginIssueRestore?: (
    isRelevant: () => boolean,
    signal?: AbortSignal,
  ) => VideoIssueRestoreLease | null;
  getDrawingDraft?: () => VideoDrawingDraft | null;
  discardDrawingDraft?: () => void;
  togglePlayback: () => void;
  jogPlayback: (dir: -1 | 1) => void;
  pausePlayback: (options?: { snapToGrid?: boolean }) => void;
  seekByFrames: (delta: number, options?: { recordHistory?: boolean }) => void;
  seekGrid: (dir: -1 | 1, options?: { recordHistory?: boolean }) => void;
  microStep: (dir: -1 | 1, options?: { recordHistory?: boolean }) => void;
  seekToKeyframe: (dir: -1 | 1, options?: { recordHistory?: boolean }) => void;
  seekToFrame: (frameIndex: number, options?: { recordHistory?: boolean }) => void;
  seekToFrameReady: (
    frameIndex: number,
    options?: { recordHistory?: boolean },
  ) => Promise<VideoFrameSeekResult>;
  toggleBookmark: () => void;
  jumpHistory: (dir: -1 | 1) => void;
  clearLoopRegion: () => void;
  toggleSelectedTrackOutside: () => void;
  toggleSelectedTrackOccluded: () => void;
  toggleSelectedTrackHidden: () => void;
  toggleSelectedTrackLocked: () => void;
  propagateSelectedTrack: () => void;
  deleteSelectedTrackKeyframe: () => boolean;
  normToClient: (pt: { x: number; y: number }) => { left: number; top: number } | null;
  cycleInCategory: (dir: -1 | 1) => void;
  stepCategory: (dir: -1 | 1) => void;
  focusObject: (id: string) => void;
  focusRegion: (bbox: { x0: number; y0: number; x1: number; y1: number }) => void;
  captureCurrentFrameJpeg: (quality?: number) => Promise<Blob | null>;
}

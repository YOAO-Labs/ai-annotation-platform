type VideoWorkbenchDiagnosticsStore = {
  activeTaskId?: string;
  byTask?: Record<string, VideoWorkbenchTaskDiagnostics>;
};

export type VideoWorkbenchDiagnosticsSnapshot = Record<string, unknown>;

export interface VideoPreciseFrameDiagnosticsSnapshot {
  enabled: boolean;
  supported: boolean;
  state: string;
  source: "webcodecs" | "native-bitmap" | "video";
  frameIndex: number;
  chunkId: number | null;
  gopStartDecodeIndex: number | null;
  targetTimestampUs: number | null;
  codec: string | null;
  fallbackReason: string | null;
  lastDemuxMs: number | null;
  lastQueueMs: number | null;
  lastCodecMs: number | null;
  lastDecodeMs: number | null;
  lastBitmapMs: number | null;
  lastDecodeMode: "bitmap-cache" | "supplemental-cache" | "session" | null;
  lastPaintMs: number | null;
  lastVisibleMs: number | null;
  paintedFrameIndex: number | null;
  cache: {
    bitmapBytes: number;
    bitmapBudgetBytes: number;
    chunkBytes: number;
    chunkBudgetBytes: number;
  };
  counters: {
    activeDecoders: number;
    liveVideoFrames: number;
    sessionCreates: number;
    sessionResets: number;
    encodedChunksSubmitted: number;
    staleResults: number;
    prefetchRequests: number;
    prefetchHits: number;
  };
}

export interface VideoWorkbenchTaskDiagnostics {
  taskId?: string;
  route?: string;
  updatedAt?: string;
  frameClock?: unknown;
  framePreview?: unknown;
  bitmap?: unknown;
  viewport?: unknown;
  minimap?: unknown;
  preciseFrame?: VideoPreciseFrameDiagnosticsSnapshot;
}

const MAX_DIAGNOSTIC_JSON_CHARS = 6000;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function getWindowDiagnostics() {
  if (typeof window === "undefined") return null;
  return window as unknown as {
    __videoWorkbenchDiagnostics?: VideoWorkbenchDiagnosticsStore;
    __videoFrameClockDiagnostics?: Record<string, unknown>;
  };
}

function stableStringify(value: unknown, maxChars = MAX_DIAGNOSTIC_JSON_CHARS) {
  const json = JSON.stringify(value, null, 2);
  if (json.length <= maxChars) return json;
  return `${json.slice(0, maxChars)}\n... truncated`;
}

export function getVideoWorkbenchDiagnosticsSnapshot(): VideoWorkbenchDiagnosticsSnapshot | null {
  const target = getWindowDiagnostics();
  if (!target) return null;

  const store = target.__videoWorkbenchDiagnostics;
  const activeTaskId = store?.activeTaskId;
  const activeSnapshot = activeTaskId ? store?.byTask?.[activeTaskId] : null;
  if (activeSnapshot && typeof activeSnapshot === "object") {
    const route = (activeSnapshot as { route?: unknown }).route;
    if (
      typeof route === "string" &&
      route !== `${window.location.pathname}${window.location.search}`
    ) {
      return null;
    }
    return activeSnapshot as VideoWorkbenchDiagnosticsSnapshot;
  }

  if (
    window.location.pathname.includes("annotate") &&
    target.__videoFrameClockDiagnostics &&
    Object.keys(target.__videoFrameClockDiagnostics).length > 0
  ) {
    return {
      updatedAt: new Date().toISOString(),
      route: `${window.location.pathname}${window.location.search}`,
      frameClock: target.__videoFrameClockDiagnostics,
    };
  }

  return null;
}

export function appendVideoWorkbenchDiagnostics(
  description: string,
  snapshot: VideoWorkbenchDiagnosticsSnapshot | null,
) {
  if (!snapshot) return description;
  return `${description}\n\n---\n\n### Video Workbench Diagnostics\n\n\`\`\`json\n${stableStringify(snapshot)}\n\`\`\``;
}

export function videoWorkbenchDiagnosticsConsoleEntry(
  snapshot: VideoWorkbenchDiagnosticsSnapshot | null,
) {
  if (!snapshot) return null;
  return {
    msg: "[video-workbench-diagnostics]",
    stack: stableStringify(snapshot),
  };
}

export function taskIdFromVideoWorkbenchDiagnostics(
  snapshot: VideoWorkbenchDiagnosticsSnapshot | null,
) {
  const taskId = typeof snapshot?.taskId === "string" ? snapshot.taskId : null;
  return taskId && UUID_RE.test(taskId) ? taskId : undefined;
}

const PRECISE_PUBLISH_MIN_INTERVAL_MS = 200;
let preciseLastPublishAt = 0;
let preciseTrailingTimer: ReturnType<typeof setTimeout> | null = null;
let preciseLastState: string | null = null;
let preciseLastFallback: string | null = null;
let preciseLastPaintedFrame: number | null = null;

function ensureDiagnosticsStore(): VideoWorkbenchDiagnosticsStore | null {
  if (typeof window === "undefined") return null;
  const w = window as unknown as { __videoWorkbenchDiagnostics?: VideoWorkbenchDiagnosticsStore };
  if (!w.__videoWorkbenchDiagnostics) {
    w.__videoWorkbenchDiagnostics = { byTask: {} };
  }
  if (!w.__videoWorkbenchDiagnostics.byTask) {
    w.__videoWorkbenchDiagnostics.byTask = {};
  }
  return w.__videoWorkbenchDiagnostics;
}

function clearPreciseTrailingTimer() {
  if (preciseTrailingTimer !== null) {
    clearTimeout(preciseTrailingTimer);
    preciseTrailingTimer = null;
  }
}

function writePreciseSnapshot(
  taskId: string,
  snapshot: VideoPreciseFrameDiagnosticsSnapshot,
  route: string,
) {
  const store = ensureDiagnosticsStore();
  if (!store || !store.byTask) return;
  const existing = store.byTask[taskId] ?? {};
  store.byTask[taskId] = {
    ...existing,
    taskId,
    route,
    updatedAt: new Date().toISOString(),
    preciseFrame: snapshot,
  };
  store.activeTaskId = taskId;
}

export function publishVideoPreciseFrameDiagnostics(
  taskId: string,
  snapshot: VideoPreciseFrameDiagnosticsSnapshot,
  route: string,
): void {
  const urgent =
    snapshot.state !== preciseLastState ||
    snapshot.fallbackReason !== preciseLastFallback ||
    snapshot.paintedFrameIndex !== preciseLastPaintedFrame;
  preciseLastState = snapshot.state;
  preciseLastFallback = snapshot.fallbackReason;
  preciseLastPaintedFrame = snapshot.paintedFrameIndex;

  clearPreciseTrailingTimer();
  const now = Date.now();
  if (urgent || now - preciseLastPublishAt >= PRECISE_PUBLISH_MIN_INTERVAL_MS) {
    writePreciseSnapshot(taskId, snapshot, route);
    preciseLastPublishAt = now;
    return;
  }
  preciseTrailingTimer = setTimeout(
    () => {
      preciseTrailingTimer = null;
      preciseLastPublishAt = Date.now();
      writePreciseSnapshot(taskId, snapshot, route);
    },
    PRECISE_PUBLISH_MIN_INTERVAL_MS - (now - preciseLastPublishAt),
  );
}

export function setActiveVideoWorkbenchTask(taskId: string | null): void {
  const store = ensureDiagnosticsStore();
  if (!store) return;
  store.activeTaskId = taskId ?? undefined;
  preciseLastPublishAt = 0;
  preciseLastState = null;
  preciseLastFallback = null;
  preciseLastPaintedFrame = null;
  clearPreciseTrailingTimer();
}

export function clearVideoPreciseFrameDiagnostics(taskId: string): void {
  clearPreciseTrailingTimer();
  const store = ensureDiagnosticsStore();
  if (!store || !store.byTask) return;
  delete store.byTask[taskId];
  if (store.activeTaskId === taskId) {
    store.activeTaskId = undefined;
  }
}

type VideoWorkbenchDiagnosticsStore = {
  activeTaskId?: string;
  byTask?: Record<string, VideoWorkbenchTaskDiagnostics>;
};

export type VideoWorkbenchDiagnosticsSnapshot = Record<string, unknown>;

/**
 * 精确帧 pipeline 的全局诊断快照。只允许枚举型 state / reason、数值型 id / 计数 /
 * 耗时与预算;禁止 signed URL、chunk bytes、base64 description、文件名或 annotation 内容。
 * typed 边界本身就是数据最小化约束——producer 不接受任意对象 spread。
 */
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

/** 单个视频 task 在全局诊断 store 里的聚合快照;preciseFrame 是精确帧诊断字段。 */
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

// === Precise-frame diagnostics producer ===
//
// 写入 window.__videoWorkbenchDiagnostics,供 BugReportDrawer 附带与排障。producer 只接受 typed
// snapshot,不 spread 任意对象;节流上限 5 Hz,state / fallback 转换立即写入,trailing 保证最后
// 一次状态一定落盘。写入 window 不触发 React 重渲染。

const PRECISE_PUBLISH_MIN_INTERVAL_MS = 200; // 5 Hz 上限
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

/**
 * 以 5 Hz 上限写入当前 task 的精确帧诊断快照。state / fallbackReason 变化视为 urgent,立即写入;
 * 其余高频字段由 trailing 定时器保证最后一次值落盘。仅浏览器端生效,SSR / 无 taskId 时 no-op。
 */
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

/** 注册当前活跃 task;切换 task 时重置节流,确保新 task 首个快照立即写入。 */
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

/** stage unmount:删除该 task 的快照,取消未发出的 trailing 写入。 */
export function clearVideoPreciseFrameDiagnostics(taskId: string): void {
  clearPreciseTrailingTimer();
  const store = ensureDiagnosticsStore();
  if (!store || !store.byTask) return;
  delete store.byTask[taskId];
  if (store.activeTaskId === taskId) {
    store.activeTaskId = undefined;
  }
}

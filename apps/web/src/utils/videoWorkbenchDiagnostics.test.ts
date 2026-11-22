import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  appendVideoWorkbenchDiagnostics,
  clearVideoPreciseFrameDiagnostics,
  getVideoWorkbenchDiagnosticsSnapshot,
  publishVideoPreciseFrameDiagnostics,
  setActiveVideoWorkbenchTask,
  taskIdFromVideoWorkbenchDiagnostics,
  videoWorkbenchDiagnosticsConsoleEntry,
  type VideoPreciseFrameDiagnosticsSnapshot,
} from "./videoWorkbenchDiagnostics";

const TASK_A = "11111111-1111-4111-8111-111111111111";
const TASK_B = "22222222-2222-4222-8222-222222222222";

interface StoredTaskSnapshot {
  taskId?: string;
  route?: string;
  updatedAt?: string;
  preciseFrame?: VideoPreciseFrameDiagnosticsSnapshot;
}

function readStore() {
  return (
    window as unknown as {
      __videoWorkbenchDiagnostics?: {
        activeTaskId?: string;
        byTask?: Record<string, StoredTaskSnapshot>;
      };
    }
  ).__videoWorkbenchDiagnostics;
}

function baseSnapshot(
  overrides: Partial<VideoPreciseFrameDiagnosticsSnapshot> = {},
): VideoPreciseFrameDiagnosticsSnapshot {
  return {
    enabled: true,
    supported: true,
    state: "ready",
    source: "webcodecs",
    frameIndex: 0,
    chunkId: 0,
    gopStartDecodeIndex: 0,
    targetTimestampUs: 0,
    codec: "avc1.42E01E",
    fallbackReason: null,
    lastDemuxMs: 2,
    lastQueueMs: 1,
    lastCodecMs: 3,
    lastDecodeMs: 5,
    lastBitmapMs: 1,
    lastDecodeMode: "session",
    lastPaintMs: 2,
    lastVisibleMs: 7,
    paintedFrameIndex: 0,
    cache: { bitmapBytes: 0, bitmapBudgetBytes: 100, chunkBytes: 0, chunkBudgetBytes: 100 },
    counters: {
      activeDecoders: 0,
      liveVideoFrames: 0,
      sessionCreates: 0,
      sessionResets: 0,
      encodedChunksSubmitted: 0,
      staleResults: 0,
      prefetchRequests: 0,
      prefetchHits: 0,
    },
    ...overrides,
  };
}

function resetStore() {
  delete (window as unknown as { __videoWorkbenchDiagnostics?: unknown })
    .__videoWorkbenchDiagnostics;
  setActiveVideoWorkbenchTask(null);
}

describe("videoWorkbenchDiagnostics precise-frame producer", () => {
  beforeEach(() => {
    resetStore();
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("publishes a typed snapshot under byTask[taskId] and marks it active", () => {
    publishVideoPreciseFrameDiagnostics(TASK_A, baseSnapshot({ frameIndex: 7 }), "/annotate/a");
    const store = readStore();
    expect(store?.activeTaskId).toBe(TASK_A);
    expect(store?.byTask?.[TASK_A]?.preciseFrame?.frameIndex).toBe(7);
    expect(store?.byTask?.[TASK_A]?.preciseFrame?.codec).toBe("avc1.42E01E");
    expect(store?.byTask?.[TASK_A]?.route).toBe("/annotate/a");
    expect(store?.byTask?.[TASK_A]?.updatedAt).toBeTruthy();
  });

  it("throttles non-urgent updates to 5 Hz but flushes the trailing value", () => {
    publishVideoPreciseFrameDiagnostics(TASK_A, baseSnapshot({ frameIndex: 1 }), "/annotate/a");
    vi.advanceTimersByTime(50);
    publishVideoPreciseFrameDiagnostics(TASK_A, baseSnapshot({ frameIndex: 2 }), "/annotate/a");
    expect(readStore()?.byTask?.[TASK_A]?.preciseFrame?.frameIndex).toBe(1);
    vi.advanceTimersByTime(200);
    expect(readStore()?.byTask?.[TASK_A]?.preciseFrame?.frameIndex).toBe(2);
  });

  it("writes immediately when state transitions (urgent)", () => {
    publishVideoPreciseFrameDiagnostics(TASK_A, baseSnapshot({ state: "ready" }), "/annotate/a");
    vi.advanceTimersByTime(50);
    publishVideoPreciseFrameDiagnostics(
      TASK_A,
      baseSnapshot({ state: "fallback", fallbackReason: "decode_failed" }),
      "/annotate/a",
    );
    expect(readStore()?.byTask?.[TASK_A]?.preciseFrame?.state).toBe("fallback");
    expect(readStore()?.byTask?.[TASK_A]?.preciseFrame?.fallbackReason).toBe("decode_failed");
  });

  it("writes immediately when fallback reason changes even if state stays fallback", () => {
    publishVideoPreciseFrameDiagnostics(
      TASK_A,
      baseSnapshot({ state: "fallback", fallbackReason: "chunk_pending" }),
      "/annotate/a",
    );
    vi.advanceTimersByTime(50);
    publishVideoPreciseFrameDiagnostics(
      TASK_A,
      baseSnapshot({ state: "fallback", fallbackReason: "codec_unsupported" }),
      "/annotate/a",
    );
    expect(readStore()?.byTask?.[TASK_A]?.preciseFrame?.fallbackReason).toBe("codec_unsupported");
  });

  it("resets throttle on task switch so the new task's first snapshot is immediate", () => {
    publishVideoPreciseFrameDiagnostics(TASK_A, baseSnapshot({ frameIndex: 1 }), "/annotate/a");
    vi.advanceTimersByTime(50);
    setActiveVideoWorkbenchTask(TASK_B);
    publishVideoPreciseFrameDiagnostics(TASK_B, baseSnapshot({ frameIndex: 9 }), "/annotate/b");
    expect(readStore()?.activeTaskId).toBe(TASK_B);
    expect(readStore()?.byTask?.[TASK_B]?.preciseFrame?.frameIndex).toBe(9);
  });

  it("clear removes the task snapshot and cancels the trailing write", () => {
    publishVideoPreciseFrameDiagnostics(TASK_A, baseSnapshot({ frameIndex: 1 }), "/annotate/a");
    vi.advanceTimersByTime(50);
    publishVideoPreciseFrameDiagnostics(TASK_A, baseSnapshot({ frameIndex: 2 }), "/annotate/a");
    clearVideoPreciseFrameDiagnostics(TASK_A);
    vi.advanceTimersByTime(300);
    expect(readStore()?.byTask?.[TASK_A]).toBeUndefined();
    expect(readStore()?.activeTaskId).toBeUndefined();
  });

  it("typed snapshot never carries sensitive media fields", () => {
    const snap = baseSnapshot();
    const keys = Object.keys(snap).sort();
    expect(keys).toEqual(
      [
        "cache",
        "chunkId",
        "codec",
        "counters",
        "enabled",
        "fallbackReason",
        "frameIndex",
        "gopStartDecodeIndex",
        "lastDemuxMs",
        "lastQueueMs",
        "lastCodecMs",
        "lastDecodeMs",
        "lastBitmapMs",
        "lastDecodeMode",
        "lastPaintMs",
        "lastVisibleMs",
        "paintedFrameIndex",
        "source",
        "state",
        "supported",
        "targetTimestampUs",
      ].sort(),
    );
    expect(JSON.stringify(snap)).not.toContain("url");
    expect(JSON.stringify(snap)).not.toContain("description");
    expect(JSON.stringify(snap)).not.toContain("bytes");
  });
});

describe("videoWorkbenchDiagnostics reader", () => {
  beforeEach(() => {
    resetStore();
  });

  it("returns the published snapshot when route matches the current location", () => {
    const route = window.location.pathname + window.location.search;
    publishVideoPreciseFrameDiagnostics(TASK_A, baseSnapshot({ codec: "avc1.42E01E" }), route);
    const snap = getVideoWorkbenchDiagnosticsSnapshot();
    expect(snap).not.toBeNull();
    expect((snap as StoredTaskSnapshot).preciseFrame?.codec).toBe("avc1.42E01E");
    expect(taskIdFromVideoWorkbenchDiagnostics(snap as never)).toBe(TASK_A);
  });

  it("returns null when the stored route does not match the current location", () => {
    publishVideoPreciseFrameDiagnostics(TASK_A, baseSnapshot(), "/annotate/a-very-different-route");
    expect(getVideoWorkbenchDiagnosticsSnapshot()).toBeNull();
  });

  it("returns null when no snapshot has been published", () => {
    expect(getVideoWorkbenchDiagnosticsSnapshot()).toBeNull();
  });

  it("strips non-UUID task ids before they can become a backend task_id", () => {
    expect(taskIdFromVideoWorkbenchDiagnostics({ taskId: "not-a-uuid" })).toBeUndefined();
    expect(taskIdFromVideoWorkbenchDiagnostics({ taskId: TASK_A })).toBe(TASK_A);
    expect(taskIdFromVideoWorkbenchDiagnostics(null)).toBeUndefined();
  });

  it("truncates oversized snapshots in the appended description", () => {
    const big = { ...baseSnapshot(), note: "x".repeat(8000) };
    const out = appendVideoWorkbenchDiagnostics("desc", big as unknown as Record<string, unknown>);
    expect(out).toContain("truncated");
    expect(out).not.toContain("x".repeat(8000));
  });

  it("leaves the description untouched without a snapshot", () => {
    expect(appendVideoWorkbenchDiagnostics("desc", null)).toBe("desc");
    expect(videoWorkbenchDiagnosticsConsoleEntry(null)).toBeNull();
  });
});

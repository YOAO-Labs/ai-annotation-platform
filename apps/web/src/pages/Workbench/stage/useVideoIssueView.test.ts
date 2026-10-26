import { useCallback, useRef, useState } from "react";
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Viewport } from "./shared/useViewportTransform";
import { useVideoIssueView, VIDEO_ISSUE_VIEW_READY_TIMEOUT_MS } from "./useVideoIssueView";
import { useVideoTimelineWindow } from "./useVideoTimelineWindow";
import type {
  VideoIssueRestoreLease,
  VideoIssueView,
  VideoIssueViewRestoreResult,
  VideoTimelineWindowControls,
} from "./videoStageControls";

const saved: VideoIssueView = {
  viewport: { center_x: 0.3, center_y: 0.6, zoom: 1.5 },
  timeline_window: { from: 64.25, to: 144.75 },
};
const defaultProps = {
  sourceKey: "A:media-A",
  taskId: "A",
  frameIndex: 120,
  selectedId: null as string | null,
  containerSize: { w: 800, h: 600 },
  mediaSize: { w: 1600, h: 900 },
  hasRealMediaSize: true,
  sourceReady: true,
  autoFitOnResize: true,
};

function setup(overrides: Partial<typeof defaultProps> = {}) {
  const writes = vi.fn();
  const focused = vi.fn();
  const props = { ...defaultProps, ...overrides };
  const hook = renderHook(
    (input) => {
      const [viewport, setViewport] = useState<Viewport>({ scale: 1, tx: 0, ty: 0 });
      const timelineRef = useRef<VideoTimelineWindowControls | null>(null);
      const cancelRef = useRef(() => {});
      const timeline = useVideoTimelineWindow({
        sourceKey: input.sourceKey,
        maxFrame: 179,
        minSpan: 48,
        controlsRef: timelineRef,
        onInteraction: () => cancelRef.current(),
      });
      const writeViewport = useCallback((update: React.SetStateAction<Viewport>) => {
        writes(update);
        setViewport(update);
      }, []);
      const view = useVideoIssueView({
        ...input,
        viewport,
        setViewport: writeViewport,
        focusSelectionEnabled: true,
        focusObject: (id) => {
          focused(id);
          setViewport({ scale: 1, tx: -200, ty: -100 });
        },
        timelineRef,
      });
      cancelRef.current = view.cancelIssueRestore;
      return { ...view, viewport, timeline };
    },
    { initialProps: props },
  );
  return { ...hook, props, writes, focused };
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("useVideoIssueView", () => {
  it("waits for actual dimensions and a committed initial fit instead of certifying the fallback", async () => {
    const { result, rerender, props } = setup({ hasRealMediaSize: false });
    const abort = new AbortController();
    expect(result.current.captureIssueView()).toBeNull();
    let ready!: Promise<boolean>;
    act(() => {
      ready = result.current.waitForIssueViewReady(abort.signal);
    });
    expect(result.current.viewReady).toBe(false);
    rerender({ ...props, hasRealMediaSize: true });
    expect(await ready).toBe(true);
    expect(result.current.viewReady).toBe(true);
    expect(result.current.captureIssueView()).toEqual({
      taskId: "A",
      frameIndex: 120,
      viewport: { center_x: 0.5, center_y: 0.5, zoom: 1 },
      timeline_window: { from: 0, to: 179 },
    });
    expect(vi.getTimerCount()).toBe(0);
  });

  it("does not certify metadata and fit while the current original source is still preparing", async () => {
    const { result, rerender, props } = setup({ sourceReady: false });
    expect(result.current.captureIssueView()).toBeNull();
    let ready!: Promise<boolean>;
    let settled = false;
    act(() => {
      ready = result.current.waitForIssueViewReady(new AbortController().signal).then((value) => {
        settled = true;
        return value;
      });
    });
    await act(async () => vi.advanceTimersByTimeAsync(100));
    expect(settled).toBe(false);
    rerender({ ...props, sourceReady: true });
    expect(await ready).toBe(true);
    expect(result.current.captureIssueView()).toMatchObject({ taskId: "A", frameIndex: 120 });
  });

  it("waits for selected props, suppresses their focus, and commits the Issue viewport once", async () => {
    const { result, rerender, props, writes, focused } = setup();
    writes.mockClear();
    let lease!: VideoIssueRestoreLease;
    let pending!: Promise<VideoIssueViewRestoreResult>;
    act(() => {
      lease = result.current.beginIssueRestore(() => true)!;
      pending = lease.restore(saved, "track");
    });
    expect(writes).not.toHaveBeenCalled();
    rerender({ ...props, selectedId: "track" });
    expect(await pending).toEqual({ status: "restored", clamped: false });
    expect(result.current.captureIssueView()).toMatchObject(saved);
    expect(writes).toHaveBeenCalledTimes(1);
    expect(focused).not.toHaveBeenCalled();
    lease.release();
    rerender({ ...props, selectedId: "another-track" });
    expect(focused).toHaveBeenCalledTimes(1);
    expect(focused).toHaveBeenCalledWith("another-track");
    expect(result.current.viewport).toEqual({ scale: 1, tx: -200, ty: -100 });
  });

  it("uses the new container's fit scale when the window resizes before selection is observed", async () => {
    const { result, rerender, props } = setup();
    let pending!: Promise<VideoIssueViewRestoreResult>;
    act(() => {
      pending = result.current.beginIssueRestore(() => true)!.restore(saved, "track");
    });
    rerender({ ...props, containerSize: { w: 400, h: 500 } });
    rerender({ ...props, containerSize: { w: 400, h: 500 }, selectedId: "track" });
    expect(await pending).toEqual({ status: "restored", clamped: false });
    const capture = result.current.captureIssueView()!;
    expect(capture.viewport.center_x).toBeCloseTo(0.3);
    expect(capture.viewport.center_y).toBeCloseTo(0.6);
    expect(capture.viewport.zoom).toBe(1.5);
    expect(result.current.viewport.scale).toBe(0.375);
    expect(capture.timeline_window).toEqual(saved.timeline_window);
  });

  it("retains only the latest lease and ignores release from a superseded request", async () => {
    const { result, rerender, props } = setup();
    let first!: VideoIssueRestoreLease;
    let oldResult!: Promise<VideoIssueViewRestoreResult>;
    let latestResult!: Promise<VideoIssueViewRestoreResult>;
    act(() => {
      first = result.current.beginIssueRestore(() => true)!;
      oldResult = first.restore(saved, "old-track");
      latestResult = result.current.beginIssueRestore(() => true)!.restore(saved, "new-track");
      first.release();
    });
    expect(await oldResult).toEqual({ status: "cancelled", clamped: false });
    rerender({ ...props, selectedId: "new-track" });
    expect((await latestResult).status).toBe("restored");
    expect(result.current.captureIssueView()).toMatchObject(saved);
  });

  it("invalidates capture, wait, begin, and pending leases across source A→B→A", async () => {
    const { result, rerender, props } = setup();
    const old = result.current;
    let pending!: Promise<VideoIssueViewRestoreResult>;
    act(() => {
      pending = old.beginIssueRestore(() => true)!.restore(saved, "track");
    });
    rerender({ ...props, sourceKey: "B:media-B", taskId: "B" });
    expect((await pending).status).toBe("cancelled");
    rerender(props);
    let current!: Promise<VideoIssueViewRestoreResult>;
    act(() => {
      current = result.current.beginIssueRestore(() => true)!.restore(saved, "current-track");
    });
    expect(old.captureIssueView()).toBeNull();
    expect(await old.waitForIssueViewReady(new AbortController().signal)).toBe(false);
    expect(old.beginIssueRestore(() => true)).toBeNull();
    rerender({ ...props, selectedId: "current-track" });
    expect((await current).status).toBe("restored");
  });

  it.each(["frame", "selection", "relevance", "window"] as const)(
    "retires a waiting restore after a user %s change",
    async (kind) => {
      const { result, rerender, props, writes } = setup();
      let relevant = true;
      let pending!: Promise<VideoIssueViewRestoreResult>;
      writes.mockClear();
      act(() => {
        pending = result.current.beginIssueRestore(() => relevant)!.restore(saved, "track");
      });
      if (kind === "frame") rerender({ ...props, frameIndex: 121 });
      if (kind === "selection") rerender({ ...props, selectedId: "user-track" });
      if (kind === "relevance") {
        relevant = false;
        rerender(props);
      }
      if (kind === "window") {
        act(() => result.current.timeline.setTimelineWindow({ from: 80, to: 160 }));
      }
      expect((await pending).status).toBe("cancelled");
      expect(writes).not.toHaveBeenCalled();
    },
  );

  it("aborts readiness and restoration immediately and clears their timers", async () => {
    const { result, rerender, props } = setup({ hasRealMediaSize: false });
    const abort = new AbortController();
    let ready!: Promise<boolean>;
    let restored!: Promise<VideoIssueViewRestoreResult>;
    act(() => {
      ready = result.current.waitForIssueViewReady(abort.signal);
      restored = result.current
        .beginIssueRestore(() => true, abort.signal)!
        .restore(saved, "track");
      abort.abort();
    });
    expect(await ready).toBe(false);
    expect((await restored).status).toBe("cancelled");
    expect(vi.getTimerCount()).toBe(0);
    rerender({ ...props, hasRealMediaSize: true });
    expect(result.current.captureIssueView()?.viewport.zoom).toBe(1);
  });

  it("times out missing dimensions and a selection that never commits", async () => {
    const { result } = setup({ hasRealMediaSize: false });
    let ready!: Promise<boolean>;
    let restored!: Promise<VideoIssueViewRestoreResult>;
    act(() => {
      ready = result.current.waitForIssueViewReady(new AbortController().signal);
      restored = result.current.beginIssueRestore(() => true)!.restore(saved, "track");
    });
    await act(async () => vi.advanceTimersByTimeAsync(VIDEO_ISSUE_VIEW_READY_TIMEOUT_MS));
    expect(await ready).toBe(false);
    expect(await restored).toEqual({ status: "unavailable", clamped: false });
    expect(vi.getTimerCount()).toBe(0);
  });

  it("settles all outstanding work on unmount", async () => {
    const { result, unmount } = setup({ containerSize: { w: 0, h: 0 } });
    let ready!: Promise<boolean>;
    let restored!: Promise<VideoIssueViewRestoreResult>;
    act(() => {
      ready = result.current.waitForIssueViewReady(new AbortController().signal);
      restored = result.current.beginIssueRestore(() => true)!.restore(saved, "track");
    });
    unmount();
    expect(await ready).toBe(false);
    expect((await restored).status).toBe("cancelled");
    expect(vi.getTimerCount()).toBe(0);
  });

  it("preserves off-image centers and reports the existing viewport/window clamps", async () => {
    const { result } = setup();
    let restored!: Promise<VideoIssueViewRestoreResult>;
    act(() => {
      restored = result.current
        .beginIssueRestore(() => true)!
        .restore(
          {
            viewport: { center_x: -0.25, center_y: 1.5, zoom: 100 },
            timeline_window: { from: -20, to: 400 },
          },
          null,
        );
    });
    expect(await restored).toEqual({ status: "restored", clamped: true });
    expect(result.current.captureIssueView()).toMatchObject({
      viewport: { center_x: -0.25, center_y: 1.5, zoom: 16 },
      timeline_window: { from: 0, to: 179 },
    });
  });

  it.each([
    { ...saved, viewport: { center_x: Infinity, center_y: 0.5, zoom: 1 } },
    { ...saved, timeline_window: { from: 150, to: 90 } },
  ])("rejects invalid input without partially restoring either owner", async (view) => {
    const { result, writes } = setup();
    writes.mockClear();
    let restored!: Promise<VideoIssueViewRestoreResult>;
    act(() => {
      restored = result.current.beginIssueRestore(() => true)!.restore(view, null);
    });
    expect(await restored).toEqual({ status: "unavailable", clamped: false });
    expect(writes).not.toHaveBeenCalled();
    expect(result.current.timeline.timelineWindow).toEqual({ from: 0, to: 179 });
  });
});

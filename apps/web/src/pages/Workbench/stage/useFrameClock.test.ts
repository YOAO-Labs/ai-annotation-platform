import { useLayoutEffect, useRef, useState } from "react";
import { act, fireEvent, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useFrameClock, type FrameSeekResult } from "./useFrameClock";
import { frameToSeekTime, type FrameTimebase } from "./frameTimebase";

const timebase: FrameTimebase = {
  fps: 30,
  frameCount: 100,
  source: "ffprobe",
  ptsMs: Array.from({ length: 100 }, (_, index) => Math.round((index / 30) * 1000)),
  durationMs: 3333,
};

function nativeVideo(withCallbacks = true) {
  const video = document.createElement("video");
  Object.defineProperties(video, {
    readyState: { configurable: true, value: HTMLMediaElement.HAVE_CURRENT_DATA },
    videoWidth: { configurable: true, value: 640 },
    videoHeight: { configurable: true, value: 360 },
    seeking: { configurable: true, value: false, writable: true },
  });
  const callbacks = new Map<number, (now: number, metadata: { mediaTime: number }) => void>();
  let id = 0;
  const cancel = vi.fn((handle: number) => callbacks.delete(handle));
  if (withCallbacks) {
    Object.assign(video, {
      requestVideoFrameCallback: vi.fn(
        (callback: (now: number, metadata: { mediaTime: number }) => void) => {
          const handle = id++;
          callbacks.set(handle, callback);
          return handle;
        },
      ),
      cancelVideoFrameCallback: cancel,
    });
  }
  const present = (frame: number, changeClock = true) => {
    if (changeClock) video.currentTime = frameToSeekTime(frame, timebase);
    const [handle, callback] = [...callbacks][0];
    callbacks.delete(handle);
    callback(performance.now(), { mediaTime: frame / 30 });
  };
  return { video, callbacks, cancel, present };
}

function setup(video: HTMLVideoElement | null, playing = false) {
  const onFrameChange = vi.fn();
  const hook = renderHook(
    ({ sourceKey, sourceVideo }) => {
      const [frameIndex, setFrameIndex] = useState(0);
      const videoRef = useRef(sourceVideo);
      videoRef.current = sourceVideo;
      return useFrameClock({
        videoRef,
        sourceKey,
        frameIndex,
        timebase,
        isPlaying: playing,
        onFrameChange: (frame) => {
          onFrameChange(frame);
          setFrameIndex(frame);
        },
      });
    },
    { initialProps: { sourceKey: "task-A", sourceVideo: video } },
  );
  return { ...hook, onFrameChange };
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("useFrameClock", () => {
  it("keeps the optimistic target while rejecting an adjacent actual media frame", async () => {
    const native = nativeVideo();
    const { result, onFrameChange } = setup(native.video);
    let pending!: Promise<FrameSeekResult>;
    act(() => {
      pending = result.current.seekToAsync(10);
    });
    expect(onFrameChange).toHaveBeenLastCalledWith(10);
    act(() => native.present(9, false));
    expect(result.current.isSeeking).toBe(true);
    expect(result.current.getFrameEvidence(10)).toBeNull();
    expect(onFrameChange).toHaveBeenLastCalledWith(10);
    await act(async () => {
      native.present(10);
      await pending;
    });
    expect(await pending).toEqual({ status: "ready", frameIndex: 10, source: "rvfc" });
    expect(result.current.getFrameEvidence(10)?.mediaTime).toBe(10 / 30);
  });

  it("does not treat seeked or currentTime as proof of source pixels", async () => {
    const { video } = nativeVideo(false);
    const { result } = setup(video);
    let pending!: Promise<FrameSeekResult>;
    act(() => {
      pending = result.current.seekToAsync(10);
    });
    await act(async () => {
      fireEvent.seeked(video);
      await pending;
    });
    expect(await pending).toEqual({ status: "unavailable", frameIndex: 10, source: "seeked" });
    expect(result.current.getFrameEvidence(10)).toBeNull();
  });

  it("admits an exact rVFC receipt that arrived before the seeked event", async () => {
    const native = nativeVideo();
    const { result } = setup(native.video);
    let pending!: Promise<FrameSeekResult>;
    act(() => {
      pending = result.current.seekToAsync(17);
    });
    Object.assign(native.video, { seeking: true });
    act(() => native.present(17));
    expect(result.current.nativeFrame?.frameIndex).toBe(17);
    expect(result.current.getFrameEvidence(17)).toBeNull();
    expect(result.current.isSeeking).toBe(true);

    await act(async () => {
      Object.assign(native.video, { seeking: false });
      fireEvent.seeked(native.video);
      await vi.advanceTimersByTimeAsync(300);
    });
    expect(result.current.getFrameEvidence(17)?.mediaTime).toBe(17 / 30);
    expect(await pending).toEqual({ status: "ready", frameIndex: 17, source: "rvfc" });
    expect(result.current.isSeeking).toBe(false);
  });

  it("reports timeout as failure and preserves the target when late neighboring pixels arrive", async () => {
    const native = nativeVideo();
    const { result, onFrameChange } = setup(native.video);
    let pending!: Promise<FrameSeekResult>;
    act(() => {
      pending = result.current.seekToAsync(10);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });
    expect(await pending).toEqual({ status: "timeout", frameIndex: 10, source: "timeout" });
    act(() => native.present(9));
    expect(result.current.currentFrame).toBe(10);
    expect(onFrameChange).toHaveBeenLastCalledWith(10);
  });

  it("settles a missing video immediately without leaving a resolver or timer", async () => {
    const { result } = setup(null);
    let resultValue!: FrameSeekResult;
    await act(async () => {
      resultValue = await result.current.seekToAsync(3);
    });
    expect(resultValue).toEqual({ status: "unavailable", frameIndex: 3, source: null });
    expect(result.current.isSeeking).toBe(false);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("cancels the previous target while ignoring its late media callback", async () => {
    const native = nativeVideo();
    const { result } = setup(native.video);
    let first!: Promise<FrameSeekResult>;
    let second!: Promise<FrameSeekResult>;
    act(() => {
      first = result.current.seekToAsync(10);
    });
    act(() => {
      second = result.current.seekToAsync(20);
    });
    expect(await first).toEqual({ status: "cancelled", frameIndex: 10, source: null });
    act(() => native.present(10, false));
    expect(result.current.isSeeking).toBe(true);
    await act(async () => {
      native.present(20);
      await second;
    });
    expect(await second).toEqual({ status: "ready", frameIndex: 20, source: "rvfc" });
  });

  it("registers a same-frame resolver before using an existing exact native receipt", async () => {
    const native = nativeVideo();
    const { result } = setup(native.video);
    act(() => native.present(0));
    let outcome!: FrameSeekResult;
    await act(async () => {
      outcome = await result.current.seekToAsync(0);
    });
    expect(outcome).toEqual({ status: "ready", frameIndex: 0, source: "rvfc" });
    expect(vi.getTimerCount()).toBe(0);
  });

  it("registers a fresh native observation when retrying after a swallowed callback", async () => {
    const native = nativeVideo();
    const { result } = setup(native.video);
    let first!: Promise<FrameSeekResult>;
    act(() => {
      first = result.current.seekToAsync(17);
    });
    native.callbacks.clear();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });
    expect((await first).status).toBe("timeout");
    let retry!: Promise<FrameSeekResult>;
    act(() => {
      retry = result.current.seekToAsync(17);
    });
    expect(native.callbacks.size).toBeGreaterThan(0);
    await act(async () => {
      native.present(17);
      await retry;
    });
    expect(await retry).toEqual({ status: "ready", frameIndex: 17, source: "rvfc" });
  });

  it("invalidates pending seeks and native callbacks across source A→B→A", async () => {
    const native = nativeVideo();
    const { result, rerender } = setup(native.video);
    const oldCallback = [...native.callbacks.values()][0];
    let pending!: Promise<FrameSeekResult>;
    act(() => {
      pending = result.current.seekToAsync(10);
    });
    rerender({ sourceKey: "task-B", sourceVideo: native.video });
    rerender({ sourceKey: "task-A", sourceVideo: native.video });
    expect(await pending).toEqual({ status: "cancelled", frameIndex: 10, source: null });
    act(() => oldCallback(0, { mediaTime: 10 / 30 }));
    expect(result.current.getFrameEvidence(10)).toBeNull();
    expect(native.cancel).toHaveBeenCalled();
    expect(result.current.isSeeking).toBe(false);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(result.current.isSeeking).toBe(false);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("releases the first-frame seeking gate when the exact timetable replaces an estimate", async () => {
    const native = nativeVideo();
    const estimated: FrameTimebase = { ...timebase, source: "estimated", ptsMs: null };
    const { result, rerender } = renderHook(
      ({ sourceTimebase }) =>
        useFrameClock({
          videoRef: { current: native.video },
          sourceKey: "task-A",
          frameIndex: 0,
          timebase: sourceTimebase,
          isPlaying: false,
          onFrameChange: vi.fn(),
        }),
      { initialProps: { sourceTimebase: estimated } },
    );
    let pending!: Promise<FrameSeekResult>;
    act(() => {
      pending = result.current.seekToAsync(0);
    });
    expect(result.current.isSeeking).toBe(true);
    const oldCallbacks = [...native.callbacks.values()];

    rerender({ sourceTimebase: timebase });
    expect(await pending).toEqual({ status: "cancelled", frameIndex: 0, source: null });
    expect(result.current.isSeeking).toBe(false);
    act(() => oldCallbacks.forEach((callback) => callback(0, { mediaTime: 0 })));
    expect(result.current.getFrameEvidence(0)).toBeNull();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(result.current.isSeeking).toBe(false);
    expect(result.current.diagnostics.seekCount).toBe(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("settles pending work and cancels the rVFC handle on unmount", async () => {
    const native = nativeVideo();
    const { result, unmount, onFrameChange } = setup(native.video);
    const oldCallback = [...native.callbacks.values()][0];
    let pending!: Promise<FrameSeekResult>;
    act(() => {
      pending = result.current.seekToAsync(10);
    });
    unmount();
    expect(await pending).toEqual({ status: "cancelled", frameIndex: 10, source: null });
    expect(native.cancel).toHaveBeenCalledWith(0);
    const calls = onFrameChange.mock.calls.length;
    oldCallback(0, { mediaTime: 10 / 30 });
    expect(onFrameChange).toHaveBeenCalledTimes(calls);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("continues normal playback from actual native frame callbacks", () => {
    const native = nativeVideo();
    const { result } = setup(native.video, true);
    act(() => native.present(12));
    expect(result.current.currentFrame).toBe(12);
  });

  it("does not certify source indices from an estimated FPS timetable", async () => {
    const native = nativeVideo();
    const { result } = renderHook(() =>
      useFrameClock({
        videoRef: { current: native.video },
        frameIndex: 17,
        timebase: { ...timebase, source: "estimated", ptsMs: null },
        isPlaying: false,
        onFrameChange: vi.fn(),
      }),
    );
    let pending!: Promise<FrameSeekResult>;
    act(() => {
      pending = result.current.seekToAsync(17);
    });
    act(() => native.present(17));
    expect(result.current.getFrameEvidence(17)).toBeNull();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });
    expect((await pending).status).toBe("timeout");
  });

  it("keeps the first seek when the video ref was assigned during the initial commit", async () => {
    const native = nativeVideo();
    const { result } = renderHook(() => {
      const videoRef = useRef<HTMLVideoElement | null>(null);
      const [frameIndex, setFrameIndex] = useState(0);
      useLayoutEffect(() => {
        videoRef.current = native.video;
      }, []);
      return useFrameClock({
        videoRef,
        sourceKey: "A",
        frameIndex,
        timebase,
        isPlaying: false,
        onFrameChange: setFrameIndex,
      });
    });
    let pending!: Promise<FrameSeekResult>;
    act(() => {
      pending = result.current.seekToAsync(17);
    });
    expect(result.current.isSeeking).toBe(true);
    await act(async () => {
      native.present(17);
      await pending;
    });
    expect(await pending).toEqual({ status: "ready", frameIndex: 17, source: "rvfc" });
  });
});

import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/api/client";
import { useAuthStore } from "@/stores/authStore";
import { tasksApi } from "@/api/tasks";
import { useVideoFramePreview } from "./useVideoFramePreview";

vi.mock("@/api/tasks", () => ({
  tasksApi: {
    getVideoFrame: vi.fn(),
    prefetchVideoFrames: vi.fn(),
  },
}));

const api = vi.mocked(tasksApi);
const METADATA_GENERATION = "10000000-0000-4000-8000-000000000001";
const FRAME_IDENTITY = {
  metadata_generation_id: METADATA_GENERATION,
  output_id: "20000000-0000-4000-8000-000000000001",
  sha256: "a".repeat(64),
};

describe("useVideoFramePreview", () => {
  beforeEach(() => {
    vi.useRealTimers();
    localStorage.setItem("token", "video-test-token");
    useAuthStore.setState({
      token: "video-test-token",
      sessionId: "video-test-session",
      user: { id: "user-1" } as NonNullable<ReturnType<typeof useAuthStore.getState>["user"]>,
    });
    api.getVideoFrame.mockReset();
    api.prefetchVideoFrames.mockReset();
    // Scrub prefetch fires alongside every preview cache miss; default to a
    // benign empty response so individual tests only need to override when they
    // assert on prefetch behavior.
    api.prefetchVideoFrames.mockResolvedValue({
      dataset_item_id: "item-1",
      task_id: "task-1",
      frames: [],
    });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("loads a ready frame preview and reuses the in-memory cache", async () => {
    api.getVideoFrame.mockResolvedValue({
      frame_index: 5,
      width: 320,
      format: "webp",
      status: "ready",
      ...FRAME_IDENTITY,
      url: "/frame-5.webp",
      retry_after: null,
      error: null,
    });

    const { result } = renderHook(() =>
      useVideoFramePreview({
        metadataGenerationId: METADATA_GENERATION,
        taskId: "task-1",
        maxFrame: 9,
      }),
    );

    act(() => result.current.previewFor(5));

    await waitFor(() => expect(result.current.preview?.status).toBe("ready"));
    expect(result.current.preview?.url).toBe("/frame-5.webp");

    act(() => result.current.previewFor(5));

    expect(api.getVideoFrame).toHaveBeenCalledTimes(1);
  });

  it("keeps polling pending frame previews until the cache is ready", async () => {
    vi.useFakeTimers();
    api.getVideoFrame
      .mockResolvedValueOnce({
        frame_index: 2,
        width: 320,
        format: "webp",
        status: "pending",
        url: null,
        retry_after: 3,
        error: null,
      })
      .mockResolvedValueOnce({
        frame_index: 2,
        width: 320,
        format: "webp",
        status: "pending",
        url: null,
        retry_after: 3,
        error: null,
      })
      .mockResolvedValueOnce({
        frame_index: 2,
        width: 320,
        format: "webp",
        status: "ready",
        ...FRAME_IDENTITY,
        url: "/frame-2.webp",
        retry_after: null,
        error: null,
      });

    const { result } = renderHook(() =>
      useVideoFramePreview({
        metadataGenerationId: METADATA_GENERATION,
        taskId: "task-1",
        maxFrame: 9,
      }),
    );

    act(() => result.current.previewFor(2));
    // previewFor coalesces network calls into the next animation frame.
    await act(async () => {
      vi.advanceTimersByTime(16);
      await Promise.resolve();
    });
    expect(result.current.preview?.status).toBe("pending");

    await act(async () => {
      vi.advanceTimersByTime(800);
      await Promise.resolve();
    });
    expect(result.current.preview?.status).toBe("pending");

    await act(async () => {
      vi.advanceTimersByTime(800);
      await Promise.resolve();
    });

    expect(result.current.preview?.status).toBe("ready");
    expect(api.getVideoFrame).toHaveBeenCalledTimes(3);
  });

  it("does not stale an in-flight preview when pointermove repeats the same frame", async () => {
    const readyFrame = {
      frame_index: 6,
      width: 320,
      format: "webp" as const,
      status: "ready" as const,
      ...FRAME_IDENTITY,
      url: "/frame-6.webp",
      retry_after: null,
      error: null,
    };
    let resolveFrame: ((value: typeof readyFrame) => void) | undefined;
    api.getVideoFrame.mockReturnValue(
      new Promise((resolve) => {
        resolveFrame = resolve;
      }),
    );

    const { result } = renderHook(() =>
      useVideoFramePreview({
        metadataGenerationId: METADATA_GENERATION,
        taskId: "task-1",
        maxFrame: 9,
      }),
    );

    act(() => result.current.previewFor(6));
    act(() => result.current.previewFor(6));

    // previewFor coalesces repeated hover events into one rAF; wait for the
    // network call to flush before asserting the in-flight state.
    await waitFor(() => expect(api.getVideoFrame).toHaveBeenCalledTimes(1));
    expect(result.current.preview?.status).toBe("pending");

    await act(async () => {
      resolveFrame?.(readyFrame);
      await Promise.resolve();
    });

    await waitFor(() => expect(result.current.preview?.status).toBe("ready"));
    expect(result.current.preview?.url).toBe("/frame-6.webp");
  });

  it("快速 A→B→A 时不会让第一个 A 请求占住最终帧", async () => {
    const calls: Array<{ frameIndex: number; signal: AbortSignal }> = [];
    api.getVideoFrame.mockImplementation(
      (_taskId, frameIndex, _params, init?: RequestInit) =>
        new Promise((resolve) => {
          const signal = init?.signal as AbortSignal;
          calls.push({ frameIndex, signal });
          if (calls.length === 3) {
            resolve({
              frame_index: frameIndex,
              width: 320,
              format: "webp",
              status: "ready",
              ...FRAME_IDENTITY,
              url: `/frame-${frameIndex}-latest.webp`,
              retry_after: null,
              error: null,
            });
          }
        }),
    );

    const { result } = renderHook(() =>
      useVideoFramePreview({
        metadataGenerationId: METADATA_GENERATION,
        taskId: "task-1",
        maxFrame: 9,
      }),
    );

    act(() => result.current.previewFor(1));
    await waitFor(() => expect(calls).toHaveLength(1));
    act(() => result.current.previewFor(2));
    await waitFor(() => expect(calls).toHaveLength(2));
    act(() => result.current.previewFor(1));
    await waitFor(() => expect(calls).toHaveLength(3));

    expect(calls.slice(0, 2).every(({ signal }) => signal.aborted)).toBe(true);
    await waitFor(() => expect(result.current.preview?.url).toBe("/frame-1-latest.webp"));
  });

  it("A→已缓存 B→A 会取消旧 A 并重新请求最终 A", async () => {
    const foregroundSignals: AbortSignal[] = [];
    let foregroundCall = 0;
    api.prefetchVideoFrames.mockResolvedValueOnce({
      dataset_item_id: "item-1",
      task_id: "task-1",
      frames: [
        {
          frame_index: 2,
          width: 320,
          format: "webp",
          status: "ready",
          ...FRAME_IDENTITY,
          url: "/frame-2-cached.webp",
          retry_after: null,
          error: null,
        },
      ],
    });
    api.getVideoFrame.mockImplementation((_taskId, frameIndex, _params, init?: RequestInit) => {
      foregroundCall += 1;
      const signal = init?.signal as AbortSignal;
      foregroundSignals.push(signal);
      if (foregroundCall === 2) {
        return Promise.resolve({
          frame_index: frameIndex,
          width: 320,
          format: "webp",
          status: "ready",
          ...FRAME_IDENTITY,
          url: "/frame-1-latest.webp",
          retry_after: null,
          error: null,
        });
      }
      return new Promise((_, reject) => {
        signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), {
          once: true,
        });
      });
    });

    const { result } = renderHook(() =>
      useVideoFramePreview({
        metadataGenerationId: METADATA_GENERATION,
        taskId: "task-1",
        maxFrame: 9,
      }),
    );
    act(() => result.current.prefetch([2]));
    await waitFor(() => expect(result.current.diagnostics.cacheSize).toBe(1));

    act(() => result.current.previewFor(1));
    await waitFor(() => expect(api.getVideoFrame).toHaveBeenCalledTimes(1));
    act(() => result.current.previewFor(2));
    await waitFor(() => expect(result.current.preview?.url).toBe("/frame-2-cached.webp"));
    act(() => result.current.previewFor(1));

    await waitFor(() => expect(api.getVideoFrame).toHaveBeenCalledTimes(2));
    expect(foregroundSignals[0]?.aborted).toBe(true);
    await waitFor(() => expect(result.current.preview?.url).toBe("/frame-1-latest.webp"));
  });

  it("prefetches unique clamped frames without binding preview state", async () => {
    api.prefetchVideoFrames.mockResolvedValue({
      dataset_item_id: "item-1",
      task_id: "task-1",
      frames: [],
    });

    const { result } = renderHook(() =>
      useVideoFramePreview({
        metadataGenerationId: METADATA_GENERATION,
        taskId: "task-1",
        maxFrame: 9,
      }),
    );

    act(() => result.current.prefetch([1, 1, 20, -2]));

    await waitFor(() =>
      expect(api.prefetchVideoFrames).toHaveBeenCalledWith(
        "task-1",
        [1, 9, 0],
        { width: 320, format: "webp" },
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      ),
    );
    expect(result.current.preview).toBeNull();
  });

  it("切题时中止旧缩略帧与预取请求", async () => {
    const foregroundSignals: AbortSignal[] = [];
    const prefetchSignals: AbortSignal[] = [];
    api.getVideoFrame.mockImplementation(
      (_taskId, _frameIndex, _params, init?: RequestInit) =>
        new Promise((_, reject) => {
          const signal = init?.signal;
          if (signal) {
            foregroundSignals.push(signal);
            signal.addEventListener(
              "abort",
              () => reject(new DOMException("aborted", "AbortError")),
              { once: true },
            );
          }
        }),
    );
    api.prefetchVideoFrames.mockImplementation(
      (_taskId, _frames, _params, init?: RequestInit) =>
        new Promise((_, reject) => {
          const signal = init?.signal;
          if (signal) {
            prefetchSignals.push(signal);
            signal.addEventListener(
              "abort",
              () => reject(new DOMException("aborted", "AbortError")),
              { once: true },
            );
          }
        }),
    );

    const { result, rerender } = renderHook(
      ({ taskId }) =>
        useVideoFramePreview({ metadataGenerationId: METADATA_GENERATION, taskId, maxFrame: 9 }),
      { initialProps: { taskId: "task-1" } },
    );
    act(() => result.current.previewFor(4));
    await waitFor(() => expect(foregroundSignals).toHaveLength(1));
    await waitFor(() => expect(prefetchSignals.length).toBeGreaterThan(0));

    rerender({ taskId: "task-2" });

    expect(foregroundSignals[0]?.aborted).toBe(true);
    expect(prefetchSignals.every((signal) => signal.aborted)).toBe(true);
  });

  it("disables frame preview for unsupported task frame-service routes", async () => {
    api.getVideoFrame.mockRejectedValue(new ApiError(404, "not found"));

    const { result } = renderHook(() =>
      useVideoFramePreview({
        metadataGenerationId: METADATA_GENERATION,
        taskId: "task-1",
        maxFrame: 9,
      }),
    );

    act(() => result.current.previewFor(3));

    await waitFor(() => expect(api.getVideoFrame).toHaveBeenCalledTimes(1));

    act(() => result.current.previewFor(4));

    expect(api.getVideoFrame).toHaveBeenCalledTimes(1);
    expect(result.current.preview).toBeNull();
  });
  it("refreshes a ready signed URL after its bounded cache lifetime", async () => {
    let now = Date.now();
    const clock = vi.spyOn(Date, "now").mockImplementation(() => now);
    const ready = {
      frame_index: 5,
      width: 320,
      format: "webp" as const,
      status: "ready" as const,
      ...FRAME_IDENTITY,
      url: "/old.webp",
      retry_after: null,
      error: null,
    };
    api.getVideoFrame.mockResolvedValueOnce(ready).mockResolvedValueOnce({
      ...ready,
      output_id: "20000000-0000-4000-8000-000000000002",
      url: "/new.webp",
    });
    const { result } = renderHook(() =>
      useVideoFramePreview({
        metadataGenerationId: METADATA_GENERATION,
        taskId: "task-1",
        maxFrame: 9,
      }),
    );
    act(() => result.current.previewFor(5));
    await waitFor(() => expect(result.current.preview?.url).toBe("/old.webp"));
    now += 30_001;
    act(() => result.current.previewFor(5));
    await waitFor(() => expect(result.current.preview?.url).toBe("/new.webp"));
    expect(api.getVideoFrame).toHaveBeenCalledTimes(2);
    clock.mockRestore();
  });

  it("rejects an old generation response after the same task regenerates", async () => {
    const ready = {
      frame_index: 5,
      width: 320,
      format: "webp" as const,
      status: "ready" as const,
      ...FRAME_IDENTITY,
      url: "/old.webp",
      retry_after: null,
      error: null,
    };
    let resolveOld: ((value: typeof ready) => void) | undefined;
    const nextGeneration = "10000000-0000-4000-8000-000000000002";
    api.getVideoFrame
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveOld = resolve;
        }),
      )
      .mockResolvedValueOnce({
        ...ready,
        metadata_generation_id: nextGeneration,
        output_id: "20000000-0000-4000-8000-000000000002",
        url: "/new.webp",
      });
    const { result, rerender } = renderHook(
      ({ generation }) =>
        useVideoFramePreview({ metadataGenerationId: generation, taskId: "task-1", maxFrame: 9 }),
      { initialProps: { generation: METADATA_GENERATION } },
    );
    act(() => result.current.previewFor(5));
    await waitFor(() => expect(api.getVideoFrame).toHaveBeenCalledTimes(1));
    const oldSignal = api.getVideoFrame.mock.calls[0][3]?.signal;
    rerender({ generation: nextGeneration });
    expect(oldSignal?.aborted).toBe(true);
    expect(result.current.preview).toBeNull();
    act(() => result.current.previewFor(5));
    await waitFor(() => expect(result.current.preview?.url).toBe("/new.webp"));
    await act(async () => {
      resolveOld?.(ready);
      await Promise.resolve();
    });
    expect(result.current.preview?.url).toBe("/new.webp");
  });

  it("rejects late preview responses after credentials rotate", async () => {
    const ready = {
      frame_index: 5,
      width: 320,
      format: "webp" as const,
      status: "ready" as const,
      ...FRAME_IDENTITY,
      url: "/retired-session.webp",
      retry_after: null,
      error: null,
    };
    let resolveOld: ((value: typeof ready) => void) | undefined;
    api.getVideoFrame.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveOld = resolve;
      }),
    );
    const { result } = renderHook(() =>
      useVideoFramePreview({
        metadataGenerationId: METADATA_GENERATION,
        taskId: "task-1",
        maxFrame: 9,
      }),
    );
    act(() => result.current.previewFor(5));
    await waitFor(() => expect(api.getVideoFrame).toHaveBeenCalledTimes(1));
    const oldSignal = api.getVideoFrame.mock.calls[0][3]?.signal;
    act(() => useAuthStore.getState().setToken("renewed-video-token"));
    expect(oldSignal?.aborted).toBe(true);
    await act(async () => {
      resolveOld?.(ready);
      await Promise.resolve();
    });
    expect(result.current.preview).toBeNull();
  });

  it("keeps an unknown output recoverable instead of permanently unsupported", async () => {
    let now = Date.now();
    const clock = vi.spyOn(Date, "now").mockImplementation(() => now);
    const ready = {
      frame_index: 5,
      width: 320,
      format: "webp" as const,
      status: "ready" as const,
      ...FRAME_IDENTITY,
      url: "/regenerated.webp",
      retry_after: null,
      error: null,
    };
    api.getVideoFrame
      .mockResolvedValueOnce({
        ...ready,
        status: "failed",
        url: null,
        error: "video_output_unrecorded",
        output_id: null,
      })
      .mockResolvedValueOnce(ready);
    const { result } = renderHook(() =>
      useVideoFramePreview({
        metadataGenerationId: METADATA_GENERATION,
        taskId: "task-1",
        maxFrame: 9,
      }),
    );
    act(() => result.current.previewFor(5));
    await waitFor(() => expect(result.current.preview?.status).toBe("error"));
    expect(result.current.diagnostics.unsupported).toBe(false);
    now += 1_001;
    act(() => result.current.previewFor(5));
    await waitFor(() => expect(result.current.preview?.url).toBe("/regenerated.webp"));
    clock.mockRestore();
  });
});

import { createElement, type ReactNode } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/api/client";
import { tasksApi } from "@/api/tasks";
import { useAuthStore } from "@/stores/authStore";
import { useVideoFrameTimetable } from "./useTasks";

vi.mock("@/api/tasks", () => ({ tasksApi: { getVideoFrameTimetable: vi.fn() } }));
const getTimetable = vi.mocked(tasksApi.getVideoFrameTimetable);
const generation = "10000000-0000-4000-8000-000000000001";
const nextGeneration = "10000000-0000-4000-8000-000000000002";
const timetable = {
  metadata_generation_id: generation,
  task_id: "task-1",
  fps: 30,
  frame_count: 1,
  source: "ffprobe" as const,
  frames: [{ frame_index: 0, pts_ms: 0, is_keyframe: true, pict_type: "I", byte_offset: null }],
};
let client: QueryClient;
function Wrapper({ children }: { children: ReactNode }) {
  return createElement(QueryClientProvider, { client }, children);
}

describe("video timetable generation ownership", () => {
  beforeEach(() => {
    client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    localStorage.setItem("token", "timetable-token");
    useAuthStore.setState({
      token: "timetable-token",
      sessionId: "session-1",
      user: { id: "user-1" } as NonNullable<ReturnType<typeof useAuthStore.getState>["user"]>,
    });
    getTimetable.mockReset();
  });
  afterEach(() => client.clear());

  it("requests and accepts only the manifest's metadata generation", async () => {
    getTimetable.mockResolvedValue(timetable);
    const { result } = renderHook(() => useVideoFrameTimetable("task-1", true, generation), {
      wrapper: Wrapper,
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(getTimetable.mock.calls[0][1]).toEqual({ expected_metadata_generation_id: generation });
    expect(result.current.data).toEqual(timetable);
  });

  it.each([null, nextGeneration])(
    "rejects unknown or changed response generation %s before caching",
    async (responseGeneration) => {
      getTimetable.mockResolvedValue({ ...timetable, metadata_generation_id: responseGeneration });
      const invalidate = vi.spyOn(client, "invalidateQueries");
      const { result } = renderHook(() => useVideoFrameTimetable("task-1", true, generation), {
        wrapper: Wrapper,
      });
      await waitFor(() => expect(result.current.isError).toBe(true));
      expect(result.current.data).toBeUndefined();
      expect(
        client
          .getQueryCache()
          .getAll()
          .every((query) => query.state.data === undefined),
      ).toBe(true);
      expect(invalidate).toHaveBeenCalledWith({
        queryKey: ["task-video-manifest", expect.any(String), "task-1"],
      });
      expect(getTimetable).toHaveBeenCalledTimes(1);
    },
  );

  it("refreshes the manifest on explicit server generation conflict without retrying old coordinates", async () => {
    getTimetable.mockRejectedValue(
      new ApiError(409, "changed", { code: "video_metadata_changed" }),
    );
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const { result } = renderHook(() => useVideoFrameTimetable("task-1", true, generation), {
      wrapper: Wrapper,
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(invalidate).toHaveBeenCalledTimes(1);
    expect(getTimetable).toHaveBeenCalledTimes(1);
  });

  it("waits for a verified manifest generation before requesting a timetable", () => {
    renderHook(() => useVideoFrameTimetable("task-1", true, null), { wrapper: Wrapper });
    expect(getTimetable).not.toHaveBeenCalled();
  });

  it("aborts a retired generation and ignores its late response", async () => {
    let resolveOld: ((value: typeof timetable) => void) | undefined;
    getTimetable
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveOld = resolve;
        }),
      )
      .mockResolvedValueOnce({
        ...timetable,
        metadata_generation_id: nextGeneration,
        frames: [{ ...timetable.frames[0], pts_ms: 7 }],
      });
    const { result, rerender } = renderHook(
      ({ current }) => useVideoFrameTimetable("task-1", true, current),
      { initialProps: { current: generation }, wrapper: Wrapper },
    );
    await waitFor(() => expect(getTimetable).toHaveBeenCalledTimes(1));
    const oldSignal = getTimetable.mock.calls[0][2]?.signal;
    rerender({ current: nextGeneration });
    await waitFor(() => expect(result.current.data?.frames[0].pts_ms).toBe(7));
    expect(oldSignal?.aborted).toBe(true);
    await act(async () => {
      resolveOld?.(timetable);
      await Promise.resolve();
    });
    expect(result.current.data?.metadata_generation_id).toBe(nextGeneration);
    expect(result.current.data?.frames[0].pts_ms).toBe(7);
  });
});

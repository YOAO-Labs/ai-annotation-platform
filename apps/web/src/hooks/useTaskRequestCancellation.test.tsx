import { createElement, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "@/stores/authStore";

const getAnnotationsMock = vi.hoisted(() => vi.fn());
const getVideoManifestMock = vi.hoisted(() => vi.fn());
const listPredictionsMock = vi.hoisted(() => vi.fn());

vi.mock("../api/tasks", () => ({
  tasksApi: {
    getAnnotations: getAnnotationsMock,
    getVideoManifest: getVideoManifestMock,
  },
}));

vi.mock("@/api/predictions", () => ({
  predictionsApi: {
    listByTask: listPredictionsMock,
  },
}));

import { usePredictions } from "./usePredictions";
import { useAnnotations, useVideoManifest } from "./useTasks";

function makeWrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
}

function pendingUntilAbort(init?: RequestInit): Promise<never> {
  return new Promise((_, reject) => {
    init?.signal?.addEventListener(
      "abort",
      () => reject(new DOMException("aborted", "AbortError")),
      { once: true },
    );
  });
}

describe("task-scoped query cancellation", () => {
  beforeEach(() => {
    localStorage.setItem("token", "video-test-token");
    useAuthStore.setState({
      token: "video-test-token",
      sessionId: "video-test-session",
      user: { id: "user-1" } as NonNullable<ReturnType<typeof useAuthStore.getState>["user"]>,
    });
    getAnnotationsMock
      .mockReset()
      .mockImplementation((_taskId: string, _segmentId?: string | null, init?: RequestInit) =>
        pendingUntilAbort(init),
      );
    getVideoManifestMock
      .mockReset()
      .mockImplementation((_taskId: string, init?: RequestInit) => pendingUntilAbort(init));
    listPredictionsMock
      .mockReset()
      .mockImplementation(
        (
          _taskId: string,
          _model?: string,
          _confidence?: number,
          _limit?: number,
          _offset?: number,
          init?: RequestInit,
        ) => pendingUntilAbort(init),
      );
  });

  it("切题时中止旧标注查询", async () => {
    const { rerender } = renderHook(({ taskId }) => useAnnotations(taskId), {
      initialProps: { taskId: "task-1" },
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(getAnnotationsMock).toHaveBeenCalledTimes(1));
    rerender({ taskId: "task-2" });
    await waitFor(() => expect(getAnnotationsMock).toHaveBeenCalledTimes(2));

    const firstSignal = getAnnotationsMock.mock.calls[0]?.[2]?.signal as AbortSignal | undefined;
    const latestSignal = getAnnotationsMock.mock.calls[1]?.[2]?.signal as AbortSignal | undefined;
    expect(firstSignal).toBeInstanceOf(AbortSignal);
    expect(firstSignal?.aborted).toBe(true);
    expect(latestSignal?.aborted).toBe(false);
  });

  it("切题时中止旧视频 manifest 查询", async () => {
    const { rerender } = renderHook(({ taskId }) => useVideoManifest(taskId), {
      initialProps: { taskId: "task-1" },
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(getVideoManifestMock).toHaveBeenCalledTimes(1));
    rerender({ taskId: "task-2" });
    await waitFor(() => expect(getVideoManifestMock).toHaveBeenCalledTimes(2));

    expect(getVideoManifestMock.mock.calls[0]?.[1]?.signal?.aborted).toBe(true);
    expect(getVideoManifestMock.mock.calls[1]?.[1]?.signal?.aborted).toBe(false);
  });

  it("切题时中止旧预测查询", async () => {
    const { rerender } = renderHook(({ taskId }) => usePredictions(taskId), {
      initialProps: { taskId: "task-1" },
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(listPredictionsMock).toHaveBeenCalledTimes(1));
    rerender({ taskId: "task-2" });
    await waitFor(() => expect(listPredictionsMock).toHaveBeenCalledTimes(2));

    expect(listPredictionsMock.mock.calls[0]?.[5]?.signal?.aborted).toBe(true);
    expect(listPredictionsMock.mock.calls[1]?.[5]?.signal?.aborted).toBe(false);
  });
});

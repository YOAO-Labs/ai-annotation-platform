import { QueryClient } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { ApiError } from "@/api/client";

const state = vi.hoisted(() => ({ online: true, drain: vi.fn(), update: vi.fn() }));
vi.mock("@/api/tasks", () => ({ tasksApi: { updateAnnotation: state.update } }));
vi.mock("@/hooks/useOnlineStatus", () => ({
  useOnlineStatus: () => ({
    online: state.online,
    queueCount: 1,
    queueReady: true,
    queueReadError: null,
  }),
}));
vi.mock("@/stores/authStore", () => ({ isCurrentAuthOwner: () => true }));
vi.mock("./offlineQueue", () => ({
  drain: state.drain,
  isOfflineCandidate: () => true,
  replaceAnnotationId: vi.fn(),
}));
import { useWorkbenchOfflineQueue } from "./useWorkbenchOfflineQueue";

it("keeps a failed operation without a render-driven retry loop and retries on reconnection", async () => {
  const client = new QueryClient();
  state.drain.mockResolvedValue({ ok: 0, failed: 0 }).mockResolvedValueOnce({ ok: 0, failed: 1 });
  const { result, rerender, unmount } = renderHook(() =>
    useWorkbenchOfflineQueue({
      userId: "alice",
      taskId: "task",
      queryClient: client,
      pushToast: vi.fn(),
      history: { replaceAnnotationId: vi.fn() },
    }),
  );
  await waitFor(() => expect(result.current.syncError).toBe("部分离线操作同步失败"));
  rerender();
  await act(async () => {
    await Promise.resolve();
  });
  expect(state.drain).toHaveBeenCalledTimes(1);
  state.online = false;
  rerender();
  state.online = true;
  rerender();
  await waitFor(() => expect(state.drain).toHaveBeenCalledTimes(2));
  unmount();
  client.clear();
});

it("authorizes each queued op by its project and retains denied drafts", async () => {
  const client = new QueryClient();
  state.online = true;
  state.drain.mockReset();
  const authorize = vi.fn(async (op: { projectId?: string }) => op.projectId !== "A");
  state.drain.mockImplementationOnce(
    async (
      _handler: unknown,
      _scope: unknown,
      options: { shouldProcess: (op: unknown) => Promise<boolean> },
    ) => {
      const opA = {
        kind: "delete",
        id: "a",
        taskId: "taskA",
        projectId: "A",
        annotationId: "x",
        ts: 1,
      };
      const opB = {
        kind: "delete",
        id: "b",
        taskId: "taskB",
        projectId: "B",
        annotationId: "y",
        ts: 2,
      };
      await options.shouldProcess(opA);
      await options.shouldProcess(opB);
      return { ok: 1, failed: 0, denied: 1 };
    },
  );
  const { result } = renderHook(() =>
    useWorkbenchOfflineQueue({
      userId: "alice",
      taskId: "task",
      queryClient: client,
      pushToast: vi.fn(),
      history: { replaceAnnotationId: vi.fn() },
      authorizeFlush: authorize,
    }),
  );
  await waitFor(() => expect(state.drain).toHaveBeenCalled());
  await waitFor(() => expect(result.current.syncError).toContain("项目权限已变更"));
  expect(authorize).toHaveBeenCalledWith(expect.objectContaining({ projectId: "A" }));
  expect(authorize).toHaveBeenCalledWith(expect.objectContaining({ projectId: "B" }));
  const options = state.drain.mock.calls[0][2] as {
    classifyError: (error: unknown) => "denied" | "missing" | "retry";
  };
  expect(options.classifyError(new ApiError(403, "x"))).toBe("denied");
  // 404 after a successful access preflight means the target is gone, not that
  // authority was revoked.
  expect(options.classifyError(new ApiError(404, "x"))).toBe("missing");
  expect(options.classifyError(new Error("network"))).toBe("retry");
  client.clear();
});

it("does not report a permission change when the access preflight fails transiently", async () => {
  const client = new QueryClient();
  state.online = true;
  state.drain.mockReset();
  const authorize = vi.fn(async () => ({ outcome: "indeterminate" as const }));
  state.drain.mockImplementationOnce(
    async (
      _handler: unknown,
      _scope: unknown,
      options: { shouldProcess: (op: unknown) => Promise<boolean | "indeterminate"> },
    ) => {
      const result = await options.shouldProcess({
        kind: "update",
        id: "c",
        taskId: "taskC",
        projectId: "C",
        annotationId: "z",
        ts: 3,
      });
      expect(result).toBe("indeterminate");
      return { ok: 0, failed: 0, denied: 0, deferred: 1 };
    },
  );
  const { result, unmount } = renderHook(() =>
    useWorkbenchOfflineQueue({
      userId: "alice",
      taskId: "task",
      queryClient: client,
      pushToast: vi.fn(),
      history: { replaceAnnotationId: vi.fn() },
      authorizeFlush: authorize,
    }),
  );
  try {
    await waitFor(() => expect(state.drain).toHaveBeenCalled());
    await waitFor(() => expect(result.current.syncError).toContain("网络不稳定"));
    expect(result.current.syncError).not.toContain("项目权限已变更");
  } finally {
    unmount();
    client.clear();
  }
});

it("replays a versioned offline update with its original precondition", async () => {
  const client = new QueryClient();
  const op = {
    kind: "update" as const,
    id: "operation",
    taskId: "task",
    userId: "alice",
    annotationId: "mask",
    payload: { attributes: { reviewed: true } },
    etag: 'W/"7"',
    ts: 1,
  };
  state.online = true;
  state.update.mockReset().mockResolvedValue(undefined);
  state.drain.mockReset().mockImplementationOnce(async (handler) => {
    await handler(op);
    return { ok: 1, failed: 0 };
  });
  const { unmount } = renderHook(() =>
    useWorkbenchOfflineQueue({
      userId: "alice",
      taskId: "task",
      queryClient: client,
      pushToast: vi.fn(),
      history: { replaceAnnotationId: vi.fn() },
    }),
  );
  try {
    await waitFor(() => expect(state.update).toHaveBeenCalledOnce());
    expect(state.update).toHaveBeenCalledWith("task", "mask", op.payload, op.etag);
  } finally {
    unmount();
    client.clear();
  }
});

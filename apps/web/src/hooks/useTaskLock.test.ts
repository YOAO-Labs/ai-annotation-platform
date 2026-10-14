import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/api";
import { tasksApi } from "@/api/tasks";
import type { TaskLockResponse } from "@/types";
import { useTaskLock } from "./useTaskLock";

vi.mock("@/api/tasks", () => ({
  tasksApi: {
    acquireLock: vi.fn(),
    heartbeatLock: vi.fn(),
    releaseLockKeepalive: vi.fn(),
  },
}));

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

function lockFor(taskId: string): TaskLockResponse {
  return {
    task_id: taskId,
    user_id: "user-1",
    unique_id: `lock-${taskId}`,
    expire_at: new Date(Date.now() + 300_000).toISOString(),
  };
}

async function settle() {
  await act(async () => {});
}

describe("useTaskLock", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(tasksApi.acquireLock)
      .mockReset()
      .mockImplementation(async (id) => lockFor(id));
    vi.mocked(tasksApi.heartbeatLock).mockReset().mockResolvedValue({ status: "renewed" });
    vi.mocked(tasksApi.releaseLockKeepalive).mockReset().mockResolvedValue(undefined);
  });

  afterEach(async () => {
    cleanup();
    await settle();
    vi.useRealTimers();
  });

  it("releases on preview and never acquires or heartbeats while disabled", async () => {
    const { result, rerender } = renderHook(({ taskId, enabled }) => useTaskLock(taskId, enabled), {
      initialProps: { taskId: "a", enabled: true },
    });
    await settle();
    expect(result.current.isLocked).toBe(true);
    rerender({ taskId: "a", enabled: false });
    expect(result.current.isLocked).toBe(false);
    await settle();
    expect(tasksApi.releaseLockKeepalive).toHaveBeenCalledTimes(1);
    expect(tasksApi.releaseLockKeepalive).toHaveBeenCalledWith("a");
    rerender({ taskId: "b", enabled: false });
    await act(async () => vi.advanceTimersByTimeAsync(180_000));
    expect(tasksApi.acquireLock).toHaveBeenCalledTimes(1);
    expect(tasksApi.acquireLock).toHaveBeenCalledWith("a");
    expect(tasksApi.heartbeatLock).not.toHaveBeenCalled();
  });

  it("releases and pauses the task lock before a task phase transition", async () => {
    const { result } = renderHook(() => useTaskLock("a"));
    await settle();
    expect(result.current.isLocked).toBe(true);

    await act(async () => {
      await result.current.releaseForPhaseTransition("a");
    });
    expect(tasksApi.releaseLockKeepalive).toHaveBeenCalledTimes(1);
    expect(result.current.isLocked).toBe(false);

    await act(async () => vi.advanceTimersByTimeAsync(180_000));
    expect(tasksApi.heartbeatLock).not.toHaveBeenCalled();

    act(() => result.current.resumeAfterPhaseTransitionFailure("a"));
    await settle();
    expect(tasksApi.acquireLock).toHaveBeenCalledTimes(2);
    expect(tasksApi.releaseLockKeepalive).toHaveBeenCalledTimes(1);
  });

  it("never exposes the previous task lock while a new task is loading", async () => {
    const next = deferred<TaskLockResponse>();
    const { result, rerender } = renderHook(({ id }) => useTaskLock(id), {
      initialProps: { id: "a" },
    });
    await settle();
    vi.mocked(tasksApi.acquireLock).mockReturnValueOnce(next.promise);
    rerender({ id: "b" });
    expect(result.current.lock).toBeNull();
    expect(result.current.isLocked).toBe(false);
    await settle();
    await act(async () => next.resolve(lockFor("b")));
    expect(result.current.lock?.task_id).toBe("b");
  });

  it("cleans an acquisition that resolves after preview started", async () => {
    const late = deferred<TaskLockResponse>();
    vi.mocked(tasksApi.acquireLock).mockReturnValueOnce(late.promise);
    const { result, rerender } = renderHook(({ enabled }) => useTaskLock("a", enabled), {
      initialProps: { enabled: true },
    });
    await settle();
    rerender({ enabled: false });
    await settle();
    expect(tasksApi.releaseLockKeepalive).not.toHaveBeenCalled();
    await act(async () => late.resolve(lockFor("a")));
    expect(tasksApi.releaseLockKeepalive).toHaveBeenCalledTimes(1);
    expect(tasksApi.releaseLockKeepalive).toHaveBeenCalledWith("a");
    expect(result.current.isLocked).toBe(false);
  });

  it("finishes old acquisition and DELETE before reacquiring the same task", async () => {
    const late = deferred<TaskLockResponse>();
    const release = deferred<Response | undefined>();
    vi.mocked(tasksApi.acquireLock).mockReturnValueOnce(late.promise);
    vi.mocked(tasksApi.releaseLockKeepalive).mockReturnValueOnce(release.promise);
    const { result, rerender } = renderHook(({ enabled }) => useTaskLock("a", enabled), {
      initialProps: { enabled: true },
    });
    await settle();
    rerender({ enabled: false });
    rerender({ enabled: true });
    await act(async () => late.resolve(lockFor("a")));
    expect(tasksApi.acquireLock).toHaveBeenCalledTimes(1);
    expect(result.current.isLocked).toBe(false);
    await act(async () => release.resolve(undefined));
    expect(tasksApi.acquireLock).toHaveBeenCalledTimes(2);
    expect(result.current.isLocked).toBe(true);
    expect(tasksApi.releaseLockKeepalive).toHaveBeenCalledTimes(1);
  });

  it("orders cleanup before same-task acquisition across hook remounts", async () => {
    const late = deferred<TaskLockResponse>();
    vi.mocked(tasksApi.acquireLock).mockReturnValueOnce(late.promise);
    const previous = renderHook(() => useTaskLock("a"));
    await settle();
    previous.unmount();
    const next = renderHook(() => useTaskLock("a"));
    await settle();
    expect(tasksApi.acquireLock).toHaveBeenCalledTimes(1);
    await act(async () => late.resolve(lockFor("a")));
    expect(tasksApi.acquireLock).toHaveBeenCalledTimes(2);
    expect(tasksApi.releaseLockKeepalive).toHaveBeenCalledTimes(1);
    expect(next.result.current.isLocked).toBe(true);
  });

  it("does not retry a cancelled failed acquire", async () => {
    const late = deferred<TaskLockResponse>();
    vi.mocked(tasksApi.acquireLock).mockReturnValueOnce(late.promise);
    const { rerender } = renderHook(({ enabled }) => useTaskLock("a", enabled), {
      initialProps: { enabled: true },
    });
    await settle();
    rerender({ enabled: false });
    await act(async () => late.reject(new Error("late acquisition failure")));
    expect(tasksApi.acquireLock).toHaveBeenCalledTimes(1);
    expect(tasksApi.releaseLockKeepalive).toHaveBeenCalledTimes(1);
  });

  it("does not reacquire after a late heartbeat failure", async () => {
    const heartbeat = deferred<{ status: string }>();
    vi.mocked(tasksApi.heartbeatLock).mockReturnValueOnce(heartbeat.promise);
    const { rerender } = renderHook(({ enabled }) => useTaskLock("a", enabled), {
      initialProps: { enabled: true },
    });
    await settle();
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    rerender({ enabled: false });
    await act(async () => heartbeat.reject(new Error("late heartbeat failure")));
    expect(tasksApi.acquireLock).toHaveBeenCalledTimes(1);
    expect(tasksApi.releaseLockKeepalive).toHaveBeenCalledTimes(1);
  });

  it("does not stack heartbeat requests and keeps real conflicts read-only", async () => {
    const heartbeat = deferred<{ status: string }>();
    vi.mocked(tasksApi.heartbeatLock).mockReturnValueOnce(heartbeat.promise);
    const { result } = renderHook(() => useTaskLock("a"));
    await settle();
    await act(async () => vi.advanceTimersByTimeAsync(180_000));
    expect(tasksApi.heartbeatLock).toHaveBeenCalledTimes(1);
    vi.mocked(tasksApi.acquireLock).mockRejectedValueOnce(
      new ApiError(409, "Locked", { reason: "task_locked_by_other", user_id: "other" }),
    );
    await act(async () => heartbeat.reject(new Error("lost")));
    expect(result.current.isLocked).toBe(false);
    expect(result.current.lockConflict?.user_id).toBe("other");
  });
});

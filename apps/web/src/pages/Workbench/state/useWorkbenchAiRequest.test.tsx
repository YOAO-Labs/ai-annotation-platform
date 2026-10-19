import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AsyncJob } from "@/api/asyncJobs";
import {
  useWorkbenchAiRequest,
  type WorkbenchAiRequestStart,
  type WorkbenchAiRequestSummary,
} from "./useWorkbenchAiRequest";

const api = vi.hoisted(() => ({ list: vi.fn(), get: vi.fn(), cancel: vi.fn() }));
vi.mock("@/api/asyncJobs", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/api/asyncJobs")>();
  return { ...original, asyncJobsApi: { ...original.asyncJobsApi, ...api } };
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

const summary = (taskId = "task-a"): WorkbenchAiRequestSummary => ({
  projectId: "project-a",
  taskId,
  frameIndex: 12,
  backendName: "Backend at submission",
  modelName: "Model at submission",
  input: { prompt: "car", params: { threshold: 0.25 }, classes: [1, 4] },
});

const job = (overrides: Partial<AsyncJob> = {}): AsyncJob => ({
  id: "async-row-a",
  celery_task_id: "celery-task-a",
  project_id: "project-a",
  kind: "batch_predict",
  status: "running",
  progress_pct: 35,
  error_message: null,
  payload: {},
  result: {},
  user_id: "user-a",
  project_display_id: null,
  project_name: null,
  started_at: null,
  completed_at: null,
  created_at: "2026-09-07T00:00:00Z",
  updated_at: "2026-09-07T00:00:00Z",
  ...overrides,
});
const completed = { kind: "completed" } as const;
const queued = { kind: "queued", celeryTaskId: "celery-task-a" } as const;
const clients: QueryClient[] = [];

function harness(scopeKey: string | null = "task-a") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  clients.push(client);
  const onCompleted = vi
    .fn<(value: WorkbenchAiRequestSummary) => Promise<void>>()
    .mockResolvedValue(undefined);
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const initialProps: {
    scopeKey: string | null;
    onCompleted?: (value: WorkbenchAiRequestSummary) => Promise<void>;
  } = { scopeKey };
  const view = renderHook(
    (props: typeof initialProps) =>
      useWorkbenchAiRequest({ ...props, onCompleted: props.onCompleted ?? onCompleted }),
    { initialProps, wrapper },
  );
  return { ...view, client, onCompleted };
}

async function startQueued(view: ReturnType<typeof harness>, value = job()) {
  api.list.mockResolvedValue({ items: [value], total: 1 });
  api.get.mockResolvedValue(value);
  const execute = vi.fn().mockResolvedValue(queued);
  act(() => {
    view.result.current.start({ summary: summary(), execute, cancellable: false });
  });
  await waitFor(() => expect(api.get).toHaveBeenCalledWith(value.id));
  await waitFor(() =>
    expect(view.result.current.presentation.progressPct).toBe(value.progress_pct),
  );
  return execute;
}

beforeEach(() => {
  api.list.mockReset();
  api.get.mockReset();
  api.cancel.mockReset();
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
  vi.useRealTimers();
});

describe("ordinary AI request ownership", () => {
  it("claims the request synchronously and freezes the full submitted summary before capture", async () => {
    const view = harness();
    const capture = deferred<typeof completed>();
    const execute = vi.fn<WorkbenchAiRequestStart["execute"]>(() => capture.promise);
    const submitted = summary();
    let first = false;
    let duplicate = true;
    act(() => {
      first = view.result.current.start({ summary: submitted, execute, cancellable: true });
      duplicate = view.result.current.start({
        summary: summary("other"),
        execute,
        cancellable: true,
      });
    });
    expect(first).toBe(true);
    expect(duplicate).toBe(false);
    expect(execute).toHaveBeenCalledTimes(1);
    (submitted.input.params as { threshold: number }).threshold = 0.9;
    (submitted.input.classes as number[]).push(8);
    const frozen = view.result.current.presentation.summary!;
    expect(frozen.input).toEqual({ prompt: "car", params: { threshold: 0.25 }, classes: [1, 4] });
    expect(Object.isFrozen(frozen)).toBe(true);
    expect(Object.isFrozen(frozen.input.params)).toBe(true);
    expect(Object.isFrozen(frozen.input.classes)).toBe(true);
    expect(execute.mock.calls[0][0].summary).toBe(frozen);
    expect(view.result.current.presentation).toMatchObject({ status: "running", canCancel: true });
    await act(async () => capture.resolve(completed));
    expect(view.onCompleted).toHaveBeenCalledWith(frozen);
    expect(view.result.current.presentation.status).toBe("completed");
  });

  it("keeps running until the original task's candidates have refreshed", async () => {
    const view = harness();
    const refreshed = deferred<void>();
    view.onCompleted.mockReturnValue(refreshed.promise);
    await act(async () => {
      view.result.current.start({
        summary: summary(),
        execute: async () => completed,
        cancellable: true,
      });
    });
    expect(view.result.current.presentation).toMatchObject({ status: "running", canCancel: false });
    expect(view.onCompleted).toHaveBeenCalledTimes(1);
    act(() => {
      expect(
        view.result.current.start({
          summary: summary(),
          execute: async () => completed,
          cancellable: true,
        }),
      ).toBe(false);
    });
    await act(async () => refreshed.resolve());
    expect(view.result.current.presentation).toMatchObject({
      status: "completed",
      progressPct: 100,
    });
  });

  it.each(["resolve", "reject"] as const)(
    "ignores an old executor's late %s after a scope change",
    async (settle) => {
      const view = harness();
      const old = deferred<typeof completed>();
      const next = deferred<typeof completed>();
      let oldContext!: Parameters<WorkbenchAiRequestStart["execute"]>[0];
      act(() => {
        view.result.current.start({
          summary: summary(),
          execute: (context) => {
            oldContext = context;
            return old.promise;
          },
          cancellable: true,
        });
      });
      const oldStart = view.result.current.start;
      view.rerender({ scopeKey: "task-b" });
      expect(view.result.current.presentation.status).toBe("idle");
      expect(oldContext.signal.aborted).toBe(true);
      expect(oldContext.isCurrent()).toBe(false);
      act(() => {
        expect(
          oldStart({ summary: summary(), execute: async () => completed, cancellable: true }),
        ).toBe(false);
        view.result.current.start({
          summary: summary("task-b"),
          execute: () => next.promise,
          cancellable: true,
        });
      });
      await act(async () => {
        if (settle === "resolve") old.resolve(completed);
        else old.reject(new Error("old request failed"));
      });
      expect(view.onCompleted).not.toHaveBeenCalled();
      expect(view.result.current.presentation).toMatchObject({
        status: "running",
        summary: { taskId: "task-b" },
        error: null,
      });
      await act(async () => next.resolve(completed));
      expect(view.onCompleted).toHaveBeenCalledTimes(1);
      expect(view.onCompleted.mock.calls[0][0].taskId).toBe("task-b");
    },
  );

  it("does not let a delayed completion refresh change the next task's presentation", async () => {
    const view = harness();
    const refresh = deferred<void>();
    view.onCompleted.mockReturnValue(refresh.promise);
    await act(async () => {
      view.result.current.start({
        summary: summary(),
        execute: async () => completed,
        cancellable: true,
      });
    });
    view.rerender({ scopeKey: "task-b" });
    act(() => {
      view.result.current.start({
        summary: summary("task-b"),
        execute: () => new Promise(() => {}),
        cancellable: true,
      });
    });
    await act(async () => refresh.reject(new Error("old refresh failed")));
    expect(view.result.current.presentation).toMatchObject({
      status: "running",
      summary: { taskId: "task-b" },
      error: null,
    });
  });

  it("retries the original executor with the original nested input and a fresh signal", async () => {
    const view = harness();
    const submitted = summary();
    const execute = vi
      .fn<WorkbenchAiRequestStart["execute"]>()
      .mockRejectedValueOnce(new Error("backend unavailable"))
      .mockResolvedValueOnce(completed);
    await act(async () => {
      view.result.current.start({ summary: submitted, execute, cancellable: true });
    });
    expect(view.result.current.presentation).toMatchObject({
      status: "error",
      error: "backend unavailable",
      canRetry: true,
    });
    submitted.input.prompt = "new form text";
    (submitted.input.params as { threshold: number }).threshold = 0.99;
    view.rerender({ scopeKey: "task-a" });
    await act(async () => {
      expect(view.result.current.retry()).toBe(true);
    });
    expect(execute).toHaveBeenCalledTimes(2);
    expect(execute.mock.calls[1][0].summary.input).toEqual({
      prompt: "car",
      params: { threshold: 0.25 },
      classes: [1, 4],
    });
    expect(execute.mock.calls[1][0].signal).not.toBe(execute.mock.calls[0][0].signal);
    expect(execute.mock.calls[1][0].idempotencyKey).toBe(execute.mock.calls[0][0].idempotencyKey);
    expect(view.result.current.presentation.status).toBe("completed");
  });

  it("retries a failed candidate refresh without executing a second prediction", async () => {
    const view = harness();
    view.onCompleted
      .mockRejectedValueOnce(new Error("refresh offline"))
      .mockResolvedValueOnce(undefined);
    const execute = vi.fn().mockResolvedValue(completed);
    await act(async () => {
      view.result.current.start({ summary: summary(), execute, cancellable: true });
    });
    expect(view.result.current.presentation).toMatchObject({ status: "error", canRetry: true });
    expect(view.result.current.presentation.error).toContain("refresh offline");
    await act(async () => {
      view.result.current.retry();
    });
    expect(view.result.current.presentation.status).toBe("completed");
    expect(execute).toHaveBeenCalledTimes(1);
    expect(view.onCompleted).toHaveBeenCalledTimes(2);
  });

  it("retains the submission's completion handler when form state changes before retry", async () => {
    const view = harness();
    const execute = vi
      .fn()
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce(completed);
    await act(async () => {
      view.result.current.start({ summary: summary(), execute, cancellable: true });
    });
    const newFormHandler = vi.fn().mockResolvedValue(undefined);
    view.rerender({ scopeKey: "task-a", onCompleted: newFormHandler });
    await act(async () => {
      view.result.current.retry();
    });
    expect(view.onCompleted).toHaveBeenCalledTimes(1);
    expect(newFormHandler).not.toHaveBeenCalled();
  });

  it.each(["resolve", "reject"] as const)(
    "local cancellation retires a late %s without disturbing a replacement run",
    async (settle) => {
      const view = harness();
      const old = deferred<typeof completed>();
      const execute = vi.fn<WorkbenchAiRequestStart["execute"]>(() => old.promise);
      act(() => {
        view.result.current.start({ summary: summary(), execute, cancellable: true });
      });
      await act(async () => {
        await view.result.current.cancel();
      });
      expect(execute.mock.calls[0][0].signal.aborted).toBe(true);
      expect(execute.mock.calls[0][0].isCurrent()).toBe(false);
      expect(view.result.current.presentation.status).toBe("cancelled");
      act(() => {
        view.result.current.start({
          summary: { ...summary(), frameIndex: 13 },
          execute: () => new Promise(() => {}),
          cancellable: true,
        });
      });
      await act(async () => {
        if (settle === "resolve") old.resolve(completed);
        else old.reject(new Error("late abort rejection"));
      });
      expect(view.result.current.presentation).toMatchObject({
        status: "running",
        summary: { frameIndex: 13 },
        error: null,
      });
      expect(view.onCompleted).not.toHaveBeenCalled();
      expect(api.cancel).not.toHaveBeenCalled();
    },
  );

  it("does not dispatch without a scope or after unmount", async () => {
    const view = harness(null);
    const execute = vi.fn().mockResolvedValue(completed);
    act(() => {
      expect(view.result.current.start({ summary: summary(), execute, cancellable: true })).toBe(
        false,
      );
    });
    view.rerender({ scopeKey: "task-a" });
    const start = view.result.current.start;
    view.unmount();
    expect(start({ summary: summary(), execute, cancellable: true })).toBe(false);
    expect(execute).not.toHaveBeenCalled();
  });
});

describe("queued prediction job identity", () => {
  it("cannot cancel an image dispatch while the non-abortable POST is still pending", async () => {
    const view = harness();
    const response = deferred<typeof queued>();
    const execute = vi.fn<WorkbenchAiRequestStart["execute"]>(() => response.promise);
    api.list.mockResolvedValue({ items: [job()], total: 1 });
    api.get.mockResolvedValue(job());
    act(() => {
      view.result.current.start({ summary: summary(), execute, cancellable: false });
    });
    await act(async () => {
      await view.result.current.cancel();
    });
    expect(view.result.current.presentation).toMatchObject({ status: "running", canCancel: false });
    expect(execute.mock.calls[0][0].signal.aborted).toBe(false);
    expect(api.cancel).not.toHaveBeenCalled();
    await act(async () => response.resolve(queued));
    await waitFor(() => expect(view.result.current.presentation.canCancel).toBe(true));
  });

  it("finds the exact Celery task across pages instead of adopting the newest project job", async () => {
    const view = harness();
    const wrong = job({ id: "newest-other-job", celery_task_id: "other-celery" });
    api.list
      .mockResolvedValueOnce({
        items: Array.from({ length: 200 }, (_, n) => ({ ...wrong, id: `other-${n}` })),
        total: 203,
      })
      .mockResolvedValueOnce({
        items: [
          job({ id: "wrong-project", project_id: "project-b" }),
          job({ id: "wrong-kind", kind: "video_tracker" }),
          job(),
        ],
        total: 203,
      });
    api.get.mockResolvedValue(job());
    await act(async () => {
      view.result.current.start({
        summary: summary(),
        execute: async () => queued,
        cancellable: false,
      });
    });
    await waitFor(() => expect(api.get).toHaveBeenCalledWith("async-row-a"));
    expect(api.list.mock.calls).toEqual([
      [{ project_id: "project-a", kind: "batch_predict", limit: 200, offset: 0 }],
      [{ project_id: "project-a", kind: "batch_predict", limit: 200, offset: 200 }],
    ]);
    expect(api.get).not.toHaveBeenCalledWith("celery-task-a");
    expect(view.result.current.presentation).toMatchObject({
      status: "running",
      progressPct: 35,
      canCancel: true,
    });
  });

  it("offers no image cancellation until its real job has been identified", async () => {
    const view = harness();
    const mapping = deferred<{ items: AsyncJob[]; total: number }>();
    api.list.mockReturnValue(mapping.promise);
    api.get.mockResolvedValue(job());
    await act(async () => {
      view.result.current.start({
        summary: summary(),
        execute: async () => queued,
        cancellable: false,
      });
    });
    expect(view.result.current.presentation).toMatchObject({ status: "running", canCancel: false });
    await act(async () => {
      await view.result.current.cancel();
    });
    expect(api.cancel).not.toHaveBeenCalled();
    await act(async () => mapping.resolve({ items: [job()], total: 1 }));
    await waitFor(() => expect(view.result.current.presentation.canCancel).toBe(true));
  });

  it("does not cancel a server job or restart execution when its owning view rerenders or retires", async () => {
    const view = harness();
    const execute = await startQueued(view);
    view.rerender({ scopeKey: "task-a" });
    expect(view.result.current.presentation.status).toBe("running");
    expect(execute).toHaveBeenCalledTimes(1);
    view.rerender({ scopeKey: "task-b" });
    expect(view.result.current.presentation.status).toBe("idle");
    view.unmount();
    expect(api.cancel).not.toHaveBeenCalled();
  });

  it("rejects a lookup result that arrives after a new scope has started", async () => {
    const view = harness();
    const mapping = deferred<{ items: AsyncJob[]; total: number }>();
    api.list.mockReturnValue(mapping.promise);
    await act(async () => {
      view.result.current.start({
        summary: summary(),
        execute: async () => queued,
        cancellable: false,
      });
    });
    view.rerender({ scopeKey: "task-b" });
    act(() => {
      view.result.current.start({
        summary: summary("task-b"),
        execute: () => new Promise(() => {}),
        cancellable: true,
      });
    });
    await act(async () => mapping.resolve({ items: [job({ status: "completed" })], total: 1 }));
    expect(api.get).not.toHaveBeenCalled();
    expect(view.onCompleted).not.toHaveBeenCalled();
    expect(view.result.current.presentation).toMatchObject({
      status: "running",
      summary: { taskId: "task-b" },
    });
  });

  it("does not refresh candidates from a late job query after leaving and returning to the same task", async () => {
    const view = harness();
    const oldQuery = deferred<AsyncJob>();
    api.list.mockResolvedValue({ items: [job()], total: 1 });
    api.get.mockReturnValue(oldQuery.promise);
    await act(async () => {
      view.result.current.start({
        summary: summary(),
        execute: async () => queued,
        cancellable: false,
      });
    });
    await waitFor(() => expect(api.get).toHaveBeenCalledWith("async-row-a"));
    view.rerender({ scopeKey: "task-b" });
    view.rerender({ scopeKey: "task-a" });
    act(() => {
      view.result.current.start({
        summary: { ...summary(), frameIndex: 20 },
        execute: () => new Promise(() => {}),
        cancellable: true,
      });
    });
    await act(async () => oldQuery.resolve(job({ status: "completed" })));
    expect(view.onCompleted).not.toHaveBeenCalled();
    expect(view.result.current.presentation).toMatchObject({
      status: "running",
      summary: { frameIndex: 20 },
      error: null,
    });
    expect(api.cancel).not.toHaveBeenCalled();
  });

  it("keeps the submitted identity after a lookup failure and retries only that lookup", async () => {
    const view = harness();
    api.list
      .mockRejectedValueOnce(new Error("list offline"))
      .mockResolvedValueOnce({ items: [job()], total: 1 });
    api.get.mockResolvedValue(job());
    const execute = vi.fn().mockResolvedValue(queued);
    await act(async () => {
      view.result.current.start({ summary: summary(), execute, cancellable: false });
    });
    expect(view.result.current.presentation).toMatchObject({
      status: "running",
      canRetry: true,
      canCancel: false,
      summary: { taskId: "task-a" },
    });
    expect(view.result.current.presentation.error).toContain("list offline");
    await act(async () => {
      expect(view.result.current.retry()).toBe(true);
    });
    await waitFor(() => expect(view.result.current.presentation.canCancel).toBe(true));
    expect(execute).toHaveBeenCalledTimes(1);
    expect(api.list).toHaveBeenCalledTimes(2);
  });

  it("backs off but automatically completes when the exact job appears on the eighth lookup", async () => {
    vi.useFakeTimers();
    const view = harness();
    api.list.mockResolvedValue({ items: [job({ celery_task_id: "somebody-else" })], total: 1 });
    const execute = vi.fn().mockResolvedValue(queued);
    await act(async () => {
      view.result.current.start({ summary: summary(), execute, cancellable: false });
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6000);
    });
    expect(api.list).toHaveBeenCalledTimes(5);
    expect(api.get).not.toHaveBeenCalled();
    expect(view.result.current.presentation).toMatchObject({
      status: "running",
      canRetry: true,
      canCancel: false,
    });
    expect(view.result.current.presentation.error).toContain("继续自动查询");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10000);
    });
    expect(api.list).toHaveBeenCalledTimes(7);
    const lateJob = job({ status: "completed", progress_pct: 100 });
    api.list.mockResolvedValue({ items: [lateJob], total: 1 });
    api.get.mockResolvedValue(lateJob);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(api.list).toHaveBeenCalledTimes(8);
    expect(execute).toHaveBeenCalledTimes(1);
    expect(view.onCompleted).toHaveBeenCalledTimes(1);
    expect(view.result.current.presentation).toMatchObject({ status: "completed", error: null });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20000);
    });
    expect(api.list).toHaveBeenCalledTimes(8);
  });

  it("shares manual and automatic lookup singleflight and clears waiting work on unmount", async () => {
    vi.useFakeTimers();
    const view = harness();
    api.list.mockResolvedValue({ items: [], total: 0 });
    await act(async () => {
      view.result.current.start({
        summary: summary(),
        execute: async () => queued,
        cancellable: false,
      });
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6000);
    });
    const manualLookup = deferred<{ items: AsyncJob[]; total: number }>();
    api.list.mockReturnValue(manualLookup.promise);
    act(() => {
      expect(view.result.current.retry()).toBe(true);
      expect(view.result.current.retry()).toBe(false);
    });
    expect(api.list).toHaveBeenCalledTimes(6);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10000);
    });
    expect(api.list).toHaveBeenCalledTimes(6);
    view.unmount();
    await act(async () =>
      manualLookup.resolve({ items: [job({ status: "completed" })], total: 1 }),
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10000);
    });
    expect(api.list).toHaveBeenCalledTimes(6);
    expect(api.get).not.toHaveBeenCalled();
    expect(view.onCompleted).not.toHaveBeenCalled();
    expect(api.cancel).not.toHaveBeenCalled();
  });

  it("bounds each paginated lookup by its first total even when new jobs keep arriving", async () => {
    const view = harness();
    const unrelated = Array.from({ length: 200 }, (_, index) =>
      job({ id: `unrelated-${index}`, celery_task_id: `unrelated-celery-${index}` }),
    );
    api.list
      .mockResolvedValueOnce({ items: unrelated, total: 201 })
      .mockResolvedValue({ items: unrelated, total: 10000 });
    await act(async () => {
      view.result.current.start({
        summary: summary(),
        execute: async () => queued,
        cancellable: false,
      });
    });
    expect(api.list).toHaveBeenCalledTimes(2);
    expect(api.list.mock.calls.map(([params]) => params.offset)).toEqual([0, 200]);
    expect(api.get).not.toHaveBeenCalled();
    view.unmount();
  });

  it("uses the existing job query for completion and awaits the candidate refresh only once", async () => {
    const view = harness();
    await startQueued(view);
    const refresh = deferred<void>();
    view.onCompleted.mockReturnValue(refresh.promise);
    act(() => {
      view.client.setQueryData(
        ["async-job", "async-row-a"],
        job({ status: "completed", progress_pct: 100 }),
      );
    });
    await waitFor(() => expect(view.onCompleted).toHaveBeenCalledTimes(1));
    expect(view.result.current.presentation.status).toBe("running");
    act(() => {
      view.client.setQueryData(
        ["async-job", "async-row-a"],
        job({ status: "completed", progress_pct: 100, updated_at: "later" }),
      );
    });
    await act(async () => refresh.resolve());
    expect(view.onCompleted).toHaveBeenCalledTimes(1);
    expect(view.result.current.presentation).toMatchObject({
      status: "completed",
      progressPct: 100,
    });
  });

  it("recovers a job-query failure without resubmitting the prediction", async () => {
    const view = harness();
    const execute = await startQueued(view);
    api.get.mockRejectedValueOnce(new Error("job query offline"));
    await act(async () => {
      await view.client.invalidateQueries({ queryKey: ["async-job", "async-row-a"], exact: true });
    });
    await waitFor(() => expect(view.result.current.presentation.canRetry).toBe(true));
    expect(view.result.current.presentation).toMatchObject({
      status: "running",
      summary: { taskId: "task-a" },
    });
    expect(view.result.current.presentation.error).toContain("job query offline");
    api.get.mockResolvedValue(job({ status: "completed", progress_pct: 100 }));
    await act(async () => {
      expect(view.result.current.retry()).toBe(true);
    });
    await waitFor(() => expect(view.result.current.presentation.status).toBe("completed"));
    expect(execute).toHaveBeenCalledTimes(1);
    expect(view.onCompleted).toHaveBeenCalledTimes(1);
  });

  it("does not replace a confirmed completion with a later query error while candidates refresh", async () => {
    const view = harness();
    const refresh = deferred<void>();
    view.onCompleted.mockReturnValue(refresh.promise);
    api.list.mockResolvedValue({
      items: [job({ status: "completed", progress_pct: 100 })],
      total: 1,
    });
    api.get.mockRejectedValue(new Error("job query offline after completion"));
    await act(async () => {
      view.result.current.start({
        summary: summary(),
        execute: async () => queued,
        cancellable: false,
      });
    });
    await waitFor(() =>
      expect(view.client.getQueryState(["async-job", "async-row-a"])?.status).toBe("error"),
    );
    expect(view.result.current.presentation).toMatchObject({ status: "running", error: null });
    await act(async () => refresh.resolve());
    expect(view.result.current.presentation).toMatchObject({ status: "completed", error: null });
    expect(view.onCompleted).toHaveBeenCalledTimes(1);
  });

  it("does not adopt a wrong Celery identity returned by the job query", async () => {
    const view = harness();
    await startQueued(view);
    act(() => {
      view.client.setQueryData(
        ["async-job", "async-row-a"],
        job({ status: "completed", celery_task_id: "other-celery" }),
      );
    });
    await waitFor(() => expect(view.result.current.presentation.canRetry).toBe(true));
    expect(view.result.current.presentation.error).toContain("不匹配");
    expect(view.result.current.presentation.status).toBe("running");
    expect(view.onCompleted).not.toHaveBeenCalled();
  });

  it("starts a new submission with captured settings after an authoritative terminal job", async () => {
    const view = harness();
    const execute = await startQueued(view);
    act(() => {
      view.client.setQueryData(
        ["async-job", "async-row-a"],
        job({ status: "failed", error_message: "model crashed" }),
      );
    });
    await waitFor(() => expect(view.result.current.presentation.status).toBe("error"));
    expect(view.result.current.presentation).toMatchObject({
      error: "model crashed",
      canRetry: true,
      canCancel: false,
      retryCreatesNewRequest: true,
    });
    execute.mockResolvedValue(completed);
    await act(async () => {
      view.result.current.retry();
    });
    expect(execute).toHaveBeenCalledTimes(2);
    expect(execute.mock.calls[1][0].summary.input).toEqual(execute.mock.calls[0][0].summary.input);
    expect(execute.mock.calls[1][0].idempotencyKey).not.toBe(
      execute.mock.calls[0][0].idempotencyKey,
    );
    expect(view.result.current.presentation.status).toBe("completed");
  });

  it("waits for a server cancellation terminal state after a cooperative cancellation acknowledgement", async () => {
    const view = harness();
    await startQueued(view);
    api.cancel.mockResolvedValue({ status: "cancel_requested", id: "async-row-a" });
    await act(async () => {
      await view.result.current.cancel();
    });
    expect(api.cancel).toHaveBeenCalledWith("async-row-a");
    expect(view.result.current.presentation).toMatchObject({
      status: "running",
      cancelling: true,
      canCancel: false,
    });
    act(() => {
      view.client.setQueryData(["async-job", "async-row-a"], job({ status: "cancelled" }));
    });
    await waitFor(() => expect(view.result.current.presentation.status).toBe("cancelled"));
    expect(view.result.current.presentation.cancelling).toBe(false);
    expect(view.onCompleted).not.toHaveBeenCalled();
  });

  it("uses a terminal cancellation response without waiting for another poll", async () => {
    const view = harness();
    await startQueued(view, job({ status: "pending", progress_pct: 0 }));
    api.cancel.mockResolvedValue({ status: "cancelled", id: "async-row-a" });
    await act(async () => {
      await view.result.current.cancel();
    });
    expect(view.result.current.presentation).toMatchObject({
      status: "cancelled",
      cancelling: false,
    });
    expect(view.onCompleted).not.toHaveBeenCalled();
  });

  it("does not apply a delayed cancellation acknowledgement to the next scope", async () => {
    const view = harness();
    await startQueued(view);
    const acknowledgement = deferred<{ status: string; id: string }>();
    api.cancel.mockReturnValue(acknowledgement.promise);
    act(() => {
      void view.result.current.cancel();
    });
    view.rerender({ scopeKey: "task-b" });
    act(() => {
      view.result.current.start({
        summary: summary("task-b"),
        execute: () => new Promise(() => {}),
        cancellable: true,
      });
    });
    await act(async () => acknowledgement.resolve({ status: "cancelled", id: "async-row-a" }));
    expect(view.result.current.presentation).toMatchObject({
      status: "running",
      summary: { taskId: "task-b" },
      cancelling: false,
      error: null,
    });
  });

  it("makes a cancellation failure recoverable while retaining the running job", async () => {
    const view = harness();
    await startQueued(view);
    api.cancel.mockRejectedValue(new Error("cancel offline"));
    await act(async () => {
      await view.result.current.cancel();
    });
    expect(view.result.current.presentation).toMatchObject({
      status: "running",
      canRetry: true,
      canCancel: true,
      cancelling: false,
    });
    expect(view.result.current.presentation.error).toContain("cancel offline");
  });

  it("handles a job that completes while its cancellation attempt is rejected", async () => {
    const view = harness();
    await startQueued(view);
    const acknowledgement = deferred<{ status: string; id: string }>();
    api.cancel.mockReturnValue(acknowledgement.promise);
    act(() => {
      void view.result.current.cancel();
    });
    act(() => {
      view.client.setQueryData(
        ["async-job", "async-row-a"],
        job({ status: "completed", progress_pct: 100 }),
      );
    });
    await waitFor(() => expect(view.result.current.presentation.progressPct).toBe(100));
    expect(view.onCompleted).not.toHaveBeenCalled();
    await act(async () => acknowledgement.reject(new Error("cannot cancel terminal job")));
    await waitFor(() => expect(view.result.current.presentation.status).toBe("completed"));
    expect(view.onCompleted).toHaveBeenCalledTimes(1);
  });
});

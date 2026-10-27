/**
 * v0.8.3 · useAnnotationHistory hook 集成单测：push / undo / redo / pushBatch /
 * replaceAnnotationId / 切任务清栈。
 *
 * 已有 useAnnotationHistory.test.ts 测的是纯函数 applyLeaf；此处补 React hook
 * 状态机层（栈管理 / busy 锁 / taskId 切换）。
 */
import { afterEach, describe, it, expect, vi } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import {
  useAnnotationHistory,
  loadHistoryFromSession,
  type Command,
  type HistoryHandlers,
  type SliceCommand,
} from "./useAnnotationHistory";
import type {
  AnnotationSliceResponse,
  AnnotationSliceRestoreRequest,
} from "@/api/annotationSlices";

function makeHandlers(): HistoryHandlers {
  return {
    createAnnotation: vi.fn(async (_p) => ({ id: "real-id" }) as any),
    deleteAnnotation: vi.fn(async () => undefined),
    updateAnnotation: vi.fn(async () => undefined),
    removeLocalCreate: vi.fn(async () => undefined),
  };
}

describe("useAnnotationHistory · 栈状态机", () => {
  it("isolates the same task history by account, including restoration", () => {
    const handlers = makeHandlers();
    const taskId = "account-isolation-task";
    const { result, rerender, unmount } = renderHook(
      ({ userId }) => useAnnotationHistory(taskId, handlers, userId),
      { initialProps: { userId: "alice" } },
    );
    act(() =>
      result.current.push({
        kind: "create",
        annotationId: "alice-object",
        payload: {
          class_name: "car",
          geometry: {} as never,
        },
      }),
    );
    expect(result.current.canUndo).toBe(true);
    rerender({ userId: "bob" });
    expect(result.current.canUndo).toBe(false);
    expect(loadHistoryFromSession(taskId, "bob")).toBeNull();
    rerender({ userId: "alice" });
    expect(result.current.canUndo).toBe(true);
    unmount();
    expect(loadHistoryFromSession(taskId, "alice")?.undo).toHaveLength(1);
    sessionStorage.removeItem(`wb:hist:alice:${taskId}`);
  });

  it("retains a failed ordinary undo so it can be retried", async () => {
    const handlers = makeHandlers();
    vi.mocked(handlers.deleteAnnotation).mockRejectedValueOnce(new TypeError("offline"));
    const { result } = renderHook(() => useAnnotationHistory("failed-undo-task", handlers));
    act(() =>
      result.current.push({
        kind: "create",
        annotationId: "created",
        payload: {
          class_name: "car",
          geometry: {} as never,
        },
      }),
    );
    await act(async () => {
      await result.current.undo();
    });
    expect(result.current.canUndo).toBe(true);
    expect(result.current.canRedo).toBe(false);
    await act(async () => {
      await result.current.undo();
    });
    expect(result.current.canRedo).toBe(true);
  });
  it("初始 canUndo / canRedo 全 false", () => {
    const handlers = makeHandlers();
    const { result } = renderHook(() => useAnnotationHistory("t1", handlers));
    expect(result.current.canUndo).toBe(false);
    expect(result.current.canRedo).toBe(false);
  });

  it("push 一条 → canUndo true，canRedo false", () => {
    const handlers = makeHandlers();
    const { result } = renderHook(() => useAnnotationHistory("t1", handlers));
    const cmd: Command = {
      kind: "update",
      annotationId: "a1",
      before: { class_name: "old" } as any,
      after: { class_name: "new" } as any,
    };
    act(() => result.current.push(cmd));
    expect(result.current.canUndo).toBe(true);
    expect(result.current.canRedo).toBe(false);
  });

  it("undo → canRedo true；redo → canUndo true", async () => {
    const handlers = makeHandlers();
    const { result } = renderHook(() => useAnnotationHistory("t1", handlers));
    const cmd: Command = {
      kind: "update",
      annotationId: "a1",
      before: { class_name: "old" } as any,
      after: { class_name: "new" } as any,
    };
    act(() => result.current.push(cmd));

    await act(async () => {
      await result.current.undo();
    });
    await waitFor(() => expect(result.current.canRedo).toBe(true));
    expect(handlers.updateAnnotation).toHaveBeenCalledWith("a1", { class_name: "old" });
    expect(handlers.updateAnnotation).toHaveBeenCalledTimes(1);

    await act(async () => {
      await result.current.redo();
    });
    await waitFor(() => expect(result.current.canUndo).toBe(true));
    expect(handlers.updateAnnotation).toHaveBeenCalledWith("a1", { class_name: "new" });
    expect(handlers.updateAnnotation).toHaveBeenCalledTimes(2);
  });

  it("新 push 清空 redo 栈", async () => {
    const handlers = makeHandlers();
    const { result } = renderHook(() => useAnnotationHistory("t1", handlers));
    const cmd1: Command = {
      kind: "update",
      annotationId: "a1",
      before: { class_name: "x" } as any,
      after: { class_name: "y" } as any,
    };
    act(() => result.current.push(cmd1));
    await act(async () => {
      await result.current.undo();
    });
    await waitFor(() => expect(result.current.canRedo).toBe(true));

    // push 新命令 → redo 栈应清空
    act(() =>
      result.current.push({
        kind: "update",
        annotationId: "a2",
        before: { class_name: "p" } as any,
        after: { class_name: "q" } as any,
      } as Command),
    );
    expect(result.current.canRedo).toBe(false);
  });

  it("pushBatch · 单条 → 不裹 batch", () => {
    const handlers = makeHandlers();
    const { result } = renderHook(() => useAnnotationHistory("t1", handlers));
    act(() =>
      result.current.pushBatch([
        {
          kind: "delete",
          annotation: {
            id: "d1",
            annotation_type: "bbox",
            class_name: "c",
            geometry: { type: "bbox", x: 0, y: 0, w: 1, h: 1 },
          } as any,
        },
      ]),
    );
    expect(result.current.canUndo).toBe(true);
  });

  it("pushBatch · 0 条 → 不入栈", () => {
    const handlers = makeHandlers();
    const { result } = renderHook(() => useAnnotationHistory("t1", handlers));
    act(() => result.current.pushBatch([]));
    expect(result.current.canUndo).toBe(false);
  });

  it("切任务 → 清栈", async () => {
    const handlers = makeHandlers();
    const { result, rerender } = renderHook(
      ({ taskId }) => useAnnotationHistory(taskId, handlers),
      { initialProps: { taskId: "t1" } },
    );
    act(() =>
      result.current.push({
        kind: "update",
        annotationId: "a",
        before: { class_name: "x" } as any,
        after: { class_name: "y" } as any,
      } as Command),
    );
    expect(result.current.canUndo).toBe(true);

    rerender({ taskId: "t2" });
    expect(result.current.canUndo).toBe(false);
  });

  it("replaceAnnotationId 同步换栈中 tmpId", async () => {
    const handlers = makeHandlers();
    const { result } = renderHook(() => useAnnotationHistory("t1", handlers));
    act(() =>
      result.current.push({
        kind: "create",
        annotationId: "tmp_xyz",
        payload: { class_name: "c", annotation_type: "bbox", geometry: {} as any },
      }),
    );
    act(() => result.current.replaceAnnotationId("tmp_xyz", "real-1"));
    // undo 应触发 deleteAnnotation("real-1")（不再是 tmp_xyz）
    await act(async () => {
      await result.current.undo();
    });
    await waitFor(() => expect(handlers.deleteAnnotation).toHaveBeenCalledWith("real-1"));
  });
});

const sliceCommand = (): SliceCommand => ({
  kind: "slice",
  operationId: "slice-operation",
  resultVersions: { source: 2, created: 1 },
  restoreExpiresAt: "2026-10-08T00:00:00Z",
});
function receipt(payload: AnnotationSliceRestoreRequest): AnnotationSliceResponse {
  return {
    operation_id: "restore-operation",
    slice_operation_id: "slice-operation",
    source_annotation_id: "source",
    created_annotation_id: "created",
    result_versions: Object.fromEntries(
      Object.entries(payload.expected_versions).map(([id, version]) => [id, version + 1]),
    ),
    active_annotation_ids: payload.target === "before" ? ["source"] : ["source", "created"],
    target: payload.target,
    restore_expires_at: "2026-10-08T00:00:00Z",
    idempotent_replay: false,
    no_op: false,
  };
}

describe("atomic slice history", () => {
  afterEach(() => vi.unstubAllGlobals());
  it("preserves a new edit's redo invalidation while an earlier undo settles", async () => {
    let resolve!: (value: AnnotationSliceResponse) => void;
    const restoreSlice = vi.fn(
      (_task: string, _operation: string, _payload: AnnotationSliceRestoreRequest) =>
        new Promise<AnnotationSliceResponse>((done) => {
          resolve = done;
        }),
    );
    const { result } = renderHook(() =>
      useAnnotationHistory("slice-new-edit", { ...makeHandlers(), restoreSlice }),
    );
    act(() => result.current.push(sliceCommand()));
    let pending!: Promise<void>;
    act(() => {
      pending = result.current.undo();
    });
    act(() =>
      result.current.push({ kind: "update", annotationId: "later", before: {}, after: {} }),
    );
    await act(async () => {
      resolve(receipt(restoreSlice.mock.calls[0][2]));
      await pending;
    });
    expect(result.current.canRedo).toBe(false);
    expect(loadHistoryFromSession("slice-new-edit")?.undo).toEqual([
      { kind: "update", annotationId: "later", before: {}, after: {} },
    ]);
  });
  it("inserts a pending redo before edits made after it started", async () => {
    let resolve!: (value: AnnotationSliceResponse) => void;
    const restoreSlice = vi.fn(
      async (_task: string, _operation: string, payload: AnnotationSliceRestoreRequest) =>
        receipt(payload),
    );
    const { result } = renderHook(() =>
      useAnnotationHistory("slice-redo-edit", { ...makeHandlers(), restoreSlice }),
    );
    act(() => result.current.push(sliceCommand()));
    await act(async () => result.current.undo());
    restoreSlice.mockImplementationOnce(
      () =>
        new Promise<AnnotationSliceResponse>((done) => {
          resolve = done;
        }),
    );
    let pending!: Promise<void>;
    act(() => {
      pending = result.current.redo();
    });
    act(() =>
      result.current.push({ kind: "update", annotationId: "later", before: {}, after: {} }),
    );
    await act(async () => {
      resolve(receipt(restoreSlice.mock.calls[1][2]));
      await pending;
    });
    expect(loadHistoryFromSession("slice-redo-edit")?.undo.map((item) => item.kind)).toEqual([
      "slice",
      "update",
    ]);
    expect(result.current.canRedo).toBe(false);
  });
  it("moves one command only after success and keeps IDs and server-returned versions", async () => {
    vi.stubGlobal("crypto", { getRandomValues: crypto.getRandomValues.bind(crypto) });
    const restoreSlice = vi.fn(
      async (_task: string, _operation: string, payload: AnnotationSliceRestoreRequest) =>
        receipt(payload),
    );
    const { result } = renderHook(() =>
      useAnnotationHistory("slice-a", { ...makeHandlers(), restoreSlice }),
    );
    act(() => result.current.push(sliceCommand()));
    await act(async () => result.current.undo());
    expect(result.current.canRedo).toBe(true);
    expect(restoreSlice.mock.calls[0].slice(0, 2)).toEqual(["slice-a", "slice-operation"]);
    expect(restoreSlice.mock.calls[0][2]).toMatchObject({
      target: "before",
      expected_versions: { source: 2, created: 1 },
    });
    await act(async () => result.current.redo());
    expect(restoreSlice.mock.calls[1][2]).toMatchObject({
      target: "after",
      expected_versions: { source: 3, created: 2 },
    });
    expect(restoreSlice.mock.calls[1][2].idempotency_key).not.toBe(
      restoreSlice.mock.calls[0][2].idempotency_key,
    );
    expect(result.current.canUndo).toBe(true);
    expect(result.current.canRedo).toBe(false);
  });
  it("retains position and retry key on a failed restore, including across reload", async () => {
    const onSliceError = vi.fn();
    const restoreSlice = vi.fn(
      async (_task: string, _operation: string, payload: AnnotationSliceRestoreRequest) =>
        receipt(payload),
    );
    restoreSlice.mockRejectedValueOnce(new Error("response lost"));
    const handlers = { ...makeHandlers(), restoreSlice, onSliceError };
    const first = renderHook(() => useAnnotationHistory("slice-retry", handlers));
    act(() => first.result.current.push(sliceCommand()));
    await act(async () => first.result.current.undo());
    expect(first.result.current.canUndo).toBe(true);
    expect(first.result.current.canRedo).toBe(false);
    expect(onSliceError).toHaveBeenCalledOnce();
    const key = restoreSlice.mock.calls[0][2].idempotency_key;
    first.unmount();
    const second = renderHook(() => useAnnotationHistory("slice-retry", handlers));
    await act(async () => second.result.current.undo());
    expect(restoreSlice.mock.calls[1][2].idempotency_key).toBe(key);
    expect(second.result.current.canRedo).toBe(true);
  });
  it("rejects concurrent undo attempts and settles only its original task", async () => {
    let resolve!: (value: AnnotationSliceResponse) => void;
    const restoreSlice = vi.fn(
      (_task: string, _operation: string, _payload: AnnotationSliceRestoreRequest) =>
        new Promise<AnnotationSliceResponse>((done) => {
          resolve = done;
        }),
    );
    const handlers = { ...makeHandlers(), restoreSlice };
    const { result, rerender } = renderHook(({ task }) => useAnnotationHistory(task, handlers), {
      initialProps: { task: "slice-owner-a" },
    });
    act(() => result.current.push(sliceCommand()));
    let pending!: Promise<void>;
    act(() => {
      pending = result.current.undo();
    });
    await act(async () => result.current.undo());
    expect(restoreSlice).toHaveBeenCalledTimes(1);
    rerender({ task: "slice-owner-b" });
    act(() => result.current.push({ kind: "update", annotationId: "b", before: {}, after: {} }));
    await act(async () => {
      resolve(receipt(restoreSlice.mock.calls[0][2]));
      await pending;
    });
    expect(result.current.canUndo).toBe(true);
    expect(result.current.canRedo).toBe(false);
    expect(loadHistoryFromSession("slice-owner-b")?.undo[0]).toMatchObject({ annotationId: "b" });
    expect(loadHistoryFromSession("slice-owner-a")?.redo).toHaveLength(1);
    rerender({ task: "slice-owner-a" });
    expect(result.current.canRedo).toBe(true);
    expect(result.current.canUndo).toBe(false);
  });
  it("places a late commit in its original task and deduplicates the same receipt", () => {
    const { result } = renderHook(() => useAnnotationHistory("slice-current", makeHandlers()));
    act(() => {
      result.current.push(sliceCommand(), "slice-retired");
      result.current.push(sliceCommand(), "slice-retired");
    });
    expect(result.current.canUndo).toBe(false);
    expect(loadHistoryFromSession("slice-retired")?.undo).toHaveLength(1);
  });
  it("keeps the original task's failed command when the view changes during restore", async () => {
    let reject!: (reason: Error) => void;
    const onSliceError = vi.fn();
    const restoreSlice = vi.fn(
      () =>
        new Promise<AnnotationSliceResponse>((_resolve, fail) => {
          reject = fail;
        }),
    );
    const handlers = { ...makeHandlers(), restoreSlice, onSliceError };
    const { result, rerender } = renderHook(({ task }) => useAnnotationHistory(task, handlers), {
      initialProps: { task: "slice-fail-a" },
    });
    act(() => result.current.push(sliceCommand()));
    let pending!: Promise<void>;
    act(() => {
      pending = result.current.undo();
    });
    rerender({ task: "slice-fail-b" });
    await act(async () => {
      reject(new Error("version conflict"));
      await pending;
    });
    expect(result.current.canUndo).toBe(false);
    expect(onSliceError).not.toHaveBeenCalled();
    rerender({ task: "slice-fail-a" });
    expect(result.current.canUndo).toBe(true);
    expect(result.current.canRedo).toBe(false);
  });
});

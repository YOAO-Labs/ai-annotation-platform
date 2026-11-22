import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import { useCanvasDraftPersistence } from "../useCanvasDraftPersistence";
import type { CanvasDraft } from "../useWorkbenchState";
import { createDiscussionDraftStore, type DiscussionDraftStore } from "../useDiscussionDraftStore";
import type { DiscussionOrigin, DiscussionTarget } from "../discussionTypes";
import { discussionTargetKey } from "../discussionTypes";
import { canvasRecoveryKey, writeCanvasDraftRecovery } from "../discussionCanvasRecovery";

const owner = { userId: "u", sessionId: "s" };
const target: DiscussionTarget = {
  projectId: "p",
  taskId: "t1",
  kind: "annotation",
  annotationId: "a1",
};
const origin: DiscussionOrigin = { owner, target, requestId: "draw-a" };
const taskTarget: DiscussionTarget = { projectId: "p", taskId: "t1", kind: "task" };
const taskOrigin: DiscussionOrigin = { owner, target: taskTarget, requestId: "draw-task" };
const shapes = [{ type: "line" as const, points: [0, 0, 0.2, 0.2] }];
const inactive: CanvasDraft = {
  active: false,
  annotationId: null,
  shapes: [],
  stroke: "#f00",
  pendingResult: null,
};
const active: CanvasDraft = { ...inactive, active: true, annotationId: "a1", shapes, origin };

function memoryStore() {
  const drafts: Record<
    string,
    {
      target: DiscussionTarget;
      canvas_drawing: { shapes: typeof shapes } | null;
      canvasOrigin: DiscussionOrigin | null;
      canvasActive: boolean;
    }
  > = {};
  let disposed = false;
  let sendTarget: DiscussionTarget | undefined;
  const saveDrawing = vi.fn(
    (
      source: DiscussionOrigin,
      drawing: { shapes: typeof shapes } | null,
      options?: { active?: boolean },
    ) => {
      if (
        disposed ||
        source.owner.userId !== owner.userId ||
        source.owner.sessionId !== owner.sessionId
      )
        return false;
      drafts[discussionTargetKey(source.target)] = {
        target: source.target,
        canvas_drawing: drawing,
        canvasOrigin: source,
        canvasActive: options?.active ?? false,
      };
      return true;
    },
  );
  const store = {
    owner,
    isOwned: (source: DiscussionOrigin) =>
      !disposed &&
      source.owner.userId === owner.userId &&
      source.owner.sessionId === owner.sessionId,
    getSnapshot: () => ({ owner, drafts, disposed }),
    getDraft: (destination: DiscussionTarget) => drafts[discussionTargetKey(destination)],
    makeOrigin: (destination: DiscussionTarget) => ({
      owner,
      target: destination,
      requestId: "restored",
    }),
    saveDrawing,
    getSendTarget: () => sendTarget,
    setSendTarget: vi.fn((_projectId: string, _taskId: string, target: DiscussionTarget) => {
      sendTarget = target;
      return true;
    }),
    dispose: () => {
      disposed = true;
    },
  } as unknown as DiscussionDraftStore;
  return { store, saveDrawing, drafts };
}

beforeEach(() => {
  sessionStorage.clear();
  vi.useRealTimers();
});
afterEach(() => {
  sessionStorage.clear();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("useCanvasDraftPersistence scoped lifecycle", () => {
  it("does not overwrite or clear recovery when the session rejects a stale canvas origin", () => {
    const { store, saveDrawing } = memoryStore();
    saveDrawing.mockReturnValue(false);
    const newer = { shapes: [{ type: "line" as const, points: [0.1, 0.1, 0.8, 0.8] }] };
    writeCanvasDraftRecovery({ ...origin, requestId: "new-drawing" }, newer);
    const key = canvasRecoveryKey({ userId: "u", projectId: "p", taskId: "t1" }, "a1");
    const { rerender } = renderHook(
      ({ draft }: { draft: CanvasDraft }) =>
        useCanvasDraftPersistence({
          taskId: "t1",
          projectId: "p",
          store,
          canvasDraft: draft,
          beginCanvasDraft: vi.fn(),
        }),
      { initialProps: { draft: active } },
    );
    expect(JSON.parse(sessionStorage.getItem(key)!).shapes).toEqual(newer.shapes);
    rerender({
      draft: { ...inactive, origin, pendingResult: { shapes }, resultId: "stale-result" },
    });
    expect(JSON.parse(sessionStorage.getItem(key)!).shapes).toEqual(newer.shapes);
  });

  it("flushes to the original draft and scoped recovery record", () => {
    const { store, saveDrawing } = memoryStore();
    renderHook(() =>
      useCanvasDraftPersistence({
        taskId: "t1",
        projectId: "p",
        store,
        canvasDraft: active,
        beginCanvasDraft: vi.fn(),
      }),
    );
    expect(saveDrawing).toHaveBeenCalledWith(origin, { shapes }, { active: true });
    expect(
      JSON.parse(
        sessionStorage.getItem(
          canvasRecoveryKey({ userId: "u", projectId: "p", taskId: "t1" }, "a1"),
        )!,
      ),
    ).toMatchObject({ annotationId: "a1", shapes });
  });

  it("flushes then releases on task change without copying old shapes to the new task", () => {
    const { store, saveDrawing } = memoryStore();
    const release = vi.fn();
    const { rerender } = renderHook(
      ({ taskId }) =>
        useCanvasDraftPersistence({
          taskId,
          projectId: "p",
          store,
          canvasDraft: active,
          beginCanvasDraft: vi.fn(),
          releaseCanvasDraft: release,
        }),
      { initialProps: { taskId: "t1" } },
    );
    rerender({ taskId: "t2" });
    expect(release).toHaveBeenCalledTimes(1);
    expect(saveDrawing.mock.calls[saveDrawing.mock.calls.length - 1]?.[0].target.taskId).toBe("t1");
    expect(
      sessionStorage.getItem(
        canvasRecoveryKey({ userId: "u", projectId: "p", taskId: "t2" }, "a1"),
      ),
    ).toBeNull();
  });

  it("settles an empty live transaction when its task changes", () => {
    const { store, saveDrawing } = memoryStore();
    const release = vi.fn();
    const emptyActive = { ...active, shapes: [] };
    const { rerender } = renderHook(
      ({ taskId }) =>
        useCanvasDraftPersistence({
          taskId,
          projectId: "p",
          store,
          canvasDraft: emptyActive,
          beginCanvasDraft: vi.fn(),
          releaseCanvasDraft: release,
        }),
      { initialProps: { taskId: "t1" } },
    );

    rerender({ taskId: "t2" });

    expect(release).toHaveBeenCalledTimes(1);
    expect(saveDrawing).toHaveBeenCalledWith(origin, { shapes: [] }, { active: false });
    expect(
      sessionStorage.getItem(
        canvasRecoveryKey({ userId: "u", projectId: "p", taskId: "t1" }, "a1"),
      ),
    ).toBeNull();
  });

  it("retains memory across route unmount beyond the recovery TTL", () => {
    vi.useFakeTimers();
    const { store } = memoryStore();
    const first = renderHook(() =>
      useCanvasDraftPersistence({
        taskId: "t1",
        projectId: "p",
        store,
        canvasDraft: active,
        beginCanvasDraft: vi.fn(),
      }),
    );
    first.unmount();
    vi.advanceTimersByTime(6 * 60 * 1000);
    const begin = vi.fn();
    renderHook(() =>
      useCanvasDraftPersistence({
        taskId: "t1",
        projectId: "p",
        store,
        annotationIds: ["a1"],
        canvasDraft: inactive,
        beginCanvasDraft: begin,
      }),
    );
    expect(begin).toHaveBeenCalledWith("a1", { shapes }, origin);
  });

  it("waits for annotation validation and restores storage into the current session", () => {
    const { store } = memoryStore();
    writeCanvasDraftRecovery(
      { ...origin, owner: { ...owner, sessionId: "old-session" } },
      { shapes },
    );
    const begin = vi.fn();
    const { rerender } = renderHook(
      ({ annotationIds }: { annotationIds?: string[] }) =>
        useCanvasDraftPersistence({
          taskId: "t1",
          projectId: "p",
          store,
          annotationIds,
          canvasDraft: inactive,
          beginCanvasDraft: begin,
        }),
      { initialProps: {} },
    );
    expect(begin).not.toHaveBeenCalled();
    rerender({ annotationIds: ["a1"] });
    expect(begin).toHaveBeenCalledWith("a1", { shapes }, { owner, target, requestId: "restored" });
  });

  it("restores a task drawing before annotation ids load and does not revive a second target", () => {
    writeCanvasDraftRecovery(origin, { shapes });
    writeCanvasDraftRecovery(taskOrigin, { shapes });
    const { store } = memoryStore();
    const begin = vi.fn();
    const { rerender } = renderHook(
      ({ annotationIds }: { annotationIds?: string[] }) =>
        useCanvasDraftPersistence({
          taskId: "t1",
          projectId: "p",
          store,
          annotationIds,
          canvasDraft: inactive,
          beginCanvasDraft: begin,
        }),
      { initialProps: {} },
    );
    expect(begin).toHaveBeenCalledWith(
      null,
      { shapes },
      expect.objectContaining({ target: taskTarget }),
    );

    rerender({ annotationIds: ["a1"] });
    expect(begin).toHaveBeenCalledTimes(1);
  });

  it("keeps recovery on the selected target instead of crossing annotation and task drafts", () => {
    const { store } = memoryStore();
    const begin = vi.fn();
    const selectedTask: DiscussionTarget = { projectId: "p", taskId: "t1", kind: "task" };
    store.setSendTarget("p", "t1", selectedTask);
    writeCanvasDraftRecovery(origin, { shapes });
    renderHook(() =>
      useCanvasDraftPersistence({
        taskId: "t1",
        projectId: "p",
        store,
        annotationIds: ["a1"],
        canvasDraft: inactive,
        beginCanvasDraft: begin,
      }),
    );
    expect(begin).not.toHaveBeenCalled();
  });

  it("keeps a legacy annotation recovery after the shell's first empty selection", () => {
    const store = createDiscussionDraftStore({ owner });
    store.followAnnotationSelection("p", "t1", null);
    writeCanvasDraftRecovery(
      { ...origin, owner: { ...owner, sessionId: "old-session" } },
      { shapes },
    );
    const begin = vi.fn();

    renderHook(() =>
      useCanvasDraftPersistence({
        taskId: "t1",
        projectId: "p",
        store,
        annotationIds: ["a1"],
        canvasDraft: inactive,
        beginCanvasDraft: begin,
      }),
    );

    expect(begin).toHaveBeenCalledWith(
      "a1",
      { shapes },
      expect.objectContaining({ target, owner }),
    );
  });

  it("does not restore deleted targets or overwrite an existing empty memory draft", () => {
    writeCanvasDraftRecovery(origin, { shapes });
    const first = memoryStore();
    const begin = vi.fn();
    const view = renderHook(() =>
      useCanvasDraftPersistence({
        taskId: "t1",
        projectId: "p",
        store: first.store,
        annotationIds: [],
        canvasDraft: inactive,
        beginCanvasDraft: begin,
      }),
    );
    expect(begin).not.toHaveBeenCalled();
    view.unmount();
    const second = memoryStore();
    second.drafts[discussionTargetKey(target)] = {
      target,
      canvas_drawing: null,
      canvasOrigin: null,
      canvasActive: false,
    };
    renderHook(() =>
      useCanvasDraftPersistence({
        taskId: "t1",
        projectId: "p",
        store: second.store,
        annotationIds: ["a1"],
        canvasDraft: inactive,
        beginCanvasDraft: begin,
      }),
    );
    expect(begin).not.toHaveBeenCalled();
  });

  it("completes into the original draft without a mounted input and acknowledges only its result", () => {
    const { store, saveDrawing } = memoryStore();
    const consume = vi.fn();
    writeCanvasDraftRecovery(origin, { shapes });
    renderHook(() =>
      useCanvasDraftPersistence({
        taskId: "t2",
        projectId: "p",
        store,
        canvasDraft: { ...inactive, origin, resultId: "result-a", pendingResult: { shapes } },
        beginCanvasDraft: vi.fn(),
        consumeCanvasResult: consume,
      }),
    );
    expect(saveDrawing).toHaveBeenCalledWith(origin, { shapes }, { active: false });
    expect(consume).toHaveBeenCalledWith("result-a");
    expect(
      sessionStorage.getItem(
        canvasRecoveryKey({ userId: "u", projectId: "p", taskId: "t1" }, "a1"),
      ),
    ).toBeNull();
  });

  it("does not revive disposed owners and removes beforeunload listeners on exit", () => {
    const { store, saveDrawing } = memoryStore();
    const remove = vi.spyOn(window, "removeEventListener");
    const { unmount } = renderHook(() =>
      useCanvasDraftPersistence({
        taskId: "t1",
        projectId: "p",
        store,
        canvasDraft: active,
        beginCanvasDraft: vi.fn(),
      }),
    );
    const count = saveDrawing.mock.calls.length;
    store.dispose();
    unmount();
    expect(saveDrawing).toHaveBeenCalledTimes(count);
    expect(remove.mock.calls.some(([event]) => event === "beforeunload")).toBe(true);
  });
});

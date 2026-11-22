import { StrictMode, useLayoutEffect } from "react";
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MeResponse } from "@/api/auth";
import { useAuthStore } from "@/stores/authStore";
import { DiscussionDraftProvider, useDiscussionDraftStore } from "../DiscussionDraftProvider";
import { useAuthenticatedDiscussionSession } from "../useAuthenticatedDiscussionSession";
import { useCanvasDraftPersistence } from "../useCanvasDraftPersistence";
import { useWorkbenchState } from "../useWorkbenchState";
import type { DiscussionDraftStore } from "../useDiscussionDraftStore";
import type { DiscussionOrigin, DiscussionTarget } from "../discussionTypes";

const config = vi.hoisted(() => ({
  config: {},
  layout: {},
  loaded: true,
  update: vi.fn(),
  setFields: vi.fn(),
  setLayout: vi.fn(),
}));
vi.mock("../useWorkbenchConfig", () => ({ useWorkbenchConfig: () => config }));

const target: DiscussionTarget = {
  projectId: "project-a",
  taskId: "task-a",
  kind: "annotation",
  annotationId: "annotation-a",
};
const shapes = [{ type: "line" as const, points: [0, 0, 1, 1] }];
let observed: { store: DiscussionDraftStore; state: ReturnType<typeof useWorkbenchState> };

function Workbench({ taskId }: { taskId: string }) {
  const store = useDiscussionDraftStore()!;
  const state = useWorkbenchState();
  useCanvasDraftPersistence({
    projectId: "project-a",
    taskId,
    store,
    annotationIds: taskId === "task-a" ? ["annotation-a"] : ["annotation-b"],
    canvasDraft: state.canvasDraft,
    beginCanvasDraft: state.beginCanvasDraft,
    releaseCanvasDraft: state.releaseCanvasDraft,
    consumeCanvasResult: state.consumeCanvasResult,
  });
  useLayoutEffect(() => {
    observed = { store, state };
  });
  return null;
}

function Shell({ visible = true, taskId = "task-a" }: { visible?: boolean; taskId?: string }) {
  const session = useAuthenticatedDiscussionSession();
  return (
    <DiscussionDraftProvider {...session}>
      {visible && <Workbench taskId={taskId} />}
    </DiscussionDraftProvider>
  );
}

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  useAuthStore.getState().setAuth("lifecycle-test-token", { id: "user-a" } as MeResponse);
});
afterEach(() => {
  cleanup();
  useAuthStore.getState().logout();
  localStorage.clear();
  sessionStorage.clear();
  vi.restoreAllMocks();
});

describe("authenticated draft provider and Workbench canvas integration", () => {
  it("retains an unfinished drawing across route unmount beyond recovery TTL", () => {
    const clock = vi.spyOn(Date, "now").mockReturnValue(1_000_000);
    const view = render(
      <StrictMode>
        <Shell />
      </StrictMode>,
    );
    const originalStore = observed.store;
    act(() => {
      const origin = originalStore.makeOrigin(target)!;
      originalStore.setSendTarget("project-a", "task-a", target);
      originalStore.startCanvasSession(origin, null);
      observed.state.beginCanvasDraft("annotation-a", null, origin);
    });
    act(() => observed.state.appendCanvasShape(shapes[0]));
    view.rerender(
      <StrictMode>
        <Shell visible={false} />
      </StrictMode>,
    );
    expect(originalStore.getDraft(target)?.canvas_drawing).toEqual({ shapes });
    clock.mockReturnValue(1_400_000);
    view.rerender(
      <StrictMode>
        <Shell taskId="task-b" />
      </StrictMode>,
    );
    expect(observed.store).toBe(originalStore);
    expect(observed.state.canvasDraft.active).toBe(false);
    view.rerender(
      <StrictMode>
        <Shell />
      </StrictMode>,
    );
    expect(observed.state.canvasDraft.active).toBe(true);
    expect(observed.state.canvasDraft.annotationId).toBe("annotation-a");
    expect(observed.state.canvasDraft.shapes).toEqual(shapes);
  });

  it("completes into the owning draft without a mounted composer", () => {
    render(<Shell />);
    act(() => {
      const origin = observed.store.makeOrigin(target)!;
      observed.store.startCanvasSession(origin, { shapes });
      observed.state.beginCanvasDraft("annotation-a", { shapes }, origin);
    });
    act(() => observed.state.endCanvasDraft());
    expect(observed.store.getDraft(target)?.canvas_drawing).toEqual({ shapes });
    expect(observed.store.getDraft(target)?.canvasActive).toBe(false);
    expect(observed.state.canvasDraft.pendingResult).toBeNull();
    expect(observed.state.canvasDraft.active).toBe(false);
  });

  it("retires old results during batched logout and same-account login", () => {
    render(<Shell />);
    const oldStore = observed.store;
    let origin!: DiscussionOrigin;
    act(() => {
      origin = oldStore.makeOrigin(target)!;
      observed.state.beginCanvasDraft("annotation-a", { shapes }, origin);
    });
    act(() => {
      useAuthStore.getState().logout();
      useAuthStore.getState().setAuth("next-lifecycle-token", { id: "user-a" } as MeResponse);
    });
    expect(observed.store).not.toBe(oldStore);
    expect(observed.state.canvasDraft.active).toBe(false);
    expect(oldStore.acceptDrawing(origin, { shapes })).toBe(false);
    expect(observed.store.getDraft(target)).toBeUndefined();
    expect(sessionStorage.length).toBe(0);
  });
});

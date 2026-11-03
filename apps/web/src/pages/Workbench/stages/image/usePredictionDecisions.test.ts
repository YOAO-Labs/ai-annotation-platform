import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AiBox } from "../../state/transforms";
import { usePredictionDecisions } from "./usePredictionDecisions";

const authState = vi.hoisted(() => ({ userId: "user-1", authenticated: true }));
vi.mock("@/stores/authStore", () => ({
  isCurrentAuthOwner: (userId: string) => authState.authenticated && authState.userId === userId,
}));

function candidate(): AiBox {
  return {
    id: "pred-p-0",
    predictionId: "p",
    shapeIndex: 0,
    annotation_type: "bbox",
    geometry: { type: "bbox", x: 0.1, y: 0.1, w: 0.2, h: 0.2 },
    x: 0.1,
    y: 0.1,
    w: 0.2,
    h: 0.2,
    cls: "Car",
    conf: 0.9,
    source: "prediction_based",
    predictionSource: "ml_backend",
  };
}

function setup() {
  const box = candidate();
  const s = {
    selectedId: box.id,
    videoFrameIndex: 0,
    editingClass: null,
    workbenchConfig: { common: { autoAdvanceOnDecide: true } },
    setSelectedId: vi.fn(),
    setEditingClass: vi.fn(),
    setActiveClass: vi.fn(),
  };
  const args = {
    taskId: "task-1",
    projectId: "project-1",
    videoSegmentId: null,
    meUserId: "user-1",
    s,
    aiBoxes: [box],
    acceptedShapeKeys: new Set<string>(),
    isLocked: false,
    accept: vi.fn(),
    reject: vi.fn(),
    history: { push: vi.fn() },
    pushToast: vi.fn(),
    recordRecentClass: vi.fn(),
    dismiss: vi.fn(),
  };
  const view = renderHook(() => usePredictionDecisions(args as never));
  return { args, box, s, view };
}

describe("usePredictionDecisions owner identity", () => {
  beforeEach(() => {
    authState.userId = "user-1";
    authState.authenticated = true;
  });

  it("cancels a result when the project owner changes", async () => {
    let complete!: (value: unknown[]) => void;
    const setupView = setup();
    setupView.args.accept.mockReturnValue(
      new Promise((resolve) => {
        complete = resolve;
      }),
    );
    let pending!: Promise<unknown>;
    act(() => {
      pending = setupView.view.result.current.acceptPrediction(setupView.box);
    });

    setupView.args.projectId = "project-2";
    setupView.view.rerender();
    await act(async () => {
      complete([]);
      await pending;
    });

    expect(setupView.args.history.push).not.toHaveBeenCalled();
    expect(setupView.s.setSelectedId).not.toHaveBeenCalled();
  });

  it("drops late callbacks when credentials are replaced in place", async () => {
    let complete!: (value: unknown[]) => void;
    const setupView = setup();
    setupView.args.accept.mockReturnValue(
      new Promise((resolve) => {
        complete = resolve;
      }),
    );
    let pending!: Promise<unknown>;
    act(() => {
      pending = setupView.view.result.current.acceptPrediction(setupView.box);
    });

    authState.userId = "user-2";
    await act(async () => {
      complete([]);
      await pending;
    });

    expect(setupView.args.history.push).not.toHaveBeenCalled();
    expect(setupView.s.setSelectedId).not.toHaveBeenCalled();
  });

  it("does not overlap batch writes to the same task", async () => {
    const setupView = setup();
    const second = { ...candidate(), id: "pred-q-0", predictionId: "q" };
    setupView.args.aiBoxes.push(second);
    let complete!: (value: unknown[]) => void;
    setupView.args.accept
      .mockReturnValueOnce(
        new Promise((resolve) => {
          complete = resolve;
        }),
      )
      .mockResolvedValueOnce([{ id: "annotation-q" }]);
    let pending!: Promise<unknown>;
    act(() => {
      pending = setupView.view.result.current.acceptAll([setupView.box, second]);
    });
    expect(setupView.args.accept).toHaveBeenCalledTimes(1);

    await act(async () => {
      complete([{ id: "annotation-p" }]);
      expect(await pending).toEqual([{ status: "success" }, { status: "success" }]);
    });
    expect(
      setupView.args.history.push.mock.calls.map(([entry]) => entry.createdAnnotationIds),
    ).toEqual([["annotation-p"], ["annotation-q"]]);
  });

  it.each(["task", "account"])("does not drain a retired %s's batch", async (changedOwner) => {
    const setupView = setup();
    const second = { ...candidate(), id: "pred-q-0", predictionId: "q" };
    setupView.args.aiBoxes.push(second);
    let complete!: (value: unknown[]) => void;
    setupView.args.accept.mockReturnValue(
      new Promise((resolve) => {
        complete = resolve;
      }),
    );
    let pending!: Promise<unknown>;
    act(() => {
      pending = setupView.view.result.current.acceptAll([setupView.box, second]);
    });
    if (changedOwner === "task") {
      setupView.args.taskId = "task-2";
      setupView.view.rerender();
    } else {
      authState.userId = "user-2";
    }
    await act(async () => {
      complete([{ id: "annotation-p" }]);
      expect(await pending).toBeNull();
    });
    expect(setupView.args.accept).toHaveBeenCalledTimes(1);
    expect(setupView.args.history.push).not.toHaveBeenCalled();
  });

  it("does not start accept, reject, or batch writes after auth replacement", async () => {
    const setupView = setup();
    authState.userId = "user-2";

    await act(async () => {
      expect(await setupView.view.result.current.acceptPrediction(setupView.box)).toEqual({
        status: "ignored",
      });
      expect(await setupView.view.result.current.rejectPrediction(setupView.box)).toEqual({
        status: "ignored",
      });
      expect(await setupView.view.result.current.acceptAll([setupView.box])).toBeNull();
    });

    expect(setupView.args.accept).not.toHaveBeenCalled();
    expect(setupView.args.reject).not.toHaveBeenCalled();
  });
});

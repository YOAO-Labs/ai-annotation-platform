import { act, renderHook } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { usePredictionPropagation } from "./usePredictionPropagation";

vi.mock("@/api/tasks", () => ({
  tasksApi: {
    getNeighbors: vi.fn(),
    propagateToTask: vi.fn(),
    propagateBatch: vi.fn(),
    interpolateRange: vi.fn(),
  },
}));
vi.mock("@/components/ui/Toast", () => ({
  useToastStore: { getState: () => ({ push: () => {} }) },
}));

import { tasksApi } from "@/api/tasks";

const NEIGHBORS = {
  scene_id: "s",
  scene_name: "sc",
  frame_index: 2,
  scene_total_frames: 5,
  prev: [{ task_id: "t1", frame_index: 1 }],
  next: [{ task_id: "t3", frame_index: 3 }],
};

function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
}

function setup(initialTaskId: string | undefined = "A") {
  const navigate = vi.fn(async () => true);
  const pushToast = vi.fn();
  const queryClient = new QueryClient();
  vi.spyOn(queryClient, "invalidateQueries").mockImplementation(() => Promise.resolve());
  const view = renderHook(
    ({ taskId }: { taskId: string | undefined }) =>
      usePredictionPropagation({
        taskId,
        selectedId: "sel-1",
        navigateToCrossFrameTask: navigate,
        pushToast,
        queryClient,
      }),
    { initialProps: { taskId: initialTaskId } },
  );
  return { ...view, navigate, pushToast };
}

describe("usePredictionPropagation · 跨帧守卫", () => {
  beforeEach(() => {
    vi.mocked(tasksApi.getNeighbors).mockResolvedValue(NEIGHBORS as never);
  });
  afterEach(() => vi.clearAllMocks());

  it("起始 task 仍是当前 task → 传播后导航 + 预约补选", async () => {
    vi.mocked(tasksApi.propagateToTask).mockResolvedValue({
      annotation: { id: "new-ann" },
      motion_compensated: true,
    } as never);
    const { result, navigate } = setup("A");
    await act(async () => {
      await result.current.crossFramePropagate("next");
    });
    expect(navigate).toHaveBeenCalledWith("t3");
    expect(result.current.pendingCrossFrameSelectRef.current).toEqual({
      taskId: "t3",
      annotationId: "new-ann",
    });
  });

  it("传播进行中用户手切 task → 不抢导航、不预约补选", async () => {
    const d = deferred<{ annotation: { id: string }; motion_compensated: boolean }>();
    vi.mocked(tasksApi.propagateToTask).mockReturnValue(d.promise as never);
    const { result, rerender, navigate } = setup("A");

    let pending!: Promise<void>;
    act(() => {
      pending = result.current.crossFramePropagate("next");
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    rerender({ taskId: "B" });
    await act(async () => {
      d.resolve({ annotation: { id: "new-ann" }, motion_compensated: true });
      await pending;
    });

    expect(navigate).not.toHaveBeenCalled();
    expect(result.current.pendingCrossFrameSelectRef.current).toBeNull();
  });

  it("离开守卫拒绝导航时不留下过期补选目标", async () => {
    vi.mocked(tasksApi.propagateToTask).mockResolvedValue({
      annotation: { id: "new-ann" },
      motion_compensated: true,
    } as never);
    const { result, navigate } = setup("A");
    navigate.mockResolvedValue(false);

    await act(async () => {
      await result.current.crossFramePropagate("next");
    });

    expect(navigate).toHaveBeenCalledWith("t3");
    expect(result.current.pendingCrossFrameSelectRef.current).toBeNull();
  });

  it("并发守卫:in-flight 期间二次触发被忽略", async () => {
    const d = deferred<{ annotation: { id: string }; motion_compensated: boolean }>();
    vi.mocked(tasksApi.propagateToTask).mockReturnValue(d.promise as never);
    const { result } = setup("A");
    let first!: Promise<void>;
    act(() => {
      first = result.current.crossFramePropagate("next");
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    await act(async () => {
      await result.current.crossFramePropagate("next");
    });
    expect(tasksApi.getNeighbors).toHaveBeenCalledTimes(1);
    await act(async () => {
      d.resolve({ annotation: { id: "x" }, motion_compensated: true });
      await first;
    });
  });
});

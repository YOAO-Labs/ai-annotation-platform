// Alt 拖动父子联动: handleCommitMove 的 batch 命令内容 / 顺序单测。
// 守护 v0.20.15 的两条不变量:
//   1. 单次 pushBatch 承担父+子, 一次撤销回到全部原位置。
//   2. Leaf 顺序 = [父, 子1, 子2, ...], undo/redo 按此顺序回放。
// 前端 render 时的 userBoxes 冻结问题 (Med#6) 在 ImageStage 侧构造 childMoves, 本层
// 只负责把 childMoves 原样折进 batch, 因此本单测钉的是"折入 batch 的形状"。

import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { useWorkbenchAnnotationActions } from "./useWorkbenchAnnotationActions";
import type { BboxGeometry, Geometry } from "@/types";

vi.mock("@/stores/authStore", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/stores/authStore")>()),
  isCurrentAuthOwner: (userId: string) => userId === "U-1",
}));

function bboxGeom(x: number, y: number, w = 0.2, h = 0.2): BboxGeometry {
  return { type: "bbox", x, y, w, h };
}

describe("useWorkbenchAnnotationActions · Alt 拖动父子联动", () => {
  it("handleCommitMove 收到 childMoves 时, pushBatch 一次落 [父, 子1, 子2] 顺序 leaves", () => {
    const pushBatch = vi.fn();
    const push = vi.fn();
    const updateMutate = vi.fn();
    const pushToast = vi.fn();
    const enqueueOnError = vi.fn();

    const stubState = {
      setSelectedId: vi.fn(),
      selectedIds: [],
      replaceSelected: vi.fn(),
    } as unknown as Parameters<typeof useWorkbenchAnnotationActions>[0]["s"];

    const stubQueryClient = {
      setQueryData: vi.fn(),
      getQueryData: vi.fn(),
    } as unknown as Parameters<typeof useWorkbenchAnnotationActions>[0]["queryClient"];

    const { result } = renderHook(() =>
      useWorkbenchAnnotationActions({
        taskId: "T-1",
        projectId: "P-1",
        meUserId: "U-1",
        queryClient: stubQueryClient,
        history: {
          pushBatch,
          push,
          canUndo: false,
          canRedo: false,
          undo: vi.fn(),
          redo: vi.fn(),
          clear: vi.fn(),
          replaceLastAfterId: vi.fn(),
        } as unknown as Parameters<typeof useWorkbenchAnnotationActions>[0]["history"],
        s: stubState,
        pushToast,
        recordRecentClass: vi.fn(),
        mutations: {
          create: { mutate: vi.fn() },
          update: { mutate: updateMutate },
          delete: { mutate: vi.fn() },
        },
        enqueueOnError,
        annotationsRef: { current: [] },
      }),
    );

    const parentBefore = { x: 0.1, y: 0.1, w: 0.2, h: 0.2 };
    const parentAfter = { x: 0.3, y: 0.3, w: 0.2, h: 0.2 };
    const c1Before: Geometry = bboxGeom(0.12, 0.12);
    const c1After: Geometry = bboxGeom(0.32, 0.32);
    const c2Before: Geometry = bboxGeom(0.15, 0.15);
    const c2After: Geometry = bboxGeom(0.35, 0.35);

    act(() => {
      result.current.handleCommitMove("P1", parentBefore, parentAfter, [
        { id: "C1", before: c1Before, after: c1After },
        { id: "C2", before: c2Before, after: c2After },
      ]);
    });

    // Persisted acceptance owns history; responses may finish out of order.
    expect(pushBatch).not.toHaveBeenCalled();
    act(() => {
      updateMutate.mock.calls[2][1].onSuccess();
      updateMutate.mock.calls[0][1].onSuccess();
    });
    expect(pushBatch).not.toHaveBeenCalled();
    act(() => updateMutate.mock.calls[1][1].onSuccess());

    // 单次 pushBatch, 不逐条 push。
    expect(pushBatch).toHaveBeenCalledOnce();
    expect(push).not.toHaveBeenCalled();

    const leaves = pushBatch.mock.calls[0][0];
    expect(leaves).toHaveLength(3);
    // 顺序钉死: 父先, 子按传入顺序在后。
    expect(leaves[0]).toMatchObject({
      kind: "update",
      annotationId: "P1",
    });
    expect(leaves[0].before.geometry).toMatchObject({ type: "bbox", x: 0.1, y: 0.1 });
    expect(leaves[0].after.geometry).toMatchObject({ type: "bbox", x: 0.3, y: 0.3 });
    expect(leaves[1]).toMatchObject({ kind: "update", annotationId: "C1" });
    expect(leaves[1].before.geometry).toEqual(c1Before);
    expect(leaves[1].after.geometry).toEqual(c1After);
    expect(leaves[2]).toMatchObject({ kind: "update", annotationId: "C2" });
    expect(leaves[2].before.geometry).toEqual(c2Before);
    expect(leaves[2].after.geometry).toEqual(c2After);

    // update.mutate 各 leaf 一次, 参数 annotationId 顺序也是父 → 子1 → 子2。
    expect(updateMutate).toHaveBeenCalledTimes(3);
    expect(updateMutate.mock.calls[0][0].annotationId).toBe("P1");
    expect(updateMutate.mock.calls[1][0].annotationId).toBe("C1");
    expect(updateMutate.mock.calls[2][0].annotationId).toBe("C2");

    // toast: 联动数以 childMoves.length 报数。
    expect(pushToast).toHaveBeenCalledWith(
      expect.objectContaining({ msg: expect.stringContaining("2 个子框") }),
    );
  });

  it("handleCommitMove 无 childMoves (未按 Alt) 走单 update, 不进 batch", () => {
    const pushBatch = vi.fn();
    const push = vi.fn();
    const updateMutate = vi.fn();

    const stubState = {
      setSelectedId: vi.fn(),
      selectedIds: [],
      replaceSelected: vi.fn(),
    } as unknown as Parameters<typeof useWorkbenchAnnotationActions>[0]["s"];

    const stubQueryClient = {
      setQueryData: vi.fn(),
      getQueryData: vi.fn(),
    } as unknown as Parameters<typeof useWorkbenchAnnotationActions>[0]["queryClient"];

    const { result } = renderHook(() =>
      useWorkbenchAnnotationActions({
        taskId: "T-1",
        projectId: "P-1",
        meUserId: "U-1",
        queryClient: stubQueryClient,
        history: {
          pushBatch,
          push,
          canUndo: false,
          canRedo: false,
          undo: vi.fn(),
          redo: vi.fn(),
          clear: vi.fn(),
          replaceLastAfterId: vi.fn(),
        } as unknown as Parameters<typeof useWorkbenchAnnotationActions>[0]["history"],
        s: stubState,
        pushToast: vi.fn(),
        recordRecentClass: vi.fn(),
        mutations: {
          create: { mutate: vi.fn() },
          update: { mutate: updateMutate },
          delete: { mutate: vi.fn() },
        },
        enqueueOnError: vi.fn(),
        annotationsRef: { current: [] },
      }),
    );

    act(() => {
      result.current.handleCommitMove(
        "P1",
        { x: 0.1, y: 0.1, w: 0.2, h: 0.2 },
        { x: 0.3, y: 0.3, w: 0.2, h: 0.2 },
      );
    });

    expect(pushBatch).not.toHaveBeenCalled();
    expect(updateMutate).toHaveBeenCalledOnce();
    expect(updateMutate.mock.calls[0][0].annotationId).toBe("P1");
    expect(push).not.toHaveBeenCalled();
    act(() => updateMutate.mock.calls[0][1].onSuccess());
    expect(push).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "update", annotationId: "P1" }),
    );
  });
});

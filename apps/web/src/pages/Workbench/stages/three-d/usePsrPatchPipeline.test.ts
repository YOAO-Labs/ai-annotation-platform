import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { usePsrPatchPipeline } from "./usePsrPatchPipeline";
import type { useUpdateAnnotation } from "@/hooks/useTasks";
import type { useThreeDHistory } from "./useThreeDHistory";
import type { AnnotationResponse } from "@/types";
import type { PsrField } from "./ThreeDWorkbench.helpers";

const VALID: Record<PsrField, string> = {
  cx: "1",
  cy: "2",
  cz: "3",
  l: "4",
  w: "5",
  h: "6",
  yaw: "0",
  pitch: "0",
  roll: "0",
};
const INVALID: Record<PsrField, string> = { ...VALID, h: "0" };

function boxAnn(): AnnotationResponse {
  return {
    id: "a1",
    geometry: { type: "box_3d", center: [0, 0, 0], size: [1, 1, 1], rotation: [0, 0, 0] },
  } as unknown as AnnotationResponse;
}

function mocks() {
  const update = { mutate: vi.fn() } as unknown as ReturnType<typeof useUpdateAnnotation>;
  const history = { commit: vi.fn() } as unknown as ReturnType<typeof useThreeDHistory>;
  return { update, history };
}

function setup(overrides: Partial<Parameters<typeof usePsrPatchPipeline>[0]> = {}) {
  const { update, history } = mocks();
  const props = {
    selectedId: "a1" as string | null,
    selectedAnn: boxAnn(),
    axisConvention: "iso_8855" as const,
    updateAnnotation: update,
    history,
    ...overrides,
  };
  const { result } = renderHook(() => usePsrPatchPipeline(props));
  return { result, update, history };
}

describe("usePsrPatchPipeline", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("有效表单 250ms 后 PATCH 并入 history(box_3d)", () => {
    const { result, history } = setup();
    act(() => result.current.schedulePatch(VALID));
    expect(result.current.hasPendingPatch).toBe(true);
    expect(history.commit).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(250));
    expect(result.current.hasPendingPatch).toBe(false);
    expect(history.commit).toHaveBeenCalledTimes(1);
    expect(history.commit).toHaveBeenCalledWith([
      expect.objectContaining({
        kind: "update",
        annotationId: "a1",
        before: expect.objectContaining({ geometry: expect.anything() }),
        after: expect.objectContaining({ geometry: expect.anything() }),
      }),
    ]);
  });

  it("非法表单(尺寸 0)不提交", () => {
    const { result, update } = setup();
    act(() => result.current.schedulePatch(INVALID));
    expect(result.current.hasInvalidDraft).toBe(true);
    act(() => vi.advanceTimersByTime(250));
    expect(update.mutate).not.toHaveBeenCalled();
  });

  it("非法新值取消之前的有效 pending，计时和卸载都不提交旧表单", () => {
    const { update, history } = mocks();
    const { result, unmount } = renderHook(() =>
      usePsrPatchPipeline({
        selectedId: "a1",
        selectedAnn: boxAnn(),
        axisConvention: "iso_8855",
        updateAnnotation: update,
        history,
      }),
    );
    act(() => result.current.schedulePatch(VALID));
    act(() => result.current.schedulePatch(INVALID));
    expect(result.current.hasPendingPatch).toBe(false);
    expect(result.current.hasInvalidDraft).toBe(true);
    act(() => vi.advanceTimersByTime(250));
    unmount();
    expect(update.mutate).not.toHaveBeenCalled();
    expect(history.commit).not.toHaveBeenCalled();
  });

  it("失焦恢复或外部表单重置后直接反映有效性，不自动提交", () => {
    const { update, history } = mocks();
    const { result, rerender } = renderHook(
      ({ form }) =>
        usePsrPatchPipeline({
          selectedId: "a1",
          selectedAnn: boxAnn(),
          axisConvention: "iso_8855",
          updateAnnotation: update,
          history,
          form,
        }),
      { initialProps: { form: INVALID } },
    );
    expect(result.current.hasInvalidDraft).toBe(true);
    rerender({ form: VALID });
    expect(result.current.hasInvalidDraft).toBe(false);
    expect(update.mutate).not.toHaveBeenCalled();
  });

  it("修正非法值后恢复防抖提交，保存状态和错误向调用方暴露", () => {
    const error = new Error("save failed");
    const update = {
      mutate: vi.fn(),
      isPending: true,
      error,
    } as unknown as ReturnType<typeof useUpdateAnnotation>;
    const { result, history } = setup({ updateAnnotation: update });
    act(() => result.current.schedulePatch(INVALID));
    act(() => result.current.schedulePatch(VALID));
    expect(result.current.hasInvalidDraft).toBe(false);
    expect(result.current.hasPendingPatch).toBe(true);
    expect(result.current.isSaving).toBe(true);
    expect(result.current.saveError).toBe(error);
    act(() => vi.advanceTimersByTime(250));
    expect(history.commit).toHaveBeenCalledTimes(1);
  });

  it("连续调用只触发最后一次(250ms 防抖)", () => {
    const { result, history } = setup();
    act(() => {
      result.current.schedulePatch(VALID);
      result.current.schedulePatch({ ...VALID, cx: "9" });
    });
    act(() => vi.advanceTimersByTime(250));
    expect(history.commit).toHaveBeenCalledTimes(1);
  });

  it("无 selectedId 时为 no-op", () => {
    const { result, update } = setup({ selectedId: null });
    act(() => result.current.schedulePatch(VALID));
    act(() => vi.advanceTimersByTime(250));
    expect(update.mutate).not.toHaveBeenCalled();
  });

  it("非 box_3d 几何仍 PATCH 但不入 history", () => {
    const ann = {
      id: "a1",
      geometry: { type: "point_mask_3d", point_indices: [] },
    } as unknown as AnnotationResponse;
    const { result, update, history } = setup({ selectedAnn: ann });
    act(() => result.current.schedulePatch(VALID));
    act(() => vi.advanceTimersByTime(250));
    expect(update.mutate).toHaveBeenCalledTimes(1);
    expect(history.commit).not.toHaveBeenCalled();
  });

  it("unmount 时 flush 未到 250ms 的防抖 PATCH(不丢编辑)", () => {
    const { update, history } = mocks();
    const { result, unmount } = renderHook(() =>
      usePsrPatchPipeline({
        selectedId: "a1",
        selectedAnn: boxAnn(),
        axisConvention: "iso_8855",
        updateAnnotation: update,
        history,
      }),
    );
    act(() => result.current.schedulePatch(VALID));
    expect(history.commit).not.toHaveBeenCalled();
    act(() => unmount());
    expect(history.commit).toHaveBeenCalledTimes(1);
  });

  it("切 selectedId 时 flush 上一对象 pending 编辑后不重复提交", () => {
    const { update, history } = mocks();
    let selectedId: string | null = "a1";
    const { result, rerender } = renderHook(() =>
      usePsrPatchPipeline({
        selectedId,
        selectedAnn: boxAnn(),
        axisConvention: "iso_8855",
        updateAnnotation: update,
        history,
      }),
    );
    act(() => result.current.schedulePatch(VALID));
    selectedId = "a2";
    act(() => rerender());
    expect(history.commit).toHaveBeenCalledTimes(1);
    expect(history.commit).toHaveBeenCalledWith([expect.objectContaining({ annotationId: "a1" })]);
    act(() => vi.advanceTimersByTime(250));
    expect(history.commit).toHaveBeenCalledTimes(1);
  });
});

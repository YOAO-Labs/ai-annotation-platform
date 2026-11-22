import { act, renderHook } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import type { MutableRefObject } from "react";

import { useConflictResolution } from "./useConflictResolution";

function setup(taskId: string | undefined = "T1") {
  const conflictCbRef = {
    current: () => {},
  } as unknown as MutableRefObject<(annotationId: string, version: number) => void>;
  const queryClient = new QueryClient();
  const invalidate = vi
    .spyOn(queryClient, "invalidateQueries")
    .mockImplementation(() => Promise.resolve());
  const view = renderHook(() => useConflictResolution(conflictCbRef, queryClient, taskId));
  return { ...view, conflictCbRef, invalidate };
}

describe("useConflictResolution", () => {
  it("初始关闭;conflictCbRef 接到 handleConflict,触发后弹层打开", () => {
    const { result, conflictCbRef } = setup();
    expect(result.current.conflictOpen).toBe(false);
    act(() => conflictCbRef.current("ann-1", 3));
    expect(result.current.conflictOpen).toBe(true);
  });

  it("reload:关闭弹层并失效该 task 标注缓存", () => {
    const { result, conflictCbRef, invalidate } = setup("T7");
    act(() => conflictCbRef.current("ann-1", 1));
    act(() => result.current.handleConflictReload());
    expect(result.current.conflictOpen).toBe(false);
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["annotations", "T7"] });
  });

  it("overwrite:仅关闭弹层,不失效缓存", () => {
    const { result, conflictCbRef, invalidate } = setup();
    act(() => conflictCbRef.current("ann-1", 1));
    act(() => result.current.handleConflictOverwrite());
    expect(result.current.conflictOpen).toBe(false);
    expect(invalidate).not.toHaveBeenCalled();
  });
});

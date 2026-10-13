import { describe, it, expect } from "vitest";
import { renderHook, act } from "@testing-library/react";
import type { MLBackendResponse } from "@/types";
import {
  useBatchBackendSelection,
  type UseBatchBackendSelectionParams,
} from "./useBatchBackendSelection";

/** Typed backend factory: only the fields batch selection reads are varied. */
function makeBackend(id: string, name: string): MLBackendResponse {
  return {
    id,
    project_id: "p1",
    name,
    url: `http://${id}.example.test`,
    state: "connected",
    is_interactive: false,
    auth_method: "none",
    extra_params: {},
    error_message: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

const b1 = makeBackend("b1", "Backend One");
const b2 = makeBackend("b2", "Backend Two");
const b3 = makeBackend("b3", "Backend Three");

const base: UseBatchBackendSelectionParams = {
  projectId: "p1",
  projectDefaultBackendId: null,
  backends: [b1, b2, b3],
};

describe("useBatchBackendSelection", () => {
  it("initializes from the project default, falling back to the first backend", () => {
    const withDefault = renderHook(
      (p: UseBatchBackendSelectionParams) => useBatchBackendSelection(p),
      { initialProps: { ...base, projectDefaultBackendId: "b2" } },
    );
    expect(withDefault.result.current.batchBackendId).toBe("b2");
    withDefault.unmount();

    const withoutDefault = renderHook(
      (p: UseBatchBackendSelectionParams) => useBatchBackendSelection(p),
      { initialProps: { ...base, projectDefaultBackendId: null } },
    );
    expect(withoutDefault.result.current.batchBackendId).toBe("b1");
  });

  it("keeps a manual pick when the project default and list order both change", () => {
    const { result, rerender } = renderHook(
      (p: UseBatchBackendSelectionParams) => useBatchBackendSelection(p),
      { initialProps: { ...base, projectDefaultBackendId: "b1" } },
    );
    expect(result.current.batchBackendId).toBe("b1");

    act(() => result.current.selectBatchBackend("b2"));
    expect(result.current.batchBackendId).toBe("b2");

    // 另一 Tab 把项目默认后端改成 b1、列表顺序变为 b3 在首: 手动选择不被静默重置
    // (默认跟随会得到 b1, 首位回落会得到 b3, 只有手动粘滞才是 b2)。
    rerender({ projectId: "p1", projectDefaultBackendId: "b1", backends: [b3, b1, b2] });
    expect(result.current.batchBackendId).toBe("b2");
  });

  it("resets the manual flag when switching project and follows the new default", () => {
    const { result, rerender } = renderHook(
      (p: UseBatchBackendSelectionParams) => useBatchBackendSelection(p),
      { initialProps: { ...base, projectDefaultBackendId: "b1" } },
    );
    act(() => result.current.selectBatchBackend("b2"));
    expect(result.current.batchBackendId).toBe("b2");

    rerender({ ...base, projectId: "p2", projectDefaultBackendId: "b1" });
    expect(result.current.batchBackendId).toBe("b1");

    // 新项目内默认变化继续跟随 (未手动选过)。
    rerender({ ...base, projectId: "p2", projectDefaultBackendId: "b2" });
    expect(result.current.batchBackendId).toBe("b2");
  });

  it("resolves the selected backend object for the current pick", () => {
    const { result } = renderHook(
      (p: UseBatchBackendSelectionParams) => useBatchBackendSelection(p),
      { initialProps: { ...base, projectDefaultBackendId: "b2" } },
    );
    expect(result.current.selectedBackend?.id).toBe("b2");
    expect(result.current.selectedBackend?.name).toBe("Backend Two");
  });
});

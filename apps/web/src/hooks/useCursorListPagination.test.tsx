import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useCursorListPagination } from "./useCursorListPagination";

describe("cursor history ownership", () => {
  it("restores previous cursors and starts from the beginning when row count changes", () => {
    const { result } = renderHook(() => useCursorListPagination("project-a:filter-a"));
    act(() => result.current.changePage(1, "cursor-20"));
    act(() => result.current.changePage(2, "cursor-40"));
    expect(result.current.cursor).toBe("cursor-40");
    act(() => result.current.changePage(1));
    expect(result.current.cursor).toBe("cursor-20");
    act(() => result.current.setPageSize(50));
    expect(result.current.pageSize).toBe(50);
    expect(result.current.cursor).toBeNull();
    expect(result.current.page).toBe(0);
  });

  it("drops history before querying a new scope and rejects a retired callback", () => {
    const { result, rerender } = renderHook(({ scope }) => useCursorListPagination(scope), {
      initialProps: { scope: "project-a:filter-a" },
    });
    act(() => result.current.changePage(1, "cursor-a"));
    const retired = result.current.changePage;
    rerender({ scope: "project-b:filter-b" });
    expect(result.current.cursor).toBeNull();
    act(() => retired(2, "retired-cursor"));
    expect(result.current.cursor).toBeNull();
    expect(result.current.page).toBe(0);
  });
});

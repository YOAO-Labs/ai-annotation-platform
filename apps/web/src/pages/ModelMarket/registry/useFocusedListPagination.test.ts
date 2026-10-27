import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useFocusedListPagination } from "./useFocusedListPagination";

describe("registry object pagination", () => {
  it("waits for data then locates a deep-linked object outside the first page", () => {
    const items = Array.from({ length: 43 }, (_, index) => ({ id: String(index) }));
    const { result, rerender } = renderHook(
      ({ rows }) => useFocusedListPagination(rows, "", "42", (item) => item.id),
      { initialProps: { rows: [] as typeof items } },
    );
    rerender({ rows: items });
    expect(result.current.page).toBe(2);
    expect(result.current.items.map((item) => item.id)).toContain("42");
    act(() => result.current.setPageSize(10));
    expect(result.current.page).toBe(0);
    expect(result.current.items).toHaveLength(10);
    act(() => result.current.setPage(4));
    rerender({ rows: items.slice(0, 1) });
    expect(result.current.page).toBe(0);
  });
});

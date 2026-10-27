import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useInfiniteListPagination } from "./useInfiniteListPagination";
import { useListPageState } from "./useListPageState";

const pages: Array<{ items: string[]; total?: number; next_cursor: string | null }> = [
  { items: ["first"], total: 45, next_cursor: "cursor-20" },
  { items: ["second"], next_cursor: "cursor-40" },
  { items: ["last"], next_cursor: null },
];
function deferred() {
  let resolve!: (value: unknown) => void;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function useHarness({
  scope,
  data,
  fetchNextPage,
}: {
  scope: string;
  data: typeof pages;
  fetchNextPage: () => Promise<unknown>;
}) {
  const state = useListPageState(scope);
  return useInfiniteListPagination(
    scope,
    { data: { pages: data }, fetchNextPage, isFetchingNextPage: false },
    state,
  );
}

describe("cursor list pages", () => {
  it("shows one actual page and restores cached history without refetching", async () => {
    const fetchNextPage = vi.fn();
    const { result } = renderHook(useHarness, {
      initialProps: { scope: "project-a", data: pages, fetchNextPage },
    });
    expect(result.current.items).toEqual(["first"]);
    await act(() => result.current.onPageChange(1));
    expect(result.current.items).toEqual(["second"]);
    expect(result.current.total).toBe(45);
    await act(() => result.current.onPageChange(2));
    expect(result.current.items).toEqual(["last"]);
    expect(result.current.hasNext).toBe(false);
    await act(() => result.current.onPageChange(0));
    expect(result.current.items).toEqual(["first"]);
    expect(fetchNextPage).not.toHaveBeenCalled();
  });

  it.each(["scope", "page size"])(
    "ignores a late next-page result after changing %s",
    async (change) => {
      const request = deferred();
      const props = {
        scope: "project-a",
        data: pages.slice(0, 1),
        fetchNextPage: vi.fn(() => request.promise),
      };
      const { result, rerender } = renderHook(useHarness, { initialProps: props });
      let pending!: Promise<void>;
      act(() => {
        pending = result.current.onPageChange(1);
      });
      if (change === "scope")
        rerender({
          ...props,
          scope: "project-b",
          data: [{ items: ["new project"], total: 1, next_cursor: null }],
        });
      else act(() => result.current.setPageSize(50));
      await act(async () => {
        request.resolve({});
        await pending;
      });
      expect(result.current.page).toBe(0);
      expect(result.current.items).toEqual(change === "scope" ? ["new project"] : ["first"]);
      expect(result.current.pageSize).toBe(change === "scope" ? 20 : 50);
    },
  );

  it("retains the current page when loading the next cursor fails", async () => {
    const { result } = renderHook(useHarness, {
      initialProps: {
        scope: "project-a",
        data: pages.slice(0, 1),
        fetchNextPage: vi.fn().mockResolvedValue({ isError: true }),
      },
    });
    await act(() => result.current.onPageChange(1));
    expect(result.current.page).toBe(0);
    expect(result.current.items).toEqual(["first"]);
  });
});

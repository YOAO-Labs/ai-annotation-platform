import { useRef } from "react";
import { useListPageState } from "./useListPageState";

interface CursorPage<T> {
  items: T[];
  next_cursor?: string | null;
  total?: number | null;
}
interface CursorQuery<T> {
  data?: { pages: CursorPage<T>[] };
  isFetchingNextPage: boolean;
  fetchNextPage: () => Promise<unknown>;
}

/** Show one actual cursor page while retaining fetched pages as backward history. */
export function useInfiniteListPagination<T>(
  scope: string,
  query: CursorQuery<T>,
  state: ReturnType<typeof useListPageState>,
) {
  const ownerKey = `${scope}:${state.pageSize}`;
  const owner = useRef(ownerKey);
  owner.current = ownerKey;
  const current = query.data?.pages[state.page];
  return {
    ...state,
    items: current?.items ?? [],
    total: query.data?.pages[0]?.total ?? undefined,
    hasNext: Boolean(current?.next_cursor),
    onPageChange: async (page: number) => {
      if (page < 0 || query.isFetchingNextPage) return;
      if (page > state.page && !query.data?.pages[page]) {
        if (!current?.next_cursor) return;
        try {
          const result = await query.fetchNextPage();
          if (result && typeof result === "object" && "isError" in result && result.isError) return;
        } catch {
          return;
        }
      }
      if (owner.current === ownerKey) state.setPage(page);
    },
  };
}

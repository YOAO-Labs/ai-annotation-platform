import { useState } from "react";
import { DEFAULT_LIST_PAGE_SIZE } from "@/components/ui/ListPagination";

/** Pagination for a complete, already filtered and sorted collection. */
export function useListPagination<T>(
  items: readonly T[],
  resetKey: string | number = "",
  initialPageSize = DEFAULT_LIST_PAGE_SIZE,
) {
  const [state, setState] = useState({ page: 0, pageSize: initialPageSize, resetKey });
  const pageCount = Math.max(1, Math.ceil(items.length / state.pageSize));
  const page = state.resetKey === resetKey ? Math.min(state.page, pageCount - 1) : 0;
  if (state.resetKey !== resetKey || state.page !== page) {
    setState({ ...state, page, resetKey });
  }
  return {
    page,
    pageSize: state.pageSize,
    total: items.length,
    items: items.slice(page * state.pageSize, (page + 1) * state.pageSize),
    setPage: (next: number) =>
      setState((previous) => ({
        ...previous,
        page: Math.max(0, Math.min(next, pageCount - 1)),
      })),
    setPageSize: (pageSize: number) => setState((previous) => ({ ...previous, pageSize, page: 0 })),
  };
}

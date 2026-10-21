import { useState } from "react";
import { DEFAULT_LIST_PAGE_SIZE } from "@/components/ui/ListPagination";

/** Cursor history belongs to one filter, account and page-size scope. */
export function useCursorListPagination(scope: string) {
  const [state, setState] = useState({
    scope,
    pageSize: DEFAULT_LIST_PAGE_SIZE,
    cursors: [null] as (string | null)[],
  });
  const cursors = state.scope === scope ? state.cursors : [null];
  if (state.scope !== scope) setState({ ...state, scope, cursors: [null] });
  return {
    page: cursors.length - 1,
    cursor: cursors[cursors.length - 1] ?? null,
    pageSize: state.pageSize,
    setPageSize: (pageSize: number) =>
      setState((previous) =>
        previous.scope === scope ? { scope, pageSize, cursors: [null] } : previous,
      ),
    changePage: (page: number, nextCursor?: string | null) =>
      setState((previous) => {
        if (previous.scope !== scope) return previous;
        if (page < cursors.length - 1)
          return { ...previous, cursors: cursors.slice(0, Math.max(1, page + 1)) };
        if (page === cursors.length - 1) return previous;
        return nextCursor ? { ...previous, cursors: [...cursors, nextCursor] } : previous;
      }),
  };
}

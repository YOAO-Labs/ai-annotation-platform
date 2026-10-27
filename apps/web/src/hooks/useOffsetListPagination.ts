import { useCallback, useMemo, useState } from "react";
import { DEFAULT_LIST_PAGE_SIZE } from "@/components/ui/ListPagination";

/** Page state for server limit/offset collections; never slices an incomplete response. */
export function useOffsetListPagination(resetKey = "") {
  const [state, setState] = useState({ page: 0, pageSize: DEFAULT_LIST_PAGE_SIZE, resetKey });
  const page = state.resetKey === resetKey ? state.page : 0;
  if (state.resetKey !== resetKey) setState({ ...state, page: 0, resetKey });
  const setPage = useCallback(
    (page: number) => setState((previous) => ({ ...previous, page: Math.max(0, page) })),
    [],
  );
  const setPageSize = useCallback(
    (pageSize: number) => setState((previous) => ({ ...previous, pageSize, page: 0 })),
    [],
  );
  return useMemo(
    () => ({
      page,
      pageSize: state.pageSize,
      offset: page * state.pageSize,
      setPage,
      setPageSize,
    }),
    [page, state.pageSize, setPage, setPageSize],
  );
}

import { useRef, useState } from "react";
import { DEFAULT_LIST_PAGE_SIZE } from "@/components/ui/ListPagination";

/** Page state owned by a query scope, never by a retired asynchronous request. */
export function useListPageState(scope: string) {
  const [state, setState] = useState({ scope, page: 0, pageSize: DEFAULT_LIST_PAGE_SIZE });
  const owner = useRef(scope);
  owner.current = scope;
  const page = state.scope === scope ? state.page : 0;
  if (state.scope !== scope) setState({ ...state, scope, page: 0 });
  return {
    page,
    pageSize: state.pageSize,
    setPage: (next: number) => {
      if (owner.current === scope)
        setState((previous) => ({ ...previous, scope, page: Math.max(0, next) }));
    },
    setPageSize: (pageSize: number) => {
      if (owner.current === scope) setState({ scope, page: 0, pageSize });
    },
  };
}

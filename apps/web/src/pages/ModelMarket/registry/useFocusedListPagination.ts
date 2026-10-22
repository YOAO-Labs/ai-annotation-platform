import { useState } from "react";
import { useListPagination } from "@/hooks/useListPagination";

/** Keep object deep links reachable after the collection is split into pages. */
export function useFocusedListPagination<T>(
  items: readonly T[],
  resetKey: string,
  focusId: string,
  getId: (item: T) => string,
) {
  const pagination = useListPagination(items, resetKey);
  const index = focusId ? items.findIndex((item) => getId(item) === focusId) : -1;
  const focusKey = index < 0 ? "" : focusId;
  const [lastFocus, setLastFocus] = useState("");
  if (focusKey !== lastFocus) {
    setLastFocus(focusKey);
    if (index >= 0) pagination.setPage(Math.floor(index / pagination.pageSize));
  }
  return pagination;
}

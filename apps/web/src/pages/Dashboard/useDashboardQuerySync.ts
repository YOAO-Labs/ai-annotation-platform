import { useCallback, useEffect, useRef, useState } from "react";
import { useLocation, useSearchParams } from "react-router-dom";
import type { UseUrlFilterStateResult } from "@/hooks/useUrlFilterState";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import type { DashboardUrlState } from "./dashboardUrlState";

export interface DashboardQuerySync {
  currentUrl: DashboardUrlState;
  query: string;
  setQuery: (next: string) => void;
  patchUrl: (update: Partial<DashboardUrlState>, options?: { replace?: boolean }) => void;
  writeSearchParams: (next: URLSearchParams, options?: { replace?: boolean }) => void;
}

export function useDashboardQuerySync(
  urlState: UseUrlFilterStateResult<DashboardUrlState>,
): DashboardQuerySync {
  const location = useLocation();
  const [, setSearchParams] = useSearchParams();
  const { state: currentUrl, patch: patchState } = urlState;
  const [query, setQueryValue] = useState(currentUrl.query);
  const lastLocationKey = useRef(location.key);
  const localUrlWrites = useRef(0);
  const syncingQuery = useRef(false);
  const debouncedQuery = useDebouncedValue(query, 250);
  useEffect(() => {
    if (lastLocationKey.current === location.key) return;
    lastLocationKey.current = location.key;
    if (localUrlWrites.current > 0) {
      localUrlWrites.current = 0;
      return;
    }
    syncingQuery.current = true;
    setQueryValue(currentUrl.query);
  }, [currentUrl.query, location.key]);
  const writeSearchParams = useCallback(
    (next: URLSearchParams, options?: { replace?: boolean }) => {
      localUrlWrites.current += 1;
      setSearchParams(next, options);
    },
    [setSearchParams],
  );
  const patchUrl = useCallback(
    (update: Partial<DashboardUrlState>, options?: { replace?: boolean }) => {
      localUrlWrites.current += 1;
      patchState(update, options);
    },
    [patchState],
  );
  useEffect(() => {
    if (syncingQuery.current) {
      if (debouncedQuery === currentUrl.query) syncingQuery.current = false;
      return;
    }
    const nextQuery = debouncedQuery.trim();
    if (nextQuery !== query.trim() || nextQuery === currentUrl.query) return;
    patchUrl({ query: nextQuery, page: 1 });
  }, [currentUrl.query, debouncedQuery, query, patchUrl]);
  const setQuery = useCallback((next: string) => {
    syncingQuery.current = false;
    setQueryValue(next);
  }, []);
  return { currentUrl, query, setQuery, patchUrl, writeSearchParams };
}

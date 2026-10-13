import { useCallback, useEffect, useRef, useState } from "react";
import { useLocation, useSearchParams } from "react-router-dom";
import type { UseUrlFilterStateResult } from "@/hooks/useUrlFilterState";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import type { DashboardUrlState } from "./dashboardUrlState";

export interface DashboardQuerySync {
  /** URL 解码后的筛选状态(含 query、分页)。 */
  currentUrl: DashboardUrlState;
  /** 搜索输入框的即时值。 */
  query: string;
  /** 输入框写入：清除外部导航回填状态，保留防抖草稿。 */
  setQuery: (next: string) => void;
  /** 标记为本组件写入的筛选 patch；location 变化不回填输入框。 */
  patchUrl: (update: Partial<DashboardUrlState>, options?: { replace?: boolean }) => void;
  /** 标记为本组件写入的 setSearchParams（用于 new/from/layout 等非筛选键）。 */
  writeSearchParams: (next: URLSearchParams, options?: { replace?: boolean }) => void;
}

/**
 * 项目总览与管理员项目总览共用的搜索输入与 URL 筛选同步。
 *
 * - 本组件发起的 URL 写入（向导开/关、翻页、视图切换、筛选应用）先计入
 *   localUrlWrites，location 变化时不回填输入框，保留防抖中的搜索草稿，
 *   由防抖 effect 在 250ms 后落回 URL；
 * - 只有外部/历史导航才按链接恢复搜索词（即使 q 未变也覆盖未生效的草稿）。
 */
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

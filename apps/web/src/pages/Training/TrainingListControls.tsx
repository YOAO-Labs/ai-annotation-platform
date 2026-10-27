import { useSearchParams } from "react-router-dom";
import type { ReactNode } from "react";
import { Search } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { ListPagination, listPageSize } from "@/components/ui/ListPagination";

export type TrainingListFilters = {
  q: string;
  status: string;
  framework: string;
  task: string;
  created_from: string;
  created_to: string;
};
const keys = ["q", "status", "framework", "task", "created_from", "created_to"] as const;
const controlClass =
  "min-w-0 rounded-md border border-border bg-background px-2.5 py-2 text-xs text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

export function useTrainingListFilters(prefix: "run" | "data_list" | "model") {
  const [params, setParams] = useSearchParams();
  const filters = Object.fromEntries(
    keys.map((key) => [key, params.get(`${prefix}_${key}`) ?? ""]),
  ) as TrainingListFilters;
  const rawPage = Number(params.get(`${prefix}_page`) ?? 0);
  const page = Number.isSafeInteger(rawPage) && rawPage >= 0 ? rawPage : 0;
  const pageSize = listPageSize(params.get(`${prefix}_page_size`));
  const update = (values: Partial<TrainingListFilters>, reset = false) => {
    setParams(
      (previous) => {
        const next = new URLSearchParams(previous);
        for (const key of keys) {
          const value = reset ? "" : values[key];
          if (value === undefined) continue;
          if (value) next.set(`${prefix}_${key}`, value);
          else next.delete(`${prefix}_${key}`);
        }
        next.delete(`${prefix}_page`);
        return next;
      },
      { replace: true },
    );
  };
  const setPage = (value: number) =>
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      if (value > 0) next.set(`${prefix}_page`, String(value));
      else next.delete(`${prefix}_page`);
      return next;
    });
  const setPageSize = (size: number) =>
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      if (size !== 20) next.set(`${prefix}_page_size`, String(size));
      else next.delete(`${prefix}_page_size`);
      next.delete(`${prefix}_page`);
      return next;
    });
  return { filters, page, pageSize, setPage, setPageSize, update, reset: () => update({}, true) };
}

export function trainingListPath(
  projectId: string,
  kind: "experiments" | "data" | "models",
  params: URLSearchParams,
) {
  const next = new URLSearchParams(params);
  [
    "run",
    "return_run",
    "attempt",
    "dataset",
    "model",
    "model_view",
    "relation",
    "cursor",
    "before",
    "trail",
    "record_pages",
    "note_pages",
    "returnTo",
    "register_run",
    "register_attempt",
    "data_view",
    "data_class",
    "data_split",
    "data_kind",
    "data_page",
    "data_baseline",
    "data_change",
  ].forEach((key) => next.delete(key));
  next.set("tab", kind === "experiments" ? "training" : kind);
  return `/projects/${projectId}/learning?${next}`;
}

export function trainingRecordPath(
  projectId: string,
  kind: "experiments" | "data" | "models",
  id: string,
  params: URLSearchParams,
) {
  const next = new URLSearchParams(params);
  ["run", "return_run", "dataset", "model", "register_run", "register_attempt"].forEach((key) =>
    next.delete(key),
  );
  return `/projects/${projectId}/learning/${kind}/${encodeURIComponent(id)}${next.size ? `?${next}` : ""}`;
}

export function TrainingListControls({
  kind,
  filters,
  onChange,
  onReset,
  statuses,
  children,
  hasExtraFilters = false,
}: {
  kind: "实验" | "数据版本" | "模型";
  filters: TrainingListFilters;
  onChange: (values: Partial<TrainingListFilters>) => void;
  onReset: () => void;
  statuses?: Record<string, string>;
  children?: ReactNode;
  hasExtraFilters?: boolean;
}) {
  return (
    <div className="flex flex-wrap items-center gap-2 p-3">
      <label className="flex min-w-48 flex-1 items-center gap-2 rounded-md border border-border bg-background px-2.5 py-2 focus-within:ring-2 focus-within:ring-ring">
        <Search aria-hidden="true" className="size-3.5 shrink-0 text-muted-foreground" />
        <input
          aria-label={kind === "实验" ? "搜索实验名称或基础权重" : `搜索${kind}名称`}
          placeholder={kind === "实验" ? "搜索实验名称或基础权重" : `搜索${kind}名称`}
          className="min-w-0 flex-1 bg-transparent text-xs outline-none"
          value={filters.q}
          onChange={(event) => onChange({ q: event.target.value })}
        />
      </label>
      {children}
      {statuses && (
        <select
          aria-label={`${kind}状态筛选`}
          className={controlClass}
          value={filters.status}
          onChange={(event) => onChange({ status: event.target.value })}
        >
          <option value="">全部状态</option>
          {Object.entries(statuses).map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
      )}
      {kind === "实验" && (
        <>
          <select
            aria-label="实验任务筛选"
            className={controlClass}
            value={filters.task}
            onChange={(event) => onChange({ task: event.target.value })}
          >
            <option value="">全部任务</option>
            <option value="detect">目标检测</option>
            <option value="segment">实例分割</option>
          </select>
        </>
      )}
      {kind !== "数据版本" && (
        <select
          aria-label={`${kind}框架筛选`}
          className={controlClass}
          value={filters.framework}
          onChange={(event) => onChange({ framework: event.target.value })}
        >
          <option value="">全部框架</option>
          <option value="ultralytics">Ultralytics</option>
          <option value="libreyolo">LibreYOLO</option>
        </select>
      )}
      <details className="text-xs">
        <summary className="cursor-pointer rounded-md px-2 py-2 text-muted-foreground hover:text-foreground">
          创建日期{filters.created_from || filters.created_to ? " · 已筛选" : ""}
        </summary>
        <div className="mt-2 flex flex-wrap items-end gap-2">
          <label className="grid gap-1 text-muted-foreground">
            开始日期
            <input
              type="date"
              aria-label={`${kind}开始日期`}
              className={controlClass}
              value={filters.created_from}
              max={filters.created_to || undefined}
              onChange={(event) => onChange({ created_from: event.target.value })}
            />
          </label>
          <label className="grid gap-1 text-muted-foreground">
            结束日期
            <input
              type="date"
              aria-label={`${kind}结束日期`}
              className={controlClass}
              value={filters.created_to}
              min={filters.created_from || undefined}
              onChange={(event) => onChange({ created_to: event.target.value })}
            />
          </label>
          <span className="py-2 text-2xs text-muted-foreground">UTC 日期</span>
        </div>
      </details>
      {(hasExtraFilters || keys.some((key) => filters[key])) && (
        <Button size="sm" variant="ghost" onClick={onReset}>
          重置筛选
        </Button>
      )}
    </div>
  );
}

export function TrainingListPagination({
  kind,
  page,
  total,
  pending,
  onPage,
  pageSize,
  onPageSize,
}: {
  kind: string;
  page: number;
  total: number;
  pending: boolean;
  onPage: (page: number) => void;
  pageSize: number;
  onPageSize: (size: number) => void;
}) {
  return (
    <ListPagination
      label={kind}
      page={page}
      pageSize={pageSize}
      total={total}
      loading={pending}
      onPageChange={onPage}
      onPageSizeChange={onPageSize}
      previousLabel={`上一页${kind}`}
      nextLabel={`下一页${kind}`}
    />
  );
}

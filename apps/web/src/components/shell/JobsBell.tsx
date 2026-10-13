import { FilterGroup, FilterToggle } from "@/components/filters/FilterControls";
/**
 * v0.10.16 · Topbar 异步任务铃铛（ROADMAP §1.7）。
 *
 * 5s polling /async-jobs?limit=20，把 in-progress 进度 + 最近完成历史塞进 drawer。
 * 与 PreannotateJobsBadge 互补：那个走 Redis pub/sub 只展示预标 job 实时进度；
 * 本组件走 polling 涵盖所有 kind（包括 predictions_import / audit_archive 等没有
 * pub/sub 通道的任务），同时记录终态历史。
 */

import { useEffect, useMemo, useState } from "react";
import type { CSSProperties } from "react";
import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Icon } from "@/components/ui/Icon";
import { useElementStyle } from "@/components/ui/useElementStyle";
import { useToastStore } from "@/components/ui/Toast";
import { AsyncJobDetailModal } from "@/components/jobs/AsyncJobDetailModal";
import {
  JOB_KIND_LABEL as KIND_LABEL,
  exportDownloadState,
  jobResultSummary,
  jobStage,
} from "@/components/jobs/asyncJobPresentation";
import {
  asyncJobsApi,
  CANCELLABLE_ASYNC_JOB_KINDS,
  type AsyncJob,
  type AsyncJobStatus,
} from "@/api/asyncJobs";
import { isCurrentAuthOwner, useAuthStore } from "@/stores/authStore";
import { ShellPopover, SHELL_POPOVER_HEADER_CLASS } from "./ShellPopover";

const POLL_INTERVAL_MS = 5000;

// v0.11.17 · 浮层显示层（纯前端，不触后端 / 不调删除接口）。
const FILTER_KEY = "wb:jobsbell:filter";
const DISMISSED_KEY = "wb:jobsbell:dismissed";

type JobFilter = "all" | "active";

const TERMINAL_STATUSES: AsyncJobStatus[] = ["completed", "failed", "cancelled"];
const isTerminal = (s: AsyncJobStatus) => TERMINAL_STATUSES.includes(s);

function scopedStorageKey(key: string, userId: string | null): string {
  return `${key}:${userId ?? "anonymous"}`;
}

function readFilter(userId: string | null): JobFilter {
  try {
    return localStorage.getItem(scopedStorageKey(FILTER_KEY, userId)) === "active"
      ? "active"
      : "all";
  } catch {
    return "all";
  }
}

function persistFilter(filter: JobFilter, userId: string | null) {
  try {
    localStorage.setItem(scopedStorageKey(FILTER_KEY, userId), filter);
  } catch {
    /* localStorage 不可用时忽略，显示退回默认 */
  }
}

function readDismissed(userId: string | null): Set<string> {
  try {
    const raw = localStorage.getItem(scopedStorageKey(DISMISSED_KEY, userId));
    if (!raw) return new Set();
    const arr: unknown = JSON.parse(raw);
    return Array.isArray(arr)
      ? new Set(arr.filter((x): x is string => typeof x === "string"))
      : new Set();
  } catch {
    return new Set();
  }
}

function persistDismissed(set: Set<string>, userId: string | null) {
  try {
    localStorage.setItem(scopedStorageKey(DISMISSED_KEY, userId), JSON.stringify([...set]));
  } catch {
    /* localStorage 不可用时忽略 */
  }
}

interface AuthOwner {
  userId: string | null;
  token: string | null;
}

function captureAuthOwner(): AuthOwner {
  const auth = useAuthStore.getState();
  return { userId: auth.user?.id ?? null, token: auth.token };
}

function isCurrentOwner(owner: AuthOwner): boolean {
  return (
    !!owner.userId &&
    !!owner.token &&
    isCurrentAuthOwner(owner.userId) &&
    useAuthStore.getState().token === owner.token
  );
}

const EXPORT_TARGET_LABELS: Record<string, string> = {
  aap_json: "AAP JSON",
  "yolo-det": "YOLO DET",
  "yolo-obb": "YOLO OBB",
  "yolo-seg": "YOLO SEG",
  "binary-png": "BINARY PNG",
  "indexed-png": "INDEXED PNG",
  "label-studio-brush": "LABEL STUDIO",
};

function exportTargetLabel(target: string): string {
  return EXPORT_TARGET_LABELS[target] ?? target.replace(/_/g, " ").toUpperCase();
}

/** 从 payload 取一段副标题（导出 job 显示「项目 display_id · 目标格式」）。 */
function jobDetail(job: AsyncJob): string | null {
  const p = job.payload || {};
  const display =
    job.project_name ??
    job.project_display_id ??
    (typeof p.project_display_id === "string" ? p.project_display_id : null);
  const dataset = typeof p.dataset_name === "string" ? p.dataset_name : null;
  const targets = Array.isArray(p.targets)
    ? p.targets.filter((target): target is string => typeof target === "string")
    : [];
  const fmt =
    targets.length > 0
      ? targets.map(exportTargetLabel).join(" + ")
      : typeof p.format === "string"
        ? exportTargetLabel(p.format)
        : null;
  const parts = [display, dataset, fmt].filter(Boolean);
  return parts.length ? parts.join(" · ") : null;
}

function exportResultDetail(result: Record<string, unknown>): string | null {
  const fileCount = typeof result.file_count === "number" ? result.file_count : null;
  const sizeBytes = typeof result.size_bytes === "number" ? result.size_bytes : null;
  if (fileCount === null && sizeBytes === null) return null;
  const parts = [fileCount === null ? null : `${fileCount} 个文件`];
  if (sizeBytes !== null) {
    const units = ["B", "KB", "MB", "GB"];
    let value = Math.max(0, sizeBytes);
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    parts.push(`${value >= 10 || unit === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[unit]}`);
  }
  return parts.filter(Boolean).join(" · ");
}

const STATUS_LABEL: Record<AsyncJobStatus, string> = {
  pending: "等待中",
  running: "进行中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

function ProgressBar({ pct, status }: { pct: number; status: AsyncJobStatus }) {
  const ref = useElementStyle<HTMLDivElement>({
    "--async-job-progress": `${pct}%`,
  } as CSSProperties);
  const fillColor =
    status === "completed"
      ? "bg-status-positive"
      : status === "failed"
        ? "bg-status-danger"
        : status === "cancelled"
          ? "bg-muted-foreground"
          : "bg-brand";
  return (
    <div className="h-[3px] overflow-hidden rounded-[2px] bg-border">
      <div
        ref={ref}
        className={`h-full w-[var(--async-job-progress)] transition-[width] duration-200 ease-out ${fillColor}`}
      />
    </div>
  );
}

function StatusPill({ status }: { status: AsyncJobStatus }) {
  const cls =
    status === "running"
      ? "bg-brand/20 text-brand"
      : status === "completed"
        ? "bg-status-positive-soft text-status-positive"
        : status === "failed"
          ? "bg-status-danger-soft text-status-danger"
          : "bg-muted text-muted-foreground";
  return (
    <span
      className={`rounded-full px-1.5 py-px text-2xs font-semibold uppercase ${cls}`}
      data-testid={`job-status-${status}`}
    >
      {STATUS_LABEL[status]}
    </span>
  );
}

function JobRow({
  job,
  onDismiss,
  onCancel,
  onDetail,
  cancelPending = false,
}: {
  job: AsyncJob;
  onDismiss?: (id: string) => void;
  onCancel?: (id: string) => void;
  onDetail: (id: string) => void;
  cancelPending?: boolean;
}) {
  const kindLabel = KIND_LABEL[job.kind] ?? job.kind;
  const pct = Math.max(0, Math.min(100, job.progress_pct));
  // v0.10.27 · 导出完成后的下载链接（预签名 URL，7 天内可反复点）。
  const download =
    job.kind === "export" && job.status === "completed" ? exportDownloadState(job.result) : null;
  const downloadUrl = download?.url;
  const detail = jobDetail(job);
  const resultDetail =
    job.kind === "export" ? exportResultDetail(job.result) : jobResultSummary(job);
  // v0.11.17 · 仅终态任务可单条本地 dismiss；进行中永不可隐藏。
  const canDismiss = onDismiss && isTerminal(job.status);
  const cancelRequested = job.payload?.cancel_requested === true;
  const canCancel =
    onCancel && !isTerminal(job.status) && CANCELLABLE_ASYNC_JOB_KINDS.has(job.kind);
  return (
    <div
      className="flex w-full flex-col gap-1 rounded-sm px-2.5 py-2 text-left"
      data-testid={`job-row-${job.id}`}
    >
      <div className="flex justify-between gap-3 text-xs font-medium text-foreground">
        <span>
          {kindLabel}
          {detail && <span className="font-normal text-muted-foreground"> · {detail}</span>}
        </span>
        <div className="inline-flex flex-shrink-0 items-center gap-1">
          <StatusPill status={job.status} />
          {canDismiss && (
            <button
              type="button"
              onClick={() => onDismiss(job.id)}
              className="inline-flex h-[18px] w-[18px] cursor-pointer appearance-none items-center justify-center rounded-sm border-0 bg-transparent text-muted-foreground transition-colors duration-200 hover:bg-muted hover:text-foreground"
              title="从列表隐藏（不影响历史记录）"
              aria-label="隐藏此任务"
              data-testid={`job-dismiss-${job.id}`}
            >
              <Icon name="x" size={12} />
            </button>
          )}
        </div>
      </div>
      <ProgressBar pct={pct} status={job.status} />
      <div className="text-xs tabular-nums text-muted-foreground">
        {job.status === "running" || job.status === "pending"
          ? `${jobStage(job)} · ${pct}%`
          : job.status === "failed"
            ? (job.error_message ?? "失败").slice(0, 80)
            : new Date(job.completed_at ?? job.updated_at).toLocaleString("zh-CN")}
      </div>
      {resultDetail && (
        <div className="text-xs tabular-nums text-muted-foreground">
          {job.kind === "export" ? "ZIP · " : ""}
          {resultDetail}
        </div>
      )}
      <button
        type="button"
        onClick={() => onDetail(job.id)}
        className="self-start rounded-sm px-1 py-0.5 text-xs font-medium text-brand hover:bg-muted"
        aria-label={`查看${kindLabel}详情`}
      >
        查看详情
      </button>
      {download?.reason && <p className="m-0 text-xs text-status-caution">{download.reason}</p>}
      {canCancel && (
        <button
          type="button"
          onClick={() => onCancel(job.id)}
          disabled={cancelPending || cancelRequested}
          className="mt-0.5 inline-flex cursor-pointer items-center gap-1 self-start rounded-sm border border-border bg-transparent px-2 py-1 text-xs font-semibold text-status-caution transition-colors duration-200 hover:border-status-caution hover:bg-status-caution-soft disabled:cursor-not-allowed disabled:opacity-50"
          data-testid={`job-cancel-${job.id}`}
        >
          <Icon name="x" size={12} />
          {cancelPending || cancelRequested ? "取消中…" : "取消"}
        </button>
      )}
      {downloadUrl && (
        <a
          href={downloadUrl}
          download
          className="mt-0.5 inline-flex cursor-pointer items-center gap-1 self-start rounded-sm border border-border bg-muted px-2 py-1 text-xs font-semibold text-brand no-underline transition-colors duration-200 hover:border-brand hover:bg-popover"
          data-testid={`job-download-${job.id}`}
        >
          <Icon name="download" size={12} />
          下载
        </a>
      )}
    </div>
  );
}

export function JobsBell() {
  const userId = useAuthStore((state) => state.user?.id ?? null);
  const token = useAuthStore((state) => state.token);
  const ownerKey = `${userId ?? "anonymous"}:${token ?? "none"}`;
  const [open, setOpen] = useState(false);
  const [selectedJob, setSelectedJob] = useState<{ id: string; ownerKey: string } | null>(null);
  const [renderedOwnerKey, setRenderedOwnerKey] = useState(ownerKey);
  const queryClient = useQueryClient();
  const pushToast = useToastStore((state) => state.push);

  const jobsQuery = useInfiniteQuery({
    queryKey: ["async-jobs", "recent", ownerKey],
    queryFn: ({ pageParam, signal }) =>
      asyncJobsApi.list({ limit: 20, offset: pageParam }, { signal }),
    initialPageParam: 0,
    getNextPageParam: (lastPage, _pages, lastOffset) =>
      lastPage.items.length > 0 && lastOffset + lastPage.items.length < lastPage.total
        ? lastOffset + lastPage.items.length
        : undefined,
    enabled: Boolean(userId && token),
    refetchInterval: POLL_INTERVAL_MS,
    refetchIntervalInBackground: false,
  });
  const data = jobsQuery.data;

  const jobs = useMemo(
    () => [
      ...new Map(
        data?.pages.flatMap((page) => page.items).map((job) => [job.id, job]) ?? [],
      ).values(),
    ],
    [data],
  );
  const runningCount = useMemo(
    () =>
      renderedOwnerKey === ownerKey
        ? jobs.filter((j) => j.status === "running" || j.status === "pending").length
        : 0,
    [jobs, ownerKey, renderedOwnerKey],
  );

  const [filter, setFilter] = useState<JobFilter>(() => readFilter(userId));
  const [dismissed, setDismissed] = useState<Set<string>>(() => readDismissed(userId));
  const cancelMut = useMutation({
    mutationFn: (jobId: string) => asyncJobsApi.cancel(jobId),
    onMutate: () => ({ owner: captureAuthOwner() }),
    onSuccess: (result, _jobId, context) => {
      if (!context || !isCurrentOwner(context.owner)) return;
      pushToast({
        msg: result.status === "cancel_requested" ? "已请求取消后台任务" : "后台任务已取消",
        kind: "success",
      });
      queryClient.invalidateQueries({ queryKey: ["async-jobs"] });
    },
    onError: (error, _jobId, context) => {
      if (!context || !isCurrentOwner(context.owner)) return;
      pushToast({ msg: "取消后台任务失败", sub: (error as Error).message, kind: "error" });
    },
  });
  const resetCancelMutation = cancelMut.reset;

  useEffect(() => {
    setRenderedOwnerKey(ownerKey);
    setOpen(false);
    setSelectedJob(null);
    setFilter(readFilter(userId));
    setDismissed(readDismissed(userId));
    resetCancelMutation();
  }, [ownerKey, resetCancelMutation, userId]);

  const changeFilter = (next: JobFilter) => {
    setFilter(next);
    persistFilter(next, userId);
  };

  // 终态任务才进 dismiss 集合；进行中无 dismiss 入口，集合永不含其 id。
  const dismissOne = (id: string) => {
    setDismissed((prev) => {
      if (prev.has(id)) return prev;
      const next = new Set(prev).add(id);
      persistDismissed(next, userId);
      return next;
    });
  };

  // dismiss 集合收敛：轮询窗口只回最近 20 条，已滑出窗口的 dismiss id 既不会再渲染、
  // 又会让 localStorage 无限增长，故每次窗口更新后用当前 id 集合求交集裁剪。
  // 守卫 data：loading 态 jobs 为空，若此时收敛会把整个集合误清空。
  useEffect(() => {
    if (!data || renderedOwnerKey !== ownerKey) return;
    setDismissed((prev) => {
      if (prev.size === 0) return prev;
      const windowIds = new Set(jobs.map((j) => j.id));
      const next = new Set([...prev].filter((id) => windowIds.has(id)));
      if (next.size === prev.size) return prev;
      persistDismissed(next, userId);
      return next;
    });
  }, [data, jobs, ownerKey, renderedOwnerKey, userId]);

  // 渲染列表：先按 filter 过滤，再剔除已 dismiss 的终态项（进行中永不隐藏）。
  const visibleJobs = useMemo(() => {
    if (renderedOwnerKey !== ownerKey) return [];
    return jobs.filter((j) => {
      if (filter === "active" && isTerminal(j.status)) return false;
      if (isTerminal(j.status) && dismissed.has(j.id)) return false;
      return true;
    });
  }, [jobs, filter, dismissed, ownerKey, renderedOwnerKey]);

  const visibleTerminalIds = useMemo(
    () => visibleJobs.filter((j) => isTerminal(j.status)).map((j) => j.id),
    [visibleJobs],
  );

  const dismissAllTerminal = () => {
    if (visibleTerminalIds.length === 0) return;
    setDismissed((prev) => {
      const next = new Set(prev);
      visibleTerminalIds.forEach((id) => next.add(id));
      persistDismissed(next, userId);
      return next;
    });
  };

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        title="后台任务"
        aria-label="后台任务"
        aria-haspopup="dialog"
        aria-expanded={open && renderedOwnerKey === ownerKey}
        aria-controls={open ? "shell-popover-jobs" : undefined}
        data-shell-popover-trigger="jobs"
        className={`relative inline-flex h-8 w-8 cursor-pointer items-center justify-center rounded-sm border bg-transparent transition-colors duration-100 ${
          open
            ? "border-border bg-muted text-foreground"
            : "border-transparent text-muted-foreground hover:bg-muted hover:text-foreground"
        }`}
        data-testid="jobs-bell-trigger"
      >
        <Icon name="layers" size={15} />
        {runningCount > 0 && (
          <span
            className="absolute right-0.5 top-0.5 inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-brand px-1 text-center text-2xs font-semibold leading-4 text-white"
            data-testid="jobs-bell-badge"
          >
            {runningCount}
          </span>
        )}
      </button>

      {open && renderedOwnerKey === ownerKey && (
        <ShellPopover id="jobs" label="后台任务" onClose={() => setOpen(false)}>
          <div className={`${SHELL_POPOVER_HEADER_CLASS} font-semibold`}>
            <span>后台任务 {runningCount > 0 ? `(${runningCount} 进行中)` : ""}</span>
          </div>
          <div className="flex shrink-0 items-center justify-between gap-2 border-b border-border px-3 py-2">
            <FilterGroup
              label="状态"
              hideLabel
              compact
              aria-label="任务筛选"
              className="inline-flex rounded-md bg-muted/40 p-0.5"
            >
              <FilterToggle
                compact
                active={filter === "all"}
                onClick={() => changeFilter("all")}
                className="px-2.5 py-1 text-xs"
                data-testid="jobs-bell-filter-all"
              >
                全部
              </FilterToggle>
              <FilterToggle
                compact
                active={filter === "active"}
                onClick={() => changeFilter("active")}
                className="px-2.5 py-1 text-xs"
                data-testid="jobs-bell-filter-active"
              >
                进行中
              </FilterToggle>
            </FilterGroup>
            {visibleTerminalIds.length > 0 && (
              <button
                type="button"
                onClick={dismissAllTerminal}
                className="cursor-pointer appearance-none rounded-sm border border-border bg-transparent px-2 py-1 text-xs font-semibold text-muted-foreground transition-colors duration-100 hover:border-muted-foreground hover:bg-muted hover:text-foreground"
                data-testid="jobs-bell-clear-terminal"
              >
                清空已结束
              </button>
            )}
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-1">
            {jobsQuery.isError && (
              <div role="alert" className="space-y-2 px-3 py-2 text-xs text-status-danger">
                <p className="m-0">后台任务加载失败，已有记录已保留。</p>
                <button
                  type="button"
                  onClick={() => void jobsQuery.refetch()}
                  className="rounded border border-border px-2 py-1 text-foreground"
                >
                  重新加载
                </button>
              </div>
            )}
            {jobsQuery.isPending ? (
              <p role="status" className="px-3 py-5 text-center text-xs text-muted-foreground">
                {jobsQuery.fetchStatus === "paused"
                  ? "当前离线，联网后加载任务"
                  : "正在加载后台任务…"}
              </p>
            ) : !jobsQuery.isError && visibleJobs.length === 0 ? (
              <div className="px-3 py-5 text-center text-xs text-muted-foreground">
                {filter === "active" ? "暂无进行中任务" : "暂无后台任务"}
              </div>
            ) : (
              visibleJobs.map((j) => (
                <JobRow
                  key={j.id}
                  job={j}
                  onDismiss={dismissOne}
                  onCancel={(jobId) => cancelMut.mutate(jobId)}
                  onDetail={(jobId) => {
                    setOpen(false);
                    setSelectedJob({ id: jobId, ownerKey });
                  }}
                  cancelPending={cancelMut.isPending && cancelMut.variables === j.id}
                />
              ))
            )}
            {jobsQuery.hasNextPage && (
              <button
                type="button"
                onClick={() => void jobsQuery.fetchNextPage()}
                disabled={jobsQuery.isFetchingNextPage}
                className="w-full rounded-sm px-3 py-2 text-xs font-medium text-brand hover:bg-muted disabled:opacity-50"
              >
                {jobsQuery.isFetchingNextPage ? "正在加载…" : "加载更早任务"}
              </button>
            )}
          </div>
        </ShellPopover>
      )}
      {selectedJob && selectedJob.ownerKey === ownerKey && renderedOwnerKey === ownerKey && (
        <AsyncJobDetailModal
          key={`${selectedJob.ownerKey}:${selectedJob.id}`}
          jobId={selectedJob.id}
          onClose={() => setSelectedJob(null)}
        />
      )}
    </div>
  );
}

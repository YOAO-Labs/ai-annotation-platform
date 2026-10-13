import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/api/client";
import { asyncJobsApi } from "@/api/asyncJobs";
import { datasetsApi } from "@/api/datasets";
import { projectsApi } from "@/api/projects";
import { isCurrentAuthOwner, useAuthStore } from "@/stores/authStore";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { useToastStore } from "@/components/ui/Toast";
import type { GuardedNavigate } from "@/utils/workbenchNavigation";
import {
  exportDownloadState,
  JOB_KIND_LABEL,
  JOB_STATUS_LABEL,
  jobNumber,
  jobResultSummary,
  jobStage,
  jobString,
} from "./asyncJobPresentation";

function detailError(error: unknown): string {
  if (error instanceof ApiError && error.status === 403) {
    return "当前账号已无权查看该任务，请联系项目负责人。";
  }
  if (error instanceof ApiError && error.status === 404) {
    return "该任务已不存在或不在当前账号的可见范围内。";
  }
  return "任务详情加载失败，请检查网络后重新加载。";
}

function formatDate(iso: string | null) {
  if (!iso || !Number.isFinite(Date.parse(iso))) return "未开始";
  return new Date(iso).toLocaleString("zh-CN");
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

/** Shared by job history, notifications and the background-job list. */
export function AsyncJobDetailModal({
  jobId,
  onClose,
  onRetryQueued,
  navigate: navigateExternal,
}: {
  jobId: string;
  onClose: () => void;
  onRetryQueued?: (queued: number) => void;
  navigate?: GuardedNavigate;
}) {
  const userId = useAuthStore((state) => state.user?.id);
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const pushToast = useToastStore((state) => state.push);
  const [targetError, setTargetError] = useState<string | null>(null);
  const [openingTarget, setOpeningTarget] = useState(false);
  const navigationRequest = useRef(0);
  const jobQ = useQuery({
    queryKey: ["async-jobs", "detail", jobId, userId],
    queryFn: () => asyncJobsApi.get(jobId),
    retry: false,
    refetchInterval: (query) =>
      ["pending", "running"].includes(query.state.data?.status ?? "") ? 5000 : false,
  });
  const retryMut = useMutation({
    mutationFn: () => asyncJobsApi.retryFailed(jobId),
    onMutate: () => ({ owner: captureAuthOwner() }),
    onSuccess: (response, _variables, context) => {
      if (!context || !isCurrentOwner(context.owner)) return;
      onRetryQueued?.(response.queued);
      if (!onRetryQueued) {
        pushToast({ msg: `已重新排队 ${response.queued} 条失败项`, kind: "success" });
      }
      void queryClient.invalidateQueries({ queryKey: ["async-jobs"] });
      void queryClient.invalidateQueries({ queryKey: ["admin", "failed-predictions"] });
    },
  });
  const resetRetryMutation = retryMut.reset;
  useEffect(() => {
    navigationRequest.current += 1;
    setTargetError(null);
    setOpeningTarget(false);
    resetRetryMutation();
    return () => {
      navigationRequest.current += 1;
    };
  }, [jobId, resetRetryMutation, userId]);
  const job = jobQ.isError ? undefined : jobQ.data;
  const failedIds = job?.result.failed_prediction_ids;
  const canRetry =
    job?.kind === "batch_predict" &&
    ["completed", "failed", "cancelled"].includes(job.status) &&
    (jobNumber(job.result, "failed_count") ?? 0) > 0 &&
    Array.isArray(failedIds) &&
    failedIds.some((id) => typeof id === "string" && id);
  const datasetId = job ? jobString(job.payload, "dataset_id") : null;
  const datasetName = job ? jobString(job.payload, "dataset_name") : null;
  const summary = job ? jobResultSummary(job) : null;
  const fileErrors = Array.isArray(job?.result.errors)
    ? job.result.errors.filter(
        (item): item is Record<string, unknown> => !!item && typeof item === "object",
      )
    : [];
  const download =
    job?.kind === "export" && job.status === "completed" ? exportDownloadState(job.result) : null;

  const openTarget = async (kind: "dataset" | "project" | "dashboard") => {
    if (!job) return;
    const request = ++navigationRequest.current;
    const owner = captureAuthOwner();
    const current = () => request === navigationRequest.current && isCurrentOwner(owner);
    setOpeningTarget(true);
    setTargetError(null);
    try {
      let url: string;
      if (kind === "dataset" && datasetId) {
        await datasetsApi.get(datasetId);
        if (!current()) return;
        url = `/datasets?dataset=${encodeURIComponent(datasetId)}`;
      } else if (kind === "dashboard") {
        url = "/dashboard";
      } else if (job.project_id) {
        await projectsApi.get(job.project_id);
        if (!current()) return;
        url = `/projects/${job.project_id}/data-manager`;
      } else return;
      if (!current()) return;
      if (navigateExternal) {
        if ((await navigateExternal(url)) === false || !current()) return;
      } else navigate(url);
      onClose();
    } catch (error) {
      if (!current()) return;
      setTargetError(
        error instanceof ApiError && [403, 404].includes(error.status)
          ? "关联目标已被删除或访问权限已变更，请联系项目负责人。该次任务记录仍保留。"
          : "关联目标暂时无法打开，请稍后重试。",
      );
    } finally {
      if (current()) setOpeningTarget(false);
    }
  };

  return (
    <Modal open onClose={onClose} title="后台任务详情" width={640}>
      {jobQ.isPending && (
        <p role="status" className="text-sm text-muted-foreground">
          {jobQ.fetchStatus === "paused" ? "当前离线，联网后加载任务详情。" : "正在加载任务详情…"}
        </p>
      )}
      {jobQ.isError && (
        <div role="alert" className="space-y-3 text-sm">
          <p>{detailError(jobQ.error)}</p>
          <Button onClick={() => void jobQ.refetch()}>重新加载</Button>
        </div>
      )}
      {job && (
        <div className="space-y-4 text-sm">
          <div className="flex items-start justify-between gap-3">
            <div>
              <h2 className="m-0 text-base font-semibold">
                {JOB_KIND_LABEL[job.kind] ?? "后台任务"}
              </h2>
              <p className="mb-0 mt-1 text-xs text-muted-foreground">
                {job.project_name ?? job.project_display_id ?? "未关联项目"}
                {datasetName ? ` · ${datasetName}` : ""}
              </p>
            </div>
            <Badge
              variant={
                job.status === "failed"
                  ? "danger"
                  : job.status === "completed"
                    ? "success"
                    : "outline"
              }
            >
              {JOB_STATUS_LABEL[job.status]}
            </Badge>
          </div>
          <div className="rounded-md border border-border bg-muted p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span>当前阶段：{jobStage(job)}</span>
              <span className="tabular-nums">{Math.max(0, Math.min(100, job.progress_pct))}%</span>
            </div>
            {summary && <p className="mb-0 mt-2 font-medium tabular-nums">{summary}</p>}
            {job.kind === "export" && jobNumber(job.result, "size_bytes") !== null && (
              <p className="mb-0 mt-1 text-xs text-muted-foreground">
                文件大小：{jobNumber(job.result, "size_bytes")?.toLocaleString()} bytes
              </p>
            )}
          </div>
          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-xs">
            {jobNumber(job.result, "total_cost") !== null && (
              <>
                <dt className="text-muted-foreground">成本</dt>
                <dd className="m-0">${jobNumber(job.result, "total_cost")?.toFixed(4)}</dd>
              </>
            )}
            {jobNumber(job.result, "duration_ms") !== null && (
              <>
                <dt className="text-muted-foreground">耗时</dt>
                <dd className="m-0">
                  {((jobNumber(job.result, "duration_ms") ?? 0) / 1000).toFixed(1)} 秒
                </dd>
              </>
            )}
            <dt className="text-muted-foreground">创建时间</dt>
            <dd className="m-0">{formatDate(job.created_at)}</dd>
            <dt className="text-muted-foreground">开始时间</dt>
            <dd className="m-0">{formatDate(job.started_at)}</dd>
            <dt className="text-muted-foreground">完成时间</dt>
            <dd className="m-0">{job.completed_at ? formatDate(job.completed_at) : "尚未结束"}</dd>
          </dl>
          {job.error_message && (
            <details className="rounded-md border border-border p-3" open={job.status === "failed"}>
              <summary className="cursor-pointer font-medium text-status-danger">失败原因</summary>
              <p className="mb-0 mt-2 whitespace-pre-wrap break-words text-xs">
                {job.error_message}
              </p>
            </details>
          )}
          {download && (
            <div className="space-y-2">
              {download.url ? (
                <a
                  className="inline-flex rounded-md border border-border px-3 py-2 font-medium text-brand"
                  href={download.url}
                  download
                >
                  下载导出文件
                </a>
              ) : (
                <p role="status" className="text-status-caution">
                  {download.reason} 请在项目列表的“导出标注数据”中重新生成。
                </p>
              )}
            </div>
          )}
          {fileErrors.length > 0 && (
            <details className="rounded-md border border-border p-3">
              <summary className="cursor-pointer font-medium">
                查看文件错误（{fileErrors.length} 条记录）
              </summary>
              <ul className="mb-0 mt-2 space-y-2 pl-4 text-xs">
                {fileErrors.map((error, index) => (
                  <li key={index} className="whitespace-pre-wrap break-words">
                    {jobString(error, "relpath") ?? jobString(error, "path") ?? `文件 ${index + 1}`}
                    ：{jobString(error, "error") ?? "导入失败"}
                  </li>
                ))}
              </ul>
            </details>
          )}
          {canRetry && (
            <div className="space-y-2">
              <p className="text-xs text-muted-foreground">
                仅重新排队已记录的失败预测，已成功项保留。
              </p>
              <Button size="sm" disabled={retryMut.isPending} onClick={() => retryMut.mutate()}>
                {retryMut.isPending ? "排队中…" : "重试失败项"}
              </Button>
              {retryMut.isError && (
                <p role="alert" className="text-xs text-status-danger">
                  重试排队失败，请重新加载详情后重试。
                </p>
              )}
            </div>
          )}
          {targetError && (
            <p role="alert" className="text-xs text-status-danger">
              {targetError}
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            {datasetId && (
              <Button size="sm" disabled={openingTarget} onClick={() => void openTarget("dataset")}>
                查看数据集
              </Button>
            )}
            {job.project_id && (
              <Button size="sm" disabled={openingTarget} onClick={() => void openTarget("project")}>
                查看项目数据
              </Button>
            )}
            {download && !download.url && (
              <Button
                size="sm"
                disabled={openingTarget}
                onClick={() => void openTarget("dashboard")}
              >
                返回项目列表
              </Button>
            )}
          </div>
        </div>
      )}
    </Modal>
  );
}

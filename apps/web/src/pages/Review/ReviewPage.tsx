import { ActiveFilterChip } from "@/components/filters/ActiveFilterChip";
import { FilterGroup } from "@/components/filters/FilterControls";
import { useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { Icon } from "@/components/ui/Icon";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Thumbnail } from "@/components/Thumbnail";
import { useToastStore } from "@/components/ui/Toast";
import { useElementStyle } from "@/components/ui/useElementStyle";
import {
  flattenTaskPages,
  useTaskList,
  useAnnotations,
  useApproveTask,
  useRejectTask,
} from "@/hooks/useTasks";
import { useReviewerStats } from "@/hooks/useDashboard";
import { ApiError } from "@/api/client";
import type { TaskResponse } from "@/types";
import type { ReviewingBatchItem } from "@/api/dashboard";
import { buildReviewWorkbenchUrl, currentWorkbenchReturnTo } from "@/utils/workbenchNavigation";
import { useAuthStore } from "@/stores/authStore";
import { useRejectBatch } from "@/hooks/useBatches";
import { inputDialog } from "@/components/ui/decisionDialog";
import { promptRejectReason } from "./rejectReasonDialog";
import type { RejectPayload } from "./rejectReasonTypes";
import { ReviewSidebar } from "./ReviewSidebar";
import { ReviewBatchCardGrid } from "./ReviewBatchCardGrid";
import {
  clearReviewAssignee,
  clearReviewSelection,
  readReviewQueueParams,
  readReviewTaskId,
  reviewQueueIsUnchanged,
  reviewQueueScopeKey,
  selectReviewBatch,
  setReviewTaskId,
} from "./reviewUrlState";
import {
  isInitialQueryPaused,
  isQueryPaused,
  isRefreshQueryPaused,
  QueryPausedNotice,
  QueryPausedState,
} from "@/pages/shared/QueryState";
import type { CSSProperties } from "react";

function ProgressFill({ pct, barClass }: { pct: number; barClass: string }) {
  const ref = useElementStyle<HTMLDivElement>({
    "--progress-pct": `${Math.min(100, pct)}%`,
  } as CSSProperties);

  return <div ref={ref} className={`h-full w-[var(--progress-pct)] ${barClass}`} />;
}

function AnnotationPreview({ taskId }: { taskId: string }) {
  const { data: annotations } = useAnnotations(taskId);
  if (!annotations || annotations.length === 0) {
    return <span className="text-xs text-muted-foreground">无标注</span>;
  }
  return (
    <div className="flex flex-wrap gap-1">
      {annotations.slice(0, 6).map((a) => (
        <span key={a.id} className="[&_span]:px-1.5 [&_span]:py-px [&_span]:text-2xs">
          <Badge variant={a.parent_prediction_id ? "ai" : "accent"}>
            {a.class_name} {a.confidence ? `${(a.confidence * 100).toFixed(0)}%` : ""}
          </Badge>
        </span>
      ))}
      {annotations.length > 6 && (
        <span className="text-2xs text-muted-foreground">+{annotations.length - 6}</span>
      )}
    </div>
  );
}

function TaskRow({
  task,
  checked,
  onToggle,
  onOpen,
}: {
  task: TaskResponse;
  checked: boolean;
  onToggle: () => void;
  onOpen: () => void;
}) {
  return (
    <div className="mb-2 grid grid-cols-[32px_48px_minmax(0,1fr)_140px_200px_96px] items-center gap-3 rounded-md border border-border bg-card px-3.5 py-2.5">
      <label className="inline-flex cursor-pointer items-center">
        <input
          type="checkbox"
          checked={checked}
          onChange={onToggle}
          onClick={(e) => e.stopPropagation()}
          className="cursor-pointer accent-brand"
        />
      </label>
      <Thumbnail src={task.thumbnail_url} blurhash={task.blurhash} width={40} height={40} />
      <div onClick={onOpen} className="min-w-0 cursor-pointer">
        <div className="flex items-center gap-2">
          <span className="mono text-xs font-semibold">{task.display_id}</span>
          <span className="overflow-hidden text-ellipsis whitespace-nowrap text-sm">
            {task.file_name}
          </span>
        </div>
        <div className="mt-0.5 text-xs text-muted-foreground">
          {task.total_annotations} 个标注 · {task.total_predictions} 个预测
        </div>
      </div>
      <div className="text-xs text-muted-foreground">
        <Badge variant="warning" dot>
          待审核
        </Badge>
      </div>
      <AnnotationPreview taskId={task.id} />
      <div className="text-right">
        <Button size="sm" variant="primary" onClick={onOpen}>
          <Icon name="target" size={11} />
          打开
        </Button>
      </div>
    </div>
  );
}

function queryErrorMessage(error: unknown, resource: string): string {
  if (error instanceof ApiError && error.status === 403) {
    return `当前账号没有权限查看${resource}，请联系项目管理员。`;
  }
  if (error instanceof ApiError && error.status === 404) {
    return `${resource}已不存在或已被移除，请刷新后重试。`;
  }
  if (error instanceof ApiError && error.status >= 500) {
    return `${resource}服务暂时不可用，请稍后重试。`;
  }
  return `${resource}加载失败，请检查网络后重试。`;
}

function QueryErrorState({
  resource,
  error,
  onRetry,
  compact = false,
}: {
  resource: string;
  error: unknown;
  onRetry: () => void;
  compact?: boolean;
}) {
  return (
    <div
      className={`flex flex-col items-center gap-2 rounded-md border border-border bg-card p-10 text-center ${compact ? "m-2 p-6" : ""}`}
      role="alert"
    >
      <Icon name="warning" size={compact ? 20 : 28} className="text-status-danger" />
      <div className="text-sm font-semibold">无法加载{resource}</div>
      <div className="max-w-[360px] text-xs leading-relaxed text-muted-foreground">
        {queryErrorMessage(error, resource)}
      </div>
      <Button size="sm" onClick={onRetry}>
        重新加载
      </Button>
    </div>
  );
}

function RefreshNotice({
  resource,
  error,
  onRetry,
}: {
  resource: string;
  error: unknown;
  onRetry: () => void;
}) {
  return (
    <div
      className="mb-3 flex items-center justify-between gap-3 rounded-md border border-border bg-status-caution-soft px-3 py-2.5 text-xs"
      role="alert"
    >
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <strong className="text-status-caution">{resource}更新失败</strong>
        <span className="text-muted-foreground">
          {queryErrorMessage(error, resource)} 当前内容已保留。
        </span>
      </div>
      <Button size="sm" onClick={onRetry}>
        重新加载
      </Button>
    </div>
  );
}

export function ReviewPage() {
  const pushToast = useToastStore((s) => s.push);
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams, setSearchParams] = useSearchParams();
  // Queue selection is URL-owned so a browser navigation updates the request
  // scope before the next render. Local selection state could briefly combine
  // a new assignee with a retired batch while React effects caught up.
  const queueScope = readReviewQueueParams(searchParams);
  const selectedProjectId = queueScope.project;
  const selectedBatchId = queueScope.batch;

  // v0.7.1 B-18：批次树数据来自 reviewer dashboard 聚合（已扩展为「reviewing 或 review_tasks>0」）。
  const reviewerStatsQuery = useReviewerStats();
  const reviewerStats = reviewerStatsQuery.data;
  const hasReviewerStats = reviewerStats !== undefined;
  const reviewerStatsLoading = reviewerStatsQuery.isLoading && !hasReviewerStats;
  const reviewerStatsInitialPaused = isInitialQueryPaused(reviewerStatsQuery, hasReviewerStats);
  const reviewerStatsRefreshPaused = isRefreshQueryPaused(reviewerStatsQuery, hasReviewerStats);
  const reviewerStatsInitialError = reviewerStatsQuery.isError && !hasReviewerStats;
  const sidebarBatches = useMemo<ReviewingBatchItem[]>(
    () => reviewerStats?.reviewing_batches ?? [],
    [reviewerStats?.reviewing_batches],
  );

  const selectedBatch = useMemo(
    () => sidebarBatches.find((b) => b.batch_id === selectedBatchId) ?? null,
    [sidebarBatches, selectedBatchId],
  );
  // 选中批次后 projectId 跟随；未选中走 selectedProjectId 兜底（用于「全部待审」筛选）。
  const projectId = selectedBatch?.project_id || selectedProjectId || undefined;

  // v0.12.5 · 绩效页项目下钻带入的 assignee 过滤(后端 tasks 已支持 assignee_id)。
  const assigneeFilter = queueScope.assignee;
  const taskListParams = useMemo(
    () => ({
      status: "review" as const,
      ...(selectedBatchId ? { batch_id: selectedBatchId } : {}),
      ...(assigneeFilter ? { assignee_id: assigneeFilter } : {}),
    }),
    [selectedBatchId, assigneeFilter],
  );
  const taskListQuery = useTaskList(projectId, taskListParams);
  const taskListData = taskListQuery.data;
  const hasTaskData = taskListData !== undefined;
  const isLoading = taskListQuery.isLoading && !hasTaskData;
  const taskListInitialPaused = isInitialQueryPaused(taskListQuery, hasTaskData);
  const taskListRefreshPaused = isRefreshQueryPaused(taskListQuery, hasTaskData);
  const taskListPaused = isQueryPaused(taskListQuery);
  const [loadMoreRequested, setLoadMoreRequested] = useState(false);
  useEffect(() => {
    if (!taskListPaused && !taskListQuery.isFetchingNextPage) {
      setLoadMoreRequested(false);
    }
  }, [taskListPaused, taskListQuery.isFetchingNextPage]);
  const tasks = useMemo(() => flattenTaskPages(taskListData?.pages), [taskListData?.pages]);
  const total = taskListData?.pages[0]?.total ?? tasks.length;

  const approveMut = useApproveTask();
  const rejectMut = useRejectTask();
  // 整批退回的批次驳回 mutation（原 RejectBatchModal 内联 hook，plan T2 迁移到调用方）
  const rejectBatchMut = useRejectBatch(projectId ?? "");
  const authOwnerKey = useAuthStore(
    (state) => `${state.user?.id ?? "anonymous"}:${state.token ?? "none"}`,
  );

  const [checkedIds, setCheckedIds] = useState<Set<string>>(new Set());
  // plan 1789527942 · T2：批次驳回 / 任务退回已迁移 decisionDialog；弹窗期间 URL 作用域可能漂移，
  // 各 async 流程在 await 后重查 queueScopeKeyRef 再提交（快照 + 复查取代原 open state 关闭逻辑）。
  const queueUrlRef = useRef({ ...queueScope, authOwnerKey });
  const queueScopeKey = reviewQueueScopeKey(authOwnerKey, queueScope);
  const queueScopeKeyRef = useRef(queueScopeKey);
  queueScopeKeyRef.current = queueScopeKey;
  const checkedIdsKey = JSON.stringify([...checkedIds].sort());
  const checkedIdsKeyRef = useRef(checkedIdsKey);
  checkedIdsKeyRef.current = checkedIdsKey;
  const queueSearch = searchParams.toString();

  // Clear cross-page selections when a URL-owned queue scope changes.
  useEffect(() => {
    const params = new URLSearchParams(queueSearch);
    const next = { ...readReviewQueueParams(params), authOwnerKey };
    const previous = queueUrlRef.current;
    queueUrlRef.current = next;
    if (!reviewQueueIsUnchanged(previous, next)) {
      setCheckedIds(new Set());
    }
  }, [authOwnerKey, queueSearch]);

  useEffect(() => {
    const taskIds = new Set(tasks.map((task) => task.id));
    setCheckedIds((previous) => {
      const next = new Set([...previous].filter((id) => taskIds.has(id)));
      if (next.size === previous.size) return previous;
      return next;
    });
  }, [tasks]);

  const handleSelectBatch = (b: ReviewingBatchItem | null) => {
    // 合并而非整体重写：保留 assignee 等下钻带入的过滤 param。
    setSearchParams(
      selectReviewBatch(
        searchParams,
        b ? { project_id: b.project_id, batch_id: b.batch_id } : null,
      ),
    );
    setCheckedIds(new Set());
  };

  const clearAssigneeFilter = () => {
    setSearchParams(clearReviewAssignee(searchParams));
    setCheckedIds(new Set());
  };

  // 返回卡片网格概览：清掉 batch / project / assignee 三类选择。
  const backToOverview = () => {
    setSearchParams(clearReviewSelection(searchParams));
    setCheckedIds(new Set());
  };

  const openTaskId = readReviewTaskId(searchParams);
  // ESC 关 drawer
  useEffect(() => {
    if (!openTaskId) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setSearchParams((previous) => setReviewTaskId(previous, null));
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [openTaskId, setSearchParams]);

  const toggleChecked = (id: string) => {
    setCheckedIds((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  };
  const toggleAll = () => {
    if (checkedIds.size === tasks.length) setCheckedIds(new Set());
    else setCheckedIds(new Set(tasks.map((t) => t.id)));
  };

  const openTask = (id: string) => {
    if (projectId) {
      navigate(
        buildReviewWorkbenchUrl(projectId, {
          batchId: selectedBatchId,
          taskId: id,
          returnTo: currentWorkbenchReturnTo(location),
        }),
      );
    } else {
      setSearchParams((previous) => setReviewTaskId(previous, id));
    }
  };
  const runBatchReject = (
    ids: string[],
    payload: RejectPayload,
    actionScopeKey: string,
    actionSelectionKey: string,
  ) => {
    let succeeded = 0;
    let failed = 0;
    let pending = ids.length;
    ids.forEach((id) => {
      rejectMut.mutate(
        { taskId: id, ...payload },
        {
          onSuccess: () => {
            succeeded++;
          },
          onError: () => {
            failed++;
          },
          onSettled: () => {
            pending--;
            if (
              pending === 0 &&
              queueScopeKeyRef.current === actionScopeKey &&
              checkedIdsKeyRef.current === actionSelectionKey
            ) {
              pushToast({
                msg: `已退回 ${succeeded}/${ids.length} 个任务`,
                sub: failed
                  ? `${failed} 项失败`
                  : `类型：${payload.reason_type}${payload.reason ? ` · ${payload.reason}` : ""}`,
                kind: failed ? "error" : "success",
              });
              setCheckedIds(new Set());
            }
          },
        },
      );
    });
  };

  // 整批退回：应用内决策弹窗填必填原因（原 RejectBatchModal，plan T2 迁移 inputDialog，copy 保留）。
  const handleBatchReject = async () => {
    if (rejectBatchMut.isPending) return;
    const batchId = selectedBatchId;
    const scopeKey = queueScopeKeyRef.current;
    const feedback = await inputDialog({
      title: `驳回批次 ${selectedBatch?.batch_display_id ?? batchId}`,
      description:
        "驳回后批次状态变为「已退回」，被分派的标注员会收到通知。已提交质检 / 已通过的任务回退到待标注，**已有标注内容会保留**，标注员可在 reviewer 留言指引下继续修改。",
      label: "驳回原因 / 留言",
      placeholder: "请说明需要标注员重做的具体问题…",
      required: true,
      maxLength: 500,
      tone: "danger",
      confirmLabel: "确认驳回",
    });
    if (!feedback) return;
    // async-await gap：弹窗期间 URL 作用域被切换时放弃，避免误驳回当前批次之外的批次。
    if (queueScopeKeyRef.current !== scopeKey) return;
    rejectBatchMut.mutate(
      { batchId, feedback },
      {
        onSuccess: () => pushToast({ msg: "批次已驳回，已通知被分派的标注员", kind: "success" }),
        onError: (e) => pushToast({ msg: "驳回失败", sub: (e as Error).message, kind: "warning" }),
      },
    );
  };

  // 批量退回：两步决策弹窗（原 RejectReasonModal，plan T2 迁移 choiceDialog + inputDialog）。
  const handleTasksReject = async () => {
    const ids = [...checkedIds];
    if (ids.length === 0) return;
    const actionScopeKey = queueScopeKeyRef.current;
    const actionSelectionKey = checkedIdsKeyRef.current;
    // v0.8.8 · 单任务退回且该任务被跳过时透传 skip_reason 到第一步提示
    const skipReasonHint =
      ids.length === 1 ? (tasks.find((t) => t.id === ids[0])?.skip_reason ?? null) : null;
    const payload = await promptRejectReason({ count: ids.length, skipReasonHint });
    if (!payload) return;
    // async-await gap：弹窗期间队列作用域或选择集合变化时放弃。
    if (
      queueScopeKeyRef.current !== actionScopeKey ||
      checkedIdsKeyRef.current !== actionSelectionKey
    ) {
      return;
    }
    runBatchReject(ids, payload, actionScopeKey, actionSelectionKey);
  };

  const runBatchApprove = (actionScopeKey: string, actionSelectionKey: string) => {
    const ids = [...checkedIds];
    let succeeded = 0;
    let failed = 0;
    let pending = ids.length;
    ids.forEach((id) => {
      approveMut.mutate(id, {
        onSuccess: () => {
          succeeded++;
        },
        onError: () => {
          failed++;
        },
        onSettled: () => {
          pending--;
          if (
            pending === 0 &&
            queueScopeKeyRef.current === actionScopeKey &&
            checkedIdsKeyRef.current === actionSelectionKey
          ) {
            pushToast({
              msg: `已通过 ${succeeded}/${ids.length} 个任务`,
              sub: failed ? `${failed} 项失败` : undefined,
              kind: failed ? "error" : "success",
            });
            setCheckedIds(new Set());
          }
        },
      });
    });
  };

  const totalTasks = selectedBatch?.total_tasks ?? 0;
  const pendingReview = selectedBatch?.review_tasks ?? 0;
  const approvedDone = selectedBatch?.completed_tasks ?? 0;
  const unsubmitted = selectedBatch
    ? Math.max(
        0,
        selectedBatch.total_tasks - selectedBatch.review_tasks - selectedBatch.completed_tasks,
      )
    : 0;
  const reviewPct = totalTasks ? Math.round((pendingReview / totalTasks) * 1000) / 10 : 0;
  const approvedPct = totalTasks ? Math.round((approvedDone / totalTasks) * 1000) / 10 : 0;

  // 纯落地态（无 batch / project / assignee 选择）展示批次卡片网格；其余维持任务列表。
  const hasQueueSelection = Boolean(selectedBatchId || selectedProjectId || assigneeFilter);
  const showOverview = !hasQueueSelection && sidebarBatches.length > 0;

  return (
    <div className="mx-auto box-border grid h-full max-w-[1480px] grid-cols-[300px_1fr] gap-4 px-6 py-5 text-foreground max-[900px]:h-auto max-[900px]:grid-cols-1 max-[900px]:p-4">
      <aside className="max-h-[calc(100vh-80px)] self-stretch overflow-auto rounded-md border border-border bg-card max-[900px]:max-h-none">
        <div className="border-b border-border px-3.5 py-3">
          <div className="text-sm font-semibold">项目 · 批次</div>
          <div className="mt-0.5 text-xs text-muted-foreground">按项目分组的待审核批次</div>
        </div>
        {reviewerStatsLoading ? (
          <div className="p-6 text-center text-xs text-muted-foreground">加载中...</div>
        ) : reviewerStatsInitialPaused ? (
          <QueryPausedState resource="审核批次" compact />
        ) : reviewerStatsInitialError ? (
          <QueryErrorState
            resource="审核批次"
            error={reviewerStatsQuery.error}
            onRetry={() => void reviewerStatsQuery.refetch()}
            compact
          />
        ) : (
          <ReviewSidebar
            batches={sidebarBatches}
            selectedBatchId={selectedBatchId}
            onSelect={handleSelectBatch}
          />
        )}
      </aside>

      <section className="max-h-[calc(100vh-80px)] min-w-0 overflow-auto max-[900px]:max-h-none">
        <div className="mb-4 flex items-start justify-between gap-4">
          <div className="min-w-0">
            {hasQueueSelection && (
              <button
                type="button"
                className="mb-1.5 inline-flex cursor-pointer appearance-none items-center gap-1 border-none bg-transparent p-0 text-xs text-muted-foreground hover:text-brand"
                onClick={backToOverview}
              >
                <Icon name="chevLeft" size={12} />
                返回全部批次
              </button>
            )}
            <h1 className="m-0 text-xl font-bold">
              {selectedBatch ? selectedBatch.batch_name : "质检审核"}
            </h1>
            <p className="mt-1 text-sm text-muted-foreground">
              {selectedBatch ? (
                <>
                  <span className="mono text-brand">{selectedBatch.batch_display_id}</span>
                  <span> · {selectedBatch.project_name}</span>
                  <span>
                    {" "}
                    · 共 {selectedBatch.total_tasks} 任务 · {selectedBatch.review_tasks} 待审 ·{" "}
                    {selectedBatch.completed_tasks} 已通过
                  </span>
                </>
              ) : (
                <>选择一个批次开始审核；点击行可在右侧画布预览，多选支持批量通过 / 退回</>
              )}
            </p>
            {assigneeFilter && (
              <FilterGroup compact label="指派范围" hideLabel className="mt-2">
                <ActiveFilterChip label="仅看指派标注员" onRemove={clearAssigneeFilter} />
              </FilterGroup>
            )}
          </div>
          {selectedBatchId && (
            <div className="flex shrink-0 gap-1.5">
              <Button
                size="sm"
                onClick={() => tasks[0] && openTask(tasks[0].id)}
                disabled={tasks.length === 0}
              >
                <Icon name="target" size={11} />
                打开画布
              </Button>
              <Button
                size="sm"
                variant="danger"
                onClick={() => void handleBatchReject()}
                disabled={rejectBatchMut.isPending}
              >
                <Icon name="x" size={11} />
                整批退回
              </Button>
            </div>
          )}
        </div>

        {reviewerStatsRefreshPaused && <QueryPausedNotice resource="审核批次" />}

        {reviewerStatsQuery.isError && hasReviewerStats && (
          <RefreshNotice
            resource="审核批次"
            error={reviewerStatsQuery.error}
            onRetry={() => void reviewerStatsQuery.refetch()}
          />
        )}

        {reviewerStatsInitialError ? (
          <QueryErrorState
            resource="审核批次"
            error={reviewerStatsQuery.error}
            onRetry={() => void reviewerStatsQuery.refetch()}
          />
        ) : reviewerStatsInitialPaused ? (
          <QueryPausedState resource="审核批次" />
        ) : reviewerStatsLoading ? (
          <div className="p-10 text-center text-muted-foreground">加载中...</div>
        ) : taskListRefreshPaused ? (
          <QueryPausedNotice resource={loadMoreRequested ? "更多任务" : "任务列表"} />
        ) : taskListQuery.isError && hasTaskData ? (
          <RefreshNotice
            resource="任务列表"
            error={taskListQuery.error}
            onRetry={() => void taskListQuery.refetch()}
          />
        ) : null}

        {selectedBatch && (
          <div className="mb-3 rounded-md border border-border bg-card px-3 py-2.5">
            <div className="mb-1.5 flex items-center justify-between">
              <span className="text-xs text-muted-foreground">批次进度</span>
              <Badge variant="warning" dot>
                审核中
              </Badge>
            </div>
            {[
              { label: "待审", pct: reviewPct, count: pendingReview, bar: "bg-amber-500" },
              { label: "通过", pct: approvedPct, count: approvedDone, bar: "bg-emerald-500" },
            ].map((r) => (
              <div
                key={r.label}
                className="mt-1 flex items-center gap-2.5 text-xs text-muted-foreground"
              >
                <span className="flex-[0_0_48px]">{r.label}</span>
                <div className="h-[5px] flex-1 overflow-hidden rounded-sm bg-muted">
                  <ProgressFill pct={r.pct} barClass={r.bar} />
                </div>
                <span className="mono flex-[0_0_100px] text-right text-muted-foreground">
                  {r.count}/{selectedBatch.total_tasks} · {r.pct}%
                </span>
              </div>
            ))}
            {unsubmitted > 0 && (
              <div className="mt-1.5 text-xs text-muted-foreground">
                仍有 {unsubmitted} 个任务尚未提交质检
              </div>
            )}
          </div>
        )}

        {reviewerStatsInitialError ||
        reviewerStatsInitialPaused ||
        reviewerStatsLoading ? null : showOverview ? (
          <ReviewBatchCardGrid batches={sidebarBatches} onSelect={handleSelectBatch} />
        ) : isLoading ? (
          <div className="p-10 text-center text-muted-foreground">加载中...</div>
        ) : taskListInitialPaused ? (
          <QueryPausedState resource="待审核任务" />
        ) : taskListQuery.isError && !hasTaskData ? (
          <QueryErrorState
            resource="待审核任务"
            error={taskListQuery.error}
            onRetry={() => void taskListQuery.refetch()}
          />
        ) : tasks.length === 0 ? (
          <div className="p-15 text-center text-muted-foreground">
            <Icon name="check" size={40} className="mx-auto mb-3 opacity-30" />
            <div className="text-sm">
              {selectedBatchId ? "该批次暂无待审核任务" : "暂无待审核任务"}
            </div>
          </div>
        ) : (
          <>
            <div
              className={`mb-3 flex items-center justify-between rounded-md border border-border px-3 py-2 text-xs text-muted-foreground ${
                checkedIds.size > 0 ? "bg-brand/10" : "bg-card"
              }`}
            >
              <label className="inline-flex cursor-pointer items-center gap-2">
                <input
                  type="checkbox"
                  checked={checkedIds.size > 0 && checkedIds.size === tasks.length}
                  onChange={toggleAll}
                  className="cursor-pointer accent-brand"
                />
                <span>
                  {checkedIds.size > 0
                    ? `已选 ${checkedIds.size}/${tasks.length}`
                    : `共 ${total} 个待审核任务${tasks.length < total ? `（已加载 ${tasks.length}）` : ""}`}
                </span>
              </label>
              {checkedIds.size > 0 && (
                <div className="flex gap-1.5">
                  <Button
                    variant="primary"
                    size="sm"
                    onClick={() =>
                      runBatchApprove(queueScopeKeyRef.current, checkedIdsKeyRef.current)
                    }
                  >
                    <Icon name="check" size={11} />
                    批量通过 ({checkedIds.size})
                  </Button>
                  <Button variant="danger" size="sm" onClick={() => void handleTasksReject()}>
                    <Icon name="x" size={11} />
                    批量退回 ({checkedIds.size})
                  </Button>
                </div>
              )}
            </div>
            {tasks.map((t) => (
              <TaskRow
                key={t.id}
                task={t}
                checked={checkedIds.has(t.id)}
                onToggle={() => toggleChecked(t.id)}
                onOpen={() => openTask(t.id)}
              />
            ))}
            {taskListQuery.hasNextPage && (
              <div className="mb-4 flex items-center justify-center gap-3 rounded-md border border-border bg-card px-3 py-3">
                <span className="text-xs text-muted-foreground">
                  已加载 {tasks.length}
                  {typeof total === "number" ? ` / ${total}` : ""} 个任务
                </span>
                <Button
                  size="sm"
                  disabled={taskListQuery.isFetchingNextPage}
                  onClick={() => {
                    setLoadMoreRequested(true);
                    void Promise.resolve(taskListQuery.fetchNextPage()).catch(() => undefined);
                  }}
                >
                  {taskListQuery.isFetchingNextPage ? "加载中…" : "加载更多"}
                </Button>
              </div>
            )}
          </>
        )}
      </section>
    </div>
  );
}

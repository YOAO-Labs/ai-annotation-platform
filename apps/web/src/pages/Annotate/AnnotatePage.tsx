import { FilterGroup, FilterToggle } from "@/components/filters/FilterControls";
import { useEffect, useMemo, useState } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { Icon } from "@/components/ui/Icon";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { confirmDialog } from "@/components/ui/decisionDialog";
import { useToastStore } from "@/components/ui/Toast";
import { Thumbnail } from "@/components/Thumbnail";
import { useElementStyle } from "@/components/ui/useElementStyle";
import { flattenTaskPages, useTaskList } from "@/hooks/useTasks";
import { useMyBatches } from "@/hooks/useDashboard";
import { useSubmitBatch } from "@/hooks/useBatches";
import { ApiError } from "@/api/client";
import type { MyBatchItem } from "@/api/dashboard";
import type { TaskResponse } from "@/types";
import { AnnotateSidebar } from "./AnnotateSidebar";
import { BatchCardGrid } from "./BatchCardGrid";
import { buildWorkbenchUrl, currentWorkbenchReturnTo } from "@/utils/workbenchNavigation";
import {
  isInitialQueryPaused,
  isQueryPaused,
  isRefreshQueryPaused,
  QueryPausedNotice,
  QueryPausedState,
} from "@/pages/shared/QueryState";
import styles from "./AnnotatePage.module.css";
import type { CSSProperties } from "react";

const STATUS_BADGE: Record<
  string,
  { label: string; variant: "accent" | "warning" | "danger" | "outline" }
> = {
  active: { label: "未开始", variant: "outline" },
  annotating: { label: "标注中", variant: "accent" },
  reviewing: { label: "审核中", variant: "warning" },
  rejected: { label: "已驳回", variant: "danger" },
};

function ProgressFill({ pct, color }: { pct: number; color: string }) {
  const ref = useElementStyle<HTMLDivElement>({
    "--progress-pct": `${Math.min(100, pct)}%`,
    "--progress-color": color,
  } as CSSProperties);

  return <div ref={ref} className={styles.progressFill} />;
}

function TaskRow({ task, onOpen }: { task: TaskResponse; onOpen: () => void }) {
  const isLocked = task.status === "review" || task.status === "completed";
  // 与工作台任务队列 (TaskQueuePanel) 的标签 + 配色保持一致
  const statusLabel =
    task.status === "completed"
      ? "已完成"
      : task.status === "review"
        ? "待审核"
        : task.status === "rejected"
          ? "待重做"
          : task.total_annotations > 0
            ? "进行中"
            : task.total_predictions > 0
              ? "AI 已预标"
              : "未开始";
  const statusVariant =
    task.status === "completed"
      ? ("success" as const)
      : task.status === "review"
        ? ("warning" as const)
        : task.status === "rejected"
          ? ("danger" as const)
          : task.total_annotations > 0
            ? ("accent" as const)
            : task.total_predictions > 0
              ? ("ai" as const)
              : ("outline" as const);

  return (
    <div onClick={onOpen} className={styles.taskRow}>
      <Thumbnail src={task.thumbnail_url} blurhash={task.blurhash} width={40} height={40} />
      <div className={styles.taskMain}>
        <div className={styles.taskTitleRow}>
          <span className={`mono ${styles.taskId}`}>{task.display_id}</span>
          <span className={styles.taskFileName}>{task.file_name}</span>
        </div>
        <div className={styles.taskMeta}>
          {task.total_annotations} 个标注 · {task.total_predictions} 个预测
        </div>
      </div>
      <div className={styles.taskStatus}>
        <Badge variant={statusVariant} dot>
          {statusLabel}
        </Badge>
      </div>
      <div className={styles.lockCell}>
        {isLocked && (
          <span title="已锁定" className={styles.lockBadge}>
            <Icon name="lock" size={11} />
            已锁定
          </span>
        )}
      </div>
      <div className={styles.actionCell}>
        <Button
          size="sm"
          variant="primary"
          onClick={(e) => {
            e.stopPropagation();
            onOpen();
          }}
        >
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
    <div className={`${styles.errorState} ${compact ? styles.errorStateCompact : ""}`} role="alert">
      <Icon name="warning" size={compact ? 20 : 28} className={styles.errorIcon} />
      <div className={styles.errorTitle}>无法加载{resource}</div>
      <div className={styles.errorMessage}>{queryErrorMessage(error, resource)}</div>
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
    <div className={styles.refreshNotice} role="alert">
      <div>
        <strong>{resource}更新失败</strong>
        <span>{queryErrorMessage(error, resource)} 当前内容已保留。</span>
      </div>
      <Button size="sm" onClick={onRetry}>
        重新加载
      </Button>
    </div>
  );
}

export function AnnotatePage() {
  const pushToast = useToastStore((s) => s.push);
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams, setSearchParams] = useSearchParams();
  const selectedBatchId = searchParams.get("batch") ?? "";
  const rejectedOnly = searchParams.get("status") === "rejected";

  const batchesQuery = useMyBatches();
  const batches = useMemo(() => batchesQuery.data ?? [], [batchesQuery.data]);
  const visibleBatches = useMemo(
    () => (rejectedOnly ? batches.filter((batch) => batch.rejected_tasks > 0) : batches),
    [batches, rejectedOnly],
  );
  const hasBatchData = batchesQuery.data !== undefined;
  const batchesLoading = batchesQuery.isLoading && !hasBatchData;
  const batchesInitialPaused = isInitialQueryPaused(batchesQuery, hasBatchData);
  const batchesRefreshPaused = isRefreshQueryPaused(batchesQuery, hasBatchData);
  const batchesInitialError = batchesQuery.isError && !hasBatchData;
  const selectedBatch = useMemo(
    () => batches.find((b) => b.batch_id === selectedBatchId) ?? null,
    [batches, selectedBatchId],
  );

  const projectId = selectedBatch?.project_id;
  const taskListParams = useMemo(
    () =>
      selectedBatchId
        ? { batch_id: selectedBatchId, ...(rejectedOnly ? { status: "rejected" } : {}) }
        : undefined,
    [selectedBatchId, rejectedOnly],
  );
  const taskListQuery = useTaskList(projectId, taskListParams);
  const taskListData = taskListQuery.data;
  const hasTaskData = taskListData !== undefined;
  const tasksLoading = taskListQuery.isLoading && !hasTaskData;
  const tasksInitialPaused = isInitialQueryPaused(taskListQuery, hasTaskData);
  const tasksRefreshPaused = isRefreshQueryPaused(taskListQuery, hasTaskData);
  const taskListPaused = isQueryPaused(taskListQuery);
  const [loadMoreRequested, setLoadMoreRequested] = useState(false);
  useEffect(() => {
    if (!taskListPaused && !taskListQuery.isFetchingNextPage) {
      setLoadMoreRequested(false);
    }
  }, [taskListPaused, taskListQuery.isFetchingNextPage]);
  const tasks = useMemo(() => flattenTaskPages(taskListData?.pages), [taskListData?.pages]);
  const total = taskListData?.pages[0]?.total ?? tasks.length;

  const submitBatchMut = useSubmitBatch(projectId ?? "");
  const submitBatch = submitBatchMut.mutate;
  const handleSubmitBatch = (batch: MyBatchItem) => {
    submitBatch(batch.batch_id, {
      onSuccess: (result) => {
        pushToast({
          msg: `已提交 ${result.submitted_tasks} 个任务质检`,
          sub:
            result.remaining_tasks > 0
              ? `仍有 ${result.remaining_tasks} 个任务未送审`
              : "等待审核员处理",
          kind: "success",
        });
      },
      onError: (e) => {
        const msg = e instanceof Error ? e.message : "提交失败";
        pushToast({ msg: "提交质检失败", sub: msg, kind: "error" });
      },
    });
  };

  const handleSelectBatch = (b: MyBatchItem | null) => {
    setSearchParams((previous) => {
      const next = new URLSearchParams(previous);
      if (b) next.set("batch", b.batch_id);
      else next.delete("batch");
      return next;
    });
  };

  const openWorkbench = (taskId?: string) => {
    if (!selectedBatch) return;
    navigate(
      buildWorkbenchUrl(selectedBatch.project_id, {
        batchId: selectedBatch.batch_id,
        taskId,
        returnTo: currentWorkbenchReturnTo(location),
      }),
    );
  };

  // B-20：分三档进度 — 已动工 / 送审 / 已通过；提交按钮不再以 allDone 强制门禁，
  // 让标注员能整批提交（剩余 pending 由 confirm 二次确认）。
  const inProgress = selectedBatch?.in_progress_tasks ?? 0;
  const startedDone = selectedBatch
    ? inProgress + selectedBatch.review_tasks + selectedBatch.completed_tasks
    : 0;
  const reviewDone = selectedBatch ? selectedBatch.review_tasks + selectedBatch.completed_tasks : 0;
  const approvedDone = selectedBatch?.completed_tasks ?? 0;
  const totalTasks = selectedBatch?.total_tasks ?? 0;
  const startedPct = totalTasks ? Math.round((startedDone / totalTasks) * 1000) / 10 : 0;
  const reviewPct = totalTasks ? Math.round((reviewDone / totalTasks) * 1000) / 10 : 0;
  const approvedPct = totalTasks ? Math.round((approvedDone / totalTasks) * 1000) / 10 : 0;
  const pendingTasks = Math.max(0, totalTasks - startedDone);
  // 可整批送审 = 尚未提交质检的任务（pending + in_progress）。rejected 需先重做，
  // 因此不计入；按钮按“是否还有未送审任务”显示，而不是只看批次状态。
  const rejectedTasks = selectedBatch?.rejected_tasks ?? 0;
  const unsubmittedTasks = Math.max(
    0,
    totalTasks - (selectedBatch?.review_tasks ?? 0) - approvedDone - rejectedTasks,
  );
  const canSubmitBatch =
    !!selectedBatch &&
    unsubmittedTasks > 0 &&
    (selectedBatch.status === "annotating" || selectedBatch.status === "reviewing");

  // 与 Dashboard MyBatchesCard 的提交确认同一 tone/voice(计划 Group B 要求)。
  const confirmSubmitBatch = async (batch: MyBatchItem, pendingCount: number) => {
    if (
      !(await confirmDialog({
        title: "提交批次质检",
        description: `批次「${batch.batch_name}」仍有 ${pendingCount} 个任务未送审，提交后将整批锁定，无法继续修改。`,
        confirmLabel: "提交质检",
      }))
    )
      return;
    handleSubmitBatch(batch);
  };

  return (
    <div className={styles.page}>
      <aside className={styles.sidebarShell}>
        <div className={styles.sidebarHeader}>
          <div className={styles.sidebarTitle}>项目 · 批次</div>
          <div className={styles.sidebarSubtitle}>按项目分组的我的批次</div>
        </div>
        {batchesLoading ? (
          <div className={styles.sidebarLoading}>加载中...</div>
        ) : batchesInitialPaused ? (
          <QueryPausedState resource="分派批次" compact />
        ) : batchesInitialError ? (
          <QueryErrorState
            resource="分派批次"
            error={batchesQuery.error}
            onRetry={() => void batchesQuery.refetch()}
            compact
          />
        ) : (
          <AnnotateSidebar
            batches={visibleBatches}
            selectedBatchId={selectedBatchId}
            onSelect={handleSelectBatch}
          />
        )}
      </aside>

      <section className={styles.content}>
        <div className={styles.header}>
          <div className={styles.headerText}>
            {selectedBatch && (
              <button
                type="button"
                className={styles.backLink}
                onClick={() => handleSelectBatch(null)}
              >
                <Icon name="chevLeft" size={12} />
                返回全部批次
              </button>
            )}
            <h1 className={styles.title}>
              {selectedBatch ? selectedBatch.batch_name : "标注工作台"}
            </h1>
            <p className={styles.subtitle}>
              {selectedBatch ? (
                <>
                  <span className={`mono ${styles.accentText}`}>
                    {selectedBatch.batch_display_id}
                  </span>
                  <span> · {selectedBatch.project_name}</span>
                  <span> · 共 {selectedBatch.total_tasks} 任务</span>
                  {pendingTasks > 0 && <span> · 待标 {pendingTasks}</span>}
                  {inProgress > 0 && <span> · 标注中 {inProgress}</span>}
                  {selectedBatch.review_tasks > 0 && (
                    <span> · 送审 {selectedBatch.review_tasks}</span>
                  )}
                  {selectedBatch.completed_tasks > 0 && (
                    <span> · 已通过 {selectedBatch.completed_tasks}</span>
                  )}
                </>
              ) : (
                <>选择一个批次开始标注；任务进度在画布内自动同步</>
              )}
            </p>
          </div>
          {selectedBatch && (
            <div className={styles.headerActions}>
              {canSubmitBatch && (
                <Button
                  variant="primary"
                  size="sm"
                  disabled={submitBatchMut.isPending}
                  title={
                    unsubmittedTasks > 0
                      ? `仍有 ${unsubmittedTasks} 个任务未送审；确认后整批提交`
                      : "整批提交质检"
                  }
                  onClick={() => {
                    void confirmSubmitBatch(selectedBatch, unsubmittedTasks);
                  }}
                >
                  <Icon name="check" size={11} />
                  提交质检
                </Button>
              )}
              <Button size="sm" onClick={() => openWorkbench()}>
                <Icon name="target" size={11} />
                打开画布
              </Button>
            </div>
          )}
        </div>

        {selectedBatch?.status === "rejected" && selectedBatch.review_feedback && (
          <div className={styles.rejectFeedback}>
            <strong className={styles.dangerText}>审核员驳回反馈：</strong>
            <div className={styles.rejectFeedbackBody}>{selectedBatch.review_feedback}</div>
          </div>
        )}

        {selectedBatch && (
          <div className={styles.progressCard}>
            <div className={styles.progressHeader}>
              <span className={styles.progressTitle}>批次进度</span>
              <Badge variant={STATUS_BADGE[selectedBatch.status]?.variant ?? "outline"} dot>
                {STATUS_BADGE[selectedBatch.status]?.label ?? selectedBatch.status}
              </Badge>
            </div>
            {[
              { label: "标注中", pct: startedPct, count: startedDone, bar: "var(--sc-brand)" },
              { label: "送审", pct: reviewPct, count: reviewDone, bar: "var(--sc-caution)" },
              { label: "通过", pct: approvedPct, count: approvedDone, bar: "var(--sc-positive)" },
            ].map((r) => (
              <div key={r.label} className={styles.progressRow}>
                <span className={styles.progressLabel}>{r.label}</span>
                <div className={styles.progressTrack}>
                  <ProgressFill pct={r.pct} color={r.bar} />
                </div>
                <span className={`mono ${styles.progressValue}`}>
                  {r.count}/{selectedBatch.total_tasks} · {r.pct}%
                </span>
              </div>
            ))}
          </div>
        )}

        <FilterGroup label="任务状态" hideLabel aria-label="任务状态筛选" className="mb-3">
          <FilterToggle
            active={!rejectedOnly}
            onClick={() =>
              setSearchParams((previous) => {
                const next = new URLSearchParams(previous);
                next.delete("status");
                return next;
              })
            }
          >
            全部任务
          </FilterToggle>
          <FilterToggle
            active={rejectedOnly}
            onClick={() =>
              setSearchParams((previous) => {
                const next = new URLSearchParams(previous);
                next.set("status", "rejected");
                return next;
              })
            }
          >
            待重做
          </FilterToggle>
        </FilterGroup>
        {batchesRefreshPaused && <QueryPausedNotice resource="分派批次" />}

        {batchesQuery.isError && hasBatchData && (
          <RefreshNotice
            resource="分派批次"
            error={batchesQuery.error}
            onRetry={() => void batchesQuery.refetch()}
          />
        )}

        {tasksRefreshPaused && (
          <QueryPausedNotice resource={loadMoreRequested ? "更多任务" : "任务列表"} />
        )}

        {selectedBatch && taskListQuery.isError && hasTaskData && (
          <RefreshNotice
            resource="任务列表"
            error={taskListQuery.error}
            onRetry={() => void taskListQuery.refetch()}
          />
        )}

        {!selectedBatch ? (
          batchesLoading ? (
            <div className={styles.loadingState}>加载中...</div>
          ) : batchesInitialPaused ? (
            <QueryPausedState resource="分派批次" />
          ) : batchesInitialError ? (
            <QueryErrorState
              resource="分派批次"
              error={batchesQuery.error}
              onRetry={() => void batchesQuery.refetch()}
            />
          ) : visibleBatches.length === 0 ? (
            <div className={styles.emptyState}>
              <Icon name="inbox" size={40} className={styles.emptyIcon} />
              <div className={styles.emptyTitle}>
                {rejectedOnly ? "没有待重做任务" : "暂无分派批次"}
              </div>
            </div>
          ) : (
            <BatchCardGrid batches={visibleBatches} onSelect={handleSelectBatch} />
          )
        ) : tasksLoading ? (
          <div className={styles.loadingState}>加载中...</div>
        ) : tasksInitialPaused ? (
          <QueryPausedState resource="任务列表" />
        ) : taskListQuery.isError && !hasTaskData ? (
          <QueryErrorState
            resource="任务列表"
            error={taskListQuery.error}
            onRetry={() => void taskListQuery.refetch()}
          />
        ) : tasks.length === 0 ? (
          <div className={styles.emptyState}>
            <Icon name="inbox" size={40} className={styles.emptyIcon} />
            <div className={styles.emptyTitle}>
              {rejectedOnly ? "该批次没有待重做任务" : "该批次暂无任务"}
            </div>
          </div>
        ) : (
          <>
            <div className={styles.taskListSummary}>
              <span>
                共 {total} 个任务{tasks.length < total && `（已加载 ${tasks.length}）`}
              </span>
              <span className={styles.taskListHint}>点击行打开画布 · 进度自动保存</span>
            </div>
            {tasks.map((t) => (
              <TaskRow key={t.id} task={t} onOpen={() => openWorkbench(t.id)} />
            ))}
            {taskListQuery.hasNextPage && (
              <div className={styles.loadMore}>
                <span className={styles.loadMoreText}>
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

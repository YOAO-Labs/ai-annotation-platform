import { useState, type CSSProperties } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Icon } from "@/components/ui/Icon";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";
import { useToastStore } from "@/components/ui/Toast";
import { AssigneeAvatarStack } from "@/components/ui/AssigneeAvatarStack";
import { confirmDialog } from "@/components/ui/decisionDialog";
import { useElementStyle } from "@/components/ui/useElementStyle";
import { useMyBatches } from "@/hooks/useDashboard";
import { ApiError } from "@/api/client";
import { batchesApi } from "@/api/batches";
import type { MyBatchItem } from "@/api/dashboard";
import {
  isInitialQueryPaused,
  isRefreshQueryPaused,
  QueryPausedNotice,
  QueryPausedState,
} from "@/pages/shared/QueryState";

const STATUS_LABEL: Record<
  string,
  { label: string; variant: "accent" | "warning" | "danger" | "outline" }
> = {
  active: { label: "未开始", variant: "outline" },
  annotating: { label: "标注中", variant: "accent" },
  reviewing: { label: "审核中", variant: "warning" },
  rejected: { label: "已驳回", variant: "danger" },
};

function ProgressFill({ pct, barClass }: { pct: number; barClass: string }) {
  const ref = useElementStyle<HTMLDivElement>({
    "--progress-pct": `${Math.min(100, pct)}%`,
  } as CSSProperties);

  return <div ref={ref} className={`h-full w-[var(--progress-pct)] ${barClass}`} />;
}

function batchesErrorMessage(error: unknown): string {
  if (error instanceof ApiError && error.status === 403) {
    return "当前账号没有权限查看分派批次，请联系项目管理员。";
  }
  if (error instanceof ApiError && error.status === 404) {
    return "分派批次已不存在或已被移除，请刷新后重试。";
  }
  if (error instanceof ApiError && error.status >= 500) {
    return "分派批次服务暂时不可用，请稍后重试。";
  }
  return "分派批次加载失败，请检查网络后重试。";
}

/** 未送审任务 = pending + in_progress（rejected 需先重做，不计入整批送审）。 */
function unsubmittedTasks(b: MyBatchItem): number {
  return Math.max(0, b.total_tasks - b.review_tasks - b.completed_tasks - b.rejected_tasks);
}

/** 整批送审入口按“是否还有未送审任务”显示，而不是只看批次状态。 */
function canBatchSubmit(b: MyBatchItem): boolean {
  return unsubmittedTasks(b) > 0 && (b.status === "annotating" || b.status === "reviewing");
}

/** B-20：标注员视角的三段进度条 — 已动工 / 送审 / 通过。
 *  三条进度独立显示，文字小号附在右侧，避免占用太多行高。 */
function ProgressTriple({
  startedPct,
  reviewPct,
  approvedPct,
  startedCount,
  reviewCount,
  approvedCount,
  total,
}: {
  startedPct: number;
  reviewPct: number;
  approvedPct: number;
  startedCount: number;
  reviewCount: number;
  approvedCount: number;
  total: number;
}) {
  const ROWS: { label: string; pct: number; count: number; bar: string }[] = [
    { label: "标注中", pct: startedPct, count: startedCount, bar: "bg-brand" },
    { label: "送审", pct: reviewPct, count: reviewCount, bar: "bg-amber-500" },
    { label: "通过", pct: approvedPct, count: approvedCount, bar: "bg-emerald-500" },
  ];
  return (
    <div className="mt-1.5 grid max-w-[420px] gap-1">
      {ROWS.map((r) => (
        <div key={r.label} className="flex items-center gap-2 text-xs text-muted-foreground">
          <span className="flex-[0_0_36px]">{r.label}</span>
          <div className="h-1 flex-1 overflow-hidden rounded-sm bg-muted">
            <ProgressFill pct={r.pct} barClass={r.bar} />
          </div>
          <span className="mono flex-[0_0_80px] text-right text-muted-foreground">
            {r.count}/{total} · {r.pct}%
          </span>
        </div>
      ))}
    </div>
  );
}

/** v0.7.1 · 标注员 dashboard 的「我的批次」卡片，行版式与 ReviewerDashboard
 *  「审核中批次」对齐：button-row + display_id · 项目 · 计数 + 右侧进度% + chev。
 *  额外的 状态徽章 / 提交质检 / 修改 等动作放在右侧 action 区。 */
export function MyBatchesCard() {
  const batchesQuery = useMyBatches();
  const batches = batchesQuery.data ?? [];
  const hasBatchData = batchesQuery.data !== undefined;
  const isLoading = batchesQuery.isLoading && !hasBatchData;
  const initialPaused = isInitialQueryPaused(batchesQuery, hasBatchData);
  const refreshPaused = isRefreshQueryPaused(batchesQuery, hasBatchData);
  const navigate = useNavigate();
  const pushToast = useToastStore((s) => s.push);
  const qc = useQueryClient();
  const [submittingId, setSubmittingId] = useState<string | null>(null);
  // B-22：多选提交 — 选中态以 batch_id 集合保存，触发批量提交时按顺序串行调用。
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [bulkSubmitting, setBulkSubmitting] = useState(false);

  const submitMut = useMutation({
    mutationFn: (b: MyBatchItem) => batchesApi.submitReview(b.project_id, b.batch_id),
    onMutate: (b) => setSubmittingId(b.batch_id),
    onSuccess: (result) => {
      pushToast({
        msg: `已提交 ${result.submitted_tasks} 个任务质检`,
        sub:
          result.remaining_tasks > 0
            ? `仍有 ${result.remaining_tasks} 个任务未送审`
            : "等待审核员处理",
        kind: "success",
      });
      qc.invalidateQueries({ queryKey: ["dashboard", "annotator"] });
      qc.invalidateQueries({ queryKey: ["batches"] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["task"] });
      qc.invalidateQueries({ queryKey: ["projects"] });
    },
    onError: (e) => {
      const msg = e instanceof Error ? e.message : "提交失败";
      pushToast({ msg: "提交质检失败", sub: msg, kind: "error" });
    },
    onSettled: () => setSubmittingId(null),
  });

  const toggleSelected = (id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  if (initialPaused) {
    return (
      <div className="mt-4">
        <Card>
          <div className="border-b border-border px-4 py-3.5">
            <h3 className="m-0 text-sm font-semibold">我的批次</h3>
          </div>
          <QueryPausedState resource="分派批次" compact />
        </Card>
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className="mt-4">
        <Card>
          <div className="border-b border-border px-4 py-3.5">
            <h3 className="m-0 text-sm font-semibold">我的批次</h3>
          </div>
          <div className="px-4 py-8 text-center text-sm text-muted-foreground">加载中...</div>
        </Card>
      </div>
    );
  }

  if (batchesQuery.isError && !hasBatchData) {
    return (
      <div className="mt-4">
        <Card>
          <div className="border-b border-border px-4 py-3.5">
            <h3 className="m-0 text-sm font-semibold">我的批次</h3>
          </div>
          <div role="alert" className="p-4 text-sm">
            <div className="text-status-danger">{batchesErrorMessage(batchesQuery.error)}</div>
            <Button size="sm" className="mt-3" onClick={() => void batchesQuery.refetch()}>
              重新加载
            </Button>
          </div>
        </Card>
      </div>
    );
  }

  if (batches.length === 0 && !batchesQuery.isError) return null;

  // annotating 排前面，rejected 次之，其他靠后；同状态按 display_id 自然序
  const STATUS_ORDER: Record<string, number> = {
    annotating: 0,
    rejected: 1,
    active: 2,
    reviewing: 3,
  };
  const sorted = [...batches].sort((a, b) => {
    const ra = STATUS_ORDER[a.status] ?? 9;
    const rb = STATUS_ORDER[b.status] ?? 9;
    if (ra !== rb) return ra - rb;
    return a.batch_display_id.localeCompare(b.batch_display_id);
  });

  // B-22 改：可批量提交的批次 = 仍有未送审任务（pending / in_progress）的 annotating /
  // reviewing 批次。用户反馈"提交质检仍无效"是因为旧门槛要求所有任务都送审，这里放宽到
  // 只要还有未送审任务即可，让标注员可以中途整批提交（剩余任务由 confirm 提示）。
  const submittable = sorted.filter(canBatchSubmit);
  const selectedSubmittable = submittable.filter((b) => selectedIds.has(b.batch_id));

  // 与 AnnotatePage 的单批提交确认同一 tone/voice(计划 Group B 要求)。
  const confirmBatchSubmit = async (b: MyBatchItem, pendingCount: number) => {
    if (
      !(await confirmDialog({
        title: "提交批次质检",
        description: `批次「${b.batch_name}」仍有 ${pendingCount} 个任务未送审，提交后将整批锁定，无法继续修改。`,
        confirmLabel: "提交质检",
      }))
    )
      return;
    submitMut.mutate(b);
  };

  const handleBulkSubmit = async () => {
    if (selectedSubmittable.length === 0) return;
    if (
      !(await confirmDialog({
        title: "批量提交质检",
        description: "选中批次的未送审任务将全部提交并锁定，无法继续修改。",
        details: `将提交 ${selectedSubmittable.length} 个批次`,
        confirmLabel: "提交质检",
      }))
    )
      return;
    setBulkSubmitting(true);
    let okCount = 0;
    let taskCount = 0;
    const errors: string[] = [];
    for (const b of selectedSubmittable) {
      try {
        const result = await batchesApi.submitReview(b.project_id, b.batch_id);
        okCount += 1;
        taskCount += result.submitted_tasks;
      } catch (e) {
        errors.push(`${b.batch_display_id}: ${e instanceof Error ? e.message : "失败"}`);
      }
    }
    setBulkSubmitting(false);
    setSelectedIds(new Set());
    qc.invalidateQueries({ queryKey: ["dashboard", "annotator"] });
    qc.invalidateQueries({ queryKey: ["batches"] });
    qc.invalidateQueries({ queryKey: ["tasks"] });
    qc.invalidateQueries({ queryKey: ["task"] });
    qc.invalidateQueries({ queryKey: ["projects"] });
    if (errors.length === 0) {
      pushToast({
        msg: `已批量提交 ${okCount} 个批次 / ${taskCount} 个任务`,
        sub: "等待审核员处理",
        kind: "success",
      });
    } else {
      pushToast({
        msg: `${okCount} 成功 / ${errors.length} 失败`,
        sub: errors.slice(0, 2).join("; "),
        kind: "warning",
      });
    }
  };

  return (
    <div className="mt-4">
      <Card>
        <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3.5">
          <h3 className="m-0 text-sm font-semibold">
            我的批次
            <span className="ml-2 text-xs">
              <Badge variant="accent">{batches.length}</Badge>
            </span>
          </h3>
          {submittable.length > 0 && (
            <div className="flex items-center gap-2">
              <span className="text-xs text-muted-foreground">
                已选 {selectedSubmittable.length} / {submittable.length} 可提交
              </span>
              <Button
                size="sm"
                variant="primary"
                disabled={selectedSubmittable.length === 0 || bulkSubmitting}
                onClick={handleBulkSubmit}
                title="批量将选中批次提交质检"
              >
                <Icon name="check" size={11} />
                批量提交质检
              </Button>
            </div>
          )}
        </div>
        {refreshPaused && <QueryPausedNotice resource="分派批次" />}
        {batchesQuery.isError && hasBatchData && (
          <div
            role="alert"
            className="mx-4 mt-3 flex flex-wrap items-center justify-between gap-2 rounded-md border border-border bg-status-caution-soft px-3 py-2 text-xs"
          >
            <span className="text-muted-foreground">
              <strong className="mr-1 text-status-caution">批次更新失败</strong>
              {batchesErrorMessage(batchesQuery.error)} 当前内容已保留。
            </span>
            <Button size="sm" onClick={() => void batchesQuery.refetch()}>
              重新加载
            </Button>
          </div>
        )}
        <div className="py-2">
          {sorted.map((b) => {
            const meta = STATUS_LABEL[b.status] ?? { label: b.status, variant: "outline" as const };
            // B-20：分三档进度，每档独立条 — "标注中"(已动工含 in_progress) / "送审" / "审核通过"。
            // 旧版只用 completed/total 计算，导致 reviewer 没批复前进度永远 0%，且与后端三态语义脱节。
            const inProgress = b.in_progress_tasks ?? 0;
            const startedDone = inProgress + b.review_tasks + b.completed_tasks;
            const reviewDone = b.review_tasks + b.completed_tasks;
            const approvedDone = b.completed_tasks;
            const pendingTasks = Math.max(0, b.total_tasks - startedDone);
            const total = b.total_tasks || 1;
            const startedPct = Math.round((startedDone / total) * 1000) / 10;
            const reviewPct = Math.round((reviewDone / total) * 1000) / 10;
            const approvedPct = Math.round((approvedDone / total) * 1000) / 10;
            const annotateUrl = `/annotate?batch=${b.batch_id}`;

            const canSelect = canBatchSubmit(b);
            const submittableCount = unsubmittedTasks(b);
            return (
              <button
                key={b.batch_id}
                type="button"
                onClick={() => navigate(annotateUrl)}
                className="flex w-full cursor-pointer appearance-none items-center justify-between gap-3 border-0 border-t border-border bg-transparent px-4 py-2.5 text-left text-inherit [font-family:inherit] first:border-t-0"
              >
                {canSelect && (
                  <input
                    type="checkbox"
                    checked={selectedIds.has(b.batch_id)}
                    onClick={(e) => e.stopPropagation()}
                    onChange={() => toggleSelected(b.batch_id)}
                    title="选中以批量提交质检"
                    className="m-0 flex-[0_0_auto] cursor-pointer"
                  />
                )}
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium">{b.batch_name}</span>
                    <Badge variant={meta.variant} dot>
                      {meta.label}
                    </Badge>
                  </div>
                  <div className="mt-0.5 text-xs text-muted-foreground">
                    <span className="mono">{b.batch_display_id}</span>
                    <span> · {b.project_name}</span>
                    <span> · 共 {b.total_tasks} 任务</span>
                    {pendingTasks > 0 && <span> · 待标 {pendingTasks}</span>}
                    {inProgress > 0 && <span> · 标注中 {inProgress}</span>}
                    {b.review_tasks > 0 && <span> · 送审 {b.review_tasks}</span>}
                    {b.completed_tasks > 0 && <span> · 已通过 {b.completed_tasks}</span>}
                  </div>
                  <ProgressTriple
                    startedPct={startedPct}
                    reviewPct={reviewPct}
                    approvedPct={approvedPct}
                    startedCount={startedDone}
                    reviewCount={reviewDone}
                    approvedCount={approvedDone}
                    total={b.total_tasks}
                  />
                  {b.reviewer && (
                    <div className="mt-1.5">
                      <AssigneeAvatarStack users={[b.reviewer]} label="审核员" max={1} />
                    </div>
                  )}
                  {b.status === "rejected" && b.review_feedback && (
                    <div
                      className="mt-1.5 max-w-[600px] border-l-2 border-rose-500 bg-status-danger-soft px-2 py-1 text-xs text-muted-foreground"
                      title={b.review_feedback}
                    >
                      <strong className="text-status-danger">驳回原因：</strong>
                      {b.review_feedback.length > 100
                        ? b.review_feedback.slice(0, 100) + "..."
                        : b.review_feedback}
                    </div>
                  )}
                </div>

                <div className="flex flex-[0_0_auto] items-center gap-2">
                  {canSelect && (
                    <Button
                      variant="primary"
                      size="sm"
                      disabled={submittingId === b.batch_id || bulkSubmitting}
                      title={`仍有 ${submittableCount} 个任务未送审；确认后整批提交`}
                      onClick={(e) => {
                        e.stopPropagation();
                        void confirmBatchSubmit(b, submittableCount);
                      }}
                    >
                      <Icon name="check" size={11} />
                      提交质检
                    </Button>
                  )}
                  {b.status === "rejected" && (
                    <Button
                      variant="primary"
                      size="sm"
                      onClick={(e) => {
                        e.stopPropagation();
                        navigate(annotateUrl);
                      }}
                    >
                      <Icon name="refresh" size={11} />
                      继续修改
                    </Button>
                  )}
                  <Icon name="chevRight" size={14} />
                </div>
              </button>
            );
          })}
        </div>
      </Card>
    </div>
  );
}

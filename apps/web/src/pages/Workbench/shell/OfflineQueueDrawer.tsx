import { FilterGroup, FilterToggle } from "@/components/filters/FilterControls";
import { useCallback, useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { confirmDialog } from "@/components/ui/decisionDialog";
import { Icon } from "@/components/ui/Icon";
import { useToastStore } from "@/components/ui/Toast";
import {
  type OfflineOp,
  getAll,
  removeDurablyById,
  drain,
  subscribe,
  type DrainOptions,
  type OfflineQueueScope,
} from "../state/offlineQueue";

interface OfflineQueueDrawerProps {
  open: boolean;
  onClose: () => void;
  currentTaskId?: string;
  queueScope?: OfflineQueueScope;
  onFlushOne: (op: OfflineOp) => Promise<void>;
  onFlushAll: () => Promise<void>;
  classifyError?: DrainOptions["classifyError"];
}

function formatTs(ts: number): string {
  const d = new Date(ts);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

const KIND_LABEL: Record<OfflineOp["kind"], string> = {
  create: "新建标注",
  update: "更新标注",
  delete: "删除标注",
};

function cn(...classes: Array<string | false | null | undefined>): string {
  return classes.filter(Boolean).join(" ");
}

function kindClassName(kind: OfflineOp["kind"]): string {
  if (kind === "create") return "text-status-positive";
  if (kind === "update") return "text-status-caution";
  return "text-status-danger";
}

function retryBadgeClassName(rc: number): string {
  if (rc >= 3) return "bg-rose-500/20 text-rose-700 dark:text-rose-400";
  if (rc >= 1) return "bg-amber-500/20 text-amber-700 dark:text-amber-400";
  return "bg-muted text-muted-foreground";
}

type TaskFilter = "all" | "current";
type RetryFilter = "all" | "failed";

export function OfflineQueueDrawer({
  open,
  onClose,
  currentTaskId,
  queueScope,
  onFlushOne,
  onFlushAll,
  classifyError,
}: OfflineQueueDrawerProps) {
  const [storedItems, setItems] = useState<OfflineOp[]>([]);
  const items = useMemo(
    () => storedItems.filter((op) => op.userId === queueScope?.userId),
    [storedItems, queueScope?.userId],
  );
  const [busyId, setBusyId] = useState<string | null>(null);
  const [flushAllBusy, setFlushAllBusy] = useState(false);
  const [taskFilter, setTaskFilter] = useState<TaskFilter>("all");
  const [retryFilter, setRetryFilter] = useState<RetryFilter>("all");
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const pushToast = useToastStore((s) => s.push);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    const refresh = () => {
      getAll(queueScope).then((q) => {
        if (!cancelled) setItems(q);
      });
    };
    const unsub = subscribe(() => refresh(), queueScope);
    refresh();
    return () => {
      cancelled = true;
      unsub();
    };
  }, [open, queueScope]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const filtered = useMemo(() => {
    return items.filter((op) => {
      if (taskFilter === "current" && (!currentTaskId || op.taskId !== currentTaskId)) return false;
      if (retryFilter === "failed" && (op.retry_count ?? 0) < 3) return false;
      return true;
    });
  }, [items, taskFilter, retryFilter, currentTaskId]);

  const grouped = useMemo(() => {
    const order: string[] = [];
    const buckets: Record<string, OfflineOp[]> = {};
    for (const op of filtered) {
      if (!buckets[op.taskId]) {
        buckets[op.taskId] = [];
        order.push(op.taskId);
      }
      buckets[op.taskId].push(op);
    }
    return { order, buckets };
  }, [filtered]);

  const taskCount = grouped.order.length;
  const currentTaskItemCount = useMemo(() => {
    if (!currentTaskId) return 0;
    return items.filter((op) => op.taskId === currentTaskId).length;
  }, [items, currentTaskId]);

  const handleRetry = useCallback(
    async (op: OfflineOp) => {
      setBusyId(op.id);
      try {
        const result = await drain(
          onFlushOne,
          {
            ...queueScope,
            userId: queueScope?.userId ?? "",
            operationId: op.id,
          },
          classifyError ? { classifyError } : undefined,
        );
        if ((result.deferred ?? 0) > 0) {
          pushToast({
            msg: "网络不稳定",
            sub: "授权检查未完成，稍后将自动重试",
            kind: "warning",
          });
          return;
        }
        if (result.failed) throw new Error("该操作仍未同步，请检查连接和任务权限后重试");
        if (!queueScope?.isCurrent?.()) return;
        pushToast({ msg: "已同步该操作", kind: "success" });
      } catch (err) {
        pushToast({ msg: "同步失败", sub: String(err), kind: "error" });
      } finally {
        setBusyId(null);
      }
    },
    [classifyError, onFlushOne, pushToast, queueScope],
  );

  const handleDelete = useCallback(
    async (op: OfflineOp) => {
      setBusyId(op.id);
      try {
        await removeDurablyById(op.id, queueScope);
        if (queueScope?.isCurrent?.()) pushToast({ msg: "已从队列删除", kind: "success" });
      } catch (error) {
        if (queueScope?.isCurrent?.())
          pushToast({ msg: "未能删除离线操作，请重试", sub: String(error), kind: "error" });
      } finally {
        setBusyId(null);
      }
    },
    [pushToast, queueScope],
  );

  const handleClearAll = useCallback(async () => {
    if (items.length === 0) return;
    const confirmedIds = items.map((op) => op.id);
    const confirmed = await confirmDialog({
      tone: "danger",
      title: `丢弃全部 ${confirmedIds.length} 条离线操作？`,
      description: "此操作不可撤销。",
      confirmLabel: "丢弃全部",
    });
    if (!confirmed || !queueScope?.isCurrent?.()) return;
    const results = await Promise.allSettled(
      confirmedIds.map((id) => removeDurablyById(id, queueScope)),
    );
    if (!queueScope.isCurrent?.()) return;
    const failed = results.filter((result) => result.status === "rejected").length;
    if (failed) pushToast({ msg: `${failed} 条离线操作未能删除，请重试`, kind: "error" });
    else pushToast({ msg: "队列已清空", kind: "warning" });
  }, [items, pushToast, queueScope]);

  const handleFlushAll = useCallback(async () => {
    setFlushAllBusy(true);
    try {
      await onFlushAll();
    } finally {
      setFlushAllBusy(false);
    }
  }, [onFlushAll]);

  if (!open) return null;

  return createPortal(
    <>
      <div onClick={onClose} className="fixed inset-0 z-drawer-backdrop bg-black/25" />
      <aside
        role="dialog"
        aria-label="离线队列"
        aria-modal="false"
        onClick={(e) => e.stopPropagation()}
        className="fixed top-0 right-0 bottom-0 z-drawer flex flex-col w-[min(420px,100vw)] border-l border-border bg-card shadow-lg"
      >
        <header className="flex items-center justify-between px-4 py-3.5 border-b border-border">
          <div className="flex items-center gap-2">
            <Icon name="inbox" size={14} />
            <div className="text-foreground text-sm font-semibold">离线队列</div>
            <div className="text-muted-foreground text-xs">
              {items.length === 0
                ? "暂无操作"
                : `${items.length} 条 · 跨 ${taskCount} 题${currentTaskId ? ` · 当前题 ${currentTaskItemCount}` : ""}`}
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭"
            className="inline-flex items-center p-1 appearance-none border-0 rounded-[var(--radius-sm)] bg-transparent text-muted-foreground cursor-pointer"
          >
            <Icon name="x" size={16} />
          </button>
        </header>

        {items.length > 0 && (
          <FilterGroup
            label="筛选"
            hideLabel
            compact
            className="border-b border-border px-4 py-2 text-xs text-muted-foreground"
          >
            <span className="mr-1">范围：</span>
            <FilterToggle
              compact
              active={taskFilter === "all"}
              onClick={() => setTaskFilter("all")}
            >
              全部
            </FilterToggle>
            <FilterToggle
              compact
              active={taskFilter === "current"}
              disabled={!currentTaskId}
              onClick={() => setTaskFilter("current")}
            >
              当前题
            </FilterToggle>
            <span className="mr-1 ml-2">状态：</span>
            <FilterToggle
              compact
              active={retryFilter === "all"}
              onClick={() => setRetryFilter("all")}
            >
              全部
            </FilterToggle>
            <FilterToggle
              compact
              active={retryFilter === "failed"}
              onClick={() => setRetryFilter("failed")}
            >
              失败 ≥ 3
            </FilterToggle>
          </FilterGroup>
        )}

        <div className="flex-1 overflow-y-auto py-2 px-0">
          {filtered.length === 0 ? (
            <div className="px-4 py-8 text-muted-foreground text-xs text-center leading-[1.6]">
              <Icon name="check" size={18} className="mb-2 text-status-positive" />
              <div>{items.length === 0 ? "暂无离线操作" : "当前筛选无匹配项"}</div>
              <div className="mt-1 text-xs">
                {items.length === 0
                  ? "所有标注操作已同步至服务器。"
                  : "调整上方筛选 chip 查看其他项。"}
              </div>
            </div>
          ) : (
            grouped.order.map((tid) => {
              const opsInTask = grouped.buckets[tid];
              const isCurrent = tid === currentTaskId;
              const isCollapsed = collapsed[tid] ?? !isCurrent;
              return (
                <div key={tid} className="border-b border-border">
                  <button
                    type="button"
                    onClick={() => setCollapsed((c) => ({ ...c, [tid]: !isCollapsed }))}
                    className={cn(
                      "flex items-center w-full gap-2 px-4 py-2 appearance-none border-0 border-b border-border text-foreground cursor-pointer text-xs font-semibold text-left",
                      isCurrent ? "bg-muted" : "bg-transparent",
                    )}
                  >
                    <Icon name={isCollapsed ? "chevRight" : "chevDown"} size={11} />
                    <span className="mono">任务 {tid.slice(0, 8)}…</span>
                    {isCurrent && <span className="text-brand text-2xs">当前</span>}
                    <span className="ml-auto text-muted-foreground font-normal">
                      {opsInTask.length} 条
                    </span>
                  </button>
                  {!isCollapsed &&
                    opsInTask.map((op) => {
                      const isBusy = busyId === op.id;
                      const rc = op.retry_count ?? 0;
                      return (
                        <div
                          key={op.id}
                          className={cn(
                            "flex flex-col gap-1.5 px-4 py-2.5 pl-8",
                            rc >= 3 ? "bg-rose-950/20 dark:bg-rose-950/20" : "bg-transparent",
                            isBusy && "opacity-50",
                          )}
                        >
                          <div className="flex items-center gap-2">
                            <span
                              className={cn(
                                "px-1.5 py-px rounded-[3px] bg-muted text-xs font-semibold",
                                kindClassName(op.kind),
                              )}
                            >
                              {KIND_LABEL[op.kind]}
                            </span>
                            <span className="text-muted-foreground text-xs mono">
                              {formatTs(op.ts)}
                            </span>
                            {rc > 0 && (
                              <span
                                className={cn(
                                  "px-1.5 py-px rounded-[3px] text-2xs font-semibold",
                                  retryBadgeClassName(rc),
                                )}
                                title={`累计同步失败 ${rc} 次`}
                              >
                                失败 ×{rc}
                              </span>
                            )}
                            {op.kind === "create" && op.tmpId && (
                              <span
                                className="text-muted-foreground text-2xs mono"
                                title={op.tmpId}
                              >
                                {op.tmpId.slice(0, 12)}…
                              </span>
                            )}
                          </div>
                          {op.kind !== "create" && (
                            <div className="text-muted-foreground text-xs mono">
                              标注 {op.annotationId.slice(0, 8)}…
                            </div>
                          )}
                          <div className="flex gap-1.5 mt-0.5">
                            <button
                              type="button"
                              disabled={isBusy}
                              onClick={() => handleRetry(op)}
                              className="px-2.5 py-1 appearance-none border border-border rounded-[var(--radius-sm)] bg-card text-foreground cursor-pointer text-xs disabled:cursor-wait"
                            >
                              重试
                            </button>
                            <button
                              type="button"
                              disabled={isBusy}
                              onClick={() => handleDelete(op)}
                              className="px-2.5 py-1 appearance-none border border-border rounded-[var(--radius-sm)] bg-transparent text-status-danger cursor-pointer text-xs disabled:cursor-wait"
                            >
                              丢弃
                            </button>
                          </div>
                        </div>
                      );
                    })}
                </div>
              );
            })
          )}
        </div>

        <footer className="flex justify-between gap-2 px-4 py-3 border-t border-border">
          <button
            type="button"
            disabled={items.length === 0}
            onClick={handleClearAll}
            className="px-3 py-1.5 appearance-none border border-border rounded-[var(--radius-sm)] bg-transparent text-status-danger cursor-pointer text-xs disabled:text-muted-foreground disabled:cursor-not-allowed disabled:opacity-50"
          >
            全部丢弃
          </button>
          <button
            type="button"
            disabled={items.length === 0 || flushAllBusy}
            onClick={handleFlushAll}
            className="px-3.5 py-1.5 appearance-none border border-brand rounded-[var(--radius-sm)] bg-brand text-white cursor-pointer text-xs font-semibold disabled:text-muted-foreground disabled:cursor-not-allowed disabled:opacity-50"
          >
            {flushAllBusy ? "同步中…" : "立即同步全部"}
          </button>
        </footer>
      </aside>
    </>,
    document.body,
  );
}

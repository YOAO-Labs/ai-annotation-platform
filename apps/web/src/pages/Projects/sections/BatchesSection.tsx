import { useMemo, useState, type ComponentProps } from "react";
import { useSearchParams } from "react-router-dom";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Badge } from "@/components/ui/Badge";
import { Modal } from "@/components/ui/Modal";
import { AssigneeAvatarStack } from "@/components/ui/AssigneeAvatarStack";
import { ProgressBar } from "@/components/ui/ProgressBar";
import { useToastStore } from "@/components/ui/Toast";
import {
  useBatches,
  useDeleteBatch,
  useTransitionBatch,
  useSubmitBatch,
  useSplitBatches,
  useBulkArchiveBatches,
  useBulkDeleteBatches,
  useBulkActivateBatches,
  useUnclassifiedTaskCount,
  useAdminLockBatch,
  useAdminUnlockBatch,
  useBulkApproveBatches,
  useBulkRejectBatches,
  useRejectBatch,
  useResetBatch,
} from "@/hooks/useBatches";
import { useBatchEventsSocket } from "@/hooks/useBatchEventsSocket";
import { useIsProjectOwner } from "@/hooks/useIsProjectOwner";
import { inputDialog } from "@/components/ui/decisionDialog";
import { BatchAssignmentModal } from "@/components/projects/BatchAssignmentModal";
import { ProjectDistributeBatchesModal } from "@/components/projects/ProjectDistributeBatchesModal";
import { BulkRejectModal } from "./BulkRejectModal";
import { BatchesKanbanView } from "./BatchesKanbanView";
import { BatchAuditLogDrawer } from "./BatchAuditLogDrawer";
import { UnbatchedTasksModal } from "./UnbatchedTasksModal";
import type { ProjectResponse } from "@/api/projects";
import type { BatchResponse, BulkBatchActionResponse } from "@/api/batches";
import { cn } from "@/lib/utils";
import styles from "./BatchesSection.module.css";

function BatchActionButton({
  icon,
  label,
  className,
  ...props
}: Omit<ComponentProps<typeof Button>, "children"> & {
  icon: ComponentProps<typeof Icon>["name"];
  label: string;
}) {
  return (
    <Button
      {...props}
      aria-label={label}
      className={cn(
        "gap-0 px-[9px] hover:translate-y-0 disabled:pointer-events-auto has-[>svg]:px-[9px] motion-reduce:transition-none",
        styles.actionButton,
        className,
      )}
    >
      <Icon name={icon} size={12} />
      <span className={styles.actionLabel} aria-hidden="true">
        <span className={styles.actionLabelClip}>
          <span className="pl-1.5">{label}</span>
        </span>
      </span>
    </Button>
  );
}

// success-colored Button 覆盖类(Button 无 success variant) — 对齐 Button danger/ai variant 风格
const SUCCESS_BTN =
  "border-emerald-500/30 bg-status-positive-soft text-status-positive hover:bg-emerald-500/15";

const STATUS_LABELS: Record<string, string> = {
  draft: "草稿",
  active: "激活",
  // B-33 · 补全 pre_annotated 标签 — 之前缺失导致列表回退为原文"pre_annotated",
  // 重置后若数据短暂残留或被误读为状态未变,容易让人以为重置未生效
  pre_annotated: "AI 预标已就绪",
  annotating: "标注中",
  reviewing: "审核中",
  approved: "已通过",
  rejected: "已退回",
  archived: "已归档",
};

const STATUS_VARIANTS: Record<string, "default" | "accent" | "success" | "warning" | "danger"> = {
  draft: "default",
  active: "accent",
  pre_annotated: "accent",
  annotating: "accent",
  reviewing: "warning",
  approved: "success",
  rejected: "danger",
  archived: "default",
};

type BulkActionKind = "archive" | "delete" | "reassign" | "activate" | "approve" | "reject";

const BULK_LABEL: Record<BulkActionKind, string> = {
  archive: "归档",
  delete: "删除",
  reassign: "改派",
  activate: "激活",
  approve: "通过",
  reject: "驳回",
};

// v0.7.3 · 逆向迁移（owner 专属）。原 ReverseTransitionModal 的文案随 plan T2 迁移 inputDialog 一并收编。
type ReverseKind = "unarchive" | "reopen_from_approved" | "reopen_from_rejected";

const REVERSE_COPY: Record<
  ReverseKind,
  {
    title: (b: BatchResponse) => string;
    description: string;
    targetStatus: string;
    success: string;
  }
> = {
  unarchive: {
    title: (b) => `撤销归档 · ${b.display_id}`,
    description:
      "批次状态会回到「激活」，由调度器在下一次任务操作时自动推进到正确阶段。被分派的标注员 / 审核员会收到通知。",
    targetStatus: "active",
    success: "已撤销归档",
  },
  reopen_from_approved: {
    title: (b) => `重开审核 · ${b.display_id}`,
    description:
      "批次会从「已通过」回到「审核中」。原审核元数据（通过时间 / 审核人 / 反馈）会被清空，审核员需重新评估。",
    targetStatus: "reviewing",
    success: "已重开审核",
  },
  reopen_from_rejected: {
    title: (b) => `直接复审 · ${b.display_id}`,
    description:
      "批次从「已退回」直接进入「审核中」，跳过标注员重做。上一次的退回原因会保留，审核员可重新评估。",
    targetStatus: "reviewing",
    success: "已直接复审",
  },
};

/** 未送审任务 = pending + in_progress（rejected 需先重做，不计入整批送审）。 */
function hasUnsubmittedTasks(batch: BatchResponse): boolean {
  return batch.total_tasks - batch.review_tasks - batch.completed_tasks - batch.rejected_tasks > 0;
}

/** 与标注页一致：annotating / reviewing 且仍有未送审任务时才提供整批送审。 */
function canSubmitReview(batch: BatchResponse): boolean {
  return (
    hasUnsubmittedTasks(batch) && (batch.status === "annotating" || batch.status === "reviewing")
  );
}

export function BatchesSection({ project }: { project: ProjectResponse }) {
  const pushToast = useToastStore((s) => s.push);
  // v0.9.13 · 后端 batch 状态变更 (transition / auto_transition) 实时刷新本页列表
  useBatchEventsSocket(project.id);
  const { data: batches = [], isLoading } = useBatches(project.id);
  const deleteBatch = useDeleteBatch(project.id);
  const transitionBatch = useTransitionBatch(project.id);
  const submitBatchReview = useSubmitBatch(project.id);
  const splitBatches = useSplitBatches(project.id);
  const bulkArchive = useBulkArchiveBatches(project.id);
  const bulkDelete = useBulkDeleteBatches(project.id);
  const bulkActivate = useBulkActivateBatches(project.id);
  const bulkApprove = useBulkApproveBatches(project.id);
  const bulkReject = useBulkRejectBatches(project.id);
  const rejectBatch = useRejectBatch(project.id);
  const resetBatch = useResetBatch(project.id);
  const adminLock = useAdminLockBatch(project.id);
  const adminUnlock = useAdminUnlockBatch(project.id);
  const isOwner = useIsProjectOwner(project);
  const { data: unclassified } = useUnclassifiedTaskCount(project.id);
  const unclassifiedCount = unclassified?.count ?? 0;

  const [showCreate, setShowCreate] = useState(false);
  const [priority, setPriority] = useState(50);
  const [nBatches, setNBatches] = useState(3);
  const [namePrefix, setNamePrefix] = useState("Batch");
  const [shuffle, setShuffle] = useState(true);
  const [confirmDelete, setConfirmDelete] = useState<BatchResponse | null>(null);
  // v0.11.25 · 删批次保护：含进行中成果/已预标时后端 409，弹此框让用户确认强制删除
  const [forceDelete, setForceDelete] = useState<{
    batch: BatchResponse;
    nonPending: number;
    predicted: number;
    affected: number;
  } | null>(null);
  const [assignTarget, setAssignTarget] = useState<BatchResponse | null>(null);
  const [distributeOpen, setDistributeOpen] = useState(false);
  // v0.12.0 · P2 · 浏览未归类任务池
  const [browseUnbatched, setBrowseUnbatched] = useState(false);

  // v0.7.3 · 多选批量操作
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [confirmBulk, setConfirmBulk] = useState<BulkActionKind | null>(null);
  const [reassignOpen, setReassignOpen] = useState(false);
  const [bulkResult, setBulkResult] = useState<{
    kind: BulkActionKind;
    data: BulkBatchActionResponse;
  } | null>(null);
  const [resultExpanded, setResultExpanded] = useState(false);

  // v0.7.3 · 操作历史抽屉（逆向迁移 / 重置 / 驳回 / 锁定在 plan T2 已迁移 inputDialog，
  // 目标批次以点击时的局部快照传入 async handler，不再需要 open state）
  const [auditTarget, setAuditTarget] = useState<BatchResponse | null>(null);

  // v0.7.6 · view toggle [list | kanban] + URL ?batch_view=kanban 持久化
  const [searchParams, setSearchParams] = useSearchParams();
  const view = (searchParams.get("batch_view") === "kanban" ? "kanban" : "list") as
    | "list"
    | "kanban";
  const setView = (next: "list" | "kanban") => {
    const params = new URLSearchParams(searchParams);
    if (next === "kanban") params.set("batch_view", "kanban");
    else params.delete("batch_view");
    setSearchParams(params, { replace: true });
  };

  const selectableBatches = useMemo(
    () => batches.filter((b) => b.display_id !== "B-DEFAULT"),
    [batches],
  );
  const selectedCount = selectedIds.size;
  const allSelected =
    selectableBatches.length > 0 && selectableBatches.every((b) => selectedIds.has(b.id));

  const toggleOne = (id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleAll = () => {
    setSelectedIds(allSelected ? new Set() : new Set(selectableBatches.map((b) => b.id)));
  };

  const clearSelection = () => setSelectedIds(new Set());

  const handleBulkResult = (kind: BulkActionKind, data: BulkBatchActionResponse) => {
    setBulkResult({ kind, data });
    setResultExpanded(false);
    clearSelection();
    const sN = data.succeeded.length;
    const skN = data.skipped.length;
    const fN = data.failed.length;
    if (fN === 0 && skN === 0) {
      pushToast({ msg: `批量${BULK_LABEL[kind]}成功 ${sN} 个`, kind: "success" });
    } else {
      pushToast({
        msg: `批量${BULK_LABEL[kind]}：成功 ${sN} / 跳过 ${skN} / 失败 ${fN}`,
        kind: fN > 0 ? "warning" : "success",
      });
    }
  };

  const runBulkArchive = () => {
    bulkArchive.mutate([...selectedIds], {
      onSuccess: (data) => {
        handleBulkResult("archive", data);
        setConfirmBulk(null);
      },
      onError: (e) => pushToast({ msg: "批量归档失败", sub: (e as Error).message }),
    });
  };

  const runBulkDelete = () => {
    bulkDelete.mutate([...selectedIds], {
      onSuccess: (data) => {
        handleBulkResult("delete", data);
        setConfirmBulk(null);
      },
      onError: (e) => pushToast({ msg: "批量删除失败", sub: (e as Error).message }),
    });
  };

  const runBulkActivate = () => {
    bulkActivate.mutate([...selectedIds], {
      onSuccess: (data) => {
        handleBulkResult("activate", data);
        setConfirmBulk(null);
      },
      onError: (e) => pushToast({ msg: "批量激活失败", sub: (e as Error).message }),
    });
  };

  const runBulkApprove = () => {
    bulkApprove.mutate([...selectedIds], {
      onSuccess: (data) => {
        handleBulkResult("approve", data);
        setConfirmBulk(null);
      },
      onError: (e) => pushToast({ msg: "批量通过失败", sub: (e as Error).message }),
    });
  };

  const runBulkReject = (feedback: string) => {
    bulkReject.mutate(
      { batchIds: [...selectedIds], feedback },
      {
        onSuccess: (data) => {
          handleBulkResult("reject", data);
          setConfirmBulk(null);
        },
        onError: (e) => pushToast({ msg: "批量驳回失败", sub: (e as Error).message }),
      },
    );
  };

  const handleAdminLock = (batch: BatchResponse, reason: string) => {
    adminLock.mutate(
      { batchId: batch.id, reason },
      {
        onSuccess: () => {
          pushToast({ msg: `批次 ${batch.display_id} 已锁定`, kind: "success" });
        },
        onError: (e) => pushToast({ msg: "锁定失败", sub: (e as Error).message }),
      },
    );
  };

  const handleAdminUnlock = (batch: BatchResponse) => {
    adminUnlock.mutate(batch.id, {
      onSuccess: () => pushToast({ msg: `批次 ${batch.display_id} 已解锁`, kind: "success" }),
      onError: (e) => pushToast({ msg: "解锁失败", sub: (e as Error).message }),
    });
  };

  const idToBatch = useMemo(() => {
    const m = new Map<string, BatchResponse>();
    for (const b of batches) m.set(b.id, b);
    return m;
  }, [batches]);

  const renderBulkResultRow = (item: { batch_id: string; reason: string }) => {
    const b = idToBatch.get(item.batch_id);
    // v0.11.25 · requires_force 的批次含进行中成果/已预标，批量删除默认跳过
    const reason =
      item.reason === "requires_force"
        ? "含进行中成果/已预标，未删除（可单独强制删除）"
        : item.reason;
    return (
      <li key={item.batch_id} className="text-xs text-muted-foreground">
        <span className="mono">{b?.display_id ?? item.batch_id.slice(0, 8)}</span>
        {b ? <span className="ml-1.5">· {b.name}</span> : null}
        <span className="ml-1.5 text-muted-foreground">— {reason}</span>
      </li>
    );
  };

  // v0.12.0 · B5 兜底：一键把全部未归类任务注入 1 个批次（random split, n_batches=1）。
  // 十万级导入后无需手动选 N，直接让任务进入工作流。
  const handleCreateAll = () => {
    splitBatches.mutate(
      { strategy: "random", n_batches: 1, shuffle: false, name_prefix: "全部未归类", priority: 50 },
      {
        onSuccess: () => {
          pushToast({
            msg: `已把 ${unclassifiedCount} 个未归类任务注入 1 个批次`,
            kind: "success",
          });
        },
        onError: (e) => pushToast({ msg: "建包失败", sub: (e as Error).message }),
      },
    );
  };

  // scene 模式项目只能按 scene 建包：未归类任务(向导自动分包之外的后补/回归任务,
  // 如删包后回归、追加 scene 数据集)按 by_scene 一键重分。name_prefix 与向导一致用 "Scene"。
  const handleCreateByScene = () => {
    splitBatches.mutate(
      { strategy: "by_scene", name_prefix: "Scene", priority: 50 },
      {
        onSuccess: (res) => {
          pushToast({ msg: `已按 scene 建 ${res.length} 个批次`, kind: "success" });
        },
        onError: (e) => pushToast({ msg: "建包失败", sub: (e as Error).message }),
      },
    );
  };

  const handleCreate = () => {
    splitBatches.mutate(
      { strategy: "random", n_batches: nBatches, shuffle, name_prefix: namePrefix, priority },
      {
        onSuccess: (res) => {
          pushToast({
            msg: res.length === 1 ? "已把未归类任务注入 1 个新批次" : `已创建 ${res.length} 个批次`,
            kind: "success",
          });
          setShowCreate(false);
        },
        onError: (e) => pushToast({ msg: "切分失败", sub: (e as Error).message }),
      },
    );
  };

  const handleTransition = (batch: BatchResponse, target: string) => {
    transitionBatch.mutate(
      { batchId: batch.id, targetStatus: target },
      {
        onSuccess: () =>
          pushToast({ msg: `批次状态已更新为 ${STATUS_LABELS[target]}`, kind: "success" }),
        onError: (e) => pushToast({ msg: "状态转移失败", sub: (e as Error).message }),
      },
    );
  };

  // v0.7.0 · 批次驳回（原 RejectBatchModal，plan T2 迁移 inputDialog，copy 原样保留）。
  const handleRejectBatch = async (batch: BatchResponse) => {
    if (rejectBatch.isPending) return;
    const feedback = await inputDialog({
      title: `驳回批次 ${batch.display_id}`,
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
    // batch 为点击时的快照，async-await gap 后提交目标不变
    rejectBatch.mutate(
      { batchId: batch.id, feedback },
      {
        onSuccess: () => pushToast({ msg: "批次已驳回，已通知被分派的标注员", kind: "success" }),
        onError: (e) => pushToast({ msg: "驳回失败", sub: (e as Error).message, kind: "warning" }),
      },
    );
  };

  // v0.7.3 · owner 逆向迁移（原 ReverseTransitionModal，plan T2 迁移 inputDialog）。
  const handleReverseTransition = async (batch: BatchResponse, kind: ReverseKind) => {
    if (transitionBatch.isPending) return;
    const copy = REVERSE_COPY[kind];
    const reason = await inputDialog({
      title: copy.title(batch),
      description: copy.description,
      label: "操作原因 · 会写入审计日志",
      placeholder: "请简要说明操作原因（运维需要 / 误判修正 / …）",
      required: true,
      maxLength: 500,
      confirmLabel: "确认",
    });
    if (!reason) return;
    transitionBatch.mutate(
      { batchId: batch.id, targetStatus: copy.targetStatus, reason },
      {
        onSuccess: () => pushToast({ msg: copy.success, kind: "success" }),
        onError: (e) => pushToast({ msg: "操作失败", sub: (e as Error).message, kind: "warning" }),
      },
    );
  };

  // v0.7.6 · owner 终极重置到 draft（原 ResetBatchModal，plan T2 迁移 inputDialog；
  // 原警告块的列表文案并入 description，≥10 字下限走 validate）。
  const handleResetBatch = async (batch: BatchResponse) => {
    if (resetBatch.isPending) return;
    const reason = await inputDialog({
      title: `重置到草稿 · ${batch.display_id}`,
      description: `这是 owner 兜底操作。批次将从 ${batch.status} 强制回到 draft：批次内 ${batch.total_tasks} 个 task 全部回 pending；已有标注记录保留（不删 annotation，不改 is_active）；会释放所有标注员锁，原审核反馈 / 审核人会被清空。`,
      label: "重置原因 · 会写入审计日志",
      placeholder: "说明为什么要把批次回退到草稿（迁移错误数据 / 整体重做 / …）",
      required: true,
      maxLength: 500,
      tone: "danger",
      confirmLabel: "确认重置",
      validate: (value) => (value.length >= 10 ? null : "至少 10 字"),
    });
    if (!reason) return;
    resetBatch.mutate(
      { batchId: batch.id, reason },
      {
        onSuccess: () => pushToast({ msg: "已重置到草稿", kind: "success" }),
        onError: (e) => pushToast({ msg: "重置失败", sub: (e as Error).message, kind: "warning" }),
      },
    );
  };

  // v0.9.15 · ADR-0008 admin-lock（原 AdminLockModal，plan T2 迁移 inputDialog）。
  const handleAdminLockPrompt = async (batch: BatchResponse) => {
    if (adminLock.isPending) return;
    const reason = await inputDialog({
      title: `锁定批次 ${batch.display_id}`,
      description:
        "锁定后，自动状态推进将被冻结，不再向该批次派发新任务。锁定原因将记录在审计日志中，并通知被分派的标注员 / 审核员。",
      label: "锁定原因",
      placeholder: "请说明锁定原因，例如：发现数据质量问题，暂停标注，待确认后解锁…",
      required: true,
      maxLength: 500,
      tone: "danger",
      confirmLabel: "确认锁定",
    });
    if (!reason) return;
    handleAdminLock(batch, reason);
  };

  // 整批送审与标注页共用同一端点：提交批次内所有未送审任务，而不是只改批次状态。
  const handleSubmitReview = (batch: BatchResponse) => {
    submitBatchReview.mutate(batch.id, {
      onSuccess: (result) =>
        pushToast({
          msg: `已提交 ${result.submitted_tasks} 个任务质检`,
          sub:
            result.remaining_tasks > 0 ? `仍有 ${result.remaining_tasks} 个任务未送审` : undefined,
          kind: "success",
        }),
      onError: (e) => pushToast({ msg: "提交质检失败", sub: (e as Error).message }),
    });
  };

  const handleDelete = (batch: BatchResponse, force = false) => {
    deleteBatch.mutate(
      { batchId: batch.id, force },
      {
        onSuccess: () => {
          pushToast({ msg: "批次已删除", kind: "success" });
          setConfirmDelete(null);
          setForceDelete(null);
        },
        onError: (e) => {
          // v0.11.25 · 409 requires_force：含进行中成果/已预标 → 弹保护框让用户确认强制删除
          const err = e as { status?: number; detailRaw?: Record<string, unknown> };
          if (err.status === 409 && err.detailRaw?.requires_force) {
            setConfirmDelete(null);
            setForceDelete({
              batch,
              nonPending: Number(err.detailRaw.non_pending ?? 0),
              predicted: Number(err.detailRaw.predicted ?? 0),
              affected: Number(err.detailRaw.affected_tasks ?? 0),
            });
            return;
          }
          pushToast({ msg: "删除失败", sub: (e as Error).message });
        },
      },
    );
  };

  return (
    <>
      <Card>
        <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2 border-b border-border px-4 py-3.5">
          <h3 className="text-sm font-semibold">批次管理</h3>
          <div className="flex flex-wrap items-center gap-1.5">
            {/* v0.7.6 · view toggle */}
            <div
              role="tablist"
              aria-label="批次视图"
              className="inline-flex overflow-hidden rounded-md border border-border bg-muted"
            >
              {(["list", "kanban"] as const).map((v) => (
                <button
                  key={v}
                  role="tab"
                  aria-selected={view === v}
                  onClick={() => setView(v)}
                  className={cn(
                    "inline-flex h-8 cursor-pointer items-center gap-1.5 appearance-none border-0 bg-transparent px-3 text-xs text-muted-foreground",
                    view === v && "bg-card text-foreground",
                    v === "list" && "border-r border-border",
                  )}
                  title={v === "list" ? "列表视图" : "看板视图（按状态分列）"}
                >
                  <Icon name={v === "list" ? "list" : "grid"} size={11} />
                  {v === "list" ? "列表" : "看板"}
                </button>
              ))}
            </div>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => setDistributeOpen(true)}
              disabled={batches.length === 0}
              title="把项目下所有批次圆周分派给所选成员（一 batch 一标注员 + 一审核员）"
            >
              <Icon name="users" />
              按项目分派批次
            </Button>
            {/* scene 模式项目分包只能 by scene：头部入口也走 by_scene，不开 random modal。 */}
            <Button
              size="sm"
              variant="ghost"
              onClick={project.scene_mode ? handleCreateByScene : () => setShowCreate(true)}
              disabled={project.scene_mode && splitBatches.isPending}
            >
              <Icon name="plus" />
              创建批次
            </Button>
          </div>
        </div>

        {isLoading && (
          <div className="p-8 text-center text-sm text-muted-foreground">加载中...</div>
        )}

        {!isLoading && batches.length === 0 && (
          <div className="p-8 text-center text-sm text-muted-foreground">暂无批次</div>
        )}

        {/* v0.7.3 · 未归类任务横带（关联数据集后但还没切分到 batch 的 task） */}
        {unclassifiedCount > 0 && (
          <div className="flex items-center gap-3 border-b border-border bg-status-caution-soft px-4 py-2 text-sm">
            <Icon name="info" size={14} />
            <span>
              本项目有 <strong>{unclassifiedCount}</strong> 个 <strong>未归类任务</strong>
              （数据集已关联但尚未划分到批次）。
            </span>
            {isOwner &&
              // scene 模式项目只能按 scene 建包：隐藏 random 系入口(一键全量/去分包),
              // 仅留「按 scene 建包」,与向导自动分包同策略,保证批次边界对齐 scene。
              (project.scene_mode ? (
                <Button
                  onClick={handleCreateByScene}
                  disabled={splitBatches.isPending}
                  className="ml-auto"
                  title="按 scene 把未归类任务分包，每个 scene 一个批次"
                >
                  <Icon name="layers" size={12} /> 按 scene 建包
                </Button>
              ) : (
                <>
                  <Button
                    onClick={handleCreateAll}
                    disabled={splitBatches.isPending}
                    className="ml-auto"
                    title="把全部未归类任务一次性注入 1 个批次，立即进入工作流"
                  >
                    <Icon name="flame" size={12} /> 一键全量建包
                  </Button>
                  <Button
                    onClick={() => setShowCreate(true)}
                    className="ml-auto"
                    title="按随机切分把未归类任务拆成 N 个批次"
                  >
                    <Icon name="layers" size={12} /> 去分包
                  </Button>
                </>
              ))}
            <Button
              onClick={() => setBrowseUnbatched(true)}
              className="ml-auto"
              title="浏览未归类任务列表"
            >
              <Icon name="list" size={12} /> 浏览未归类
            </Button>
          </div>
        )}

        {/* v0.7.3 · 多选浮层操作条（仅 owner 可见） */}
        {isOwner && selectedCount > 0 && (
          <div className="flex items-center gap-3 border-b border-border bg-brand/10 px-4 py-2 text-sm">
            <span>
              已选 <strong>{selectedCount}</strong> 个批次
            </span>
            <div className="ml-auto flex gap-1.5">
              <Button
                onClick={() => setConfirmBulk("activate")}
                title="对选中的 draft 批次批量激活"
              >
                <Icon name="play" size={12} /> 激活
              </Button>
              <Button
                onClick={() => setConfirmBulk("approve")}
                className={SUCCESS_BTN}
                title="批量通过审核（仅审核中的批次生效）"
              >
                <Icon name="check" size={12} /> 通过
              </Button>
              <Button
                variant="danger"
                onClick={() => setConfirmBulk("reject")}
                title="批量驳回（仅审核中的批次生效，需填写驳回原因）"
              >
                <Icon name="x" size={12} /> 驳回
              </Button>
              <Button onClick={() => setReassignOpen(true)} title="批量改派 annotator / reviewer">
                <Icon name="users" size={12} /> 改派
              </Button>
              <Button onClick={() => setConfirmBulk("archive")} title="批量归档">
                <Icon name="inbox" size={12} /> 归档
              </Button>
              <Button variant="danger" onClick={() => setConfirmBulk("delete")} title="批量删除">
                <Icon name="trash" size={12} /> 删除
              </Button>
              <Button onClick={clearSelection} title="取消选择">
                取消
              </Button>
            </div>
          </div>
        )}

        {/* v0.7.3 · 上次批量操作结果（partial-success） */}
        {bulkResult && (
          <div className="block border-b border-border bg-muted px-4 py-2 text-xs">
            <div className="flex items-center gap-2">
              <span>
                上次批量{BULK_LABEL[bulkResult.kind]}：
                <strong className="text-status-positive">
                  {" "}
                  成功 {bulkResult.data.succeeded.length}
                </strong>
                {bulkResult.data.skipped.length > 0 && (
                  <strong className="ml-2 text-status-caution">
                    跳过 {bulkResult.data.skipped.length}
                  </strong>
                )}
                {bulkResult.data.failed.length > 0 && (
                  <strong className="ml-2 text-status-danger">
                    失败 {bulkResult.data.failed.length}
                  </strong>
                )}
              </span>
              {(bulkResult.data.skipped.length > 0 || bulkResult.data.failed.length > 0) && (
                <button
                  type="button"
                  onClick={() => setResultExpanded((v) => !v)}
                  className="cursor-pointer appearance-none border-0 bg-transparent text-xs text-brand"
                >
                  {resultExpanded ? "收起" : "查看详情"}
                </button>
              )}
              <button
                type="button"
                onClick={() => setBulkResult(null)}
                className="ml-auto cursor-pointer appearance-none border-0 bg-transparent text-muted-foreground"
                title="关闭"
              >
                <Icon name="x" size={12} />
              </button>
            </div>
            {resultExpanded && (
              <ul className="mt-2 ml-4 list-disc p-0">
                {bulkResult.data.skipped.map(renderBulkResultRow)}
                {bulkResult.data.failed.map(renderBulkResultRow)}
              </ul>
            )}
          </div>
        )}

        {!isLoading && batches.length > 0 && view === "kanban" && (
          <BatchesKanbanView batches={batches} isOwner={isOwner} onTransition={handleTransition} />
        )}

        {!isLoading && batches.length > 0 && view === "list" && (
          <div className="w-full overflow-x-auto">
            <table className="w-full min-w-[1080px] border-collapse text-sm [&_td:last-child]:min-w-[360px] [&_th:last-child]:min-w-[360px]">
              <thead>
                <tr className="border-b border-border">
                  {isOwner && (
                    <th className="w-7 py-2 pr-0 pl-3">
                      <input
                        type="checkbox"
                        checked={allSelected}
                        onChange={toggleAll}
                        title={allSelected ? "取消全选" : "全选"}
                        className="cursor-pointer"
                      />
                    </th>
                  )}
                  {["批次", "状态", "分派", "优先级", "截止日期", "进度", "操作"].map((h) => (
                    <th
                      key={h}
                      className="px-3 py-2 text-left text-xs font-medium whitespace-nowrap text-muted-foreground"
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {batches.map((b) => (
                  <tr key={b.id} className="border-b border-border">
                    {isOwner && (
                      <td className="w-7 py-2.5 pr-0 pl-3">
                        {b.display_id !== "B-DEFAULT" ? (
                          <input
                            type="checkbox"
                            checked={selectedIds.has(b.id)}
                            onChange={() => toggleOne(b.id)}
                            className="cursor-pointer"
                          />
                        ) : null}
                      </td>
                    )}
                    <td className="px-3 py-2.5 align-middle">
                      <div
                        className="max-w-[220px] overflow-hidden font-medium text-ellipsis whitespace-nowrap"
                        title={b.name}
                      >
                        {b.name}
                      </div>
                      <div className="mono text-xs whitespace-nowrap text-muted-foreground">
                        {b.display_id}
                      </div>
                    </td>
                    <td className="px-3 py-2.5 align-middle">
                      <div className="flex flex-nowrap items-center gap-1">
                        <Badge variant={STATUS_VARIANTS[b.status] ?? "default"} dot>
                          {STATUS_LABELS[b.status] ?? b.status}
                        </Badge>
                        {b.admin_locked && (
                          <span title={b.admin_lock_reason ?? "已锁定"}>
                            <Badge variant="warning">
                              <Icon name="lock" size={10} /> 已锁定
                            </Badge>
                          </span>
                        )}
                      </div>
                    </td>
                    <td className="px-3 py-2.5 align-middle">
                      {(() => {
                        const unassigned = !b.annotator_id && !b.reviewer_id;
                        const assignees = [b.annotator, b.reviewer].filter(Boolean) as NonNullable<
                          typeof b.annotator
                        >[];
                        return (
                          <button
                            type="button"
                            onClick={() => setAssignTarget(b)}
                            title={unassigned ? "未分派 · 点击设置" : "点击修改分派"}
                            className={cn(
                              "inline-flex cursor-pointer items-center gap-1 rounded-full border border-dashed border-border bg-transparent px-1.5 py-0.5 text-xs whitespace-nowrap text-muted-foreground",
                              unassigned && "border-amber-500 text-status-caution",
                            )}
                          >
                            {unassigned ? (
                              <>
                                <Icon name="users" size={11} />
                                未分派
                              </>
                            ) : (
                              <AssigneeAvatarStack users={assignees} max={2} />
                            )}
                          </button>
                        );
                      })()}
                    </td>
                    <td className="px-3 py-2.5 align-middle">{b.priority}</td>
                    <td className="px-3 py-2.5 align-middle whitespace-nowrap text-muted-foreground">
                      {b.deadline ?? "—"}
                    </td>
                    <td className="min-w-[140px] px-3 py-2.5 align-middle">
                      <ProgressBar value={b.progress_pct} />
                      <div className="mt-0.5 text-xs whitespace-nowrap text-muted-foreground">
                        <span className="mono">
                          {b.completed_tasks} / {b.total_tasks}
                        </span>
                      </div>
                    </td>
                    <td className="px-3 py-2.5 align-middle">
                      <div className="flex flex-nowrap gap-1 whitespace-nowrap">
                        {b.status === "draft" && (
                          <BatchActionButton
                            icon="play"
                            label="激活"
                            onClick={() => handleTransition(b, "active")}
                            disabled={!b.annotator_id || b.total_tasks === 0}
                            title={
                              !b.annotator_id
                                ? "请先分派标注员"
                                : b.total_tasks === 0
                                  ? "批次内无任务，无法激活"
                                  : "激活"
                            }
                          />
                        )}
                        {canSubmitReview(b) && (
                          <BatchActionButton
                            icon="check"
                            label="提交质检"
                            onClick={() => handleSubmitReview(b)}
                            title={
                              b.status === "reviewing"
                                ? "批次审核中但仍有未送审任务，整批补交质检"
                                : "整批提交质检（owner / 被分派标注员）"
                            }
                          />
                        )}
                        {b.status === "reviewing" && (
                          <>
                            <BatchActionButton
                              icon="check"
                              label="通过"
                              onClick={() => handleTransition(b, "approved")}
                              title="批次通过审核（reviewer / owner）"
                              className={SUCCESS_BTN}
                            />
                            <BatchActionButton
                              icon="x"
                              label="驳回"
                              variant="danger"
                              onClick={() => void handleRejectBatch(b)}
                              title="批次驳回（reviewer / owner）"
                            />
                          </>
                        )}
                        {b.status === "rejected" && (
                          <BatchActionButton
                            icon="refresh"
                            label="重新激活"
                            onClick={() => handleTransition(b, "active")}
                            title="重新激活"
                          />
                        )}
                        {/* v0.7.3 · owner 专属逆向迁移按钮 */}
                        {isOwner && b.status === "rejected" && (
                          <BatchActionButton
                            icon="refresh"
                            label="直接复审"
                            onClick={() => void handleReverseTransition(b, "reopen_from_rejected")}
                            title="跳过重标，直接复审"
                          />
                        )}
                        {isOwner && b.status === "approved" && (
                          <BatchActionButton
                            icon="refresh"
                            label="重开审核"
                            onClick={() => void handleReverseTransition(b, "reopen_from_approved")}
                            title="重开审核"
                          />
                        )}
                        {isOwner && b.status === "archived" && (
                          <BatchActionButton
                            icon="refresh"
                            label="撤销归档"
                            onClick={() => void handleReverseTransition(b, "unarchive")}
                            title="撤销归档"
                          />
                        )}
                        {/* v0.7.6 · owner 终极重置到 draft（任意非 draft 状态） */}
                        {isOwner && b.status !== "draft" && (
                          <BatchActionButton
                            icon="refresh"
                            label="重置"
                            onClick={() => void handleResetBatch(b)}
                            title="重置到草稿（owner 兜底）"
                          />
                        )}
                        {!["archived", "approved"].includes(b.status) && (
                          <BatchActionButton
                            icon="inbox"
                            label="归档"
                            onClick={() => handleTransition(b, "archived")}
                            title="归档"
                          />
                        )}
                        {b.display_id !== "B-DEFAULT" && (
                          <BatchActionButton
                            icon="trash"
                            label="删除"
                            onClick={() => setConfirmDelete(b)}
                            title="删除"
                          />
                        )}
                        {/* v0.7.3 · 操作历史抽屉 */}
                        <BatchActionButton
                          icon="clock"
                          label="操作历史"
                          onClick={() => setAuditTarget(b)}
                          title="操作历史"
                        />
                        {/* v0.9.15 · ADR-0008 admin-lock */}
                        {isOwner && !b.admin_locked && (
                          <BatchActionButton
                            icon="lock"
                            label="锁定批次"
                            onClick={() => void handleAdminLockPrompt(b)}
                            title="锁定批次（冻结自动推进，阻止新派单）"
                            className="text-status-caution"
                          />
                        )}
                        {isOwner && b.admin_locked && (
                          <BatchActionButton
                            icon="unlock"
                            label="解锁批次"
                            onClick={() => handleAdminUnlock(b)}
                            title="解锁批次"
                            className="text-status-positive"
                          />
                        )}
                      </div>
                      {b.status === "rejected" && b.review_feedback && (
                        <div
                          className="mt-1.5 max-w-[300px] border-l-2 border-rose-500 bg-status-danger-soft px-2 py-1.5 text-xs text-muted-foreground"
                          title={b.review_feedback}
                        >
                          <strong className="text-status-danger">驳回原因：</strong>
                          {b.review_feedback.length > 80
                            ? b.review_feedback.slice(0, 80) + "…"
                            : b.review_feedback}
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* 创建批次 Modal */}
      <Modal open={showCreate} title="创建批次" onClose={() => setShowCreate(false)}>
        <div className="flex flex-col gap-4 px-1">
          <label className="flex flex-col gap-1 text-sm">
            批次数量
            <input
              type="number"
              min={1}
              max={100}
              value={nBatches}
              onChange={(e) => setNBatches(Number(e.target.value))}
              className="w-20 appearance-none rounded-sm border border-border bg-background px-2.5 py-1.5 text-sm text-foreground"
            />
          </label>
          {nBatches === 1 && (
            <p className="m-0 text-xs text-muted-foreground">把全部未归类任务注入一个新批次。</p>
          )}
          <label className="flex flex-col gap-1 text-sm">
            {nBatches === 1 ? "批次名称" : "名称前缀"}
            <input
              value={namePrefix}
              onChange={(e) => setNamePrefix(e.target.value)}
              className="appearance-none rounded-sm border border-border bg-background px-2.5 py-1.5 text-sm text-foreground"
              placeholder={nBatches === 1 ? "例如：第 1 批" : "Batch"}
            />
          </label>
          <div className="flex gap-2">
            <Button
              variant={!shuffle ? "primary" : "default"}
              onClick={() => setShuffle(false)}
              title="按任务导入顺序切分（不打乱）"
            >
              顺序切分
            </Button>
            <Button
              variant={shuffle ? "primary" : "default"}
              onClick={() => setShuffle(true)}
              title="随机打乱后切分"
            >
              打乱切分
            </Button>
          </div>

          <label className="flex flex-col gap-1 text-sm">
            优先级: {priority}
            <input
              type="range"
              min={0}
              max={100}
              value={priority}
              onChange={(e) => setPriority(Number(e.target.value))}
              className="w-full"
            />
          </label>

          <div className="flex justify-end gap-2">
            <Button onClick={() => setShowCreate(false)}>取消</Button>
            <Button variant="primary" onClick={handleCreate} disabled={!namePrefix.trim()}>
              {nBatches === 1 ? "注入 1 个批次" : `切分为 ${nBatches} 个批次`}
            </Button>
          </div>
        </div>
      </Modal>

      {/* 删除确认 */}
      <Modal open={!!confirmDelete} title="确认删除" onClose={() => setConfirmDelete(null)}>
        <div className="text-sm">
          <p>
            确定删除批次 <strong>{confirmDelete?.name}</strong>？ 其中的{" "}
            {confirmDelete?.total_tasks ?? 0} 个任务将变为未归类（可重新分包）。
          </p>
          <div className="mt-4 flex justify-end gap-2">
            <Button onClick={() => setConfirmDelete(null)}>取消</Button>
            <Button variant="danger" onClick={() => confirmDelete && handleDelete(confirmDelete)}>
              删除
            </Button>
          </div>
        </div>
      </Modal>

      {/* v0.11.25 · 删除保护：含进行中成果/已预标 → 强制删除确认 */}
      <Modal open={!!forceDelete} title="该批次有进行中的成果" onClose={() => setForceDelete(null)}>
        <div className="text-sm">
          <p>
            批次 <strong>{forceDelete?.batch.name}</strong> 将影响{" "}
            <strong>{forceDelete?.affected ?? 0}</strong> 个任务
            {forceDelete?.nonPending || forceDelete?.predicted ? (
              <>
                （含进行中/已完成成果 {forceDelete?.nonPending ?? 0}、AI 预标成果{" "}
                {forceDelete?.predicted ?? 0}，可能重叠）
              </>
            ) : null}
            。强制删除会把这些任务<strong>重置为待标注</strong>并<strong>清除 AI 预标</strong>
            （人工标注保留）。
          </p>
          <p className="text-xs text-muted-foreground">若只想暂停而不丢进度，建议改用「归档」。</p>
          <div className="mt-4 flex justify-end gap-2">
            <Button onClick={() => setForceDelete(null)}>取消</Button>
            <Button
              variant="danger"
              onClick={() => forceDelete && handleDelete(forceDelete.batch, true)}
            >
              强制删除
            </Button>
          </div>
        </div>
      </Modal>

      {/* v0.6.7 B-12-②：分派 Modal */}
      {assignTarget && (
        <BatchAssignmentModal
          projectId={project.id}
          batch={assignTarget}
          onClose={() => setAssignTarget(null)}
        />
      )}

      {/* v0.7.2：项目级 batch 分派 Modal */}
      {distributeOpen && (
        <ProjectDistributeBatchesModal
          projectId={project.id}
          onClose={() => setDistributeOpen(false)}
        />
      )}

      {/* v0.7.3：批量操作二次确认 Modal */}
      <Modal
        open={
          confirmBulk === "archive" ||
          confirmBulk === "delete" ||
          confirmBulk === "activate" ||
          confirmBulk === "approve"
        }
        title={`批量${confirmBulk ? BULK_LABEL[confirmBulk] : ""}`}
        onClose={() => setConfirmBulk(null)}
      >
        <div className="text-sm">
          {confirmBulk === "archive" && (
            <p>
              将把已选 <strong>{selectedCount}</strong> 个批次归档。归档后批次进入终态，可由 owner
              通过「撤销归档」恢复。
            </p>
          )}
          {confirmBulk === "delete" && (
            <p className="text-status-danger">
              将永久删除已选 <strong>{selectedCount}</strong>{" "}
              个批次。批次内的任务会回归默认批次（无默认批次时变为未归类）。此操作不可撤销。
            </p>
          )}
          {confirmBulk === "activate" && (
            <p>
              将激活已选 <strong>{selectedCount}</strong> 个 draft
              批次。前置条件不满足（未指派标注员或任务为空）的批次会失败但不影响其他。
            </p>
          )}
          {confirmBulk === "approve" && (
            <p>
              将把已选 <strong>{selectedCount}</strong>{" "}
              个批次中的「审核中」批次全部通过。非审核中状态的批次会自动跳过。
            </p>
          )}
          <div className="mt-4 flex justify-end gap-2">
            <Button onClick={() => setConfirmBulk(null)}>取消</Button>
            <Button
              variant={confirmBulk === "delete" ? "danger" : "primary"}
              onClick={() => {
                if (confirmBulk === "archive") runBulkArchive();
                else if (confirmBulk === "delete") runBulkDelete();
                else if (confirmBulk === "activate") runBulkActivate();
                else if (confirmBulk === "approve") runBulkApprove();
              }}
              disabled={
                bulkArchive.isPending ||
                bulkDelete.isPending ||
                bulkActivate.isPending ||
                bulkApprove.isPending
              }
              className={cn(confirmBulk === "approve" && SUCCESS_BTN)}
            >
              确认{confirmBulk ? BULK_LABEL[confirmBulk] : ""}
            </Button>
          </div>
        </div>
      </Modal>

      {/* v0.7.3：批量改派 Modal */}
      {reassignOpen && (
        <ProjectDistributeBatchesModal
          projectId={project.id}
          batchIds={[...selectedIds]}
          onClose={() => setReassignOpen(false)}
        />
      )}

      {/* v0.7.3：操作历史抽屉 */}
      {auditTarget && (
        <BatchAuditLogDrawer
          projectId={project.id}
          batch={auditTarget}
          onClose={() => setAuditTarget(null)}
        />
      )}

      {/* v0.12.0：浏览未归类任务池（虚拟滚动） */}
      {browseUnbatched && (
        <UnbatchedTasksModal
          projectId={project.id}
          count={unclassifiedCount}
          onClose={() => setBrowseUnbatched(false)}
        />
      )}

      {/* v0.9.15：批量驳回 Modal */}
      {confirmBulk === "reject" && (
        <BulkRejectModal
          count={selectedCount}
          onClose={() => setConfirmBulk(null)}
          onSubmit={runBulkReject}
          pending={bulkReject.isPending}
        />
      )}
    </>
  );
}

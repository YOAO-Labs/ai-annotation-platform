import { useMemo, useRef, useState } from "react";
import { clsx } from "clsx";
import { ApiError } from "@/api/client";
import { isCurrentAuthOwner, useAuthStore } from "@/stores/authStore";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { UserAvatar } from "@/components/ui/UserAvatar";
import { Badge } from "@/components/ui/Badge";
import { Icon } from "@/components/ui/Icon";
import { useToastStore } from "@/components/ui/Toast";
import { useProjectMembers } from "@/hooks/useProjects";
import { useApplyDistributeBatches, usePreviewDistributeBatches } from "@/hooks/useBatches";
import type { BatchDistributionPreview } from "@/api/batches";
import styles from "./ProjectDistributeBatchesModal.module.css";

interface Props {
  projectId: string;
  batchIds?: string[];
  onClose: () => void;
}

/**
 * v0.7.2 · 项目级 batch 圆周分派：把项目内未分派 / 全部 batch 在所选 annotator/reviewer 间均分。
 * 一 batch 落到 1 个 annotator + 1 个 reviewer。
 */
export function ProjectDistributeBatchesModal({ projectId, batchIds, onClose }: Props) {
  const pushToast = useToastStore((s) => s.push);
  const { data: members = [], isLoading } = useProjectMembers(projectId);
  const distribute = useApplyDistributeBatches(projectId);
  const previewMut = usePreviewDistributeBatches(projectId);

  const ownerId = useAuthStore((state) => state.user?.id);
  const scopeRef = useRef("");
  const [annotators, setAnnotators] = useState<Set<string>>(new Set());
  const [reviewers, setReviewers] = useState<Set<string>>(new Set());
  const [scope, setScope] = useState<"unassigned" | "all">("unassigned");
  const [preview, setPreview] = useState<BatchDistributionPreview | null>(null);

  const selectionKey = JSON.stringify([
    ownerId,
    projectId,
    batchIds,
    [...annotators],
    [...reviewers],
    scope,
  ]);
  scopeRef.current = selectionKey;
  const busy = distribute.isPending || previewMut.isPending;

  const annotatorMembers = useMemo(() => members.filter((m) => m.role === "annotator"), [members]);
  const reviewerMembers = useMemo(() => members.filter((m) => m.role === "reviewer"), [members]);

  const toggle = (set: Set<string>, setter: (s: Set<string>) => void, id: string) => {
    const next = new Set(set);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setter(next);
    setPreview(null);
  };

  const onSubmit = () => {
    if (busy || !ownerId || !isCurrentAuthOwner(ownerId)) return;
    const current = () => scopeRef.current === selectionKey && isCurrentAuthOwner(ownerId);
    if (annotators.size === 0 && reviewers.size === 0) {
      pushToast({ msg: "请至少勾选一个标注员或审核员" });
      return;
    }
    if (!preview) {
      previewMut.mutate(
        {
          annotator_ids: Array.from(annotators),
          reviewer_ids: Array.from(reviewers),
          only_unassigned: scope === "unassigned",
          ...(batchIds ? { batch_ids: batchIds } : {}),
        },
        {
          onSuccess: (data) => {
            if (current()) setPreview(data);
          },
          onError: (e) => pushToast({ msg: "预览失败", sub: (e as Error).message, kind: "error" }),
        },
      );
      return;
    }
    if (!preview.preview_version) {
      pushToast({ msg: "预览已失效，请重新生成", kind: "warning" });
      setPreview(null);
      return;
    }
    distribute.mutate(
      {
        annotatorIds: Array.from(annotators),
        reviewerIds: Array.from(reviewers),
        onlyUnassigned: scope === "unassigned",
        previewVersion: preview.preview_version,
        batchIds,
      },
      {
        onSuccess: (data) => {
          if (!current()) return;
          pushToast({
            msg: `已圆周分派 ${data.distributed_batches} 个批次`,
            kind: "success",
          });
          onClose();
        },
        onError: (e) => {
          if (!current()) return;
          if (e instanceof ApiError && e.status === 409) setPreview(null);
          pushToast({ msg: "分派失败，请重新预览", sub: (e as Error).message, kind: "error" });
        },
      },
    );
  };

  return (
    <Modal
      open
      onClose={() => {
        if (!busy) onClose();
      }}
      title={batchIds ? "分派选中批次" : "按项目分派批次"}
      width={560}
    >
      <div className={styles.description}>
        {batchIds ? "把选中的 " + batchIds.length + " 个批次" : "把项目下的批次"}
        圆周均分给所选标注员 / 审核员。每个批次落到 <strong>1 个标注员 + 1 个审核员</strong>。
      </div>

      <div className={styles.scopeTabs}>
        <button
          type="button"
          disabled={busy}
          onClick={() => {
            setScope("unassigned");
            setPreview(null);
          }}
          className={clsx(styles.scopeChip, scope === "unassigned" && styles.scopeChipActive)}
          title="只分派那些 annotator/reviewer 为空的批次（不覆盖已分派）"
        >
          仅未分派的批次
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => {
            setScope("all");
            setPreview(null);
          }}
          className={clsx(styles.scopeChip, scope === "all" && styles.scopeChipActive)}
          title={batchIds ? "覆盖所选批次中的已有分派" : "覆盖所有非归档批次（含已分派）"}
        >
          {batchIds ? "覆盖选中批次的分派" : "覆盖全部批次"}
        </button>
      </div>

      {preview && (
        <DistributionPreview
          preview={preview}
          members={[...annotatorMembers, ...reviewerMembers]}
        />
      )}

      {isLoading && <div className={styles.loading}>加载中…</div>}

      {!isLoading && (
        <div className={styles.columns}>
          <Column
            title="参与标注员"
            members={annotatorMembers}
            selected={annotators}
            onToggle={(id) => {
              if (!busy) toggle(annotators, setAnnotators, id);
            }}
            roleColor="accent"
          />
          <Column
            title="参与审核员"
            members={reviewerMembers}
            selected={reviewers}
            onToggle={(id) => {
              if (!busy) toggle(reviewers, setReviewers, id);
            }}
            roleColor="warning"
          />
        </div>
      )}

      <div className={styles.footer}>
        <span className={styles.selectionSummary}>
          标注员 <strong className={styles.selectionCount}>{annotators.size}</strong>
          {" · "}
          审核员 <strong className={styles.selectionCount}>{reviewers.size}</strong>
        </span>
        <div className={styles.actions}>
          <Button onClick={onClose} disabled={busy}>
            取消
          </Button>
          <Button
            variant="primary"
            onClick={onSubmit}
            disabled={
              distribute.isPending ||
              previewMut.isPending ||
              (annotators.size === 0 && reviewers.size === 0)
            }
          >
            {distribute.isPending
              ? "分派中…"
              : previewMut.isPending
                ? "生成预览…"
                : preview
                  ? "确认分派"
                  : "预览分派"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

export function DistributionPreview({
  preview,
  members,
}: {
  preview: BatchDistributionPreview;
  members: { user_id: string; user_name: string }[];
}) {
  const [showAll, setShowAll] = useState(false);
  const names = new Map(members.map((member) => [member.user_id, member.user_name]));
  const label = (id: string | null) => (id ? (names.get(id) ?? id.slice(0, 8)) : "未分派");
  const visibleItems = showAll ? preview.items : preview.items.slice(0, 80);
  return (
    <div className="flex max-h-72 flex-col gap-2 overflow-y-auto rounded-md border border-border bg-muted/30 p-3 text-xs">
      <div className="flex flex-wrap gap-2 text-muted-foreground">
        <span>候选批次 {preview.candidate_batches}</span>
        <span>将变更 {preview.changed_batches}</span>
        <span>跳过 {preview.skipped_batches}</span>
        {preview.only_unassigned && <span>只处理未分派</span>}
      </div>
      {preview.recipient_summary && preview.recipient_summary.length > 0 && (
        <div className="rounded border border-border bg-card px-2 py-1.5">
          {preview.recipient_summary.map((item) => (
            <div key={item.user_id} className="flex justify-between gap-2 py-0.5">
              <span>
                {names.get(item.user_id) ?? item.user_id.slice(0, 8)} ·{" "}
                {item.role === "annotator" ? "标注员" : "审核员"}
              </span>
              <span className="text-muted-foreground">
                新增待办 {item.new_task_count} · 已有待办 {item.existing_backlog_count}
              </span>
            </div>
          ))}
        </div>
      )}
      {visibleItems.map((item) => (
        <div
          key={item.batch_id}
          className="flex items-center justify-between gap-2 border-t border-border pt-1.5"
        >
          <span className="min-w-0 truncate">
            {item.display_id} · {item.name} · 任务 {item.task_count}
          </span>
          <span className={item.will_change ? "text-foreground" : "text-muted-foreground"}>
            {label(item.before_annotator_id)} → {label(item.after_annotator_id)}
            {" · "}
            {label(item.before_reviewer_id)} → {label(item.after_reviewer_id)}
          </span>
        </div>
      ))}
      {preview.items.length > 80 && (
        <button
          type="button"
          className="self-start text-xs text-brand underline-offset-2 hover:underline"
          onClick={() => setShowAll((value) => !value)}
        >
          {showAll ? "收起批次明细" : `查看全部 ${preview.items.length} 个批次`}
        </button>
      )}
    </div>
  );
}

function Column({
  title,
  members,
  selected,
  onToggle,
  roleColor,
}: {
  title: string;
  members: {
    id: string;
    user_id: string;
    user_name: string;
    user_email: string;
    role: string;
    /** 头像引用；结构上是 ProjectMemberResponse 的子集，缺省 = 回退首字母。 */
    avatar_ref?: string | null;
  }[];
  selected: Set<string>;
  onToggle: (userId: string) => void;
  roleColor: "accent" | "warning";
}) {
  return (
    <div className={styles.column}>
      <div className={styles.columnHeader}>
        <Badge variant={roleColor} dot>
          {title}
        </Badge>
      </div>
      {members.length === 0 && <div className={styles.emptyMembers}>暂无成员</div>}
      {members.map((m) => {
        const checked = selected.has(m.user_id);
        return (
          <button
            key={m.id}
            type="button"
            onClick={() => onToggle(m.user_id)}
            className={clsx(styles.memberButton, checked && styles.memberButtonChecked)}
          >
            <span className={clsx(styles.checkMark, checked && styles.checkMarkChecked)}>
              {checked && <Icon name="check" size={10} />}
            </span>
            <UserAvatar
              size="sm"
              user={{ name: m.user_name, email: m.user_email, avatar_ref: m.avatar_ref }}
            />
            <span className={styles.memberText}>
              <span className={styles.memberName}>{m.user_name}</span>
              <span className={styles.memberEmail}>{m.user_email}</span>
            </span>
          </button>
        );
      })}
    </div>
  );
}

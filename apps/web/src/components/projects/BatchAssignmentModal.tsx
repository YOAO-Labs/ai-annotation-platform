import { useEffect, useMemo, useRef, useState } from "react";
import { clsx } from "clsx";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { UserAvatar } from "@/components/ui/UserAvatar";
import { Icon } from "@/components/ui/Icon";
import { Badge } from "@/components/ui/Badge";
import { useToastStore } from "@/components/ui/Toast";
import { useProjectMembers } from "@/hooks/useProjects";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/api/client";
import { batchesApi, type BatchDistributionPreview } from "@/api/batches";
import { isCurrentAuthOwner, useAuthStore } from "@/stores/authStore";
import { DistributionPreview } from "./ProjectDistributeBatchesModal";
import type { BatchResponse } from "@/api/batches";
import styles from "./BatchAssignmentModal.module.css";

interface Props {
  projectId: string;
  batch: BatchResponse;
  onClose: () => void;
}

/**
 * v0.7.2 · 一 batch = 一标注员 + 一审核员（单选语义）。
 * 提交后 PATCH /batches/{id}（写 annotator_id / reviewer_id）。
 */
export function BatchAssignmentModal({ projectId, batch, onClose }: Props) {
  const pushToast = useToastStore((s) => s.push);
  const { data: members = [], isLoading } = useProjectMembers(projectId);
  const qc = useQueryClient();
  const ownerId = useAuthStore((state) => state.user?.id);
  const [preview, setPreview] = useState<BatchDistributionPreview | null>(null);
  const previewMutation = useMutation({
    mutationFn: (payload: { annotator_id: string | null; reviewer_id: string | null }) =>
      batchesApi.previewAssignment(projectId, batch.id, payload),
  });
  const update = useMutation({
    mutationFn: (payload: {
      annotator_id: string | null;
      reviewer_id: string | null;
      preview_version: string;
    }) => batchesApi.applyAssignment(projectId, batch.id, payload),
    onSuccess: () => {
      for (const key of [
        ["batches", projectId],
        ["batch", projectId, batch.id],
        ["tasks"],
        ["dashboard"],
      ])
        void qc.invalidateQueries({ queryKey: key });
    },
  });
  const scopeRef = useRef("");

  const [annotatorId, setAnnotatorId] = useState<string | null>(batch.annotator_id);
  const [reviewerId, setReviewerId] = useState<string | null>(batch.reviewer_id);

  useEffect(() => {
    setPreview(null);
    setAnnotatorId(batch.annotator_id);
    setReviewerId(batch.reviewer_id);
  }, [batch.id, batch.annotator_id, batch.reviewer_id]);

  const annotators = useMemo(() => members.filter((m) => m.role === "annotator"), [members]);
  const reviewers = useMemo(() => members.filter((m) => m.role === "reviewer"), [members]);

  const busy = update.isPending || previewMutation.isPending;
  const selectionKey = JSON.stringify([ownerId, projectId, batch.id, annotatorId, reviewerId]);
  scopeRef.current = selectionKey;
  const onSave = () => {
    if (busy || !ownerId || !isCurrentAuthOwner(ownerId)) return;
    const current = () => scopeRef.current === selectionKey && isCurrentAuthOwner(ownerId);
    const payload = { annotator_id: annotatorId, reviewer_id: reviewerId };
    if (!preview) {
      previewMutation.mutate(payload, {
        onSuccess: (data) => {
          if (current()) setPreview(data);
        },
        onError: (error) => {
          if (current())
            pushToast({ msg: "预览失败", sub: (error as Error).message, kind: "error" });
        },
      });
      return;
    }
    update.mutate(
      { ...payload, preview_version: preview.preview_version! },
      {
        onSuccess: () => {
          if (current()) {
            pushToast({ msg: "已更新分派", kind: "success" });
            onClose();
          }
        },
        onError: (error) => {
          if (current()) {
            if (error instanceof ApiError && error.status === 409) setPreview(null);
            pushToast({
              msg: "保存失败，请重新预览",
              sub: (error as Error).message,
              kind: "error",
            });
          }
        },
      },
    );
  };

  const dirty = annotatorId !== batch.annotator_id || reviewerId !== batch.reviewer_id;

  return (
    <Modal
      open
      onClose={() => {
        if (!busy) onClose();
      }}
      title={`分派批次 · ${batch.name}`}
      width={520}
    >
      <div className={styles.description}>
        每个批次由 <strong>1 名标注员</strong> 负责标注、<strong>1 名审核员</strong> 负责审核。
        若需要批量分派项目下多个批次，请用「批次列表 → 按项目分派批次」。
      </div>

      {preview && <DistributionPreview preview={preview} members={members} />}

      {isLoading && <div className={styles.loading}>加载成员…</div>}

      {!isLoading && (
        <div className={styles.columns}>
          <Column
            title="标注员"
            members={annotators}
            selectedId={annotatorId}
            onSelect={(id) => {
              if (!busy) {
                setAnnotatorId(id);
                setPreview(null);
              }
            }}
            roleColor="accent"
          />
          <Column
            title="审核员"
            members={reviewers}
            selectedId={reviewerId}
            onSelect={(id) => {
              if (!busy) {
                setReviewerId(id);
                setPreview(null);
              }
            }}
            roleColor="warning"
          />
        </div>
      )}

      <div className={styles.footer}>
        <span className={styles.selectionSummary}>
          {annotatorId ? "已选标注员" : "未选标注员"}
          {" · "}
          {reviewerId ? "已选审核员" : "未选审核员"}
        </span>
        <div className={styles.actions}>
          <Button onClick={onClose} disabled={busy}>
            取消
          </Button>
          <Button variant="primary" onClick={onSave} disabled={busy || !dirty}>
            {update.isPending
              ? "保存中…"
              : previewMutation.isPending
                ? "预览中…"
                : preview
                  ? "确认分派"
                  : "预览分派"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

function Column({
  title,
  members,
  selectedId,
  onSelect,
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
  selectedId: string | null;
  onSelect: (userId: string | null) => void;
  roleColor: "accent" | "warning";
}) {
  return (
    <div className={styles.column}>
      <div className={styles.columnHeader}>
        <Badge variant={roleColor} dot>
          {title}
        </Badge>
        {selectedId && (
          <button
            type="button"
            onClick={() => onSelect(null)}
            className={styles.clearButton}
            title="清除选择"
          >
            <Icon name="x" size={11} /> 清除
          </button>
        )}
      </div>
      {members.length === 0 && (
        <div className={styles.emptyMembers}>暂无成员，请先在「成员管理」中添加</div>
      )}
      {members.map((m) => {
        const checked = selectedId === m.user_id;
        return (
          <button
            key={m.id}
            type="button"
            onClick={() => onSelect(checked ? null : m.user_id)}
            className={clsx(styles.memberButton, checked && styles.memberButtonChecked)}
          >
            <span className={clsx(styles.radioMark, checked && styles.radioMarkChecked)}>
              {checked && <span className={styles.radioMarkInner} />}
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

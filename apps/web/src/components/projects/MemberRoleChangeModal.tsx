import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { useToastStore } from "@/components/ui/Toast";
import { ApiError } from "@/api/client";
import { PROJECT_ROLE_LABELS, PROJECT_ROLES } from "@/constants/roles";
import { usePreviewProjectMemberRole, useChangeProjectMemberRole } from "@/hooks/useProjects";
import type {
  ProjectMemberResponse,
  ProjectMemberRolePreviewResponse,
  ProjectRole,
} from "@/api/projects";

interface Props {
  open: boolean;
  projectId: string;
  member: ProjectMemberResponse | null;
  members: ProjectMemberResponse[];
  onClose: () => void;
}

const INPUT_CLASS =
  "box-border w-full rounded-md border border-border bg-card px-3 py-2 text-sm text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring";

/**
 * Project-member role change: a read-only preview returns blockers plus a
 * resource-snapshot token; the write echoes `expected_version` + `preview_token`
 * so a stale preview (new assignments, receiver role change) is rejected with a
 * 409 instead of silently overwriting concurrent work.
 *
 * The preview is bound to the exact member/target/receiver inputs. Changing any
 * input invalidates the previous preview, late responses from an older request
 * are discarded, and Save stays disabled until a preview matching the current
 * inputs succeeds.
 */
export function MemberRoleChangeModal({ open, projectId, member, members, onClose }: Props) {
  const pushToast = useToastStore((s) => s.push);
  const previewMutation = usePreviewProjectMemberRole(projectId);
  const change = useChangeProjectMemberRole(projectId);
  const [targetRole, setTargetRole] = useState<ProjectRole>(member?.role ?? "annotator");
  const [reason, setReason] = useState("");
  const [replacementAnnotatorId, setReplacementAnnotatorId] = useState("");
  const [replacementReviewerId, setReplacementReviewerId] = useState("");
  const [preview, setPreview] = useState<ProjectMemberRolePreviewResponse | null>(null);
  const [previewKey, setPreviewKey] = useState<string | null>(null);
  const [previewPending, setPreviewPending] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const requestRef = useRef(0);
  // Dialog-lifetime token: bumped whenever the dialog closes/reopens or the
  // project/member/version context changes, so a late success/error from a
  // previous dialog cannot close or mutate the new one.
  const dialogRef = useRef(0);

  const inputKey = member
    ? `${projectId}|${member.id}|${member.version}|${targetRole}|${replacementAnnotatorId}|${replacementReviewerId}`
    : "";

  const annotatorOptions = useMemo(
    () => members.filter((m) => m.role === "annotator" && m.user_id !== member?.user_id),
    [members, member?.user_id],
  );
  const reviewerOptions = useMemo(
    () => members.filter((m) => m.role === "reviewer" && m.user_id !== member?.user_id),
    [members, member?.user_id],
  );

  const runPreview = useCallback(async () => {
    if (!member) return;
    const key = `${projectId}|${member.id}|${member.version}|${targetRole}|${replacementAnnotatorId}|${replacementReviewerId}`;
    const dialog = dialogRef.current;
    const request = ++requestRef.current;
    setPreviewPending(true);
    setPreview(null);
    setPreviewKey(null);
    setPreviewError(null);
    setStale(false);
    change.reset();
    try {
      const data = await previewMutation.mutateAsync({
        memberId: member.id,
        payload: {
          project_role: targetRole,
          ...(replacementAnnotatorId ? { replacement_annotator_id: replacementAnnotatorId } : {}),
          ...(replacementReviewerId ? { replacement_reviewer_id: replacementReviewerId } : {}),
        },
      });
      if (request !== requestRef.current || dialog !== dialogRef.current) return;
      setPreview(data);
      setPreviewKey(key);
    } catch (error) {
      if (request !== requestRef.current || dialog !== dialogRef.current) return;
      if (error instanceof ApiError && error.status === 409) setStale(true);
      else setPreviewError(error instanceof Error ? error.message : String(error));
    } finally {
      if (request === requestRef.current && dialog === dialogRef.current) setPreviewPending(false);
    }
  }, [
    member,
    projectId,
    targetRole,
    replacementAnnotatorId,
    replacementReviewerId,
    previewMutation,
    change,
  ]);

  useEffect(() => {
    if (!open || !member) return;
    setTargetRole(member.role);
    setReason("");
    setReplacementAnnotatorId("");
    setReplacementReviewerId("");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, member?.id]);

  // Context lifetime: a project/member/version change or close/reopen invalidates
  // any in-flight preview and the write callbacks bound to the old dialog.
  useEffect(() => {
    dialogRef.current += 1;
    requestRef.current += 1;
    setPreview(null);
    setPreviewKey(null);
    setStale(false);
    return () => {
      dialogRef.current += 1;
      requestRef.current += 1;
    };
  }, [open, projectId, member?.id, member?.version]);

  useEffect(() => {
    if (open && member) void runPreview();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    open,
    projectId,
    member?.id,
    member?.version,
    targetRole,
    replacementAnnotatorId,
    replacementReviewerId,
  ]);

  if (!member) return null;

  const blockers = preview?.blockers ?? [];
  const requiresHandoff = preview?.requires_handoff ?? false;
  // Only a preview produced for exactly these inputs may authorize the write.
  const previewMatches = !!preview && previewKey === inputKey && !previewPending;
  const canSubmit =
    previewMatches &&
    !previewError &&
    !stale &&
    blockers.length === 0 &&
    reason.trim().length > 0 &&
    !change.isPending;

  const submit = () => {
    if (!canSubmit || !preview) return;
    const dialog = dialogRef.current;
    const submittingMemberId = member.id;
    change.mutate(
      {
        memberId: submittingMemberId,
        payload: {
          project_role: targetRole,
          expected_version: preview.current_version,
          preview_token: preview.preview_token,
          reason: reason.trim(),
          ...(replacementAnnotatorId ? { replacement_annotator_id: replacementAnnotatorId } : {}),
          ...(replacementReviewerId ? { replacement_reviewer_id: replacementReviewerId } : {}),
        },
      },
      {
        onSuccess: () => {
          // Ignore a late response once the dialog closed/reopened or moved to
          // another project/member/version.
          if (dialog !== dialogRef.current) return;
          pushToast({
            msg: `已将 ${member.user_name} 的职责改为${PROJECT_ROLE_LABELS[targetRole]}`,
            kind: "success",
          });
          onClose();
        },
        onError: (error) => {
          if (dialog !== dialogRef.current) return;
          if (error instanceof ApiError && error.status === 409) {
            setStale(true);
            pushToast({ msg: "预览已过期，请重新预览后再保存", kind: "warning" });
          } else {
            pushToast({ msg: "更改职责失败", sub: (error as Error).message, kind: "error" });
          }
        },
      },
    );
  };

  return (
    <Modal open={open} onClose={onClose} title={`更改职责 · ${member.user_name}`} width={520}>
      <div className="flex flex-col gap-3.5 text-sm">
        <label className="flex flex-col gap-1">
          目标项目职责
          <select
            aria-label="目标项目职责"
            value={targetRole}
            onChange={(e) => setTargetRole(e.target.value as ProjectRole)}
            className={INPUT_CLASS}
            disabled={change.isPending}
          >
            {PROJECT_ROLES.map((role) => (
              <option key={role} value={role}>
                {PROJECT_ROLE_LABELS[role]}
              </option>
            ))}
          </select>
        </label>

        {requiresHandoff && (
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="flex flex-col gap-1">
              标注工作交接给
              <select
                aria-label="标注工作交接给"
                value={replacementAnnotatorId}
                onChange={(e) => setReplacementAnnotatorId(e.target.value)}
                className={INPUT_CLASS}
              >
                <option value="">— 不交接 —</option>
                {annotatorOptions.map((m) => (
                  <option key={m.user_id} value={m.user_id}>
                    {m.user_name}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1">
              质检工作交接给
              <select
                aria-label="质检工作交接给"
                value={replacementReviewerId}
                onChange={(e) => setReplacementReviewerId(e.target.value)}
                className={INPUT_CLASS}
              >
                <option value="">— 不交接 —</option>
                {reviewerOptions.map((m) => (
                  <option key={m.user_id} value={m.user_id}>
                    {m.user_name}
                  </option>
                ))}
              </select>
            </label>
          </div>
        )}

        <div className="rounded-md border border-border bg-muted/40 p-3 text-xs">
          <div className="flex items-center justify-between">
            <span className="font-medium">变更影响预览</span>
            <Button
              type="button"
              size="sm"
              onClick={() => void runPreview()}
              disabled={previewPending}
            >
              {previewPending ? "读取中…" : "重新预览"}
            </Button>
          </div>
          {previewError && <div className="mt-1 text-status-danger">{previewError}</div>}
          {stale && (
            <div className="mt-1 text-status-caution" role="alert">
              预览已过期：请重新预览，确认最新任务与锁状态后再保存。
            </div>
          )}
          {blockers.length > 0 && (
            <div className="mt-1 text-status-danger">存在阻塞项：{blockers.join("；")}</div>
          )}
          {previewMatches && blockers.length === 0 && !stale && (
            <div className="mt-1 text-muted-foreground">
              当前职责 {PROJECT_ROLE_LABELS[preview.current_role]} → 目标职责{" "}
              {PROJECT_ROLE_LABELS[preview.target_role]}（版本 {preview.current_version}）。
            </div>
          )}
        </div>

        <label className="flex flex-col gap-1">
          变更原因
          <textarea
            aria-label="变更原因"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            rows={3}
            className={INPUT_CLASS}
            placeholder="例如：调整项目分工"
          />
        </label>

        {change.isError && (
          <div className="flex items-center gap-2 rounded-md border border-status-danger bg-status-danger-soft px-3 py-2 text-status-danger">
            <Icon name="warning" size={12} />
            {(change.error as Error)?.message ?? "保存失败"}
          </div>
        )}

        <div className="flex justify-end gap-2">
          <Button type="button" onClick={onClose} disabled={change.isPending}>
            取消
          </Button>
          <Button type="button" variant="primary" onClick={submit} disabled={!canSubmit}>
            {change.isPending ? "保存中…" : "保存职责"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

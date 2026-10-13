import { useEffect, useMemo, useRef, useState } from "react";
import { clsx } from "clsx";
import { useQuery } from "@tanstack/react-query";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { UserAvatar } from "@/components/ui/UserAvatar";
import { Icon } from "@/components/ui/Icon";
import { useToastStore } from "@/components/ui/Toast";
import { useAddProjectMember } from "@/hooks/useProjects";
import { usersApi } from "@/api/users";
import type { ProjectMemberResponse } from "@/api/projects";
import styles from "./AssignMemberModal.module.css";

interface Props {
  open: boolean;
  projectId: string;
  existing: ProjectMemberResponse[];
  onClose: () => void;
}

type MemberRole = "annotator" | "reviewer";

/** Per-role selections. A user lives in at most one role's list at a time. */
type Selection = Record<MemberRole, string[]>;

const ROLE_LABEL: Record<MemberRole, string> = {
  annotator: "标注员",
  reviewer: "审核员",
};

const ROLES: MemberRole[] = ["annotator", "reviewer"];
const EMPTY_SELECTION: Selection = { annotator: [], reviewer: [] };
const otherRole = (role: MemberRole): MemberRole =>
  role === "annotator" ? "reviewer" : "annotator";

export function AssignMemberModal({ open, projectId, existing, onClose }: Props) {
  const pushToast = useToastStore((s) => s.push);
  const [role, setRole] = useState<MemberRole>("annotator");
  const [selected, setSelected] = useState<Selection>(EMPTY_SELECTION);
  const [query, setQuery] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const add = useAddProjectMember(projectId);

  // Every open/project is an independent assignment context. Reset all local
  // state on entry and invalidate the previous context on exit (close, project
  // switch or unmount) so a late result cannot close or clear a newer context.
  const contextEpoch = useRef(0);
  useEffect(() => {
    contextEpoch.current += 1;
    setRole("annotator");
    setSelected(EMPTY_SELECTION);
    setQuery("");
    setSubmitting(false);
    return () => {
      contextEpoch.current += 1;
    };
  }, [open, projectId]);

  const { data: users = [], isLoading } = useQuery({
    // Assignment candidates are ordinary active employees; the membership role
    // is chosen separately below and rechecked by the server on write.
    queryKey: ["users", "assign-member", "employee"],
    queryFn: () => usersApi.list({ role: "employee" }),
    enabled: open,
  });

  const existingIds = useMemo(() => new Set(existing.map((m) => m.user_id)), [existing]);

  const candidates = useMemo(() => {
    const q = query.trim().toLowerCase();
    return users.filter((u) => {
      if (existingIds.has(u.id)) return false;
      if (!q) return true;
      return (
        u.name.toLowerCase().includes(q) ||
        u.email.toLowerCase().includes(q) ||
        (u.group_name?.toLowerCase().includes(q) ?? false)
      );
    });
  }, [users, query, existingIds]);

  const annotatorCount = selected.annotator.length;
  const reviewerCount = selected.reviewer.length;
  const totalCount = annotatorCount + reviewerCount;

  const toggleSelected = (userId: string) => {
    if (submitting) return;
    setSelected((prev) => {
      // A user selected in the opposite role stays there until unselected on
      // that role's tab; selecting here would silently transfer them.
      if (prev[otherRole(role)].includes(userId)) return prev;
      const inRole = prev[role].includes(userId);
      return {
        ...prev,
        [role]: inRole ? prev[role].filter((id) => id !== userId) : [...prev[role], userId],
      };
    });
  };

  const onConfirm = async () => {
    if (submitting || totalCount === 0) return;
    const payloads = ROLES.flatMap((r) =>
      selected[r].map((userId) => ({ userId, role: r as MemberRole })),
    );
    const epoch = contextEpoch.current;
    setSubmitting(true);
    // The shared mutation observer's isPending tracks a single concurrent
    // request, so lock on the whole batch and release only when all settle.
    const results = await Promise.allSettled(
      payloads.map((p) => add.mutateAsync({ user_id: p.userId, role: p.role })),
    );
    // A close/reopen or project switch started a new context: discard results.
    if (contextEpoch.current !== epoch) return;

    const failed: Selection = { annotator: [], reviewer: [] };
    let successCount = 0;
    results.forEach((result, index) => {
      if (result.status === "fulfilled") {
        successCount += 1;
      } else {
        failed[payloads[index].role].push(payloads[index].userId);
      }
    });
    const failCount = payloads.length - successCount;

    if (successCount > 0) {
      pushToast({ msg: `已指派 ${successCount} 名成员`, kind: "success" });
    }
    if (failCount > 0) {
      const firstError = results.find((r) => r.status === "rejected");
      pushToast({
        msg: `${failCount} 名成员指派失败`,
        sub: firstError && firstError.status === "rejected" ? String(firstError.reason) : undefined,
        kind: "error",
      });
      // Keep only the failed users, with their original roles, for retry.
      setSelected(failed);
      setSubmitting(false);
      return;
    }

    setSelected(EMPTY_SELECTION);
    setQuery("");
    setSubmitting(false);
    onClose();
  };

  return (
    <Modal open={open} onClose={onClose} title="添加项目成员" width={560}>
      <div className={styles.stack}>
        <div className={styles.roleTabs} aria-label="成员角色">
          {ROLES.map((nextRole) => (
            <button
              key={nextRole}
              type="button"
              onClick={() => setRole(nextRole)}
              disabled={submitting}
              aria-pressed={role === nextRole}
              className={clsx(styles.roleButton, role === nextRole && styles.roleButtonActive)}
            >
              {ROLE_LABEL[nextRole]}
            </button>
          ))}
        </div>
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="按姓名、邮箱、分组搜索"
          className={styles.searchInput}
        />
        <div className={styles.list} aria-busy={submitting}>
          {isLoading && <div className={styles.emptyState}>加载中...</div>}
          {!isLoading && candidates.length === 0 && (
            <div className={styles.emptyState}>没有可添加的{ROLE_LABEL[role]}</div>
          )}
          {candidates.map((u) => {
            const active = selected[role].includes(u.id);
            const takenBy = otherRole(role);
            const takenByOther = selected[takenBy].includes(u.id);
            return (
              <button
                key={u.id}
                type="button"
                onClick={() => toggleSelected(u.id)}
                disabled={takenByOther || submitting}
                aria-pressed={active}
                title={takenByOther ? `已选为${ROLE_LABEL[takenBy]}` : undefined}
                className={clsx(
                  styles.userButton,
                  active && styles.userButtonActive,
                  takenByOther && styles.userButtonDisabled,
                )}
              >
                <span
                  aria-hidden
                  className={clsx(styles.checkbox, active && styles.checkboxActive)}
                >
                  {active && <Icon name="check" size={12} />}
                </span>
                <UserAvatar user={u} size="sm" />
                <div className={styles.userBody}>
                  <div className={styles.userName}>{u.name}</div>
                  <div className={styles.userMeta}>
                    {u.email}
                    {u.group_name ? ` · ${u.group_name}` : ""}
                  </div>
                </div>
                {takenByOther && (
                  <span className={styles.otherRoleHint}>已选为{ROLE_LABEL[takenBy]}</span>
                )}
              </button>
            );
          })}
        </div>
        <div
          className={styles.selectionSummary}
          aria-live="polite"
          data-testid="assign-member-summary"
        >
          已选择 标注员 {annotatorCount} 名 · 审核员 {reviewerCount} 名（共 {totalCount} 人）
        </div>
        <div className={styles.actions}>
          <Button variant="ghost" onClick={onClose} disabled={submitting}>
            取消
          </Button>
          <Button
            variant="primary"
            disabled={totalCount === 0 || submitting}
            aria-busy={submitting}
            onClick={onConfirm}
          >
            {submitting ? "指派中..." : `确认指派 ${totalCount} 人`}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

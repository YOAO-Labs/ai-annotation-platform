import { CopyableError } from "@/components/ui/CopyableError";
import { useEffect, useState, type FormEvent } from "react";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { useToastStore } from "@/components/ui/Toast";
import { useAssignUserGroup, useChangeUserRole } from "@/hooks/useUsers";
import { useGroups } from "@/hooks/useGroups";
import { isCurrentAuthOwner, useAuthStore } from "@/stores/authStore";
import { usePermissions } from "@/hooks/usePermissions";
import { ROLE_LABELS, PROJECT_ROLE_LABELS, roleLabel } from "@/constants/roles";
import type { UserResponse } from "@/api/users";
import { usersApi, type RoleImpactPreview } from "@/api/users";
import type { PlatformRole } from "@/types";
import styles from "./EditUserModal.module.css";

interface Props {
  open: boolean;
  user: UserResponse | null;
  onClose: () => void;
}

const ASSIGNABLE_ROLES_BY_ACTOR: Record<PlatformRole, PlatformRole[]> = {
  super_admin: ["super_admin", "project_admin", "employee", "viewer"],
  project_admin: [],
  employee: [],
  viewer: [],
};

export function EditUserModal({ open, user, onClose }: Props) {
  const { role: actorRole } = usePermissions();
  const allowedRoles = ASSIGNABLE_ROLES_BY_ACTOR[actorRole] ?? [];
  const canManagePlatformRoles = allowedRoles.length > 0;

  const { data: groups = [] } = useGroups(open);
  const changeRole = useChangeUserRole();
  const assignGroup = useAssignUserGroup();
  const pushToast = useToastStore((s) => s.push);

  const ownerId = useAuthStore((state) => state.user?.id);
  const targetRole = user?.role as PlatformRole | undefined;
  const canEditRole =
    !!ownerId &&
    !!user &&
    ownerId !== user.id &&
    user.is_managed !== false &&
    targetRole !== undefined &&
    allowedRoles.includes(targetRole);
  const [previewRevision, setPreviewRevision] = useState(0);
  const [roleVal, setRoleVal] = useState<PlatformRole>("employee");
  const [groupId, setGroupId] = useState<string>("");
  const [rolePreview, setRolePreview] = useState<RoleImpactPreview | null>(null);
  const [rolePreviewPending, setRolePreviewPending] = useState(false);
  const [rolePreviewError, setRolePreviewError] = useState<string | null>(null);

  useEffect(() => {
    if (open && user) {
      setRoleVal(user.role as PlatformRole);
      setGroupId(user.group_id ?? "");
      changeRole.reset();
      assignGroup.reset();
      setRolePreview(null);
      setRolePreviewError(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, user?.id]);

  useEffect(() => {
    if (!open || !user || !ownerId || !canEditRole) return;
    let alive = true;
    setRolePreview(null);
    setRolePreviewPending(true);
    setRolePreviewError(null);
    void usersApi
      .previewRoleChange(user.id, roleVal)
      .then((data) => {
        if (alive && isCurrentAuthOwner(ownerId)) setRolePreview(data);
      })
      .catch((error) => {
        if (alive && isCurrentAuthOwner(ownerId))
          setRolePreviewError(error instanceof Error ? error.message : String(error));
      })
      .finally(() => {
        if (alive) setRolePreviewPending(false);
      });
    return () => {
      alive = false;
    };
  }, [open, user?.id, ownerId, roleVal, previewRevision, canEditRole]);

  if (!user || targetRole === undefined) return null;

  const dirtyRole = canEditRole && roleVal !== user.role;
  const dirtyGroup = (groupId || null) !== (user.group_id ?? null);
  const dirty = dirtyRole || dirtyGroup;

  const loadRolePreview = () => setPreviewRevision((value) => value + 1);
  const busy = changeRole.isPending || assignGroup.isPending;
  const currentPreview =
    rolePreview?.user_id === user.id && rolePreview.requested_role === roleVal ? rolePreview : null;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (busy || !ownerId || !isCurrentAuthOwner(ownerId)) return;
    const token = useAuthStore.getState().token;
    const current = () => isCurrentAuthOwner(ownerId) && useAuthStore.getState().token === token;
    if (!dirty) {
      onClose();
      return;
    }
    if (dirtyRole && !currentPreview) {
      loadRolePreview();
      return;
    }
    if (dirtyRole && currentPreview && !currentPreview.can_change) return;
    try {
      if (dirtyRole) {
        await changeRole.mutateAsync({ userId: user.id, role: roleVal });
      }
      if (!current()) return;
      if (dirtyGroup) {
        await assignGroup.mutateAsync({ userId: user.id, groupId: groupId || null });
      }
      if (!current()) return;
      pushToast({ msg: "已保存", kind: "success" });
      onClose();
    } catch (err) {
      if (!current()) return;
      pushToast({
        msg: "保存失败",
        sub: err instanceof Error ? err.message : String(err),
        kind: "error",
      });
    }
  };

  const error = changeRole.error || assignGroup.error;

  const roleOptions: PlatformRole[] = Array.from(
    new Set<PlatformRole>([targetRole, ...allowedRoles]),
  );

  const editRoleHint =
    actorRole === "project_admin"
      ? "平台角色由超级管理员管理；项目职责请在该项目的成员管理中调整"
      : !canEditRole
        ? "你无权修改该用户的角色"
        : "";

  return (
    <Modal
      open={open}
      onClose={() => {
        if (!busy) onClose();
      }}
      title={`编辑成员 · ${user.name}`}
      width={520}
    >
      <form onSubmit={submit} className={styles.form}>
        <Field label="邮箱">
          <input value={user.email} readOnly className={`${styles.input} ${styles.mutedInput}`} />
        </Field>

        {canManagePlatformRoles ? (
          <>
            <Field label={`角色${editRoleHint ? `（${editRoleHint}）` : ""}`}>
              <select
                value={roleVal}
                onChange={(e) => {
                  setRoleVal(e.target.value as PlatformRole);
                  setRolePreview(null);
                  setRolePreviewError(null);
                }}
                disabled={!canEditRole || busy}
                className={`${styles.input} ${canEditRole ? "" : styles.disabledInput}`}
              >
                {roleOptions.map((r) => (
                  <option key={r} value={r} disabled={!canEditRole && r !== targetRole}>
                    {ROLE_LABELS[r] ?? r}
                  </option>
                ))}
              </select>
            </Field>

            {canEditRole && (
              <div className="flex flex-col gap-2 rounded-md border border-border bg-muted/40 p-3 text-xs">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="font-medium text-foreground">角色影响预览</span>
                  <Button
                    type="button"
                    size="sm"
                    onClick={loadRolePreview}
                    disabled={rolePreviewPending || busy}
                  >
                    {rolePreviewPending ? "读取中…" : rolePreview ? "重新预览" : "查看影响"}
                  </Button>
                </div>
                {rolePreviewError && (
                  <div className="text-status-danger">
                    <CopyableError message={rolePreviewError} />
                  </div>
                )}
                {currentPreview && (
                  <>
                    <div className="text-muted-foreground">
                      平台角色：{roleLabel(currentPreview.current_role)}
                      。项目身份与可操作范围见下方明细。
                    </div>
                    {currentPreview.other_project_count > 0 && (
                      <div className="text-status-caution">
                        另涉及 {currentPreview.other_project_count}{" "}
                        个管理范围外的项目，详情由对应负责人管理。
                      </div>
                    )}
                    {currentPreview.warnings.map((warning) => (
                      <div key={warning} className="text-status-caution">
                        {warning}
                      </div>
                    ))}
                    {!currentPreview.can_change && (
                      <div className="text-status-danger">
                        无法修改：{currentPreview.blockers.join("；")}
                      </div>
                    )}
                    <div className="text-muted-foreground">
                      将影响 {currentPreview.projects.length} 个项目、
                      {currentPreview.assigned_batch_count} 个已分派批次、
                      {currentPreview.assigned_task_count} 个待办任务和{" "}
                      {currentPreview.review_task_count} 个审核任务。
                    </div>
                    {currentPreview.projects.length > 0 && (
                      <ul className="m-0 list-disc space-y-1 pl-4 text-muted-foreground">
                        {currentPreview.projects.map((project) => (
                          <li key={project.project_id}>
                            {project.project_name} · 项目身份{" "}
                            {project.membership_role
                              ? (PROJECT_ROLE_LABELS[
                                  project.membership_role as keyof typeof PROJECT_ROLE_LABELS
                                ] ?? project.membership_role)
                              : "非成员"}{" "}
                            · 标注批次 {project.annotator_batch_count} · 审核批次{" "}
                            {project.reviewer_batch_count} · 待办{" "}
                            {project.assigned_task_count + project.review_task_count}
                          </li>
                        ))}
                      </ul>
                    )}
                  </>
                )}
              </div>
            )}
          </>
        ) : (
          <Field label="角色">
            <input
              value={ROLE_LABELS[targetRole] ?? targetRole}
              readOnly
              className={`${styles.input} ${styles.mutedInput}`}
            />
            {editRoleHint && <div className={styles.fieldLabel}>{editRoleHint}</div>}
          </Field>
        )}

        <Field label="数据组">
          <select
            value={groupId}
            disabled={busy}
            onChange={(e) => setGroupId(e.target.value)}
            className={styles.input}
          >
            <option value="">— 未分配 —</option>
            {groups.map((g) => (
              <option key={g.id} value={g.id}>
                {g.name}
              </option>
            ))}
          </select>
        </Field>

        {error && (
          <div className={styles.errorBox}>
            <Icon name="warning" size={13} />{" "}
            <CopyableError message={(error as Error)?.message ?? "操作失败"} />
          </div>
        )}

        <div className={styles.footer}>
          <div className={styles.actions}>
            <Button type="button" disabled={busy} onClick={onClose}>
              取消
            </Button>
            <Button
              type="submit"
              variant="primary"
              disabled={!dirty || busy || (dirtyRole && !currentPreview?.can_change)}
            >
              {busy ? "保存中…" : "保存"}
            </Button>
          </div>
        </div>
      </form>
    </Modal>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className={styles.field}>
      <div className={styles.fieldLabel}>{label}</div>
      {children}
    </label>
  );
}

import { useEffect, useState } from "react";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Badge } from "@/components/ui/Badge";
import { useToastStore } from "@/components/ui/Toast";
import { useInviteUser } from "@/hooks/useInvitation";
import { useSendInvitationEmail } from "@/hooks/useInvitations";
import { groupsApi, type GroupResponse } from "@/api/groups";
import { projectsApi, type ProjectResponse } from "@/api/projects";
import { ROLE_LABELS, PROJECT_ROLE_LABELS } from "@/constants/roles";
import { usePermissions } from "@/hooks/usePermissions";
import type { InvitationCreated } from "@/api/users";
import type { PlatformRole, ProjectRole } from "@/types";
import styles from "./InviteUserModal.module.css";

interface Props {
  open: boolean;
  onClose: () => void;
}

/**
 * Platform identity and project responsibility are separate invitation fields.
 * A project invitation carries a compatible pair (`role` + `project_member_role`);
 * a non-project invitation only creates the platform account.
 */
const INVITABLE_PLATFORM_ROLES_BY_ACTOR: Record<PlatformRole, PlatformRole[]> = {
  super_admin: ["super_admin", "project_admin", "employee", "viewer"],
  project_admin: ["employee", "viewer"],
  employee: [],
  viewer: [],
};

const ALL_PROJECT_ROLES: ProjectRole[] = ["annotator", "reviewer", "viewer"];

function compatibleProjectRoles(platformRole: PlatformRole): ProjectRole[] {
  return platformRole === "viewer" ? ["viewer"] : ALL_PROJECT_ROLES;
}

export function InviteUserModal({ open, onClose }: Props) {
  const { role } = usePermissions();
  const allowedPlatformRoles = INVITABLE_PLATFORM_ROLES_BY_ACTOR[role] ?? [];
  const [email, setEmail] = useState("");
  const [platformRole, setPlatformRole] = useState<PlatformRole>(
    allowedPlatformRoles[0] ?? "employee",
  );
  const [projectRole, setProjectRole] = useState<ProjectRole>("annotator");
  const [groupName, setGroupName] = useState("");
  const [projectId, setProjectId] = useState("");
  const [projectQuery, setProjectQuery] = useState("");
  const [groups, setGroups] = useState<GroupResponse[]>([]);
  const [projects, setProjects] = useState<ProjectResponse[]>([]);
  const [result, setResult] = useState<InvitationCreated | null>(null);
  const invite = useInviteUser();
  const sendEmail = useSendInvitationEmail();
  const pushToast = useToastStore((s) => s.push);

  const projectRoleOptions = compatibleProjectRoles(platformRole);

  useEffect(() => {
    if (!projectRoleOptions.includes(projectRole)) {
      setProjectRole(projectRoleOptions[0] ?? "annotator");
    }
  }, [projectRole, projectRoleOptions]);

  useEffect(() => {
    if (!open) return;
    let active = true;
    void groupsApi
      .list()
      .then((items) => {
        if (active) setGroups(Array.isArray(items) ? items : []);
      })
      .catch(() => {
        if (active) setGroups([]);
      });
    void projectsApi
      .list()
      .then((items) => {
        if (active) setProjects(Array.isArray(items) ? items : []);
      })
      .catch(() => {
        if (active) setProjects([]);
      });
    return () => {
      active = false;
    };
  }, [open]);

  useEffect(() => {
    if (!open) {
      setEmail("");
      setGroupName("");
      setProjectId("");
      setProjectQuery("");
      setPlatformRole(allowedPlatformRoles[0] ?? "employee");
      setProjectRole("annotator");
      setResult(null);
      invite.reset();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!email.trim() || !platformRole) return;
    invite.mutate(
      {
        email: email.trim().toLowerCase(),
        role: platformRole,
        ...(projectId ? { project_id: projectId, project_member_role: projectRole } : {}),
        group_name: groupName.trim() || undefined,
      },
      {
        onSuccess: (data) => setResult(data),
      },
    );
  };

  const copy = async () => {
    if (!result) return;
    try {
      await navigator.clipboard.writeText(result.invite_url);
      pushToast({ msg: "邀请链接已复制", kind: "success" });
    } catch {
      pushToast({ msg: "复制失败，请手动选择" });
    }
  };

  const sendInvitationEmail = () => {
    if (!result?.id || sendEmail.isPending) return;
    sendEmail.mutate(result.id, {
      onSuccess: (response) =>
        pushToast({
          msg: response.ok ? "邀请邮件已发送" : "邀请邮件未发送",
          sub: response.message || `收件人：${response.email}`,
          kind: response.ok ? "success" : "warning",
        }),
      onError: (error) =>
        pushToast({
          msg: "邀请邮件发送失败",
          sub: error instanceof Error ? error.message : String(error),
          kind: "warning",
        }),
    });
  };

  return (
    <Modal open={open} onClose={onClose} title={result ? "邀请已生成" : "邀请新成员"} width={520}>
      {!result ? (
        <form onSubmit={submit} className={styles.form}>
          <Field label="邮箱">
            <input
              type="email"
              required
              autoFocus
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="newuser@your-org.com"
              className={styles.input}
            />
          </Field>

          <Field label="账号角色（平台身份）">
            <select
              required
              value={platformRole}
              onChange={(e) => setPlatformRole(e.target.value as PlatformRole)}
              className={styles.input}
            >
              {allowedPlatformRoles.map((r) => (
                <option key={r} value={r}>
                  {ROLE_LABELS[r]}
                </option>
              ))}
            </select>
          </Field>

          <Field label="数据组（可选）">
            <input
              type="text"
              list="invite-group-options"
              value={groupName}
              onChange={(e) => setGroupName(e.target.value)}
              placeholder="例如：标注组A"
              className={styles.input}
            />
            <datalist id="invite-group-options">
              {groups.map((group) => (
                <option key={group.id} value={group.name} />
              ))}
            </datalist>
          </Field>

          <Field label="目标项目（可选）">
            <input
              type="text"
              list="invite-project-options"
              value={projectQuery}
              onChange={(e) => {
                const value = e.target.value;
                const project = projects.find(
                  (candidate) =>
                    candidate.id === value ||
                    candidate.name === value ||
                    candidate.display_id === value,
                );
                setProjectQuery(value);
                setProjectId(project?.id ?? "");
              }}
              placeholder="输入或选择项目名称"
              className={styles.input}
            />
            <datalist id="invite-project-options">
              {projects.map((project) => (
                <option key={project.id} value={project.name}>
                  {project.display_id}
                </option>
              ))}
            </datalist>
            <div className={styles.fieldHint}>
              留空表示接受后再分配项目。指定项目时需同时选择项目职责；平台观察者只能是观察者，管理员通过项目负责人身份管理项目而非成员职责。
            </div>
          </Field>

          {projectId && (
            <Field label="项目职责">
              <select
                required
                value={projectRole}
                onChange={(e) => setProjectRole(e.target.value as ProjectRole)}
                className={styles.input}
              >
                {projectRoleOptions.map((r) => (
                  <option key={r} value={r}>
                    {PROJECT_ROLE_LABELS[r]}
                  </option>
                ))}
              </select>
            </Field>
          )}

          {invite.isError && (
            <div className={styles.errorBox}>
              <Icon name="warning" size={13} />
              {(invite.error as Error)?.message ?? "邀请失败"}
            </div>
          )}

          <div className={styles.actions}>
            <Button type="button" onClick={onClose}>
              取消
            </Button>
            <Button type="submit" variant="primary" disabled={invite.isPending}>
              {invite.isPending ? "生成中..." : "生成邀请链接"}
            </Button>
          </div>
        </form>
      ) : (
        <div className={styles.result}>
          <div className={styles.successBox}>
            <Icon name="check" size={14} className={styles.successIcon} />
            <div>
              <div className={styles.successTitle}>
                邀请已写入审计日志，链接 {formatExpiry(result.expires_at)} 内有效
              </div>
              <div className={styles.successText}>
                请妥善转发链接给被邀请人。链接始终可复制；邮件发送需要在下方明确触发。
              </div>
            </div>
          </div>

          <Field label="一次性注册链接">
            <div className={styles.linkRow}>
              <input
                readOnly
                value={result.invite_url}
                onClick={(e) => (e.currentTarget as HTMLInputElement).select()}
                className={`${styles.input} ${styles.monoInput}`}
              />
              <Button type="button" onClick={copy}>
                <Icon name="link" size={12} />
                复制
              </Button>
            </div>
          </Field>

          <div className={styles.metaRow}>
            <Badge variant="outline">{ROLE_LABELS[platformRole]}</Badge>
            {projectId && <Badge variant="outline">{PROJECT_ROLE_LABELS[projectRole]}</Badge>}
            {groupName && <Badge variant="outline">{groupName}</Badge>}
            {result.project_name && <Badge variant="outline">项目：{result.project_name}</Badge>}
            <span className={`mono ${styles.expiresAt}`}>
              过期：{new Date(result.expires_at).toLocaleString("zh-CN")}
            </span>
          </div>

          <div className={styles.actions}>
            <Button
              type="button"
              onClick={sendInvitationEmail}
              disabled={!result.id || sendEmail.isPending}
              title={!result.id ? "当前 API 未返回邀请编号，请使用复制链接" : undefined}
            >
              <Icon name="mail" size={12} />
              {sendEmail.isPending ? "发送中..." : "发送邀请邮件"}
            </Button>
            <Button type="button" onClick={() => setResult(null)}>
              继续邀请
            </Button>
            <Button type="button" variant="primary" onClick={onClose}>
              完成
            </Button>
          </div>
        </div>
      )}
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

function formatExpiry(iso: string): string {
  const ms = new Date(iso).getTime() - Date.now();
  const days = Math.max(0, Math.round(ms / 86400000));
  return `${days} 天`;
}

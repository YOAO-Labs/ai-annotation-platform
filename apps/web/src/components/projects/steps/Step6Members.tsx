// v0.10.18 · CreateProjectWizard 第 6 步: 项目成员选择 (annotator / reviewer).
// 从 CreateProjectWizard.tsx 抽出.
// v0.25.x · 候选人统一为平台员工，项目职责（标注员 / 质检员）在选中后单独选择。

import { useMemo, useState } from "react";
import { clsx } from "clsx";
import { UserAvatar } from "@/components/ui/UserAvatar";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { useToastStore } from "@/components/ui/Toast";
import { useAddProjectMember } from "@/hooks/useProjects";
import { useUsers } from "@/hooks/useUsers";
import { PROJECT_ROLE_LABELS } from "@/constants/roles";
import type { UserResponse } from "@/api/users";
import type { ProjectResponse } from "@/api/projects";
import type { FormState } from "../CreateProjectWizard";
import styles from "../CreateProjectWizard.module.css";

type MemberRole = "annotator" | "reviewer";

const SELECTABLE_PROJECT_ROLES: MemberRole[] = ["annotator", "reviewer"];

export function Step6Members({
  project,
  form,
  setForm,
  onNext,
}: {
  project: ProjectResponse;
  form: FormState;
  setForm: React.Dispatch<React.SetStateAction<FormState>>;
  onNext: (added: number) => void;
}) {
  const pushToast = useToastStore((s) => s.push);
  const addMember = useAddProjectMember(project.id);
  // Assignment candidates are ordinary active employees; the project role is
  // chosen per selected member (server rechecks compatibility on write).
  const employeesQuery = useUsers({ role: "employee" });
  const isLoading = employeesQuery.isLoading;
  const users = useMemo(
    () =>
      [...(employeesQuery.data ?? [])].sort((a: UserResponse, b: UserResponse) =>
        b.created_at.localeCompare(a.created_at),
      ),
    [employeesQuery.data],
  );
  const [adding, setAdding] = useState(false);

  const toggle = (userId: string, role: MemberRole) => {
    setForm((s) => {
      const exists = s.members.find((m) => m.userId === userId);
      if (exists) return { ...s, members: s.members.filter((m) => m.userId !== userId) };
      return { ...s, members: [...s.members, { userId, role }] };
    });
  };

  const setMemberRole = (userId: string, role: MemberRole) => {
    setForm((s) => ({
      ...s,
      members: s.members.map((m) => (m.userId === userId ? { ...m, role } : m)),
    }));
  };

  const onContinue = async () => {
    if (form.members.length === 0) {
      onNext(0);
      return;
    }
    setAdding(true);
    let ok = 0;
    for (const m of form.members) {
      try {
        await addMember.mutateAsync({ user_id: m.userId, role: m.role });
        ok++;
      } catch (e) {
        pushToast({
          msg: "添加成员失败",
          sub: (e as Error).message,
          kind: "error",
        });
      }
    }
    setAdding(false);
    pushToast({ msg: `已添加 ${ok} 位成员`, kind: "success" });
    onNext(ok);
  };

  return (
    <div className={styles.formStack}>
      <div className={styles.sectionHint}>
        选择项目成员并指定项目职责（可空）。账号平台身份与项目职责相互独立。
      </div>

      {isLoading && <div className={styles.inlineLoading}>加载用户…</div>}

      {!isLoading && users.length === 0 && (
        <div className={styles.emptyPanel}>暂无员工账号，可跳过此步骤。</div>
      )}

      {!isLoading && users.length > 0 && (
        <div className={styles.memberList}>
          {users.map((u) => {
            const selected = form.members.find((m) => m.userId === u.id);
            const checked = !!selected;
            return (
              <div key={u.id} className={styles.memberRow}>
                <button
                  type="button"
                  onClick={() => toggle(u.id, selected?.role ?? "annotator")}
                  className={clsx(styles.choiceButton, checked && styles.choiceButtonChecked)}
                >
                  <span className={clsx(styles.checkMark, checked && styles.checkMarkChecked)}>
                    {checked && <Icon name="check" size={10} />}
                  </span>
                  <UserAvatar user={u} size="sm" />
                  <span className={styles.choiceBody}>
                    <div className={styles.choiceTitle}>{u.name || u.email}</div>
                    <div className={styles.choiceMeta}>{u.email}</div>
                  </span>
                </button>
                {checked && (
                  <select
                    aria-label={`项目职责 ${u.email}`}
                    value={selected.role}
                    onChange={(e) => setMemberRole(u.id, e.target.value as MemberRole)}
                    className={styles.roleSelect}
                  >
                    {SELECTABLE_PROJECT_ROLES.map((role) => (
                      <option key={role} value={role}>
                        {PROJECT_ROLE_LABELS[role]}
                      </option>
                    ))}
                  </select>
                )}
              </div>
            );
          })}
        </div>
      )}

      <div className={styles.stepActions}>
        <Button variant="ghost" onClick={() => onNext(0)} disabled={adding}>
          跳过
        </Button>
        <Button variant="primary" onClick={onContinue} disabled={adding}>
          {adding
            ? "添加中…"
            : form.members.length === 0
              ? "完成"
              : `添加 ${form.members.length} 位并完成`}
        </Button>
      </div>
    </div>
  );
}

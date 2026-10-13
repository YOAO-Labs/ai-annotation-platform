import { useState, useEffect, useRef, lazy, Suspense } from "react";
import { clsx } from "clsx";
import { Card } from "@/components/ui/Card";
import { Icon } from "@/components/ui/Icon";
import { useToastStore } from "@/components/ui/Toast";
import { usePermissions } from "@/hooks/usePermissions";
import { useAuthStore } from "@/stores/authStore";
import { useLogoutAll } from "@/hooks/useAuth";
import {
  useChangePassword,
  useUpdateProfile,
  useRequestDeactivation,
  useCancelDeactivation,
  useSetAvatarRef,
  useClearAvatar,
  useUploadAvatar,
} from "@/hooks/useMe";
import { UserAvatar } from "@/components/ui/UserAvatar";

// 选择器(含内置头像目录请求)只在用户点开时按需加载,避免给入口 chunk 增加预算压力。
const AvatarPickerDialog = lazy(() =>
  import("@/components/users/AvatarPickerDialog").then((module) => ({
    default: module.AvatarPickerDialog,
  })),
);
import { ROLE_LABELS } from "@/constants/roles";
import { bugReportsApi, type BugReportResponse } from "@/api/bug-reports";
import { NotificationPreferencesPanel } from "@/components/notifications/NotificationPreferencesPanel";
import { useWorkbenchConfig } from "@/pages/Workbench/state/useWorkbenchConfig";
import { SettingsFieldControl } from "@/pages/Workbench/components/SettingsFieldControl";
import { ApiKeysPanel } from "@/components/users/ApiKeysPanel";
import { ConnectorAllowlistSettings } from "@/components/connections/ConnectorAllowlistSettings";
import {
  getVisibleWorkbenchSettingFields,
  groupWorkbenchSettings,
  buildFieldPatch,
  getFieldValue,
  isLocalSettingField,
  type WorkbenchSettingField,
  type WorkbenchSettingValue,
} from "@/pages/Workbench/state/workbenchSettingsFields";
import type { UserRole } from "@/types";
import { PageContainer } from "@/components/layout/PageContainer";
import { getPasswordValidationErrors, isPasswordStrong } from "@/utils/password";
import { SystemSettingsSection } from "./SystemSettingsSection";
import { useUnsavedSettingsGuard } from "./useUnsavedSettingsGuard";

type SectionKey = "profile" | "workbench" | "apikeys" | "feedback" | "notifications" | "system";

// 共用类
const FORM_CLASS = "flex flex-col gap-3.5 p-4";
const LABEL_CLASS = "mb-[5px] text-xs font-medium text-muted-foreground";
const GROUP_LABEL_CLASS = "mb-2 text-xs font-medium text-muted-foreground";
const INPUT_CLASS =
  "box-border w-full appearance-none rounded-md border border-border bg-card px-3 py-2 text-sm text-foreground outline-none";
const INPUT_BUTTON_CLASS =
  "w-auto cursor-pointer rounded-md border border-border bg-card px-3.5 py-2 text-sm text-foreground disabled:cursor-not-allowed";
const ACTIONS_END_CLASS = "flex justify-end";
const SECTION_HEADER_CLASS = "flex items-center justify-between border-b border-border px-4 py-3";

export function SettingsPage() {
  const { role } = usePermissions();
  const currentUserId = useAuthStore((state) => state.user?.id);
  const isAdmin = role === "super_admin";
  const [section, setSection] = useState<SectionKey>("profile");
  const [systemDirty, setSystemDirty] = useState(false);
  // 与路由级离开守卫共用同一确认 (文案 + 防重入),分区切换也不再弹原生 confirm。
  const confirmLeaveSystemSettings = useUnsavedSettingsGuard(systemDirty);

  const sections: {
    key: SectionKey;
    label: string;
    icon: "user" | "flag" | "bell" | "settings" | "image" | "key";
  }[] = [
    { key: "profile", label: "个人资料", icon: "user" },
    { key: "workbench", label: "标注偏好", icon: "image" },
    { key: "apikeys", label: "API 密钥", icon: "key" },
    { key: "feedback", label: "我的反馈", icon: "flag" },
    { key: "notifications", label: "通知偏好", icon: "bell" },
    ...(isAdmin
      ? [{ key: "system" as SectionKey, label: "系统设置", icon: "settings" as const }]
      : []),
  ];

  return (
    <PageContainer>
      <header className="mb-4">
        <h1 className="mb-1 text-xl font-semibold">设置</h1>
        <p className="text-sm text-muted-foreground">管理你的账号信息与平台配置</p>
      </header>

      <div className="grid grid-cols-[200px_1fr] gap-4 max-[760px]:grid-cols-1">
        <nav>
          <Card>
            <ul className="m-0 list-none p-1.5">
              {sections.map((s) => {
                const active = section === s.key;
                return (
                  <li key={s.key}>
                    <button
                      onClick={() => {
                        if (section === "system" && systemDirty) {
                          confirmLeaveSystemSettings(() => setSection(s.key));
                          return;
                        }
                        setSection(s.key);
                      }}
                      className={clsx(
                        "flex w-full cursor-pointer appearance-none items-center gap-2 rounded-md border-0 bg-transparent px-3 py-2.5 text-left text-sm font-medium max-[760px]:whitespace-nowrap",
                        active ? "bg-muted font-semibold text-foreground" : "text-muted-foreground",
                      )}
                    >
                      <Icon name={s.icon} size={13} />
                      {s.label}
                    </button>
                  </li>
                );
              })}
            </ul>
          </Card>
        </nav>

        {/* grid 1fr 列默认 min-width:auto，会被内部宽表格(API 密钥)撑超页宽；min-w-0 让超宽表格由内部 overflow-x 滚动 */}
        <div className="min-w-0">
          {section === "profile" && <ProfileSection />}
          {section === "workbench" && <WorkbenchPreferencesSection />}
          {section === "apikeys" && <ApiKeysSection />}
          {section === "feedback" && <MyFeedbackSection />}
          {section === "notifications" && <NotificationPreferencesSection />}
          {section === "system" && isAdmin && (
            <div className="flex flex-col gap-4">
              <SystemSettingsSection key={currentUserId} onDirtyChange={setSystemDirty} />
              <ConnectorAllowlistSettings />
            </div>
          )}
        </div>
      </div>
    </PageContainer>
  );
}

function ProfileSection() {
  const user = useAuthStore((s) => s.user);
  const setUser = useAuthStore((s) => s.setUser);
  const pushToast = useToastStore((s) => s.push);
  const updateProfile = useUpdateProfile();
  const changePwd = useChangePassword();

  const [name, setName] = useState(user?.name ?? "");
  const [oldPwd, setOldPwd] = useState("");
  const [newPwd, setNewPwd] = useState("");
  const [newPwd2, setNewPwd2] = useState("");
  if (!user) return null;

  const submitName = (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim() || name === user.name) return;
    updateProfile.mutate(
      { name: name.trim() },
      { onSuccess: () => pushToast({ msg: "资料已更新", kind: "success" }) },
    );
  };

  const submitPwd = (e: React.FormEvent) => {
    e.preventDefault();
    if (!oldPwd || !isPasswordStrong(newPwd) || !newPwd2 || newPwd !== newPwd2) return;
    changePwd.mutate(
      { old_password: oldPwd, new_password: newPwd },
      {
        onSuccess: () => {
          pushToast({ msg: "密码已修改", kind: "success" });
          // /auth/me/password 返回 204；同步清除登录态里的临时密码标记，
          // 否则刷新前会继续把用户送回强制改密入口。
          // 回调可能晚于账号切换到达，先确认 store 仍属于提交者。
          const latestUser = useAuthStore.getState().user;
          if (latestUser?.id === user.id) {
            setUser({ ...latestUser, password_admin_reset_at: null });
          }
          setOldPwd("");
          setNewPwd("");
          setNewPwd2("");
        },
      },
    );
  };

  const passwordsMatch = newPwd === newPwd2;
  const passwordValid = isPasswordStrong(newPwd);
  const showPasswordMismatch = newPwd2.length > 0 && !passwordsMatch;
  const requiresPasswordChange = Boolean(user.password_admin_reset_at);

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <SectionHeader title="基本资料" />
        <div className={FORM_CLASS}>
          <AvatarField />
        </div>
        <form onSubmit={submitName} className={FORM_CLASS}>
          <ReadOnly label="邮箱" value={user.email} mono />
          <ReadOnly label="角色" value={ROLE_LABELS[user.role as UserRole] ?? user.role} />
          {user.group_name && <ReadOnly label="数据组" value={user.group_name} />}
          <Field label="姓名">
            <input
              required
              value={name}
              onChange={(e) => setName(e.target.value)}
              maxLength={100}
              className={INPUT_CLASS}
            />
          </Field>
          {updateProfile.isError && <ErrorBanner msg={(updateProfile.error as Error).message} />}
          <div className={ACTIONS_END_CLASS}>
            <button
              type="submit"
              disabled={!name.trim() || name === user.name || updateProfile.isPending}
              className={primaryButtonClassName(updateProfile.isPending)}
            >
              {updateProfile.isPending ? "保存中..." : "保存"}
            </button>
          </div>
        </form>
      </Card>

      <Card>
        <SectionHeader title={requiresPasswordChange ? "请先修改密码" : "修改密码"} />
        <form onSubmit={submitPwd} className={FORM_CLASS}>
          {requiresPasswordChange && (
            <div
              role="alert"
              className="rounded-md border border-status-warning/40 bg-status-warning-soft px-3 py-2 text-sm text-foreground"
            >
              管理员为你生成了临时密码，请先设置个人密码。
            </div>
          )}
          <Field label="原密码">
            <input
              required
              type="password"
              value={oldPwd}
              onChange={(e) => setOldPwd(e.target.value)}
              className={INPUT_CLASS}
            />
          </Field>
          <Field label="新密码（至少 8 位，需含大小写字母和数字）">
            <input
              required
              type="password"
              value={newPwd}
              onChange={(e) => setNewPwd(e.target.value)}
              minLength={8}
              maxLength={128}
              className={INPUT_CLASS}
            />
            {newPwd && !passwordValid && (
              <div className="mt-1 text-xs text-status-danger">
                还需：{getPasswordValidationErrors(newPwd).join("、")}
              </div>
            )}
          </Field>
          <Field label="再次输入新密码">
            <input
              required
              type="password"
              value={newPwd2}
              onChange={(e) => setNewPwd2(e.target.value)}
              className={clsx(INPUT_CLASS, showPasswordMismatch && "border-status-danger")}
            />
            {showPasswordMismatch && (
              <div className="mt-1 text-xs text-status-danger">两次密码不一致</div>
            )}
          </Field>
          {changePwd.isError && <ErrorBanner msg={(changePwd.error as Error).message} />}
          <div className={ACTIONS_END_CLASS}>
            <button
              type="submit"
              disabled={
                !oldPwd || !passwordValid || !newPwd2 || !passwordsMatch || changePwd.isPending
              }
              className={primaryButtonClassName(changePwd.isPending)}
            >
              {changePwd.isPending ? "提交中..." : "修改密码"}
            </button>
          </div>
        </form>
      </Card>

      <SecuritySessionsCard />
      <DangerZoneCard />
    </div>
  );
}

function SecuritySessionsCard() {
  const logoutAllMut = useLogoutAll();
  const pushToast = useToastStore((s) => s.push);

  const logoutOtherDevices = () => {
    logoutAllMut.mutate(undefined, {
      onSuccess: () =>
        pushToast({ msg: "已退出其他设备", sub: "当前设备仍保持登录", kind: "success" }),
      onError: (error) =>
        pushToast({ msg: "退出其他设备失败", sub: (error as Error).message, kind: "warning" }),
    });
  };

  return (
    <Card>
      <SectionHeader title="登录会话" />
      <div className="flex flex-col gap-3 p-4">
        <div className="text-sm text-muted-foreground">
          退出其他设备上的登录会话，当前设备继续使用。个人 API 密钥是独立入口，不会被此操作撤销。
        </div>
        <div className={ACTIONS_END_CLASS}>
          <button
            type="button"
            className={INPUT_BUTTON_CLASS}
            disabled={logoutAllMut.isPending}
            onClick={logoutOtherDevices}
          >
            {logoutAllMut.isPending ? "退出中..." : "退出其他设备"}
          </button>
        </div>
      </div>
    </Card>
  );
}

function DangerZoneCard() {
  const user = useAuthStore((s) => s.user);
  const pushToast = useToastStore((s) => s.push);
  const requestMut = useRequestDeactivation();
  const cancelMut = useCancelDeactivation();
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);

  if (!user) return null;
  const scheduledAt = user.deactivation_scheduled_at ?? null;
  const requestedAt = user.deactivation_requested_at ?? null;
  const isPending = !!scheduledAt;

  const submit = () => {
    requestMut.mutate(reason.trim(), {
      onSuccess: () => {
        pushToast({ msg: "注销申请已提交，7 天后自动生效", kind: "success" });
        setConfirmOpen(false);
        setReason("");
        setAcknowledged(false);
      },
      onError: (e) => pushToast({ msg: "提交失败", sub: (e as Error).message, kind: "warning" }),
    });
  };
  const cancel = () => {
    cancelMut.mutate(undefined, {
      onSuccess: () => pushToast({ msg: "已撤销注销申请", kind: "success" }),
      onError: (e) => pushToast({ msg: "撤销失败", sub: (e as Error).message, kind: "warning" }),
    });
  };

  return (
    <div className="[&>*]:border [&>*]:border-rose-500">
      <Card>
        <div className="flex items-center justify-between border-b border-rose-500 px-4 py-3">
          <h3 className="m-0 text-sm font-semibold text-status-danger">危险区</h3>
          <Icon name="warning" size={14} className="text-status-danger" />
        </div>
        <div className="flex flex-col gap-3 p-4">
          {isPending ? (
            <>
              <div className="text-sm text-foreground">
                <div className="mb-1 font-medium">注销申请已提交</div>
                <div className="text-muted-foreground">
                  提交时间：{requestedAt ? new Date(requestedAt).toLocaleString("zh-CN") : "—"}
                </div>
                <div className="text-muted-foreground">
                  生效时间：{new Date(scheduledAt!).toLocaleString("zh-CN")}（届时账号自动停用）
                </div>
              </div>
              <div>
                <button
                  type="button"
                  onClick={cancel}
                  disabled={cancelMut.isPending}
                  className={primaryButtonClassName(cancelMut.isPending)}
                >
                  {cancelMut.isPending ? "撤销中..." : "撤销注销申请"}
                </button>
              </div>
            </>
          ) : confirmOpen ? (
            <>
              <div className="text-sm text-foreground">
                注销账号后，您将无法再登录此系统；标注历史与审计记录会保留以满足合规要求。
                <strong>提交后将进入 7 天冷静期，期间可随时撤销。</strong>
              </div>
              <Field label="注销原因（可选）">
                <textarea
                  rows={3}
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  maxLength={500}
                  placeholder="如：不再使用 / 切换账号 / 隐私顾虑..."
                  className={clsx(INPUT_CLASS, "resize-y [font:inherit]")}
                />
              </Field>
              <label className="inline-flex cursor-pointer items-center gap-2 text-sm text-muted-foreground">
                <input
                  type="checkbox"
                  checked={acknowledged}
                  onChange={(e) => setAcknowledged(e.target.checked)}
                />
                我已知晓 7 天冷静期 + 历史数据保留
              </label>
              {requestMut.isError && <ErrorBanner msg={(requestMut.error as Error).message} />}
              <div className="flex justify-end gap-2">
                <button
                  type="button"
                  onClick={() => {
                    setConfirmOpen(false);
                    setAcknowledged(false);
                    setReason("");
                  }}
                  className="w-auto cursor-pointer rounded-md border border-border bg-card px-3.5 py-2 text-sm text-foreground"
                >
                  取消
                </button>
                <button
                  type="button"
                  disabled={!acknowledged || requestMut.isPending}
                  onClick={submit}
                  className={clsx(
                    "cursor-pointer rounded-md border-0 px-4 py-2 text-sm font-medium",
                    acknowledged
                      ? "bg-rose-500 text-white"
                      : "cursor-not-allowed bg-muted text-muted-foreground",
                    "disabled:cursor-not-allowed disabled:bg-muted disabled:text-muted-foreground",
                  )}
                >
                  {requestMut.isPending ? "提交中..." : "确认申请注销"}
                </button>
              </div>
            </>
          ) : (
            <>
              <div className="text-sm text-muted-foreground">
                如不再需要本账号，可申请自助注销。提交后将进入 7 天冷静期，期间可撤销。
              </div>
              <div>
                <button
                  type="button"
                  onClick={() => setConfirmOpen(true)}
                  className="cursor-pointer rounded-md border border-rose-500 bg-transparent px-3.5 py-2 text-sm font-medium text-status-danger"
                >
                  申请注销账号
                </button>
              </div>
            </>
          )}
        </div>
      </Card>
    </div>
  );
}

function WorkbenchPreferencesSection() {
  const { config, loaded, saving, update } = useWorkbenchConfig();
  const pushToast = useToastStore((s) => s.push);

  if (!loaded) {
    return (
      <Card>
        <SectionHeader title="标注偏好" />
        <div className="p-5 text-sm text-muted-foreground">加载中…</div>
      </Card>
    );
  }

  const commit = (field: WorkbenchSettingField, value: WorkbenchSettingValue) => {
    update(buildFieldPatch(field, value)).catch(() =>
      pushToast({ msg: "保存失败", kind: "warning" }),
    );
  };

  const groups = groupWorkbenchSettings(
    getVisibleWorkbenchSettingFields().filter((field) => !isLocalSettingField(field)),
  );

  return (
    <Card>
      <SectionHeader title="标注偏好" />
      <div className={FORM_CLASS}>
        {groups.map(({ key, label, sections }) => (
          <div key={key} className="mt-2.5">
            <div className="mb-3 text-sm font-semibold">{label}</div>
            {sections.map(({ key: section, label: sectionLabel, fields }) => (
              <section key={section} aria-label={sectionLabel} className="mb-3">
                <div className={GROUP_LABEL_CLASS}>{sectionLabel}</div>
                <div className="flex flex-col gap-0.5">
                  {fields
                    .filter((field) => !field.parentKey)
                    .map((field) => {
                      const fieldValue = getFieldValue(config, field);
                      const childFields = fields.filter((child) => child.parentKey === field.key);
                      return (
                        <div key={field.key} className="flex flex-col gap-px">
                          <SettingsFieldControl
                            field={field}
                            value={fieldValue}
                            disabled={saving}
                            onCommit={(value) => commit(field, value)}
                          />
                          {childFields.map((child) => (
                            <SettingsFieldControl
                              key={child.key}
                              field={child}
                              value={getFieldValue(config, child)}
                              nested
                              disabled={saving || !fieldValue}
                              onCommit={(value) => commit(child, value)}
                            />
                          ))}
                        </div>
                      );
                    })}
                </div>
              </section>
            ))}
          </div>
        ))}
        {saving && <div className="text-xs text-muted-foreground">保存中…</div>}
      </div>
    </Card>
  );
}

function ApiKeysSection() {
  return (
    <Card>
      <SectionHeader title="API 密钥" />
      <ApiKeysPanel active />
    </Card>
  );
}

function MyFeedbackSection() {
  const [reports, setReports] = useState<BugReportResponse[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    (async () => {
      try {
        const data = await bugReportsApi.listMine(20);
        setReports(data.items);
      } catch {
        // silent
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  const statusLabel: Record<string, string> = {
    new: "新提交",
    triaged: "已确认",
    in_progress: "处理中",
    fixed: "已修复",
    wont_fix: "不修复",
    duplicate: "重复",
  };
  if (loading) {
    return (
      <Card>
        <div className="p-5 text-sm text-muted-foreground">加载中...</div>
      </Card>
    );
  }

  if (reports.length === 0) {
    return (
      <Card>
        <div className="p-5 text-center text-sm text-muted-foreground">
          暂无反馈记录。遇到问题？点击右下角的反馈按钮提交。
        </div>
      </Card>
    );
  }

  const thClass = "px-3 py-2.5 text-left font-medium text-muted-foreground whitespace-nowrap";

  return (
    <Card>
      <div className="max-w-full overflow-x-auto">
        <table className="w-full min-w-[680px] border-collapse text-sm">
          <thead>
            <tr className="border-b border-border">
              <th className={thClass}>ID</th>
              <th className={thClass}>标题</th>
              <th className={thClass}>严重度</th>
              <th className={thClass}>状态</th>
              <th className={thClass}>时间</th>
            </tr>
          </thead>
          <tbody>
            {reports.map((r) => (
              <tr key={r.id} className="border-b border-border">
                <td className="mono px-3 py-2 text-xs">{r.display_id}</td>
                <td
                  className="max-w-[320px] overflow-hidden text-ellipsis whitespace-nowrap px-3 py-2 text-foreground"
                  title={r.title}
                >
                  {r.title}
                </td>
                <td className="whitespace-nowrap px-3 py-2">
                  <span className={clsx("font-medium", severityClassName(r.severity))}>
                    {r.severity}
                  </span>
                </td>
                <td className="whitespace-nowrap px-3 py-2">
                  <span className="rounded-[3px] bg-muted px-1.5 py-px text-xs">
                    {statusLabel[r.status] ?? r.status}
                  </span>
                </td>
                <td className="whitespace-nowrap px-3 py-2 text-xs text-muted-foreground">
                  {new Date(r.created_at).toLocaleDateString("zh-CN")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function SectionHeader({ title, right }: { title: string; right?: React.ReactNode }) {
  return (
    <div className={SECTION_HEADER_CLASS}>
      <h3 className="m-0 text-sm font-semibold">{title}</h3>
      {right}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <div className={LABEL_CLASS}>{label}</div>
      {children}
    </label>
  );
}

function ReadOnly({
  label,
  value,
  mono,
  hint,
}: {
  label: string;
  value: string;
  mono?: boolean;
  hint?: React.ReactNode;
}) {
  return (
    <div>
      <div className={LABEL_CLASS}>{label}</div>
      <div className="flex items-center gap-2">
        <div
          className={clsx(
            "flex-1 rounded-md border border-border bg-muted px-3 py-2 text-sm text-foreground",
            mono && "mono",
          )}
        >
          {value}
        </div>
        {hint}
      </div>
    </div>
  );
}

/** 头像可通过的 MIME 与上限，与后端 services/avatar_image.py 保持一致。 */
const AVATAR_ACCEPT = "image/png,image/jpeg,image/webp";
const AVATAR_MAX_BYTES = 2 * 1024 * 1024;
/**
 * 客户端声明的类型白名单。空串 / `application/octet-stream` 表示浏览器无从判断，
 * 后端会按字节真实格式校验，因此这里放行而不是把合法图片挡在门前。
 */
const AVATAR_DECLARED_TYPES = new Set([
  "image/png",
  "image/jpeg",
  "image/webp",
  "",
  "application/octet-stream",
]);

/**
 * 头像：预览 + 上传 / 选择内置像素头像 / 恢复默认。
 *
 * 客户端只做「格式与大小」的早退提示，真正的校验与方形裁剪在服务端完成；三个动作共用
 * hooks 里的成功处理（写回 auth store），因此顶栏与预览会一起更新。
 */
function AvatarField() {
  const user = useAuthStore((s) => s.user);
  const pushToast = useToastStore((s) => s.push);
  const setAvatarRef = useSetAvatarRef();
  const clearAvatar = useClearAvatar();
  const uploadAvatar = useUploadAvatar();
  const inputRef = useRef<HTMLInputElement>(null);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [progress, setProgress] = useState<number | null>(null);

  if (!user) return null;

  const pending = uploadAvatar.isPending || setAvatarRef.isPending || clearAvatar.isPending;
  const failed = uploadAvatar.isError || setAvatarRef.isError || clearAvatar.isError;
  const failureMessage = (
    (uploadAvatar.error ?? setAvatarRef.error ?? clearAvatar.error) as Error | null
  )?.message;

  const handleFile = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    // 清空 value，否则连续选择同一个文件不会触发 change。
    event.target.value = "";
    if (!file) return;
    if (!AVATAR_DECLARED_TYPES.has(file.type)) {
      pushToast({ msg: "仅支持 PNG / JPEG / WebP 图片", kind: "warning" });
      return;
    }
    if (file.size > AVATAR_MAX_BYTES) {
      pushToast({ msg: "图片不能超过 2 MB", kind: "warning" });
      return;
    }
    // 三个动作共享同一处错误横幅，开始新一轮前清掉旧动作的陈旧报错。
    setAvatarRef.reset();
    clearAvatar.reset();
    setProgress(0);
    uploadAvatar.mutate(
      { file, onProgress: setProgress },
      {
        onSuccess: () => pushToast({ msg: "头像已更新", kind: "success" }),
        onSettled: () => setProgress(null),
      },
    );
  };

  const selectPreset = (ref: string) => {
    uploadAvatar.reset();
    clearAvatar.reset();
    setAvatarRef.mutate(ref, {
      onSuccess: () => {
        pushToast({ msg: "头像已更新", kind: "success" });
        setPickerOpen(false);
      },
    });
  };

  const restoreDefault = () => {
    uploadAvatar.reset();
    setAvatarRef.reset();
    clearAvatar.mutate(undefined, {
      onSuccess: () => {
        pushToast({ msg: "已恢复默认头像", kind: "success" });
        setPickerOpen(false);
      },
    });
  };

  return (
    <>
      <div className="flex items-center gap-4">
        <UserAvatar user={user} size="lg" className="size-14 text-lg" />
        <div className="flex min-w-0 flex-col gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              className={INPUT_BUTTON_CLASS}
              disabled={pending}
              onClick={() => inputRef.current?.click()}
            >
              {progress === null ? "上传图片" : `上传中 ${Math.round(progress)}%`}
            </button>
            <button
              type="button"
              className={INPUT_BUTTON_CLASS}
              disabled={pending}
              onClick={() => setPickerOpen(true)}
            >
              选择内置头像
            </button>
            <button
              type="button"
              className={INPUT_BUTTON_CLASS}
              disabled={pending || !user.avatar_ref}
              onClick={restoreDefault}
            >
              恢复默认
            </button>
          </div>
          <p className="m-0 text-xs text-muted-foreground">
            PNG / JPEG / WebP，不超过 2 MB；上传后自动居中裁成方形。未设置头像时显示姓名首字母。
          </p>
        </div>
        <input
          ref={inputRef}
          type="file"
          accept={AVATAR_ACCEPT}
          className="hidden"
          onChange={handleFile}
        />
      </div>
      {failed && failureMessage && <ErrorBanner msg={failureMessage} />}
      {pickerOpen && (
        <Suspense fallback={null}>
          <AvatarPickerDialog
            open
            onOpenChange={setPickerOpen}
            currentRef={user.avatar_ref}
            pending={pending}
            onSelect={selectPreset}
            onClear={restoreDefault}
          />
        </Suspense>
      )}
    </>
  );
}

function ErrorBanner({ msg }: { msg: string }) {
  return (
    <div className="flex items-center gap-2 rounded-md border border-rose-500 bg-status-danger-soft px-3 py-2 text-sm text-status-danger">
      <Icon name="warning" size={13} />
      {msg}
    </div>
  );
}

const primaryButtonClassName = (pending: boolean) =>
  clsx(
    "cursor-pointer rounded-md border-0 bg-brand px-4 py-2 text-sm font-medium text-white",
    "disabled:cursor-not-allowed disabled:bg-brand/60",
    pending && "cursor-not-allowed bg-brand/60",
  );

function severityClassName(severity: string) {
  switch (severity) {
    case "low":
      return "text-status-info-alt";
    case "medium":
      return "text-status-caution";
    case "high":
      return "text-orange-600 dark:text-orange-400";
    case "critical":
      return "text-status-danger";
    default:
      return "text-muted-foreground";
  }
}

function NotificationPreferencesSection() {
  return (
    <Card>
      <SectionHeader title="通知偏好" />
      <div className="px-4 pb-4 pt-3">
        <NotificationPreferencesPanel />
      </div>
    </Card>
  );
}

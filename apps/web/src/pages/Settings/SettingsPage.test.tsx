/**
 * SettingsPage 单测 — 加载态 / 个人资料 / 系统设置 / 标注偏好 / 通知偏好 主路径.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { DEFAULT_WORKBENCH_PREFERENCES } from "@/api/auth";

const mockPushToast = vi.fn();

// --- auth store ---
const mockSettingsUser = vi.hoisted(() => ({
  id: "u1",
  name: "Alice",
  email: "alice@example.com",
  role: "super_admin",
  group_name: null,
  avatar_ref: null as string | null,
  password_admin_reset_at: null as string | null,
  deactivation_scheduled_at: null,
  deactivation_requested_at: null,
}));
vi.mock("@/stores/authStore", () => {
  const store = {
    token: "tok",
    user: mockSettingsUser,
    setAuth: vi.fn(),
    setUser: vi.fn(),
  };
  const useAuthStore = Object.assign((sel: (s: any) => any) => sel(store), {
    getState: () => store,
  });
  return { useAuthStore };
});

// --- permissions ---
vi.mock("@/hooks/usePermissions", () => ({
  usePermissions: () => ({
    role: "super_admin",
    hasPermission: () => true,
    hasAnyPermission: () => true,
    canAccessPage: () => true,
    allowedPages: [],
  }),
}));

// --- useMe mutations ---
const mockUpdateProfile = { mutate: vi.fn(), isPending: false, isError: false };
const mockChangePassword = { mutate: vi.fn(), isPending: false, isError: false };
const mockRequestDeactivation = { mutate: vi.fn(), isPending: false, isError: false };
const mockCancelDeactivation = { mutate: vi.fn(), isPending: false, isError: false };
const mockSetAvatarRef = { mutate: vi.fn(), reset: vi.fn(), isPending: false, isError: false };
const mockClearAvatar = { mutate: vi.fn(), reset: vi.fn(), isPending: false, isError: false };
const mockUploadAvatar = { mutate: vi.fn(), reset: vi.fn(), isPending: false, isError: false };
vi.mock("@/hooks/useMe", () => ({
  useUpdateProfile: () => mockUpdateProfile,
  useChangePassword: () => mockChangePassword,
  useRequestDeactivation: () => mockRequestDeactivation,
  useCancelDeactivation: () => mockCancelDeactivation,
  useSetAvatarRef: () => mockSetAvatarRef,
  useClearAvatar: () => mockClearAvatar,
  useUploadAvatar: () => mockUploadAvatar,
}));

// --- session control ---
const mockLogoutAll = { mutate: vi.fn(), isPending: false };
vi.mock("@/hooks/useAuth", () => ({
  useLogoutAll: () => mockLogoutAll,
}));

// --- system settings ---
const mockSystemSettingsData = {
  environment: "development",
  allow_open_registration: false,
  invitation_ttl_days: 7,
  frontend_base_url: "http://localhost:3000",
  smtp: {
    host: "smtp.example.com",
    port: 587,
    user: "user@example.com",
    from_address: "noreply@example.com",
    configured: true,
    password_set: true,
  },
};
const mockUseSystemSettings = vi.fn();
const mockUpdateSystemSettings = { mutate: vi.fn(), isPending: false, isError: false };
const mockResetSystemSettings = { mutate: vi.fn(), isPending: false, isError: false };
const mockTestSmtp = { mutate: vi.fn(), isPending: false };
vi.mock("@/hooks/useSystemSettings", () => ({
  useSystemSettings: () => mockUseSystemSettings(),
  useUpdateSystemSettings: () => mockUpdateSystemSettings,
  useResetSystemSettings: () => mockResetSystemSettings,
  useTestSmtp: () => mockTestSmtp,
}));

// --- workbench config ---
// v0.15.3 · 偏好四分树形态(common/image/video/pointcloud 子树)。
const mockWorkbenchConfig = structuredClone(DEFAULT_WORKBENCH_PREFERENCES);
const mockWorkbenchUpdate = vi.fn();
vi.mock("@/pages/Workbench/state/useWorkbenchConfig", () => ({
  useWorkbenchConfig: () => ({
    config: mockWorkbenchConfig,
    loaded: true,
    saving: false,
    update: mockWorkbenchUpdate,
  }),
}));

// --- bug reports API ---
vi.mock("@/api/bug-reports", () => ({
  bugReportsApi: {
    listMine: vi.fn().mockResolvedValue({ items: [] }),
  },
}));

// --- notifications API ---
vi.mock("@/api/notifications", () => ({
  notificationsApi: {
    getPreferences: vi.fn().mockResolvedValue({
      items: [
        { type: "batch.rejected", in_app: true, email: false, toast: true },
        { type: "bug_report.status_changed", in_app: false, email: false, toast: false },
        { type: "feedback.reply_created", in_app: true, email: false, toast: true },
        { type: "feedback.status_changed", in_app: true, email: false, toast: false },
        { type: "feedback.comment_mentioned", in_app: true, email: false, toast: true },
        { type: "annotation.comment_mentioned", in_app: true, email: false, toast: true },
        { type: "job.completed", in_app: true, email: false, toast: false },
      ],
    }),
    updatePreference: vi.fn().mockResolvedValue(undefined),
  },
}));

// --- 通知偏好共享面板（真实组件在 NotificationPreferencesPanel.test 覆盖）---
const preferencesMock = vi.hoisted(() => ({
  data: null as { items: { type: string; in_app: boolean; email: false; toast: boolean }[] } | null,
  isPending: false,
  isError: false,
  isSuccess: true,
  refetch: vi.fn(),
}));
vi.mock("@/hooks/useNotificationPreferences", () => ({
  useNotificationPreferences: () => preferencesMock,
  useUpdateNotificationPreference: () => ({ mutateAsync: vi.fn().mockResolvedValue({ ok: true }) }),
}));

// --- toast ---
vi.mock("@/components/ui/Toast", async () => {
  const actual = await vi.importActual<any>("@/components/ui/Toast");
  return {
    ...actual,
    useToastStore: <T,>(sel: (s: any) => T) => sel({ push: mockPushToast }),
  };
});

vi.mock("@/components/connections/ConnectorAllowlistSettings", () => ({
  ConnectorAllowlistSettings: () => <div>连接器主机白名单</div>,
}));

import { SettingsPage } from "./SettingsPage";

function renderUI() {
  // 头像选择器读取内置头像目录（react-query），因此页面必须有 QueryClientProvider；
  // 线上由 App 根提供，这里补上同一层。
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <SettingsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("SettingsPage", () => {
  beforeEach(() => {
    mockSettingsUser.password_admin_reset_at = null;
    mockSettingsUser.avatar_ref = null;
    mockPushToast.mockReset();
    mockUpdateProfile.mutate.mockReset();
    mockLogoutAll.mutate.mockReset();
    mockChangePassword.mutate.mockReset();
    mockSetAvatarRef.mutate.mockReset();
    mockSetAvatarRef.reset.mockReset();
    mockClearAvatar.mutate.mockReset();
    mockClearAvatar.reset.mockReset();
    mockUploadAvatar.mutate.mockReset();
    mockUploadAvatar.reset.mockReset();
    mockUpdateSystemSettings.mutate.mockReset();
    mockResetSystemSettings.mutate.mockReset();
    mockTestSmtp.mutate.mockReset();
    mockWorkbenchUpdate.mockReset().mockResolvedValue(undefined);
    mockUseSystemSettings.mockReturnValue({
      data: mockSystemSettingsData,
      isLoading: false,
      error: null,
    });
  });

  it("默认渲染个人资料 tab，显示邮箱与姓名", () => {
    renderUI();
    expect(screen.getByText("设置")).toBeInTheDocument();
    expect(screen.getByText("个人资料")).toBeInTheDocument();
    expect(screen.getByText("alice@example.com")).toBeInTheDocument();
  });

  it("保存按钮在姓名未变化时 disabled", () => {
    renderUI();
    const btn = screen.getByRole("button", { name: "保存" });
    expect(btn).toBeDisabled();
  });

  it("修改姓名后保存按钮 enabled，点击触发 updateProfile.mutate", () => {
    renderUI();
    const input = screen.getByDisplayValue("Alice");
    fireEvent.change(input, { target: { value: "Alice New" } });
    const btn = screen.getByRole("button", { name: "保存" });
    expect(btn).not.toBeDisabled();
    fireEvent.click(btn);
    expect(mockUpdateProfile.mutate).toHaveBeenCalledTimes(1);
    expect(mockUpdateProfile.mutate.mock.calls[0][0]).toEqual({ name: "Alice New" });
  });

  it("头像：浏览器无法判断 MIME（空字符串）时仍允许上传，交给服务端校验", () => {
    renderUI();
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(input, {
      target: { files: [new File(["x"], "a.png", { type: "" })] },
    });
    expect(mockUploadAvatar.mutate).toHaveBeenCalledTimes(1);
  });

  it("头像：明确不支持的 MIME 直接拒绝，不发起上传", () => {
    renderUI();
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(input, {
      target: { files: [new File(["x"], "a.gif", { type: "image/gif" })] },
    });
    expect(mockUploadAvatar.mutate).not.toHaveBeenCalled();
    expect(mockPushToast).toHaveBeenCalledWith(expect.objectContaining({ kind: "warning" }));
  });

  it("头像：发起另一个动作前清除旧动作的错误状态", () => {
    mockSettingsUser.avatar_ref = "preset:pixel-01";
    renderUI();
    fireEvent.click(screen.getByRole("button", { name: "恢复默认" }));
    expect(mockUploadAvatar.reset).toHaveBeenCalled();
    expect(mockSetAvatarRef.reset).toHaveBeenCalled();
    expect(mockClearAvatar.mutate).toHaveBeenCalled();
  });

  it("修改密码：两次密码不一致时显示错误提示", () => {
    renderUI();
    // 按 label 定位（页面里还有头像的文件选择框，不能用「空值 input」的下标选择）。
    fireEvent.change(screen.getByLabelText("原密码"), { target: { value: "oldpass" } });
    fireEvent.change(screen.getByLabelText(/^新密码/), { target: { value: "newpass1" } });
    fireEvent.change(screen.getByLabelText("再次输入新密码"), {
      target: { value: "different2" },
    });
    expect(screen.getByText("两次密码不一致")).toBeInTheDocument();
  });

  it("修改密码：与服务端规则不一致时禁用提交", () => {
    renderUI();
    fireEvent.change(screen.getByLabelText("原密码"), { target: { value: "oldpass" } });
    fireEvent.change(screen.getByLabelText(/^新密码/), { target: { value: "abc12345" } });
    fireEvent.change(screen.getByLabelText("再次输入新密码"), { target: { value: "abc12345" } });
    expect(screen.getByText(/还需：.*含大写字母/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "修改密码" })).toBeDisabled();
    expect(mockChangePassword.mutate).not.toHaveBeenCalled();
  });

  it("管理员临时密码标记 → 设置页提示先完成改密", () => {
    mockSettingsUser.password_admin_reset_at = "2026-09-09T00:00:00Z";
    renderUI();
    expect(screen.getByRole("alert")).toHaveTextContent("管理员为你生成了临时密码");
    expect(screen.getByText("请先修改密码")).toBeInTheDocument();
  });

  it("退出其他设备保留当前会话并调用既有 logout-all action", () => {
    renderUI();
    fireEvent.click(screen.getByRole("button", { name: "退出其他设备" }));
    expect(mockLogoutAll.mutate).toHaveBeenCalledWith(undefined, expect.any(Object));
  });

  it("点击「系统设置」tab → 显示系统设置表单（super_admin 才可见）", () => {
    renderUI();
    fireEvent.click(screen.getByRole("button", { name: /系统设置/ }));
    expect(screen.getByText(/开放注册/)).toBeInTheDocument();
    expect(screen.getByText(/SMTP 邮件/)).toBeInTheDocument();
    expect(screen.getByText("连接器主机白名单")).toBeInTheDocument();
  });

  it("系统设置 - 系统设置 tab 加载中状态", () => {
    mockUseSystemSettings.mockReturnValue({ data: undefined, isLoading: true, error: null });
    renderUI();
    fireEvent.click(screen.getByRole("button", { name: /系统设置/ }));
    expect(screen.getByText("加载中...")).toBeInTheDocument();
  });

  it("系统设置 - 修改 open registration 后保存触发 mutation", () => {
    renderUI();
    fireEvent.click(screen.getByRole("button", { name: /系统设置/ }));
    // toggle the checkbox to dirty the form
    const checkbox = screen.getByLabelText(/已关闭.*仅邀请注册/);
    fireEvent.click(checkbox);
    fireEvent.submit(checkbox.closest("form")!);
    expect(mockUpdateSystemSettings.mutate).toHaveBeenCalledTimes(1);
    const patch = mockUpdateSystemSettings.mutate.mock.calls[0][0];
    expect(patch).toHaveProperty("allow_open_registration", true);
  });

  it("点击「标注偏好」tab → 显示图像平滑选项", () => {
    renderUI();
    fireEvent.click(screen.getByRole("button", { name: /标注偏好/ }));
    expect(screen.getByText(/图像平滑/)).toBeInTheDocument();
    expect(screen.getAllByTestId(/^setting-field-/)).toHaveLength(45);
    for (const label of [
      "界面布局",
      "标注显示",
      "编辑与辅助",
      "画布与视角",
      "播放与轨迹",
      "性能与实验",
    ]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    expect(screen.getByRole("region", { name: "图片画布" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "视频画布" })).toBeInTheDocument();
    expect(screen.queryByTestId("setting-field-image.snapToGrid")).not.toBeInTheDocument();
    expect(screen.queryAllByTestId(/^setting-field-experiment\./)).toHaveLength(0);
  });

  it("点击「通知偏好」tab → 显示共享偏好面板（每类接收+弹出两开关）", async () => {
    preferencesMock.data = {
      items: [
        { type: "batch.rejected", in_app: true, email: false, toast: true },
        { type: "bug_report.status_changed", in_app: false, email: false, toast: false },
        { type: "feedback.reply_created", in_app: true, email: false, toast: true },
        { type: "annotation.comment_mentioned", in_app: true, email: false, toast: true },
        { type: "job.completed", in_app: true, email: false, toast: false },
      ],
    };
    renderUI();
    fireEvent.click(screen.getByRole("button", { name: /通知偏好/ }));
    await waitFor(() =>
      expect(screen.getByTestId("notification-preferences-panel")).toBeInTheDocument(),
    );
    expect(screen.getByText("批次被驳回")).toBeInTheDocument();
    expect(screen.getByText("问题收到新回复")).toBeInTheDocument();
    expect(screen.getByText("标注评论提到了你")).toBeInTheDocument();
    expect(screen.getByText("后台任务完成")).toBeInTheDocument();
    // 两开关 + 接收关闭时弹出禁用
    expect(screen.getByRole("checkbox", { name: "批次被驳回 接收通知" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "批次被驳回 弹出提示" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "BUG 反馈：状态变更 接收通知" })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: "BUG 反馈：状态变更 弹出提示" })).toBeDisabled();
  });

  it("点击「我的反馈」tab → 显示空态提示", async () => {
    renderUI();
    fireEvent.click(screen.getByRole("button", { name: /我的反馈/ }));
    await waitFor(() => expect(screen.getByText(/暂无反馈记录/)).toBeInTheDocument());
  });

  it("危险区：点击「申请注销账号」展开确认面板", () => {
    renderUI();
    fireEvent.click(screen.getByRole("button", { name: /申请注销账号/ }));
    expect(screen.getAllByText(/7 天冷静期/).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: /确认申请注销/ })).toBeDisabled();
  });
});

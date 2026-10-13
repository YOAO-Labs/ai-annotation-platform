/**
 * WhatsNewDialog 单测:
 * - 登录瞬间自动弹出(刷新恢复会话不弹) / 「知道了」写入已读版本 / 关闭=下次登录再提醒
 * - 顶栏版本号手动打开:与已读状态无关,随时可看
 * - 单调写回:已确认更高版本的旧前端不回退跨设备标记
 * - 响应竞态:只并入已读版本,账号切换后不写回
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { MeResponse, UserPreferences } from "@/api/auth";
import { useAuthStore } from "@/stores/authStore";

const mockUpdatePreferences = vi.fn();
const mockLoadNotes = vi.fn();
vi.mock("@/api/auth", () => ({
  authApi: {
    updatePreferences: (...args: unknown[]) => mockUpdatePreferences(...args),
  },
}));

vi.mock("@/utils/releaseNotes", () => {
  const triple = (value: string) =>
    value
      .trim()
      .replace(/^v/i, "")
      .split("+")[0]
      .split("-")[0]
      .split(".")
      .map((part) => Number.parseInt(part, 10) || 0);
  const compareSemver = (a: string, b: string) => {
    const left = triple(a);
    const right = triple(b);
    for (let i = 0; i < 3; i += 1) {
      if (left[i] !== right[i]) return left[i] < right[i] ? -1 : 1;
    }
    return 0;
  };
  return {
    appVersion: "2.0.0",
    compareSemver,
    loadCurrentReleaseNotes: (...args: unknown[]) => mockLoadNotes(...args),
    shouldShowReleaseNotes: (seen: string | null | undefined, current: string) =>
      compareSemver(current, seen ?? "") > 0,
  };
});

import { WhatsNewDialog, useWhatsNewStore } from "./WhatsNewDialog";

const NOTES = {
  version: "2.0.0",
  date: "2026-02-03",
  groups: [{ key: "Added", label: "新增", items: ["新功能甲"] }],
};

function signIn(preferences?: MeResponse["preferences"]) {
  useAuthStore
    .getState()
    .setAuth("whats-new-token", { id: "u1", role: "employee", preferences } as MeResponse);
}

const savedPreferences = {
  ui: { theme: "system", secondary_bar_hidden: false, changelog_seen_version: "2.0.0" },
} as UserPreferences;

describe("WhatsNewDialog", () => {
  beforeEach(() => {
    localStorage.clear();
    useAuthStore.setState({ token: null, user: null });
    useWhatsNewStore.setState({ manualOpen: false });
    mockUpdatePreferences.mockReset();
    mockUpdatePreferences.mockResolvedValue(savedPreferences);
    mockLoadNotes.mockReset();
    mockLoadNotes.mockResolvedValue(NOTES);
  });

  it("登录瞬间(user 从无到有)→ 自动弹出并展示要点;「知道了」写入已读版本", async () => {
    render(<WhatsNewDialog />); // 挂载时未登录(登录页场景)
    act(() => signIn({ ui: { changelog_seen_version: "" } }));

    expect(await screen.findByText("v2.0.0")).toBeTruthy();
    expect(screen.getByText("新功能甲")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "知道了" }));
    await waitFor(() =>
      expect(mockUpdatePreferences).toHaveBeenCalledWith({
        ui: { changelog_seen_version: "2.0.0" },
      }),
    );
    await waitFor(() =>
      expect(useAuthStore.getState().user?.preferences?.ui?.changelog_seen_version).toBe("2.0.0"),
    );
  });

  it("刷新恢复会话(挂载时已登录)→ 不自动弹出;下次真正登录再弹", async () => {
    signIn({ ui: { changelog_seen_version: "" } });
    render(<WhatsNewDialog />);
    expect(screen.queryByText("v2.0.0")).toBeNull();

    // 登出 → 再登录:仍未确认当前版本,重新弹出。
    act(() => useAuthStore.getState().logout());
    act(() => signIn({ ui: { changelog_seen_version: "" } }));
    expect(await screen.findByText(/v2.0.0/)).toBeTruthy();
  });

  it("已确认过当前版本 → 登录也不弹", () => {
    render(<WhatsNewDialog />);
    act(() => signIn({ ui: { changelog_seen_version: "2.0.0" } }));
    expect(screen.queryByText("v2.0.0")).toBeNull();
    expect(mockUpdatePreferences).not.toHaveBeenCalled();
  });

  it("直接关闭(X)→ 不写入;本次登录不再自动弹,下次登录再提醒", async () => {
    render(<WhatsNewDialog />);
    act(() => signIn({ ui: { changelog_seen_version: "" } }));
    await screen.findByText(/v2.0.0/);

    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByText("v2.0.0")).toBeNull());
    expect(mockUpdatePreferences).not.toHaveBeenCalled();

    // 同一登录态内 user 对象被重取替换(如主题同步 setUser)→ 不再自动弹。
    act(() =>
      useAuthStore.getState().setUser({
        id: "u1",
        role: "employee",
        preferences: { ui: { changelog_seen_version: "" } },
      } as MeResponse),
    );
    expect(screen.queryByText("v2.0.0")).toBeNull();
  });

  it("关闭后登出再登录同一账号(不刷新)→ 清空已弹登记,重新提醒", async () => {
    render(<WhatsNewDialog />);
    act(() => signIn({ ui: { changelog_seen_version: "" } }));
    await screen.findByText(/v2.0.0/);

    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByText("v2.0.0")).toBeNull());

    // 会话结束再登录:不能因缓存的「用户+版本」键而吞掉提醒。
    act(() => useAuthStore.getState().logout());
    act(() => signIn({ ui: { changelog_seen_version: "" } }));
    expect(await screen.findByText(/v2.0.0/)).toBeTruthy();
  });

  it("确认写入失败 → 本地保持未读(下次登录再提醒),弹窗仍正常关闭", async () => {
    mockUpdatePreferences.mockRejectedValue(new Error("network"));
    render(<WhatsNewDialog />);
    act(() => signIn({ ui: { changelog_seen_version: "" } }));
    await screen.findByText(/v2.0.0/);

    fireEvent.click(screen.getByRole("button", { name: "知道了" }));
    await waitFor(() => expect(mockUpdatePreferences).toHaveBeenCalled());
    await waitFor(() =>
      expect(useAuthStore.getState().user?.preferences?.ui?.changelog_seen_version ?? "").toBe(""),
    );
  });

  it("StrictMode 双调用 effect(回归:登录路径弹窗不被吞)→ 弹窗正常打开", async () => {
    // app 全局开 React.StrictMode,dev 下 mount effect 走 setup-cleanup-setup;
    // 曾因 shownRef 提前登记 + cleanup 使加载失效导致弹窗永不打开。
    render(
      <React.StrictMode>
        <WhatsNewDialog />
      </React.StrictMode>,
    );
    act(() => signIn({ ui: { changelog_seen_version: "" } }));
    expect(await screen.findByText(/v2.0.0/)).toBeTruthy();
    expect(mockUpdatePreferences).not.toHaveBeenCalled();
  });

  it("更新要点加载失败 → 仍弹出并回落为简短提示;「知道了」仍可确认", async () => {
    mockLoadNotes.mockRejectedValue(new Error("chunk"));
    render(<WhatsNewDialog />);
    act(() => signIn({ ui: { changelog_seen_version: "" } }));

    expect(await screen.findByText(/v2.0.0/)).toBeTruthy();
    expect(screen.getByText(/本次更新内容整理中/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "知道了" }));
    await waitFor(() =>
      expect(mockUpdatePreferences).toHaveBeenCalledWith({
        ui: { changelog_seen_version: "2.0.0" },
      }),
    );
  });

  it("顶栏版本号手动打开(未确认)→ 可看要点;「知道了」写入已读", async () => {
    render(<WhatsNewDialog />);
    act(() => signIn({ ui: { changelog_seen_version: "" } }));
    await screen.findByText(/v2.0.0/);
    // 先关掉登录自动弹窗,再手动打开,隔离手动入口。
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByText(/v2.0.0/)).toBeNull());

    act(() => useWhatsNewStore.getState().openManually());
    expect(await screen.findByText(/v2.0.0/)).toBeTruthy();
    expect(screen.getByText("新功能甲")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "知道了" }));
    await waitFor(() =>
      expect(useAuthStore.getState().user?.preferences?.ui?.changelog_seen_version).toBe("2.0.0"),
    );
  });

  it("已确认过当前版本时手动查看 → 点「知道了」不做降级写回", async () => {
    signIn({ ui: { changelog_seen_version: "2.0.0" } });
    render(<WhatsNewDialog />);
    act(() => useWhatsNewStore.getState().openManually());
    expect(await screen.findByText(/v2.0.0/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "知道了" }));
    await waitFor(() => expect(screen.queryByText(/v2.0.0/)).toBeNull());
    expect(mockUpdatePreferences).not.toHaveBeenCalled();
  });

  it("账号已确认更高版本(回滚)→ 旧前端确认不回写,保留更高标记", async () => {
    signIn({ ui: { changelog_seen_version: "3.0.0" } });
    render(<WhatsNewDialog />);
    act(() => useWhatsNewStore.getState().openManually());
    await screen.findByText(/v2.0.0/);

    fireEvent.click(screen.getByRole("button", { name: "知道了" }));
    await waitFor(() => expect(screen.queryByText(/v2.0.0/)).toBeNull());
    expect(mockUpdatePreferences).not.toHaveBeenCalled();
    expect(useAuthStore.getState().user?.preferences?.ui?.changelog_seen_version).toBe("3.0.0");
  });

  it("确认响应只并入已读版本 → 不覆盖并发更新的主题/面板偏好", async () => {
    let resolveUpdate: (value: unknown) => void = () => {};
    mockUpdatePreferences.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveUpdate = resolve;
        }),
    );
    render(<WhatsNewDialog />);
    act(() =>
      signIn({ ui: { changelog_seen_version: "", theme: "light", secondary_bar_hidden: false } }),
    );
    await screen.findByText(/v2.0.0/);
    fireEvent.click(screen.getByRole("button", { name: "知道了" }));
    await waitFor(() => expect(mockUpdatePreferences).toHaveBeenCalled());

    // PATCH 在途时用户改了主题(乐观更新,useTheme 不回读响应)。
    const inFlightUser = useAuthStore.getState().user as MeResponse;
    act(() =>
      useAuthStore.getState().setUser({
        ...inFlightUser,
        preferences: {
          ui: { changelog_seen_version: "", theme: "dark", secondary_bar_hidden: true },
        },
      }),
    );
    await act(async () => {
      resolveUpdate({
        ui: { theme: "light", secondary_bar_hidden: false, changelog_seen_version: "2.0.0" },
      });
    });

    const ui = useAuthStore.getState().user?.preferences?.ui;
    expect(ui?.changelog_seen_version).toBe("2.0.0");
    expect(ui?.theme).toBe("dark");
    expect(ui?.secondary_bar_hidden).toBe(true);
  });

  it("确认请求在途时账号切换 → 不把旧账号响应写入新账号", async () => {
    let resolveUpdate: (value: unknown) => void = () => {};
    mockUpdatePreferences.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveUpdate = resolve;
        }),
    );
    render(<WhatsNewDialog />);
    act(() => signIn({ ui: { changelog_seen_version: "" } }));
    await screen.findByText(/v2.0.0/);
    fireEvent.click(screen.getByRole("button", { name: "知道了" }));
    await waitFor(() => expect(mockUpdatePreferences).toHaveBeenCalled());

    act(() =>
      useAuthStore.getState().setAuth("other-token", {
        id: "u2",
        role: "employee",
        preferences: { ui: { changelog_seen_version: "" } },
      } as MeResponse),
    );
    await act(async () => {
      resolveUpdate({ ui: { changelog_seen_version: "2.0.0" } });
    });

    expect(useAuthStore.getState().user?.id).toBe("u2");
    expect(useAuthStore.getState().user?.preferences?.ui?.changelog_seen_version).toBe("");
  });

  it("手动打开时要点加载失败 → 不打断,显示回落提示", async () => {
    signIn({ ui: { changelog_seen_version: "2.0.0" } });
    render(<WhatsNewDialog />);
    mockLoadNotes.mockRejectedValueOnce(new Error("chunk"));

    act(() => useWhatsNewStore.getState().openManually());
    expect(await screen.findByText(/v2.0.0/)).toBeTruthy();
    expect(screen.getByText(/本次更新内容整理中/)).toBeTruthy();
  });
});

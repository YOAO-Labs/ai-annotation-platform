/**
 * useInteractiveBackendPref 单测.
 *
 * 覆盖 claude[bot] P1 修复点:
 * - 切账号/登出 (userId → null/undefined): byProject 必须清, 否则下一用户读到上一用户偏好
 * - unmount 时 pending 节流写须 fire-and-forget flush, 否则节流窗内最后一次选择会丢
 *
 * 600ms 节流不用 fake timer, 直接用真实 timer + waitFor; unmount flush 是同步 path 不等。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";

import { useAuthStore } from "@/stores/authStore";
import { useInteractiveBackendPref } from "./useInteractiveBackendPref";

const getPreferencesMock = vi.fn();
const updatePreferencesMock = vi.fn();

// hook 拉取改走共享 useUserPreferences (react-query), 需 QueryClientProvider。
let queryClient: QueryClient;
const wrapper = ({ children }: { children: ReactNode }) =>
  createElement(QueryClientProvider, { client: queryClient }, children);

vi.mock("@/api/auth", () => ({
  authApi: {
    getPreferences: (...args: unknown[]) => getPreferencesMock(...args),
    updatePreferences: (...args: unknown[]) => updatePreferencesMock(...args),
  },
}));

function setUser(id: string | null) {
  if (id) useAuthStore.setState({ user: { id } as never });
  else useAuthStore.setState({ user: null });
}

describe("useInteractiveBackendPref", () => {
  beforeEach(() => {
    getPreferencesMock.mockReset();
    updatePreferencesMock.mockReset();
    setUser(null);
    queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    });
  });
  afterEach(() => setUser(null));

  it("登出 (userId → null) 清 byProject, 下一个用户首渲染不读到旧值 (P1 回归)", async () => {
    // 上一个用户 u1: 项目 p1 选过 backend b1。
    setUser("u1");
    getPreferencesMock.mockResolvedValueOnce({
      ai: { interactive_backend_by_project: { p1: "b1" } },
    });
    const { result, rerender } = renderHook(() => useInteractiveBackendPref("p1"), { wrapper });
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(result.current.savedBackendId).toBe("b1");

    // 登出: byProject 必须清空 (不是仅复位 loaded)。
    act(() => setUser(null));
    rerender();
    expect(result.current.savedBackendId).toBeUndefined();
    expect(result.current.loaded).toBe(false);

    // 新用户 u2: 项目 p1 未选过, getPreferences 返回空。
    getPreferencesMock.mockResolvedValueOnce({ ai: {} });
    act(() => setUser("u2"));
    rerender();
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(result.current.savedBackendId).toBeUndefined();
  });

  it("unmount 时 pending 节流写 fire-and-forget flush (P1 回归)", async () => {
    setUser("u1");
    getPreferencesMock.mockResolvedValueOnce({ ai: {} });
    updatePreferencesMock.mockResolvedValue(undefined);

    const { result, unmount } = renderHook(() => useInteractiveBackendPref("p1"), { wrapper });
    await waitFor(() => expect(result.current.loaded).toBe(true));

    // 立刻切换 → 进入 600ms 节流窗口, 还没真正 PATCH。
    act(() => result.current.save("b9"));
    expect(updatePreferencesMock).not.toHaveBeenCalled();

    // 卸载 → 必须 flush pending。
    unmount();
    expect(updatePreferencesMock).toHaveBeenCalledTimes(1);
    expect(updatePreferencesMock).toHaveBeenCalledWith({
      ai: { interactive_backend_by_project: { p1: "b9" } },
    });
  });

  it("无 pending 时 unmount 不发多余写", async () => {
    setUser("u1");
    getPreferencesMock.mockResolvedValueOnce({
      ai: { interactive_backend_by_project: { p1: "b1" } },
    });
    const { result, unmount } = renderHook(() => useInteractiveBackendPref("p1"), { wrapper });
    await waitFor(() => expect(result.current.loaded).toBe(true));
    unmount();
    expect(updatePreferencesMock).not.toHaveBeenCalled();
  });

  it("save(null) 删 projectId 条目", async () => {
    setUser("u1");
    getPreferencesMock.mockResolvedValueOnce({
      ai: { interactive_backend_by_project: { p1: "b1", p2: "b2" } },
    });
    updatePreferencesMock.mockResolvedValue(undefined);
    const { result, unmount } = renderHook(() => useInteractiveBackendPref("p1"), { wrapper });
    await waitFor(() => expect(result.current.loaded).toBe(true));

    act(() => result.current.save(null));
    unmount();
    expect(updatePreferencesMock).toHaveBeenCalledWith({
      ai: { interactive_backend_by_project: { p2: "b2" } },
    });
  });
});

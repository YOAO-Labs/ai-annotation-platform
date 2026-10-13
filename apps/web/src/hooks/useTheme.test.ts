/**
 * v0.15.25 · useTheme 服务端偏好持久化单测:
 * 登出态只写 localStorage;登录态 PATCH 服务端 + 乐观写 authStore;服务端主题采纳为真值源。
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";

const { updatePreferences } = vi.hoisted(() => ({
  updatePreferences: vi.fn(),
}));
vi.mock("@/api/auth", async (importActual) => {
  const actual = await importActual<typeof import("@/api/auth")>();
  return { ...actual, authApi: { ...actual.authApi, updatePreferences } };
});

import type { MeResponse, ThemePref } from "@/api/auth";
import { useAuthStore } from "@/stores/authStore";
import { useTheme } from "./useTheme";

function fakeUser(theme?: ThemePref): MeResponse {
  return {
    id: "u1",
    email: "a@b.c",
    name: "A",
    role: "employee",
    group_name: null,
    status: "active",
    created_at: "2026-01-01T00:00:00Z",
    preferences: theme ? { ui: { theme } } : {},
  };
}

beforeEach(() => {
  updatePreferences.mockReset().mockResolvedValue({});
  localStorage.clear();
  useAuthStore.setState({ token: null, user: null });
  document.documentElement.removeAttribute("data-theme");
});

describe("useTheme", () => {
  it("登出态:setTheme 写 localStorage + data-theme,不调服务端", () => {
    const { result } = renderHook(() => useTheme());
    act(() => result.current.setTheme("dark"));
    expect(localStorage.getItem("anno.theme")).toBe("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(result.current.resolved).toBe("dark");
    expect(updatePreferences).not.toHaveBeenCalled();
  });

  it("登录态:setTheme PATCH 服务端 {ui:{theme}} 并乐观写入 authStore", () => {
    useAuthStore.setState({ token: "t", user: fakeUser() });
    const { result } = renderHook(() => useTheme());
    act(() => result.current.setTheme("light"));
    expect(updatePreferences).toHaveBeenCalledWith({ ui: { theme: "light" } });
    expect(useAuthStore.getState().user?.preferences?.ui?.theme).toBe("light");
  });

  it("登录用户带服务端主题 → 初始即采纳(覆盖本地缓存)", () => {
    localStorage.setItem("anno.theme", "light");
    useAuthStore.setState({ token: "t", user: fakeUser("dark") });
    const { result } = renderHook(() => useTheme());
    expect(result.current.theme).toBe("dark");
    expect(result.current.resolved).toBe("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  it("登录后服务端主题到达 → 采纳(post-mount hydration)", () => {
    const { result } = renderHook(() => useTheme());
    expect(result.current.theme).toBe("system"); // 初始本地默认
    act(() => useAuthStore.setState({ token: "t", user: fakeUser("dark") }));
    expect(result.current.theme).toBe("dark");
  });
});

/**
 * usePermissions 单测：平台角色 × 页面访问 / 权限分支。
 * 项目职责（标注员 / 质检员）由 useProjectAccess 覆盖，不再走这里。
 */
import { describe, it, expect, beforeEach } from "vitest";
import { renderHook } from "@testing-library/react";
import { useAuthStore } from "@/stores/authStore";
import { usePermissions } from "./usePermissions";
import type { MeResponse } from "@/api/auth";

function setUser(role: string | null) {
  if (role === null) {
    useAuthStore.setState({ user: null });
    return;
  }
  useAuthStore.setState({
    user: {
      id: "u1",
      email: "u@x.com",
      name: "U",
      role,
      status: "online",
    } as unknown as MeResponse,
  });
}

describe("usePermissions", () => {
  beforeEach(() => {
    useAuthStore.setState({ token: null, user: null });
  });

  it("无登录用户 → 默认 viewer，仅有 dashboard/datasets/settings", () => {
    const { result } = renderHook(() => usePermissions());
    expect(result.current.role).toBe("viewer");
    expect(result.current.canAccessPage("dashboard")).toBe(true);
    expect(result.current.canAccessPage("users")).toBe(false);
  });

  it("super_admin → hasPermission(任意) 全 true", () => {
    setUser("super_admin");
    const { result } = renderHook(() => usePermissions());
    expect(result.current.role).toBe("super_admin");
    expect(result.current.hasPermission("user.invite")).toBe(true);
    expect(result.current.hasPermission("audit.view")).toBe(true);
    expect(result.current.hasAnyPermission("project.create")).toBe(true);
  });

  it("employee → 标注与审核入口都可达，无平台管理权限", () => {
    setUser("employee");
    const { result } = renderHook(() => usePermissions());
    expect(result.current.role).toBe("employee");
    expect(result.current.isEmployee).toBe(true);
    expect(result.current.canAccessPage("annotate")).toBe(true);
    expect(result.current.canAccessPage("review")).toBe(true);
    expect(result.current.canAccessPage("users")).toBe(false);
    expect(result.current.hasPermission("task.annotate")).toBe(true);
    expect(result.current.hasPermission("task.review")).toBe(true);
    expect(result.current.hasPermission("project.create")).toBe(false);
    expect(result.current.hasPermission("user.invite")).toBe(false);
  });

  it("project_admin → 项目相关权限有，audit 没有", () => {
    setUser("project_admin");
    const { result } = renderHook(() => usePermissions());
    expect(result.current.hasPermission("project.create")).toBe(true);
    expect(result.current.hasPermission("audit.view")).toBe(false);
    expect(result.current.isManager).toBe(true);
  });

  it("viewer → 只读页面，没有标注 / 审核入口", () => {
    setUser("viewer");
    const { result } = renderHook(() => usePermissions());
    expect(result.current.canAccessPage("dashboard")).toBe(true);
    expect(result.current.canAccessPage("review")).toBe(false);
    expect(result.current.canAccessPage("annotate")).toBe(false);
    expect(result.current.hasPermission("task.approve")).toBe(false);
  });

  it("hasAnyPermission 多个权限或值", () => {
    setUser("employee");
    const { result } = renderHook(() => usePermissions());
    expect(result.current.hasAnyPermission("user.invite", "task.annotate")).toBe(true);
    expect(result.current.hasAnyPermission("user.invite", "audit.view")).toBe(false);
  });

  it("allowedPages 暴露用户可达页面列表", () => {
    setUser("employee");
    const { result } = renderHook(() => usePermissions());
    expect(result.current.allowedPages).toContain("dashboard");
    expect(result.current.allowedPages).not.toContain("users");
  });

  it("历史遗留全局角色不再授予任何平台权限（fail closed）", () => {
    setUser("reviewer");
    const { result } = renderHook(() => usePermissions());
    expect(result.current.allowedPages).toEqual([]);
    expect(result.current.hasPermission("task.approve")).toBe(false);
  });
});

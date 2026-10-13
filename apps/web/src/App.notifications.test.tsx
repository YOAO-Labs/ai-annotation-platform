/**
 * v0.9 · 通知连接的应用生命周期测试。
 *
 * useNotificationSocket 挂在 App（路由切换不拆连接）：主界面与全屏
 * 标注/审核工作台（直接进入或路由切换）共享同一次挂载；AppShell 不再
 * 单独挂载。这里用探针 mock 断言挂载/卸载次数，真实连接行为由
 * useNotificationSocket.test.tsx 覆盖。
 */
import { useEffect } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const socketLifecycle = vi.hoisted(() => ({ mounts: 0, unmounts: 0 }));

vi.mock("@/hooks/useNotificationSocket", async () => {
  const { useEffect: useProbeEffect } = await import("react");
  return {
    useNotificationSocket: () => {
      useProbeEffect(() => {
        socketLifecycle.mounts += 1;
        return () => {
          socketLifecycle.unmounts += 1;
        };
      }, []);
    },
  };
});
vi.mock("@/components/shell/TopBar", () => ({
  TopBar: () => <header data-testid="shell-topbar" />,
}));
vi.mock("@/components/shell/Sidebar", () => ({
  Sidebar: () => <aside data-testid="shell-sidebar" />,
}));
vi.mock("@/components/shell/SidebarDrawer", () => ({ SidebarDrawer: () => null }));
vi.mock("@/components/PerfHud", () => ({
  PerfHud: () => null,
  usePerfHudStore: Object.assign(
    (selector: (state: { visible: boolean }) => unknown) => selector({ visible: false }),
    { getState: () => ({ toggle: vi.fn() }) },
  ),
}));
vi.mock("@/hooks/useHeartbeat", () => ({ useHeartbeat: () => {} }));
vi.mock("@/components/bugreport/BugReportFAB", () => ({ BugReportFAB: () => null }));
vi.mock("@/pages/Workbench/WorkbenchPage", () => ({
  WorkbenchPage: () => <div data-testid="workbench-page" />,
}));
vi.mock("@/hooks/useProjects", () => ({
  useProject: () => ({
    data: { id: "p1", name: "项目", display_id: "P-1", type_key: "image" },
    isLoading: false,
  }),
}));
vi.mock("@/hooks/useProjectAccess", () => ({
  useProjectAccess: () => ({
    access: {
      project_id: "p1",
      user_id: "user-1",
      platform_role: "employee",
      project_role: "annotator",
      capabilities: ["project.read", "task.read", "annotation.write", "review.write"],
    },
    capabilities: new Set(["project.read", "task.read", "annotation.write", "review.write"]),
    hasCapability: () => true,
    projectRole: "annotator",
    membershipVersion: 1,
    isManager: false,
    isPending: false,
    isLoading: false,
    isError: false,
    error: null,
    refetch: vi.fn(),
  }),
}));

import type { MeResponse } from "@/api/auth";
import { useAuthStore } from "@/stores/authStore";
import { App } from "@/App";

const user: MeResponse = {
  id: "user-1",
  email: "annotator@example.com",
  name: "Annotator",
  role: "employee",
  group_name: null,
  status: "active",
  created_at: "2026-05-10T00:00:00Z",
};

function RouteJumper({ targets }: { targets: { label: string; to: string }[] }) {
  const navigate = useNavigate();
  useEffect(() => {
    const holder = document.createElement("div");
    document.body.append(holder);
    return () => holder.remove();
  }, []);
  return (
    <>
      {targets.map((target) => (
        <button
          key={target.label}
          type="button"
          data-testid={`jump-${target.label}`}
          onClick={() => navigate(target.to)}
        >
          {target.label}
        </button>
      ))}
    </>
  );
}

function mountApp(initialEntry: string, targets: { label: string; to: string }[] = []) {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <RouteJumper targets={targets} />
      <App />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  socketLifecycle.mounts = 0;
  socketLifecycle.unmounts = 0;
  useAuthStore.getState().setAuth("fixture-token", user);
});

describe("notification socket application lifetime", () => {
  it("直接进入标注工作台也挂载通知连接", async () => {
    mountApp("/projects/p1/annotate");
    expect(await screen.findByTestId("workbench-page")).toBeInTheDocument();
    expect(socketLifecycle.mounts).toBe(1);
    expect(socketLifecycle.unmounts).toBe(0);
  });

  it("直接进入审核工作台同样挂载", async () => {
    mountApp("/projects/p1/review");
    expect(await screen.findByTestId("workbench-page")).toBeInTheDocument();
    expect(socketLifecycle.mounts).toBe(1);
    expect(socketLifecycle.unmounts).toBe(0);
  });

  it("主界面 shell 挂载一次，不因 AppShell 重复挂载", () => {
    mountApp("/unauthorized");
    expect(screen.getByTestId("shell-topbar")).toBeInTheDocument();
    expect(socketLifecycle.mounts).toBe(1);
  });

  it("工作台 ↔ 主界面切换不拆连接", async () => {
    mountApp("/projects/p1/annotate", [
      { label: "shell", to: "/unauthorized" },
      { label: "review", to: "/projects/p1/review" },
    ]);
    expect(socketLifecycle.mounts).toBe(1);

    fireEvent.click(screen.getByTestId("jump-shell"));
    expect(screen.getByTestId("shell-topbar")).toBeInTheDocument();
    expect(socketLifecycle.mounts).toBe(1);
    expect(socketLifecycle.unmounts).toBe(0);

    fireEvent.click(screen.getByTestId("jump-review"));
    expect(await screen.findByTestId("workbench-page")).toBeInTheDocument();
    expect(socketLifecycle.mounts).toBe(1);
    expect(socketLifecycle.unmounts).toBe(0);
  });

  it("登出后重新渲染（token 变化）不产生第二个并存挂载", () => {
    const view = mountApp("/unauthorized");
    expect(socketLifecycle.mounts).toBe(1);
    useAuthStore.getState().logout();
    expect(socketLifecycle.mounts).toBe(1);
    expect(socketLifecycle.unmounts).toBe(0);
    view.unmount();
    expect(socketLifecycle.unmounts).toBe(1);
  });
});

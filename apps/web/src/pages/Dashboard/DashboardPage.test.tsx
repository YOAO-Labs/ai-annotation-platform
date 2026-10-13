/**
 * DashboardPage 单测 — 加载态 / 空态 / 正常渲染 / 视图切换 / 活动日志过滤.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { useEffect } from "react";
import type { ReactNode } from "react";

type MockAuthState = {
  user: { id: string; role: string; email: string } | null;
};

type OpenModalProps = {
  open?: boolean;
};

const mockUseProjectPage = vi.fn();
const mockUseProjectStats = vi.fn();
const mockUseAuditLogs = vi.fn();
const mockPushToast = vi.fn();
const mockBuildWorkbenchUrl = vi.hoisted(() => vi.fn((id: string) => `/workbench/${id}`));
const mockAuthStore = vi.fn(<T,>(sel: (s: MockAuthState) => T) => sel({ user: null }));

vi.mock("@/hooks/useProjects", () => ({
  useProjectPage: (...args: unknown[]) => mockUseProjectPage(...args),
  useProjectStats: () => mockUseProjectStats(),
}));

vi.mock("@/hooks/useAudit", () => ({
  useAuditLogs: (...args: unknown[]) => mockUseAuditLogs(...args),
}));

vi.mock("@/stores/authStore", () => ({
  useAuthStore: <T,>(sel: (s: MockAuthState) => T) => mockAuthStore(sel),
}));

vi.mock("@/components/projects/CreateProjectWizard", () => ({
  CreateProjectWizard: ({ open }: OpenModalProps) =>
    open ? <div data-testid="cp-wizard" /> : null,
}));

vi.mock("@/components/datasets/ImportDatasetWizard", () => ({
  ImportDatasetWizard: ({ open }: OpenModalProps) =>
    open ? <div data-testid="id-wizard" /> : null,
}));

vi.mock("./ProjectFilterControl", () => ({ ProjectFilterControl: () => null }));
vi.mock("./ProjectFilterSummary", () => ({ ProjectFilterSummary: () => null }));

vi.mock("./ProjectGrid", () => ({
  ProjectGrid: () => <div data-testid="project-grid" />,
}));

vi.mock("./ExportModal", () => ({
  ExportModal: () => null,
}));

vi.mock("@/components/guards/Can", () => ({
  Can: ({ children }: { children: ReactNode }) => <>{children}</>,
}));

vi.mock("@/utils/workbenchNavigation", () => ({
  buildProjectEntryUrl: (id: string) => mockBuildWorkbenchUrl(id),
  currentWorkbenchReturnTo: () => "/dashboard",
}));

vi.mock("@/components/ui/Toast", async () => {
  const actual =
    await vi.importActual<typeof import("@/components/ui/Toast")>("@/components/ui/Toast");
  return {
    ...actual,
    useToastStore: <T,>(sel: (s: { push: typeof mockPushToast }) => T) =>
      sel({ push: mockPushToast }),
  };
});

function pageResult(items: unknown[] = [], total = items.length, page = 1, pageSize = 20) {
  return { items, total, page, page_size: pageSize, pages: Math.ceil(total / pageSize) };
}

import { DashboardPage } from "./DashboardPage";

const baseUser = { id: "u1", role: "super_admin", email: "admin@x.com" };

function renderUI(initialPath = "/dashboard", navigateTo?: string) {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      {navigateTo ? <NavigateOnMount to={navigateTo} /> : null}
      <LocationProbe />
      <DashboardPage />
    </MemoryRouter>,
  );
}

function NavigateOnMount({ to }: { to: string }) {
  const navigate = useNavigate();
  useEffect(() => {
    navigate(to, { replace: true });
  }, [navigate, to]);
  return null;
}

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location-search">{location.search}</output>;
}

describe("DashboardPage", () => {
  beforeEach(() => {
    mockPushToast.mockReset();
    mockBuildWorkbenchUrl.mockClear();
    mockAuthStore.mockImplementation(<T,>(sel: (s: MockAuthState) => T) => sel({ user: baseUser }));
    mockUseProjectStats.mockReturnValue({ data: undefined });
    mockUseAuditLogs.mockReturnValue({ data: { items: [] } });
    mockUseProjectPage.mockReturnValue({ data: pageResult([]), isLoading: false });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("isLoading=true → 显示加载中", () => {
    mockUseProjectPage.mockReturnValue({ data: pageResult([]), isLoading: true });
    renderUI();
    expect(screen.getAllByText("加载中...").length).toBeGreaterThan(0);
  });

  it("空项目列表 → 显示「没有匹配的项目」", () => {
    renderUI();
    expect(screen.getByText("没有匹配的项目")).toBeInTheDocument();
  });

  it("有项目 → 渲染项目行", () => {
    mockUseProjectPage.mockReturnValue({
      data: pageResult([
        {
          id: "p1",
          display_id: "P-1",
          name: "Demo项目",
          type_label: "图像 · 目标检测",
          type_key: "image-det",
          data_type: "image",
          tool_bindings: { bbox: { enabled: true } },
          owner_id: "u1",
          owner_name: "Alice",
          member_count: 3,
          status: "in_progress",
          total_tasks: 10,
          completed_tasks: 5,
          review_tasks: 2,
          in_progress_tasks: 1,
          ai_enabled: false,
          updated_at: "2026-01-01T00:00:00Z",
        },
      ]),
      isLoading: false,
    });
    renderUI();
    expect(screen.getByText("Demo项目")).toBeInTheDocument();
    expect(screen.getByText("Alice")).toBeInTheDocument();
    expect(screen.getByText("P-1")).toBeInTheDocument();
    expect(screen.getByText("图片")).toBeInTheDocument();
    expect(screen.queryByText("图像 · 目标检测")).not.toBeInTheDocument();
  });

  it("项目已解绑 backend 时显示未接入模型", () => {
    mockUseProjectPage.mockReturnValue({
      data: pageResult([
        {
          id: "p1",
          display_id: "P-1",
          name: "Demo项目",
          type_label: "视频 · 时序追踪",
          type_key: "video-track",
          data_type: "video",
          tool_bindings: { bbox: { enabled: true } },
          owner_id: "u1",
          owner_name: "Alice",
          member_count: 1,
          status: "in_progress",
          total_tasks: 2,
          completed_tasks: 0,
          review_tasks: 0,
          in_progress_tasks: 0,
          ai_enabled: true,
          ml_backend_id: null,
          updated_at: "2026-05-22T00:00:00Z",
        },
      ]),
      isLoading: false,
    });
    renderUI();
    expect(screen.getByText("未接入模型")).toBeInTheDocument();
    expect(screen.getByText("视频")).toBeInTheDocument();
    expect(screen.queryByText("视频 · 时序追踪")).not.toBeInTheDocument();
  });

  it("列表行空白点击不进入工作台，只有打开按钮进入", () => {
    mockUseProjectPage.mockReturnValue({
      data: pageResult([
        {
          id: "p1",
          display_id: "P-1",
          name: "Demo项目",
          type_label: "图像 · 目标检测",
          type_key: "image-det",
          data_type: "image",
          owner_id: "u1",
          owner_name: "Alice",
          member_count: 3,
          status: "in_progress",
          total_tasks: 10,
          completed_tasks: 5,
          review_tasks: 0,
          in_progress_tasks: 0,
          ai_enabled: false,
        },
      ]),
      isLoading: false,
    });
    renderUI();
    fireEvent.click(screen.getByText("Demo项目"));
    expect(mockBuildWorkbenchUrl).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /打开/ }));
    expect(mockBuildWorkbenchUrl).toHaveBeenCalledWith("p1");
  });

  it("有 stats → stat 卡片渲染正确数值", () => {
    mockUseProjectStats.mockReturnValue({
      data: {
        total_data: 1234,
        completed: 567,
        ai_rate: 42,
        pending_review: 89,
        total_data_series: [1000, 1050, 1100, 1120, 1150, 1170, 1190, 1200, 1210, 1220, 1230, 1234],
        completed_series: [500, 505, 510, 520, 530, 540, 548, 552, 558, 562, 565, 567],
        ai_rate_series: [30, 31, 32, 34, 35, 37, 38, 39, 40, 41, 42, 42],
        pending_review_series: [100, 95, 90, 89, 88, 90, 92, 91, 90, 89, 89, 89],
      },
    });
    renderUI();
    expect(screen.getByText("1,234")).toBeInTheDocument();
    expect(screen.getByText("42%")).toBeInTheDocument();
    expect(screen.getAllByText("近 12 周").length).toBeGreaterThan(0);
    expect(screen.queryByText("↑ 12%")).not.toBeInTheDocument();
  });

  it("以 business_only 向服务端请求业务事件（http.* 由后端排除）", () => {
    mockUseAuditLogs.mockReturnValue({
      data: {
        items: [
          {
            id: "2",
            action: "user.login",
            actor_email: "b@x.com",
            target_type: "user",
            target_id: "u2",
            created_at: new Date().toISOString(),
          },
        ],
      },
    });
    renderUI();
    expect(mockUseAuditLogs).toHaveBeenCalledWith(expect.objectContaining({ business_only: true }));
    expect(screen.getByText("b@x.com")).toBeInTheDocument();
  });

  it("audit 全空 → 显示「暂无业务事件」", () => {
    renderUI();
    expect(screen.getByText("暂无业务事件")).toBeInTheDocument();
  });

  it("点击视图切换按钮 → URL 追加 layout=grid 并渲染 ProjectGrid", () => {
    mockUseProjectPage.mockReturnValue({ data: pageResult([]), isLoading: false });
    renderUI();
    // 切换到网格视图
    const toggleBtn = screen.getByTitle(/切换到网格视图/);
    fireEvent.click(toggleBtn);
    expect(screen.getByTestId("project-grid")).toBeInTheDocument();
  });

  it("初始 ?new=1 → CreateProjectWizard 立即 open", () => {
    renderUI("/dashboard?new=1");
    expect(screen.getByTestId("cp-wizard")).toBeInTheDocument();
  });

  it("restores URL filters and a tab change owns the single status value", async () => {
    renderUI(
      "/dashboard?q=car&status=pending_review&data_type=video&data_type=image&member_id=u1&created_from=2026-01-01&created_to=2026-02-01&new=1&from=p1&layout=grid",
    );
    expect(screen.getByPlaceholderText("搜索项目...")).toHaveValue("car");
    await waitFor(() => {
      expect(mockUseProjectPage).toHaveBeenLastCalledWith(
        expect.objectContaining({
          status: "pending_review",
          search: "car",
          data_type: ["image", "video"],
          member_id: "u1",
        }),
      );
    });

    fireEvent.click(screen.getByRole("button", { name: "进行中" }));
    expect(screen.getByTestId("location-search").textContent).toContain("status=in_progress");
    expect(screen.getByTestId("location-search").textContent).toContain("new=1");
    expect(screen.getByTestId("location-search").textContent).toContain("layout=grid");
    expect(mockUseProjectPage).toHaveBeenLastCalledWith(
      expect.objectContaining({ status: "in_progress" }),
    );
  });

  it("applies debounced search and page reset to the URL together", async () => {
    vi.useFakeTimers();
    renderUI();
    const input = screen.getByPlaceholderText("搜索项目...");
    fireEvent.change(input, { target: { value: "car" } });
    expect(mockUseProjectPage).toHaveBeenLastCalledWith(
      expect.objectContaining({ search: undefined }),
    );
    expect(screen.getByTestId("location-search").textContent).not.toContain("q=car");

    await act(async () => {
      vi.advanceTimersByTime(250);
    });
    expect(mockUseProjectPage).toHaveBeenLastCalledWith(expect.objectContaining({ search: "car" }));
  });

  it("keeps the pending search draft when the wizard opens before debounce", async () => {
    vi.useFakeTimers();
    renderUI();
    const input = screen.getByPlaceholderText("搜索项目...");
    fireEvent.change(input, { target: { value: "car" } });
    fireEvent.click(screen.getByRole("button", { name: /新建项目/ }));
    expect(screen.getByTestId("cp-wizard")).toBeInTheDocument();
    expect(input).toHaveValue("car");
    expect(screen.getByTestId("location-search").textContent).toContain("new=1");

    await act(async () => {
      vi.advanceTimersByTime(250);
    });
    expect(input).toHaveValue("car");
    expect(mockUseProjectPage).toHaveBeenLastCalledWith(expect.objectContaining({ search: "car" }));
    const search = screen.getByTestId("location-search").textContent ?? "";
    expect(search).toContain("q=car");
    expect(search).toContain("new=1");
  });

  it("applies q and status together after external URL navigation", async () => {
    renderUI("/dashboard?q=old&status=in_progress", "/dashboard?q=new&status=pending_review");
    await waitFor(() => {
      expect(mockUseProjectPage).toHaveBeenLastCalledWith(
        expect.objectContaining({ status: "pending_review", search: "new" }),
      );
    });
    expect(
      mockUseProjectPage.mock.calls.some(
        ([params]) => params.status === "pending_review" && params.search === "old",
      ),
    ).toBe(false);
  });
});

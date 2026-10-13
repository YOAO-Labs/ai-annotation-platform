/**
 * AdminPeoplePage 单测
 * 覆盖: 加载态 / 空列表 / 成员卡片渲染 / 筛选按钮交互 / 点击卡片打开抽屉 /
 *       抽屉加载态 / 抽屉关闭
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, fireEvent, waitFor } from "@testing-library/react";
import { Link, MemoryRouter, useLocation } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useAuthStore } from "@/stores/authStore";

const mockUseAdminPeople = vi.fn();
const mockUseAdminPersonDetail = vi.fn();

const { mockNavigate, mockExportPeople } = vi.hoisted(() => ({
  mockNavigate: vi.fn(),
  mockExportPeople: vi.fn(),
}));

vi.mock("@/hooks/useDashboard", () => ({
  useAdminPeople: (...args: unknown[]) => mockUseAdminPeople(...args),
  useAdminPersonDetail: (...args: unknown[]) => mockUseAdminPersonDetail(...args),
}));

vi.mock("@/api/dashboard", () => ({
  dashboardApi: { exportPeople: (...args: unknown[]) => mockExportPeople(...args) },
}));

const mockUseProjects = vi.fn();
vi.mock("@/hooks/useProjects", () => ({
  useProjects: () => mockUseProjects(),
}));

const mockUsePermissions = vi.fn();
vi.mock("@/hooks/usePermissions", () => ({
  usePermissions: () => mockUsePermissions(),
}));

const mockListByProject = vi.fn();
vi.mock("@/api/tasks", () => ({
  tasksApi: { listByProject: (...args: unknown[]) => mockListByProject(...args) },
}));

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<any>("react-router-dom");
  return { ...actual, useNavigate: () => mockNavigate };
});

vi.mock("@/components/ui/Toast", async () => {
  const actual = await vi.importActual<any>("@/components/ui/Toast");
  return {
    ...actual,
    useToastStore: <T,>(sel: (s: any) => T) => sel({ push: vi.fn() }),
  };
});

import { AdminPeoplePage, platformRoleBadgeVariant, platformRoleLabel } from "./AdminPeoplePage";

const basePerson = {
  user_id: "u1",
  name: "Alice",
  email: "alice@test.com",
  role: "employee",
  status: "online",
  project_count: 3,
  main_metric: 120,
  main_metric_label: "任务/天",
  weekly_compare_pct: 10,
  throughput_score: 80,
  quality_score: 90,
  activity_score: 70,
  sparkline_7d: [10, 20, 30, 40, 50, 60, 70],
  rejected_rate: 5,
  alerts: [],
};

const baseDetail = {
  user_id: "u1",
  name: "Alice",
  email: "alice@test.com",
  role: "employee",
  project_count: 3,
  throughput: 120,
  quality_score: 90,
  active_minutes: 480,
  composite_score: 87,
  weekly_compare_pct: 10,
  trend_throughput: [100, 110, 120, 130],
  trend_quality: [85, 87, 89, 90],
  project_distribution: [{ project_id: "p1", project_name: "项目A", count: 50 }],
  duration_histogram: [],
  p50_duration_ms: null,
  p95_duration_ms: null,
  timeline: [],
  reject_reason_breakdown: [],
  class_distribution: [],
  first_pass_yield: null,
};

function LocationProbe() {
  const { search } = useLocation();
  return (
    <>
      <output data-testid="location-search">{search}</output>
      <Link to="/admin/people?q=restored&role=reviewer&period=1m&keep=yes">恢复筛选链接</Link>
    </>
  );
}

function renderUI(initialPath = "/admin/people") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initialPath]}>
        <LocationProbe />
        <AdminPeoplePage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("AdminPeoplePage", () => {
  beforeEach(() => {
    useAuthStore.getState().setAuth("people-test-token", {
      id: "people-owner",
      name: "People owner",
      email: "people@test.local",
      role: "super_admin",
      group_name: null,
      status: "active",
      created_at: "2026-01-01T00:00:00Z",
    });
    mockUseAdminPeople.mockReturnValue({ data: undefined, isLoading: false });
    mockUseAdminPersonDetail.mockReturnValue({ data: undefined, isLoading: true });
    mockNavigate.mockClear();
    mockExportPeople.mockReset();
    mockExportPeople.mockResolvedValue(undefined);
    mockUseProjects.mockReturnValue({
      data: [{ id: "pd", name: "下拉项目" }],
    });
    mockUsePermissions.mockReturnValue({ role: "super_admin" });
    mockListByProject.mockReset();
    mockListByProject.mockResolvedValue({ items: [], total: 0, limit: 20, offset: 0 });
  });

  it("resumes search during navigation rehydration without a stale facet write", async () => {
    vi.useFakeTimers();
    const view = renderUI("/admin/people?q=old&role=reviewer&period=1m&keep=yes");
    const params = () =>
      new URLSearchParams(screen.getByTestId("location-search").textContent ?? "");
    try {
      fireEvent.click(screen.getByRole("link", { name: "恢复筛选链接" }));
      const input = screen.getByPlaceholderText("姓名 / 邮箱");
      expect(input).toHaveValue("restored");
      await act(async () => vi.advanceTimersByTime(100));
      fireEvent.change(input, { target: { value: "Alice" } });
      fireEvent.click(screen.getByRole("button", { name: "标注员" }));
      await act(async () => vi.advanceTimersByTime(249));
      expect(params().get("q")).toBe("restored");
      await act(async () => vi.advanceTimersByTime(1));
      expect(params().get("q")).toBe("Alice");
      expect(mockUseAdminPeople).toHaveBeenLastCalledWith(
        expect.objectContaining({ q: "Alice", role: "annotator", period: "1m" }),
      );
      expect(params().get("keep")).toBe("yes");
      fireEvent.change(input, { target: { value: "Alice updated" } });
      fireEvent.keyDown(input, { key: "Enter" });
      expect(params().get("q")).toBe("Alice updated");
      await act(async () => vi.advanceTimersByTime(500));
      expect(params().get("q")).toBe("Alice updated");
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it("isLoading=true → 显示加载中", () => {
    mockUseAdminPeople.mockReturnValue({ data: undefined, isLoading: true });
    renderUI();
    expect(screen.getByText("加载中...")).toBeInTheDocument();
  });

  it("空列表 → 显示「暂无成员数据」提示", () => {
    mockUseAdminPeople.mockReturnValue({
      data: { items: [], total: 0, period: "7d" },
      isLoading: false,
    });
    renderUI();
    expect(screen.getByText("暂无成员数据")).toBeInTheDocument();
  });

  it("有成员时渲染卡片内容", () => {
    mockUseAdminPeople.mockReturnValue({
      data: { items: [basePerson], total: 1, period: "7d" },
      isLoading: false,
    });
    renderUI();
    expect(screen.getByText("Alice")).toBeInTheDocument();
    expect(screen.getByText("员工")).toBeInTheDocument();
    // main_metric 渲染
    expect(screen.getByText("120")).toBeInTheDocument();
    // project_count
    expect(screen.getByText(/3 项目/)).toBeInTheDocument();
  });

  it("平台角色徽章按平台身份呈现, 不把平台角色与项目职责比较", () => {
    // 平台身份: employee 是唯一的普通员工身份; 项目职责 (annotator/reviewer)
    // 在卡片其它位置由 main_metric_label 体现, 徽章不再据此变色.
    expect(platformRoleLabel("employee")).toBe("员工");
    expect(platformRoleLabel("viewer")).toBe("观察者");
    expect(platformRoleLabel("super_admin")).toBe("超级管理员");
    expect(platformRoleLabel("legacy_annotator")).toBe("legacy_annotator");
    expect(platformRoleBadgeVariant("employee")).toBe("ai");
    expect(platformRoleBadgeVariant("viewer")).toBe("default");
    expect(platformRoleBadgeVariant("super_admin")).toBe("danger");
    // 项目职责字符串即使被误当平台角色也不应命中旧的 annotator 强调分支.
    expect(platformRoleBadgeVariant("annotator")).toBe("default");

    const viewer = { ...basePerson, user_id: "u2", name: "Vera", role: "viewer" };
    mockUseAdminPeople.mockReturnValue({
      data: { items: [basePerson, viewer], total: 2, period: "7d" },
      isLoading: false,
    });
    renderUI();
    expect(screen.getByText("员工")).toHaveClass("bg-status-info-soft");
    expect(screen.getByText("观察者")).toHaveClass("bg-muted");
  });

  it("成员有 high_rejected alert → 显示退回率 badge", () => {
    const personWithAlert = { ...basePerson, alerts: ["high_rejected"], rejected_rate: 20 };
    mockUseAdminPeople.mockReturnValue({
      data: { items: [personWithAlert], total: 1, period: "7d" },
      isLoading: false,
    });
    renderUI();
    expect(screen.getByText(/退回率 20%/)).toBeInTheDocument();
  });

  it("点击角色筛选按钮更新 URL 参数", () => {
    mockUseAdminPeople.mockReturnValue({
      data: { items: [], total: 0, period: "7d" },
      isLoading: false,
    });
    renderUI();
    const annotatorBtn = screen.getByRole("button", { name: "标注员" });
    fireEvent.click(annotatorBtn);
    // 筛选按钮触发后, useAdminPeople 会被以新参数调用
    expect(mockUseAdminPeople).toHaveBeenCalled();
  });

  it("点击卡片打开个人抽屉", () => {
    mockUseAdminPeople.mockReturnValue({
      data: { items: [basePerson], total: 1, period: "7d" },
      isLoading: false,
    });
    mockUseAdminPersonDetail.mockReturnValue({ data: undefined, isLoading: true });
    renderUI();
    // 卡片可点击 (Card onClick)
    const card = screen.getByText("Alice").closest("[class]")!;
    fireEvent.click(card);
    // 抽屉加载态
    expect(screen.getAllByText("加载中...").length).toBeGreaterThan(0);
  });

  it("抽屉有数据时显示成员详情", async () => {
    mockUseAdminPeople.mockReturnValue({
      data: { items: [basePerson], total: 1, period: "7d" },
      isLoading: false,
    });
    mockUseAdminPersonDetail.mockReturnValue({ data: baseDetail, isLoading: false });
    renderUI();
    // 点开抽屉
    const card = screen.getByText("Alice").closest("[class]")!;
    fireEvent.click(card);
    await waitFor(() => {
      expect(screen.getAllByText("产能").length).toBeGreaterThan(0);
      expect(screen.getAllByText("质量").length).toBeGreaterThan(0);
      expect(screen.getByText("4 周趋势")).toBeInTheDocument();
    });
  });

  it("keeps member detail in the applied project and period", () => {
    mockUseAdminPeople.mockReturnValue({
      data: { items: [basePerson], total: 1, period: "1m" },
      isLoading: false,
    });
    mockUseAdminPersonDetail.mockReturnValue({ data: baseDetail, isLoading: false });
    renderUI("/admin/people?project=p1&period=1m");
    fireEvent.click(screen.getByText("Alice").closest("[class]")!);
    expect(mockUseAdminPersonDetail).toHaveBeenLastCalledWith("u1", "1m", "p1");
    expect(screen.getByText("4 周趋势")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "今日" }));
    expect(mockUseAdminPersonDetail).toHaveBeenLastCalledWith("u1", "today", "p1");
    expect(screen.getByText("4 周趋势")).toBeInTheDocument();
  });

  it("点击抽屉关闭按钮关闭抽屉", async () => {
    mockUseAdminPeople.mockReturnValue({
      data: { items: [basePerson], total: 1, period: "7d" },
      isLoading: false,
    });
    mockUseAdminPersonDetail.mockReturnValue({ data: baseDetail, isLoading: false });
    renderUI();
    // 打开抽屉
    const card = screen.getByText("Alice").closest("[class]")!;
    fireEvent.click(card);
    await waitFor(() => expect(screen.getByLabelText("关闭")).toBeInTheDocument());
    fireEvent.click(screen.getByLabelText("关闭"));
    // 抽屉关闭后详情不在 DOM 中
    await waitFor(() => expect(screen.queryByText("4 周趋势")).not.toBeInTheDocument());
  });

  it("页面标题「成员绩效」始终可见", () => {
    mockUseAdminPeople.mockReturnValue({
      data: { items: [], total: 0, period: "7d" },
      isLoading: false,
    });
    renderUI();
    expect(screen.getByText("成员绩效")).toBeInTheDocument();
  });

  it("v0.12.5 · 点击「导出 CSV」调用 exportPeople（带当前筛选）", async () => {
    mockUseAdminPeople.mockReturnValue({
      data: { items: [basePerson], total: 1, period: "7d" },
      isLoading: false,
    });
    renderUI("/admin/people?role=annotator&sort=quality");
    fireEvent.click(screen.getByRole("button", { name: /导出 CSV/ }));
    await waitFor(() =>
      expect(mockExportPeople).toHaveBeenCalledWith(
        expect.objectContaining({ role: "annotator", sort: "quality" }),
      ),
    );
  });

  it("v0.12.5 · 点击项目分布行 → 下钻到 review 队列（带 project + assignee）", async () => {
    mockUseAdminPeople.mockReturnValue({
      data: { items: [basePerson], total: 1, period: "7d" },
      isLoading: false,
    });
    mockUseAdminPersonDetail.mockReturnValue({ data: baseDetail, isLoading: false });
    renderUI();
    fireEvent.click(screen.getByText("Alice").closest("[class]")!);
    await waitFor(() => expect(screen.getByText("项目A")).toBeInTheDocument());
    fireEvent.click(screen.getByText("项目A"));
    expect(mockNavigate).toHaveBeenCalledWith("/review?project=p1&assignee=u1");
  });

  it("v0.12.6 · super_admin 项目下拉含「全部项目」+ 项目选项", () => {
    mockUseAdminPeople.mockReturnValue({
      data: { items: [], total: 0, period: "7d" },
      isLoading: false,
    });
    renderUI();
    const select = screen.getByLabelText("项目范围");
    expect(select).toBeInTheDocument();
    expect(screen.getByText("全部项目（全局）")).toBeInTheDocument();
    expect(screen.getByText("下拉项目")).toBeInTheDocument();
  });

  it("v0.12.6 · 项目模式下点 reject 原因行 → 下钻调 tasksApi（带 project+assignee+reason）", async () => {
    const detailWithReject = {
      ...baseDetail,
      reject_reason_breakdown: [{ reason_type: "missing", count: 3, pct: 100 }],
    };
    mockUseAdminPeople.mockReturnValue({
      data: { items: [basePerson], total: 1, period: "7d" },
      isLoading: false,
    });
    mockUseAdminPersonDetail.mockReturnValue({
      data: detailWithReject,
      isLoading: false,
    });
    renderUI("/admin/people?project=pd");
    fireEvent.click(screen.getByText("Alice").closest("[class]")!);
    await waitFor(() => expect(screen.getByText("漏标")).toBeInTheDocument());
    fireEvent.click(screen.getByText("漏标"));
    await waitFor(() =>
      expect(mockListByProject).toHaveBeenCalledWith(
        "pd",
        expect.objectContaining({ assignee_id: "u1", reject_reason_type: "missing" }),
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      ),
    );
  });
});

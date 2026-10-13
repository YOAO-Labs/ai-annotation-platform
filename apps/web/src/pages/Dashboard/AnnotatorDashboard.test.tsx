/**
 * v0.8.5 · AnnotatorDashboard 单测：加载态 / 空项目 / hour_buckets 直方图渲染 /
 * formatMs 边界 / sortedProjects 排序。
 *
 * 用 vi.mock 拦截 hooks（useAnnotatorStats / useProjects / useMyBatches）避免
 * 引入 react-query / MSW 依赖。
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { ApiError } from "@/api/client";

const mockUseAnnotatorStats = vi.fn();
const mockUseProjects = vi.fn();
const mockUseMyBatches = vi.fn();
const mockUseOnboardingProjectSummary = vi.fn();

vi.mock("@/hooks/useDashboard", () => ({
  useAnnotatorStats: () => mockUseAnnotatorStats(),
  useMyBatches: () => mockUseMyBatches(),
  useOnboardingProjectSummary: (projectId: string) => mockUseOnboardingProjectSummary(projectId),
}));
vi.mock("@/hooks/useProjects", () => ({
  useProjects: () => mockUseProjects(),
}));
// MyBatchesCard 内部用 useQueryClient，避免引入 QueryClientProvider，整体 mock 掉。
vi.mock("./MyBatchesCard", () => ({
  MyBatchesCard: () => <div data-testid="my-batches-card-stub" />,
}));

import { AnnotatorDashboard } from "./AnnotatorDashboard";

const fullStats = {
  assigned_tasks: 5,
  today_completed: 3,
  weekly_completed: 18,
  total_completed: 120,
  personal_accuracy: 92.5,
  daily_counts: [1, 2, 3, 4, 5, 6, 7],
  median_duration_ms: 45000,
  rejected_rate: 2.1,
  reopened_avg: 0.3,
  weekly_compare_pct: 5.4,
  weekly_target: 200,
  active_minutes_today: 42,
  streak_days: 7,
  hour_buckets: Array(24)
    .fill(0)
    .map((_, h) => (h === 9 ? 5 : h === 14 ? 2 : 0)),
};

function renderUI() {
  return render(
    <MemoryRouter>
      <AnnotatorDashboard />
    </MemoryRouter>,
  );
}

describe("AnnotatorDashboard", () => {
  beforeEach(() => {
    mockUseAnnotatorStats.mockReset();
    mockUseProjects.mockReset();
    mockUseMyBatches.mockReset();
    mockUseOnboardingProjectSummary.mockReset();
    mockUseMyBatches.mockReturnValue({ data: [], isLoading: false });
    mockUseOnboardingProjectSummary.mockReturnValue({
      data: undefined,
      isError: false,
      refetch: vi.fn(),
    });
  });

  it("isLoading=true → 显示加载中文案", () => {
    mockUseAnnotatorStats.mockReturnValue({ data: undefined, isLoading: true });
    mockUseProjects.mockReturnValue({ data: [] });
    renderUI();
    expect(screen.getByText("加载中...")).toBeInTheDocument();
  });

  it("统计首屏离线暂停 → 显示等待网络连接", () => {
    mockUseAnnotatorStats.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: false,
      isPaused: true,
      fetchStatus: "paused",
    });
    mockUseProjects.mockReturnValue({ data: [] });
    renderUI();
    expect(screen.getByRole("status")).toHaveTextContent(
      "网络连接已断开，标注统计会在恢复后自动继续",
    );
  });

  it("stats=null（未加载完）→ 显示加载中文案，不崩", () => {
    mockUseAnnotatorStats.mockReturnValue({ data: null, isLoading: false });
    mockUseProjects.mockReturnValue({ data: [] });
    renderUI();
    expect(screen.getByText("加载中...")).toBeInTheDocument();
  });

  it("统计初始失败 → 显示错误态并支持重试", () => {
    const refetch = vi.fn();
    mockUseAnnotatorStats.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: new Error("server unavailable"),
      refetch,
    });
    mockUseProjects.mockReturnValue({ data: [] });
    renderUI();
    expect(screen.getByRole("alert")).toHaveTextContent("无法加载标注统计");
    fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
    expect(refetch).toHaveBeenCalledOnce();
  });

  it("统计刷新失败 → 保留上一次 KPI 并提示更新失败", () => {
    const refetch = vi.fn();
    mockUseAnnotatorStats.mockReturnValue({
      data: fullStats,
      isLoading: false,
      isError: true,
      error: new Error("network"),
      refetch,
    });
    mockUseProjects.mockReturnValue({ data: [] });
    renderUI();
    expect(screen.getByText("5")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("标注统计更新失败");
    fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
    expect(refetch).toHaveBeenCalledOnce();
  });

  it("项目查询 403 → 独立显示权限错误，不把项目列表当成空态", () => {
    const refetch = vi.fn();
    mockUseAnnotatorStats.mockReturnValue({ data: fullStats, isLoading: false });
    mockUseProjects.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: new ApiError(403, "forbidden"),
      refetch,
    });
    renderUI();
    expect(screen.getByRole("alert")).toHaveTextContent("没有权限查看项目列表");
    expect(screen.queryByText("暂无分配项目")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
    expect(refetch).toHaveBeenCalledOnce();
  });

  it("项目首屏离线暂停 → 显示等待网络连接而不是暂无分配项目", () => {
    mockUseAnnotatorStats.mockReturnValue({ data: fullStats, isLoading: false });
    mockUseProjects.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: false,
      isPaused: true,
      fetchStatus: "paused",
    });
    renderUI();
    expect(screen.getByRole("status")).toHaveTextContent(
      "网络连接已断开，项目列表会在恢复后自动继续",
    );
    expect(screen.queryByText("暂无分配项目")).not.toBeInTheDocument();
  });

  it("项目刷新失败 → 保留已有项目行并提示更新失败", () => {
    const refetch = vi.fn();
    mockUseAnnotatorStats.mockReturnValue({ data: fullStats, isLoading: false });
    mockUseProjects.mockReturnValue({
      data: [
        {
          id: "p1",
          my_project_role: "annotator",
          display_id: "P-1",
          name: "项目一",
          type_label: "图像检测",
          total_tasks: 3,
          completed_tasks: 1,
        },
      ],
      isLoading: false,
      isError: true,
      error: new Error("network"),
      refetch,
    });
    renderUI();
    expect(screen.getByText("项目一")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("项目列表更新失败");
    fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
    expect(refetch).toHaveBeenCalledOnce();
  });

  it("有 stats + 0 项目 → 显示「暂无分配项目」空态", () => {
    mockUseAnnotatorStats.mockReturnValue({ data: fullStats, isLoading: false });
    mockUseProjects.mockReturnValue({ data: [] });
    renderUI();
    expect(screen.getByText("暂无分配项目")).toBeInTheDocument();
  });

  it("有 stats → 渲染 24-bar 直方图（hour_buckets 长度 24）", () => {
    mockUseAnnotatorStats.mockReturnValue({ data: fullStats, isLoading: false });
    mockUseProjects.mockReturnValue({ data: [] });
    const { container } = renderUI();
    expect(screen.getByText("今日专注时段分布")).toBeInTheDocument();
    // bar 都有 borderRadius "2px 2px 0 0"
    const bars = Array.from(container.querySelectorAll("div")).filter(
      (el) => (el as HTMLElement).style.borderRadius === "2px 2px 0 0",
    );
    expect(bars.length).toBe(24);
  });

  it("hour_buckets 为 undefined → 用全 0 兜底渲染 24 bar", () => {
    const stats = { ...fullStats, hour_buckets: undefined };
    mockUseAnnotatorStats.mockReturnValue({ data: stats, isLoading: false });
    mockUseProjects.mockReturnValue({ data: [] });
    const { container } = renderUI();
    const bars = Array.from(container.querySelectorAll("div")).filter(
      (el) => (el as HTMLElement).style.borderRadius === "2px 2px 0 0",
    );
    expect(bars.length).toBe(24);
  });

  it("产能/质量/投入卡片显示具体数值", () => {
    mockUseAnnotatorStats.mockReturnValue({ data: fullStats, isLoading: false });
    mockUseProjects.mockReturnValue({ data: [] });
    renderUI();
    // 产能
    expect(screen.getByText("待标任务")).toBeInTheDocument();
    expect(screen.getByText("5")).toBeInTheDocument();
    // 质量原创比例
    expect(screen.getByText("92.5%")).toBeInTheDocument();
    // 投入活跃时长
    expect(screen.getByText("42m")).toBeInTheDocument();
    expect(screen.getByText("7天")).toBeInTheDocument();
  });

  it("active_minutes_today=null → 显示 — 占位", () => {
    const s = { ...fullStats, active_minutes_today: null, streak_days: null };
    mockUseAnnotatorStats.mockReturnValue({ data: s, isLoading: false });
    mockUseProjects.mockReturnValue({ data: [] });
    renderUI();
    // 至少 3 个 — 占位（活跃 / streak / rejected_rate 等可能 null 的）
    const dashes = screen.getAllByText("—");
    expect(dashes.length).toBeGreaterThanOrEqual(2);
  });

  it("median_duration_ms < 60s → 显示 X.Xs，>= 60s → XmYYs", () => {
    // < 60s
    let s = { ...fullStats, median_duration_ms: 45000 };
    mockUseAnnotatorStats.mockReturnValue({ data: s, isLoading: false });
    mockUseProjects.mockReturnValue({ data: [] });
    const { unmount } = renderUI();
    expect(screen.getByText("45.0s")).toBeInTheDocument();
    unmount();

    // >= 60s
    s = { ...fullStats, median_duration_ms: 125000 };
    mockUseAnnotatorStats.mockReturnValue({ data: s, isLoading: false });
    mockUseProjects.mockReturnValue({ data: [] });
    renderUI();
    expect(screen.getByText("2m05s")).toBeInTheDocument();
  });

  it("项目按剩余任务数倒序", () => {
    const projects = [
      {
        id: "a",
        my_project_role: "annotator",
        display_id: "P-A",
        name: "A",
        type_label: "图像检测",
        total_tasks: 10,
        completed_tasks: 8,
      }, // 剩 2
      {
        id: "b",
        my_project_role: "annotator",
        display_id: "P-B",
        name: "B",
        type_label: "图像检测",
        total_tasks: 10,
        completed_tasks: 0,
      }, // 剩 10
      {
        id: "c",
        my_project_role: "annotator",
        display_id: "P-C",
        name: "C",
        type_label: "图像检测",
        total_tasks: 10,
        completed_tasks: 5,
      }, // 剩 5
    ];
    mockUseAnnotatorStats.mockReturnValue({ data: fullStats, isLoading: false });
    mockUseProjects.mockReturnValue({ data: projects });
    renderUI();
    const rows = screen.getAllByText(/^[ABC]$/);
    expect(rows[0].textContent).toBe("B"); // 剩 10
    expect(rows[1].textContent).toBe("C"); // 剩 5
    expect(rows[2].textContent).toBe("A"); // 剩 2
  });

  it("only offers annotation entries for annotator memberships", () => {
    mockUseAnnotatorStats.mockReturnValue({ data: fullStats, isLoading: false });
    mockUseProjects.mockReturnValue({
      data: ["annotator", "reviewer", "viewer", null].map((role, i) => ({
        id: `p${i}`,
        name: `Project ${role}`,
        display_id: `P-${i}`,
        my_project_role: role,
        type_label: "图像检测",
      })),
    });
    renderUI();
    expect(screen.getByText("Project annotator")).toBeInTheDocument();
    expect(screen.queryByText("Project reviewer")).not.toBeInTheDocument();
    expect(screen.queryByText("Project viewer")).not.toBeInTheDocument();
    expect(screen.queryByText("Project null")).not.toBeInTheDocument();
  });
});

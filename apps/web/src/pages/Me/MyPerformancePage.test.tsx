/**
 * MyPerformancePage 渲染冒烟测试。
 *
 * 重点：验证 recharts（首次引入本仓）能正常 mount + 数据流通到 hero KPI，
 * 不抛运行时错误。图表在 jsdom 下 0 尺寸不渲染内部内容，故只断言图表容器外的文案。
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const mockGetMyPerformance = vi.fn();
vi.mock("@/api/dashboard", () => ({
  dashboardApi: {
    getMyPerformance: (...args: unknown[]) => mockGetMyPerformance(...args),
  },
}));

import { MyPerformancePage } from "./MyPerformancePage";

const sample = {
  user_id: "u1",
  name: "Alice",
  period: "4w",
  throughput: 128,
  quality_score: 95,
  weekly_compare_pct: 12.5,
  trend_throughput: [10, 20, 30, 40],
  trend_quality: [90, 92, 94, 95],
  team_trend_throughput: [12.0, 18.0, 25.0, 33.0],
  duration_histogram: [
    { upper_ms: 5000, count: 4 },
    { upper_ms: 10000, count: 2 },
  ],
  p50_duration_ms: 4200,
  p95_duration_ms: 9100,
  reject_reason_breakdown: [
    { reason_type: "missing", count: 3, pct: 60.0 },
    { reason_type: "wrong_label", count: 2, pct: 40.0 },
  ],
  class_distribution: [
    { class_name: "car", count: 80, pct: 62.5 },
    { class_name: "person", count: 48, pct: 37.5 },
  ],
  first_pass_yield: 0.82,
};

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <MyPerformancePage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("MyPerformancePage", () => {
  it("渲染 hero KPI 与图表标题（recharts mount 不崩）", async () => {
    mockGetMyPerformance.mockResolvedValue(sample);
    renderPage();

    expect(await screen.findByText("我的绩效")).toBeInTheDocument();
    // hero KPI 数据流通
    await waitFor(() => expect(screen.getByText("128")).toBeInTheDocument());
    expect(screen.getByText("+12.5%")).toBeInTheDocument();
    // 图表卡片标题（容器外文案）
    expect(screen.getByText("产出趋势 · 我 vs 团队均线（近 4 周）")).toBeInTheDocument();
    expect(screen.getByText("标注耗时分布")).toBeInTheDocument();
    // 质量归因块
    expect(screen.getByText("82%")).toBeInTheDocument(); // 首过率
    expect(screen.getByText("Reject 原因分布")).toBeInTheDocument();
    expect(screen.getByText(/类别覆盖/)).toBeInTheDocument();
  });

  it("接口报错时显示降级文案", async () => {
    mockGetMyPerformance.mockRejectedValue(new Error("boom"));
    renderPage();
    expect(await screen.findByText("绩效数据暂不可用，请稍后再试。")).toBeInTheDocument();
  });
});

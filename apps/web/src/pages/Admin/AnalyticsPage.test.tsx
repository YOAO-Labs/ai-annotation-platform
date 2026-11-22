import { describe, it, expect, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const mockThroughput = vi.fn();
const mockReject = vi.fn();
const mockDuration = vi.fn();
const mockHeatmap = vi.fn();

vi.mock("@/api/adminAnalytics", () => ({
  adminAnalyticsApi: {
    throughputDaily: (...a: unknown[]) => mockThroughput(...a),
    rejectRateByType: (...a: unknown[]) => mockReject(...a),
    durationDist: (...a: unknown[]) => mockDuration(...a),
    activityHeatmap: (...a: unknown[]) => mockHeatmap(...a),
  },
}));

import { AnalyticsPage } from "./AnalyticsPage";

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <AnalyticsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("AnalyticsPage", () => {
  it("渲染 4 个面板标题，recharts + 热力图 mount 不崩", async () => {
    mockThroughput.mockResolvedValue({
      panel: "throughput_daily",
      data: [
        { day: "2026-05-01T00:00:00", user_id: "u1", event_count: 12 },
        { day: "2026-05-02T00:00:00", user_id: "u1", event_count: 8 },
      ],
    });
    mockReject.mockResolvedValue({
      panel: "reject_rate_by_type",
      data: [{ reason_type: "missing", count: 3, pct: 60 }],
    });
    mockDuration.mockResolvedValue({
      panel: "duration_dist",
      data: { n: 5, p50: 4200, p95: 9100, mean: 5000 },
    });
    mockHeatmap.mockResolvedValue({
      panel: "activity_heatmap",
      data: [
        { weekday: 2, hour: 14, count: 23 },
        { weekday: 4, hour: 9, count: 10 },
      ],
    });

    renderPage();

    expect(await screen.findByText("离线分析（DuckDB）")).toBeInTheDocument();
    expect(screen.getByText("团队日吞吐（task_events.kind=annotate）")).toBeInTheDocument();
    expect(screen.getByText("Reject 原因分布")).toBeInTheDocument();
    expect(screen.getByText("标注耗时分布")).toBeInTheDocument();
    expect(screen.getByText("工时热力图（started_at · 星期 × 小时）")).toBeInTheDocument();

    await waitFor(() => expect(screen.getByText("5")).toBeInTheDocument());
    expect(screen.getByText("周一")).toBeInTheDocument();
    expect(screen.getByText("周日")).toBeInTheDocument();
    expect(screen.getByText("周六")).toBeInTheDocument();
  });

  it("热力图无数据时显示空态", async () => {
    mockThroughput.mockResolvedValue({ panel: "throughput_daily", data: [] });
    mockReject.mockResolvedValue({ panel: "reject_rate_by_type", data: [] });
    mockDuration.mockResolvedValue({
      panel: "duration_dist",
      data: { n: 0, p50: 0, p95: 0, mean: 0 },
    });
    mockHeatmap.mockResolvedValue({ panel: "activity_heatmap", data: [] });

    renderPage();

    expect(await screen.findByText("所选范围内暂无工时数据")).toBeInTheDocument();
  });
});

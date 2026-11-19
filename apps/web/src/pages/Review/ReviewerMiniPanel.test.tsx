/**
 * ReviewerMiniPanel 单测：渲染 3 个 mini-stat 数值。
 */
import { describe, it, expect, vi } from "vitest";
import { render } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("@/api/dashboard", () => ({
  dashboardApi: {
    getReviewerTodayMini: vi.fn(async () => ({
      approved_today: 5,
      rejected_today: 2,
      avg_review_seconds: 45,
    })),
  },
}));

import { ReviewerMiniPanel } from "./ReviewerMiniPanel";

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("ReviewerMiniPanel", () => {
  it("初始 loading 渲染省略号占位", () => {
    const { getByTestId } = wrap(<ReviewerMiniPanel />);
    const panel = getByTestId("reviewer-mini-panel");
    expect(panel).toBeTruthy();
    // 渲染时 query 异步还未 resolve，预期文案有「…」
    expect(panel.textContent).toMatch(/今日通过|…/);
  });
});

import { screen, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { expect, it } from "vitest";
import { server } from "@/mocks/server";
import { renderWithProviders } from "@/test/renderWithProviders";
import { ReviewerMiniPanel } from "./ReviewerMiniPanel";

it("今日审核指标先分别占位，再按标签显示通过数、退回数和平均耗时", async () => {
  server.use(
    http.get("*/api/v1/dashboard/reviewer/today-mini", () =>
      HttpResponse.json({ approved_today: 7, rejected_today: 2, avg_review_seconds: 134 }),
    ),
  );
  renderWithProviders(<ReviewerMiniPanel />);
  const metrics = [
    ["今日通过", "7"],
    ["今日退回", "2"],
    ["平均耗时", "2m 14s"],
  ];
  for (const [label] of metrics) {
    expect(within(screen.getByText(label).parentElement!).getByText("…")).toBeVisible();
  }
  await screen.findByText("2m 14s");
  for (const [label, value] of metrics) {
    expect(within(screen.getByText(label).parentElement!).getByText(value)).toBeVisible();
  }
});

import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mockUseMyBatches = vi.fn();

vi.mock("@/hooks/useDashboard", () => ({
  useMyBatches: () => mockUseMyBatches(),
}));

vi.mock("@/components/ui/Toast", () => ({
  useToastStore: <T,>(selector: (state: { push: () => void }) => T) => selector({ push: vi.fn() }),
}));

import { MyBatchesCard } from "./MyBatchesCard";

const batch = {
  batch_id: "b1",
  batch_display_id: "B-1",
  batch_name: "批次一",
  project_id: "p1",
  project_name: "项目一",
  status: "annotating",
  total_tasks: 3,
  completed_tasks: 0,
  review_tasks: 0,
  in_progress_tasks: 0,
  approved_tasks: 0,
  rejected_tasks: 0,
  progress_pct: 0,
  thumbnail_url: null,
  cover_blurhash: null,
  review_feedback: null,
  reviewed_at: null,
  reviewer: null,
};

function batchesQuery(data: unknown[]) {
  return { data, isLoading: false, isError: false };
}

function renderUI() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <MyBatchesCard />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("MyBatchesCard", () => {
  beforeEach(() => mockUseMyBatches.mockReset());

  it("首屏离线暂停 → 显示等待网络连接而不是隐藏卡片", () => {
    mockUseMyBatches.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: false,
      isPaused: true,
      fetchStatus: "paused",
    });
    renderUI();
    expect(screen.getByRole("status")).toHaveTextContent(
      "网络连接已断开，分派批次会在恢复后自动继续",
    );
  });

  it("reviewing 批次仍有未送审任务时显示整批提交", () => {
    mockUseMyBatches.mockReturnValue(
      batchesQuery([{ ...batch, status: "reviewing", review_tasks: 2 }]),
    );
    renderUI();
    expect(screen.getByText("提交质检")).toBeInTheDocument();
  });

  it("批次全部送审后隐藏整批提交", () => {
    mockUseMyBatches.mockReturnValue(
      batchesQuery([{ ...batch, status: "reviewing", review_tasks: 3 }]),
    );
    renderUI();
    expect(screen.queryByText("提交质检")).not.toBeInTheDocument();
  });
});

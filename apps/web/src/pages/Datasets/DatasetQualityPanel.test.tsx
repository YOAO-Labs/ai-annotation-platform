import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { DatasetQualityPanel } from "./DatasetQualityPanel";
import { datasetQualityApi, type QualityRun, type QualityResult } from "@/api/datasetQuality";
vi.mock("./DatasetQualityBulkReview", () => ({
  DatasetQualityBulkReview: () => <p>批量复核入口</p>,
}));
const mocks = vi.hoisted(() => ({
  run: undefined as unknown,
  rows: [] as unknown[],
  list: [] as unknown[],
  selected: vi.fn(),
}));
vi.mock("@/hooks/useDatasets", () => ({
  useDatasetOwner: () => ({ assert: vi.fn(), revision: 1 }),
}));
vi.mock("@/hooks/useDatasetQuality", () => ({
  qualityActive: (r?: { status: string }) => !!r && ["pending", "running"].includes(r.status),
  useQualityRuns: () => ({
    data: { items: mocks.list, total: mocks.list.length },
    isLoading: false,
  }),
  useQualityRun: (_: string, selected: string) => {
    mocks.selected(selected);
    return { data: mocks.run, refetch: vi.fn() };
  },
  useQualityResults: () => ({
    data: { items: mocks.rows, total: mocks.rows.length },
    refetch: vi.fn(),
  }),
}));
vi.mock("@/api/datasetQuality", () => ({
  datasetQualityApi: { start: vi.fn(), review: vi.fn(), resume: vi.fn() },
}));
vi.mock("@/components/jobs/AsyncJobDetailModal", () => ({
  AsyncJobDetailModal: () => <p>统一任务详情</p>,
}));
const run: QualityRun = {
  id: "run-a",
  dataset_id: "dataset-a",
  async_job_id: "job-a",
  rule_version: "image-quality-1",
  config: { min_width: 64, min_height: 64, max_width: 8192, max_height: 8192 },
  status: "completed",
  total: 1,
  processed: 1,
  summary: { checked_count: 1, issue_count: 1 },
  error_code: null,
  created_at: "2026-10-23T02:00:00Z",
  completed_at: "2026-10-23T02:01:00Z",
};
const row: QualityResult = {
  id: "result-a",
  run_id: "run-a",
  item_id: "item-a",
  asset_id: "asset-a",
  file_name: "damaged.png",
  sha256: "a".repeat(64),
  size_bytes: 10,
  state: "checked",
  reason_code: "invalid_image",
  bytes_verified: true,
  findings: ["decode_failed"],
  width: null,
  height: null,
  observed_at: "2026-10-23T02:01:00Z",
  duplicate_count: 0,
  stale: false,
  review_state: "open",
  review_note: "",
  review_revision: 3,
  reviewed_by: null,
  reviewed_at: null,
};
function Location() {
  const location = useLocation(),
    navigate = useNavigate();
  return (
    <>
      <output data-testid="url">{location.search}</output>
      <button onClick={() => navigate("?tab=quality&quality_run=run-b")}>切换运行</button>
    </>
  );
}
function mount(url = "/datasets/dataset-a?tab=quality&quality_run=run-a") {
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <MemoryRouter initialEntries={[url]}>
        <DatasetQualityPanel datasetId="dataset-a" canManage />
        <Location />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
beforeEach(() => {
  vi.clearAllMocks();
  mocks.run = run;
  mocks.rows = [row];
  mocks.list = [run];
});
afterEach(cleanup);
describe("dataset quality review", () => {
  it("submits the full dataset with explicit image rules, independent of item filters", async () => {
    vi.mocked(datasetQualityApi.start).mockResolvedValue({ ...run, id: "new-run" });
    mount("/datasets/dataset-a?tab=quality&q=car&quality_run=run-a");
    fireEvent.click(screen.getByRole("button", { name: "新建检测" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText(/提交时固定整份数据集/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "开始检测" }));
    await waitFor(() =>
      expect(datasetQualityApi.start).toHaveBeenCalledWith("dataset-a", run.config),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByTestId("url").textContent).toContain("quality_run=new-run");
    expect(screen.getByTestId("url").textContent).toContain("q=car");
  });
  it("reviews the frozen result and optimistic revision; hints remain outside the select", async () => {
    vi.mocked(datasetQualityApi.review).mockResolvedValue({});
    mount();
    fireEvent.click(screen.getByRole("button", { name: "复核详情" }));
    const dialog = screen.getByRole("dialog");
    const select = within(dialog).getByLabelText("复核决定");
    expect(select).toHaveAccessibleDescription(/决定只记录在这份内容/);
    expect(select.textContent).not.toContain("决定只记录");
    fireEvent.change(select, { target: { value: "dismissed" } });
    fireEvent.change(within(dialog).getByLabelText("复核原因"), {
      target: { value: "经人工复核忽略" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存复核" }));
    await waitFor(() =>
      expect(datasetQualityApi.review).toHaveBeenCalledWith("dataset-a", "run-a", "result-a", {
        state: "dismissed",
        note: "经人工复核忽略",
        expected_revision: 3,
      }),
    );
  });
  it("retires a pending dialog callback after URL navigation", async () => {
    let resolve!: (value: QualityRun) => void;
    vi.mocked(datasetQualityApi.start).mockImplementation(
      () =>
        new Promise((r) => {
          resolve = r;
        }),
    );
    mount();
    fireEvent.click(screen.getByRole("button", { name: "新建检测" }));
    fireEvent.click(screen.getByRole("button", { name: "开始检测" }));
    await waitFor(() => expect(datasetQualityApi.start).toHaveBeenCalled());
    // Browser navigation is allowed while the request still belongs to its old dialog.
    fireEvent.click(screen.getByRole("button", { name: "切换运行", hidden: true }));
    await act(async () => resolve({ ...run, id: "late-run" }));
    expect(screen.getByTestId("url").textContent).toContain("quality_run=run-b");
    expect(screen.getByTestId("url").textContent).not.toContain("late-run");
  });
  it("closing a pending start retires its later navigation", async () => {
    let resolve!: (value: QualityRun) => void;
    vi.mocked(datasetQualityApi.start).mockReturnValue(
      new Promise((done) => {
        resolve = done;
      }),
    );
    mount();
    fireEvent.click(screen.getByRole("button", { name: "新建检测" }));
    fireEvent.click(screen.getByRole("button", { name: "开始检测" }));
    fireEvent.click(screen.getAllByRole("button", { name: "关闭" })[0]);
    await act(async () => resolve({ ...run, id: "late-run" }));
    expect(screen.getByTestId("url").textContent).toContain("quality_run=run-a");
    expect(screen.queryByRole("dialog")).toBeNull();
  });
  it("shrinking filtered results recovers an out-of-range page", async () => {
    mount("/datasets/dataset-a?tab=quality&quality_run=run-a&quality_review=open&quality_page=2");
    await waitFor(() =>
      expect(screen.getByTestId("url").textContent).not.toContain("quality_page"),
    );
    expect(screen.getByTestId("url").textContent).toContain("quality_review=open");
  });
  it("keeps an explicit missing run instead of falling back to another history entry", () => {
    mount("/datasets/dataset-a?tab=quality&quality_run=missing-run");
    expect(mocks.selected).toHaveBeenLastCalledWith("missing-run");
  });
  it("opens the shared job dialog and preserves item inspection URL context", () => {
    mount();
    fireEvent.click(screen.getByRole("button", { name: "任务详情" }));
    expect(screen.getByText("统一任务详情")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "damaged.png" }));
    expect(screen.getByTestId("url").textContent).toContain("item=item-a");
    expect(screen.getByTestId("url").textContent).toContain("quality_run=run-a");
  });
});
it("opens a paged exact duplicate group while keeping the selected run", () => {
  mocks.rows = [{ ...row, duplicate_count: 2 }];
  mount();
  fireEvent.click(screen.getByRole("button", { name: "精确重复 · 2 个成员" }));
  expect(screen.getByTestId("url").textContent).toContain(`quality_sha=${row.sha256}`);
  expect(screen.getByTestId("url").textContent).toContain("quality_run=run-a");
  fireEvent.click(screen.getByRole("button", { name: "退出重复组" }));
  expect(screen.getByTestId("url").textContent).not.toContain("quality_sha");
});

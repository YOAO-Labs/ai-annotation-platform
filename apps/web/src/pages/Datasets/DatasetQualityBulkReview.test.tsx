import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { DatasetQualityBulkReview } from "./DatasetQualityBulkReview";
import { datasetQualityReviewApi as api, type ReviewBatch } from "@/api/datasetQualityReview";
import type { QualityRun } from "@/api/datasetQuality";

const mock = vi.hoisted(() => ({
  batch: undefined as ReviewBatch | undefined,
  members: [] as unknown[],
  owner: vi.fn(),
}));
vi.mock("@/api/datasetQualityReview", () => ({
  datasetQualityReviewApi: { preview: vi.fn(), execute: vi.fn(), resume: vi.fn() },
}));
vi.mock("@/hooks/useDatasets", () => ({
  useDatasetOwner: () => ({ assert: mock.owner, revision: 1 }),
}));
vi.mock("@/hooks/useDatasetQualityReview", () => ({
  reviewBatchActive: (batch?: ReviewBatch) =>
    !!batch && ["pending", "running"].includes(batch.status),
  useReviewBatches: () => ({ data: { items: [], total: 0 } }),
  useReviewBatch: () => ({ data: mock.batch }),
  useReviewMembers: () => ({ data: { items: mock.members, total: mock.members.length } }),
}));
vi.mock("@/components/jobs/AsyncJobDetailModal", () => ({ AsyncJobDetailModal: () => null }));
const preview: ReviewBatch = {
  id: "batch-a",
  quality_run_id: "run-a",
  created_by: "actor",
  async_job_id: null,
  selection: { scope: "filtered" },
  proof: { rule_version: "image-quality-1" },
  scope_digest: "a".repeat(64),
  decision: "confirmed",
  note: "逐项核验",
  status: "preview",
  total: 101,
  actionable_count: 100,
  historical_count: 2,
  processed: 1,
  summary: { applied_count: 0, conflict_count: 0, blocked_count: 1, partial: true },
  error_code: null,
  created_at: "2026-10-23T00:00:00Z",
  expires_at: "2099-10-23T00:10:00Z",
  completed_at: null,
};
const props = {
  datasetId: "dataset",
  run: { id: "run-a", status: "completed" } as QualityRun,
  selectedIds: ["result-a", "result-b"],
  clearSelection: vi.fn(),
  query: { finding: "exact_duplicate" as const },
  context: "run-a/filter",
};
function Location() {
  const navigate = useNavigate();
  const location = useLocation();
  return (
    <>
      <output aria-label="当前地址">{location.search}</output>
      <button onClick={() => navigate("?tab=quality&quality_batch=batch-b")}>切换批次</button>
    </>
  );
}
function view(current = props, path = "/?tab=quality") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const content = (value: typeof props) => (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <DatasetQualityBulkReview {...value} />
        <Location />
      </MemoryRouter>
    </QueryClientProvider>
  );
  const rendered = render(content(current));
  return { ...rendered, rerenderProps: (value: typeof props) => rendered.rerender(content(value)) };
}
beforeEach(() => {
  vi.clearAllMocks();
  mock.batch = undefined;
  mock.members = [];
  mock.owner.mockImplementation(() => {});
  vi.mocked(api.preview).mockResolvedValue(preview);
  vi.mocked(api.execute).mockResolvedValue({ ...preview, status: "pending", async_job_id: "job" });
});
afterEach(cleanup);
function open() {
  fireEvent.click(screen.getByRole("button", { name: "批量复核" }));
  fireEvent.change(screen.getByLabelText("复核原因"), { target: { value: "逐项核验" } });
}

it("requires a real fixed preview before execution and submits frozen selected IDs", async () => {
  view();
  open();
  expect(screen.queryByRole("button", { name: "按固定范围执行" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "生成影响预览" }));
  await screen.findByRole("region", { name: "固定范围影响预览" });
  expect(api.preview).toHaveBeenCalledWith("dataset", "run-a", {
    scope: "selected",
    result_ids: ["result-a", "result-b"],
    query: { finding: "exact_duplicate" },
    state: "confirmed",
    note: "逐项核验",
  });
  expect(api.execute).not.toHaveBeenCalled();
  expect(screen.getByText(/包含 2 项历史证据/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "按固定范围执行" }));
  await waitFor(() => expect(api.execute).toHaveBeenCalledWith("dataset", "run-a", preview));
  await waitFor(() =>
    expect(screen.getByLabelText("当前地址").textContent).toContain("quality_batch=batch-a"),
  );
  expect(props.clearSelection).toHaveBeenCalledOnce();
});

it("all filtered results omit pagination and editing the reason invalidates preview", async () => {
  view();
  open();
  fireEvent.change(screen.getByLabelText("选择范围"), { target: { value: "filtered" } });
  fireEvent.click(screen.getByRole("button", { name: "生成影响预览" }));
  await screen.findByRole("region", { name: "固定范围影响预览" });
  expect(vi.mocked(api.preview).mock.calls[0][2]).toEqual({
    scope: "filtered",
    result_ids: [],
    query: { finding: "exact_duplicate" },
    state: "confirmed",
    note: "逐项核验",
  });
  fireEvent.change(screen.getByLabelText("复核原因"), { target: { value: "修改后必须重新预览" } });
  expect(screen.queryByRole("button", { name: "按固定范围执行" })).toBeNull();
  expect(screen.getByRole("button", { name: "生成影响预览" })).toBeTruthy();
});

it("retires a delayed preview when filter context changes", async () => {
  let resolve!: (value: ReviewBatch) => void;
  vi.mocked(api.preview).mockReturnValue(
    new Promise((done) => {
      resolve = done;
    }),
  );
  const rendered = view();
  open();
  fireEvent.click(screen.getByRole("button", { name: "生成影响预览" }));
  rendered.rerenderProps({ ...props, context: "run-a/new-filter" });
  await act(async () => resolve(preview));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(screen.getByLabelText("当前地址").textContent).not.toContain("quality_batch");
});

it("closing an in-flight confirmation cannot reopen its record", async () => {
  let resolve!: (value: ReviewBatch) => void;
  vi.mocked(api.execute).mockReturnValue(
    new Promise((done) => {
      resolve = done;
    }),
  );
  view();
  open();
  fireEvent.click(screen.getByRole("button", { name: "生成影响预览" }));
  fireEvent.click(await screen.findByRole("button", { name: "按固定范围执行" }));
  fireEvent.click(screen.getAllByRole("button", { name: "关闭" })[0]);
  await act(async () => resolve({ ...preview, status: "pending" }));
  expect(screen.getByLabelText("当前地址").textContent).not.toContain("quality_batch");
});

it("expired or entirely blocked previews cannot execute", async () => {
  vi.mocked(api.preview).mockResolvedValue({
    ...preview,
    actionable_count: 0,
    expires_at: "2000-01-01T00:00:00Z",
  });
  view();
  open();
  fireEvent.click(screen.getByRole("button", { name: "生成影响预览" }));
  const button = await screen.findByRole("button", { name: "按固定范围执行" });
  expect((button as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByText("预览已过期，请重新生成。")).toBeTruthy();
});

it("partial completion explains skipped conflicts and does not offer resume", () => {
  mock.batch = {
    ...preview,
    status: "partial",
    processed: 101,
    summary: { applied_count: 99, conflict_count: 1, blocked_count: 1, partial: true },
  };
  mock.members = [
    {
      id: "member",
      result_id: "result",
      snapshot: { file_name: "changed.png", review_revision: 2 },
      state: "conflict",
      applied_revision: null,
    },
  ];
  view(props, "/?tab=quality&quality_batch=batch-a");
  expect(screen.getByText("部分完成 · 已确认")).toBeTruthy();
  expect(screen.getByText("已跳过 · 证据或复核修订变化")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "恢复未完成项" })).toBeNull();
});

it("detection in progress disables bulk preview", () => {
  view({ ...props, run: { ...props.run, status: "running" } });
  expect((screen.getByRole("button", { name: "批量复核" }) as HTMLButtonElement).disabled).toBe(
    true,
  );
});

it("switching batch URL retires a delayed resume error", async () => {
  let reject!: (value: Error) => void;
  vi.mocked(api.resume).mockReturnValue(
    new Promise((_done, failed) => {
      reject = failed;
    }),
  );
  mock.batch = { ...preview, status: "cancelled", processed: 1 };
  view(props, "/?tab=quality&quality_batch=batch-a");
  fireEvent.click(screen.getByRole("button", { name: "恢复未完成项" }));
  fireEvent.click(screen.getByRole("button", { name: "切换批次", hidden: true }));
  await act(async () => reject(new Error("旧批次恢复失败")));
  expect(screen.getByLabelText("当前地址").textContent).toContain("batch-b");
  expect(screen.queryByText("旧批次恢复失败")).toBeNull();
  expect((screen.getByRole("button", { name: "恢复未完成项" }) as HTMLButtonElement).disabled).toBe(
    false,
  );
});

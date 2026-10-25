import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import { createTestQueryClient } from "@/test/queryClient";
import { StorageLocationSourceSummary } from "./StorageLocationSourceSummary";
const state = vi.hoisted(() => ({ role: "project_admin", session: "session", revision: 1 }));
const api = vi.hoisted(() => ({ locationPage: vi.fn(), sourceSummary: vi.fn() }));
vi.mock("@/api/datasetSources", () => ({ datasetSourcesApi: api }));
vi.mock("./StorageFaultsPanel", () => ({
  StorageFaultsPanel: ({ locationId }: { locationId: string }) => (
    <output aria-label="故障观测位置">{locationId}</output>
  ),
}));
vi.mock("@/hooks/usePermissions", () => ({ usePermissions: () => ({ role: state.role }) }));
vi.mock("@/hooks/useSessionGuard", () => ({ useSessionRevision: () => state.revision }));
vi.mock("@/stores/authStore", () => ({
  useAuthStore: (select: (value: unknown) => unknown) =>
    select({ user: { id: "user" }, sessionId: state.session }),
}));
vi.mock("@/components/jobs/AsyncJobDetailModal", () => ({
  AsyncJobDetailModal: ({ jobId }: { jobId: string }) => <output data-testid="job">{jobId}</output>,
}));
const run = {
  run_id: "run",
  scan_run_id: "scan",
  job_id: "job",
  status: "failed",
  phase: "enumerating",
  complete: false,
  matches_current_config: false,
  created_at: "2026-10-23T00:00:00Z",
  completed_at: null,
};
const diagnostics = {
  references: {
    scope: "stored_reference_items",
    total: 3,
    original_counts: {
      verified: 1,
      missing: 1,
      changed: 1,
      preview_only: 0,
      unavailable: 0,
      unverified: 0,
    },
    preview_counts: { ready: 1, failed: 0, unrecorded: 2, not_requested: 0, blocked: 0 },
    observed_from: "2026-10-22T00:00:00Z",
    observed_through: "2026-10-23T00:00:00Z",
    preview_recorded_at: null,
  },
  scan: {
    recent: run,
    last_complete: {
      ...run,
      run_id: "complete",
      scan_run_id: "old-scan",
      status: "completed",
      complete: true,
      completed_at: "2026-10-22T00:00:00Z",
    },
  },
  preflight: { recent: null, last_complete: null },
};
const usage = {
  scope: "visible_current_accepted_source_heads",
  source: "accepted_source_index",
  unit: "bytes",
  coverage: "complete",
  is_estimate: false,
  deduplication_basis: "location_frozen_address_and_object_version",
  includes_versions: false,
  reference_count: 2,
  file_count: 1,
  duplicate_reference_count: 1,
  known_size_bytes: 1536,
  total_size_bytes: 1536,
  unknown_reference_count: 0,
  conflict_file_count: 0,
  incomparable_file_count: 0,
  inactive_reference_count: 0,
  observed_from: null,
  observed_through: null,
  measured_at: "2026-10-25T03:00:00Z",
};
const clients: ReturnType<typeof createTestQueryClient>[] = [];
afterEach(() => clients.splice(0).forEach((client) => client.clear()));
function mount() {
  const client = createTestQueryClient();
  clients.push(client);
  const tree = () => (
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <StorageLocationSourceSummary />
      </MemoryRouter>
    </QueryClientProvider>
  );
  const view = render(tree());
  return { client, refresh: () => view.rerender(tree()) };
}
beforeEach(() => {
  vi.clearAllMocks();
  state.role = "project_admin";
  state.session = "session";
  state.revision = 1;
  api.locationPage.mockResolvedValue({
    items: [
      { id: "location", name: "CephFS", kind: "filesystem", enabled: true },
      { id: "s3", name: "S3", kind: "s3", enabled: false },
    ],
    total: 21,
  });
  api.sourceSummary.mockResolvedValue({
    usage,
    dataset_count: 2,
    total: 25,
    items: [
      {
        source_id: "source",
        source_name: "Fleet",
        dataset_id: "dataset",
        dataset_name: "Vehicles",
        enabled: false,
        unregistered_at: null,
        diagnostics,
      },
    ],
    jobs: {
      total: 22,
      items: [
        { id: "job", kind: "dataset_scan", status: "failed", created_at: "2026-10-23T00:00:00Z" },
      ],
    },
  });
});
it("queries one selected location, paginates sources/jobs independently and uses existing job details", async () => {
  mount();
  await screen.findByRole("option", { name: "CephFS · filesystem" });
  expect(api.sourceSummary).not.toHaveBeenCalled();
  fireEvent.change(screen.getByRole("combobox", { name: "查看关联来源的存储位置" }), {
    target: { value: "location" },
  });
  const link = await screen.findByRole("link", { name: "Vehicles / Fleet" });
  expect(link).toHaveAttribute("href", "/datasets/dataset?tab=sources&source=source");
  expect(screen.getByText("关联来源 · 25")).toBeInTheDocument();
  expect(screen.getByLabelText("故障观测位置")).toHaveTextContent("location");
  fireEvent.click(screen.getByRole("button", { name: "下一页来源" }));
  await waitFor(() =>
    expect(api.sourceSummary).toHaveBeenCalledWith("location", 20, 0, expect.any(AbortSignal)),
  );
  fireEvent.click(await screen.findByText("关联后台任务历史 · 22"));
  await waitFor(() => expect(screen.getByRole("button", { name: "下一页任务" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "下一页任务" }));
  await waitFor(() =>
    expect(api.sourceSummary).toHaveBeenCalledWith("location", 20, 20, expect.any(AbortSignal)),
  );
  fireEvent.click(await screen.findByRole("button", { name: /扫描 · 失败/ }));
  expect(screen.getByTestId("job")).toHaveTextContent("job");
  fireEvent.change(screen.getByRole("combobox", { name: "查看关联来源的存储位置" }), {
    target: { value: "s3" },
  });
  await waitFor(() =>
    expect(api.sourceSummary).toHaveBeenCalledWith("s3", 0, 0, expect.any(AbortSignal)),
  );
  expect(screen.queryByTestId("job")).not.toBeInTheDocument();
});
it("paginates all backend locations and clears selection on session change", async () => {
  const view = mount();
  await screen.findByRole("option", { name: "S3 · s3（停用）" });
  fireEvent.click(screen.getByRole("button", { name: "下一页位置" }));
  await waitFor(() => expect(api.locationPage).toHaveBeenCalledWith(20, expect.any(AbortSignal)));
  state.session = "new";
  state.revision++;
  view.refresh();
  await waitFor(() =>
    expect(api.locationPage).toHaveBeenLastCalledWith(0, expect.any(AbortSignal)),
  );
  expect(screen.getByRole("combobox", { name: "查看关联来源的存储位置" })).toHaveValue("");
  expect(api.sourceSummary).not.toHaveBeenCalled();
});

it("links a super administrator to the current source fault history", async () => {
  state.role = "super_admin";
  mount();
  await screen.findByRole("option", { name: "CephFS · filesystem" });
  fireEvent.change(screen.getByRole("combobox", { name: "查看关联来源的存储位置" }), {
    target: { value: "location" },
  });
  const link = await screen.findByRole("link", { name: "查看此来源故障记录" });
  expect(link).toHaveAttribute(
    "href",
    "/audit?action=storage.fault&target_type=storage_location&detail_key=source_id&detail_value=source",
  );
});

it("keeps the independent fault view available when source-summary loading fails", async () => {
  api.sourceSummary.mockRejectedValue({ status: 503 });
  mount();
  await screen.findByRole("option", { name: "CephFS · filesystem" });
  fireEvent.change(screen.getByRole("combobox", { name: "查看关联来源的存储位置" }), {
    target: { value: "location" },
  });
  expect(await screen.findByLabelText("故障观测位置")).toHaveTextContent("location");
});

it("does not expose global audit navigation to a project administrator", async () => {
  mount();
  await screen.findByRole("option", { name: "CephFS · filesystem" });
  fireEvent.change(screen.getByRole("combobox", { name: "查看关联来源的存储位置" }), {
    target: { value: "location" },
  });
  await screen.findByRole("link", { name: "Vehicles / Fleet" });
  expect(screen.queryByRole("link", { name: "查看此来源故障记录" })).not.toBeInTheDocument();
});
it("does not query or render management navigation for an annotator", () => {
  state.role = "annotator";
  mount();
  expect(api.locationPage).not.toHaveBeenCalled();
  expect(screen.queryByRole("region", { name: "存储位置关联来源" })).not.toBeInTheDocument();
});

it("clears retained private source names and task detail after location access is revoked", async () => {
  const view = mount();
  await screen.findByRole("option", { name: "CephFS · filesystem" });
  fireEvent.change(screen.getByRole("combobox", { name: "查看关联来源的存储位置" }), {
    target: { value: "location" },
  });
  await screen.findByRole("link", { name: "Vehicles / Fleet" });
  expect(await screen.findByText("1.5 KiB")).toBeInTheDocument();
  fireEvent.click(await screen.findByRole("button", { name: "扫描任务详情" }));
  expect(screen.getByTestId("job")).toBeInTheDocument();
  api.sourceSummary.mockRejectedValue(Object.assign(new Error("Access revoked"), { status: 403 }));
  await act(async () => {
    await view.client.invalidateQueries({ queryKey: ["storage-source-summary"] });
  });
  await screen.findByRole("alert");
  expect(screen.queryByRole("link", { name: "Vehicles / Fleet" })).not.toBeInTheDocument();
  expect(screen.queryByRole("region", { name: "当前已采纳源索引规模" })).not.toBeInTheDocument();
  expect(screen.queryByTestId("job")).not.toBeInTheDocument();
});

it("separates saved original/preview evidence and preserves current and successful scan links", async () => {
  mount();
  await screen.findByRole("option", { name: "CephFS · filesystem" });
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "location" } });
  await screen.findByText("原件最近观测");
  expect(screen.getByText("失踪 1 · 内容变化 1 · 不可读 0")).toBeInTheDocument();
  expect(screen.getByText("已有记录 1 · 未生成或记录失效 2")).toBeInTheDocument();
  expect(screen.getByText("扫描未完整 · 历史配置")).toBeInTheDocument();
  expect(screen.getByText("尚无预检 / 登记记录")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "查看轮次" })).toHaveAttribute(
    "href",
    "/datasets/dataset?tab=sources&source=source&scan=scan",
  );
  expect(screen.getByRole("link", { name: "查看成功轮次" })).toHaveAttribute(
    "href",
    "/datasets/dataset?tab=sources&source=source&scan=old-scan",
  );
  fireEvent.click(screen.getByText("观测范围与数量明细"));
  expect(screen.getByText(/包含保留的历史修订/)).toBeInTheDocument();
  expect(screen.queryByText("生成失败 0")).not.toBeInTheDocument();
});

it("marks retained results when a refresh fails and offers a read-only retry", async () => {
  mount();
  await screen.findByRole("option", { name: "CephFS · filesystem" });
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "location" } });
  await screen.findByText("原件最近观测");
  api.sourceSummary.mockRejectedValue(new Error("offline"));
  fireEvent.click(screen.getByRole("button", { name: "刷新诊断" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("诊断刷新失败");
  expect(screen.getByRole("link", { name: "Vehicles / Fleet" })).toBeInTheDocument();
  expect(screen.getByText("1.5 KiB")).toBeInTheDocument();
});

it("keeps the empty reference scope distinct from healthy originals or missing previews", async () => {
  const response = await api.sourceSummary();
  api.sourceSummary.mockResolvedValue({
    ...response,
    items: [
      {
        ...response.items[0],
        diagnostics: { ...diagnostics, references: { ...diagnostics.references, total: 0 } },
      },
    ],
  });
  mount();
  await screen.findByRole("option", { name: "CephFS · filesystem" });
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "location" } });
  await screen.findByText("尚无原地引用样本");
  expect(screen.getByText("暂无可统计样本")).toBeInTheDocument();
  expect(screen.queryByText(/已有记录 1/)).not.toBeInTheDocument();
});

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { DirectoryRegistrationProgress } from "./DirectoryRegistrationProgress";

const api = vi.hoisted(() => ({
  registration: vi.fn(),
  registrationEntries: vi.fn(),
  changes: vi.fn(),
  confirmRegistration: vi.fn(),
}));
vi.mock("@/api/datasetSources", () => ({ datasetSourcesApi: api }));
vi.mock("@/hooks/useSessionGuard", () => ({ useSessionGuard: () => () => () => true }));
beforeEach(() => {
  api.registration.mockResolvedValue({
    id: "reg",
    source_revision: 1,
    phase: "preflight",
    status: "completed",
    comparison_available: true,
    comparison_complete: true,
    created_at: "2026-10-23T00:00:00Z",
    ownership_mode: "reference",
    cache_policy: "none",
    counts: { ready_count: 1, registered_count: 1, unchanged_count: 0 },
  });
  api.registrationEntries.mockResolvedValue({ total: 0, items: [] });
  api.confirmRegistration.mockResolvedValue({});
});
function mount(remaining: number) {
  api.changes.mockResolvedValue({
    complete: true,
    total: 0,
    items: [],
    counts: { added: 1, unchanged: 1 },
    remaining_added_count: remaining,
  });
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <DirectoryRegistrationProgress
        scanId="scan"
        datasetId="dataset"
        owner="session"
        sourceRevision={1}
      />
    </QueryClientProvider>,
  );
}
describe("重定位后的新增接纳", () => {
  it("纯预检完成后刷新工作台、分页和检查器，保留其他数据集缓存", async () => {
    api.registration.mockResolvedValue({
      id: "reg",
      source_revision: 1,
      generation: 2,
      phase: "preflight",
      status: "completed",
      comparison_complete: true,
      counts: { ready_count: 0, registered_count: 0, unchanged_count: 0 },
    });
    api.changes.mockResolvedValue({ complete: true, total: 0, items: [], counts: {} });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const keys = [
      ["dataset-workbench", "dataset", "session"],
      ["dataset-items", "dataset", { limit: 20 }],
      ["dataset-item", "dataset", "item"],
    ];
    for (const key of keys) client.setQueryData(key, { previous: true });
    client.setQueryData(["dataset-workbench", "other"], { previous: true });
    const view = render(
      <QueryClientProvider client={client}>
        <DirectoryRegistrationProgress
          scanId="scan"
          datasetId="dataset"
          owner="session"
          sourceRevision={1}
        />
      </QueryClientProvider>,
    );
    await waitFor(() => {
      for (const key of keys) expect(client.getQueryState(key)?.isInvalidated).toBe(true);
    });
    expect(client.getQueryState(["dataset-workbench", "other"])?.isInvalidated).toBe(false);
    view.unmount();
    client.clear();
  });

  it("冻结added计数保留，但全部目标已消费时禁止再次登记", async () => {
    mount(0);
    await screen.findByText(/没有待登记的新增路径/);
    fireEvent.click(screen.getByRole("checkbox", { name: /已核对差异/ }));
    expect(screen.getByRole("button", { name: "确认仅登记新增" })).toBeDisabled();
    expect(api.confirmRegistration).not.toHaveBeenCalled();
  });
  it("移动后仍有未消费新增时可确认", async () => {
    mount(1);
    const checkbox = await screen.findByRole("checkbox", { name: /已核对差异/ });
    expect(screen.getByLabelText("本轮增量差异").closest("details")).toBeNull();
    const records = screen.getByText("预检与登记明细", { exact: false }).closest("details")!;
    expect(records).not.toHaveAttribute("open");
    fireEvent.click(records.querySelector("summary")!);
    expect(records).toHaveAttribute("open");
    await waitFor(() => expect(screen.queryByText(/正在刷新差异/)).not.toBeInTheDocument());
    fireEvent.click(checkbox);
    expect(screen.getByRole("button", { name: "确认仅登记新增" })).toBeEnabled();
  });
});

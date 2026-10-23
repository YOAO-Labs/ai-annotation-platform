import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import { createTestQueryClient } from "@/test/queryClient";
import { StorageLocationSourceSummary } from "./StorageLocationSourceSummary";
const state = vi.hoisted(() => ({ role: "project_admin", session: "session", revision: 1 }));
const api = vi.hoisted(() => ({ locationPage: vi.fn(), sourceSummary: vi.fn() }));
vi.mock("@/api/datasetSources", () => ({ datasetSourcesApi: api }));
vi.mock("@/hooks/usePermissions", () => ({ usePermissions: () => ({ role: state.role }) }));
vi.mock("@/hooks/useSessionGuard", () => ({ useSessionRevision: () => state.revision }));
vi.mock("@/stores/authStore", () => ({
  useAuthStore: (select: (value: unknown) => unknown) =>
    select({ user: { id: "user" }, sessionId: state.session }),
}));
vi.mock("@/components/jobs/AsyncJobDetailModal", () => ({
  AsyncJobDetailModal: ({ jobId }: { jobId: string }) => <output data-testid="job">{jobId}</output>,
}));
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
  fireEvent.click(screen.getByRole("button", { name: "下一页来源" }));
  await waitFor(() =>
    expect(api.sourceSummary).toHaveBeenCalledWith("location", 20, 0, expect.any(AbortSignal)),
  );
  await waitFor(() => expect(screen.getByRole("button", { name: "下一页任务" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "下一页任务" }));
  await waitFor(() =>
    expect(api.sourceSummary).toHaveBeenCalledWith("location", 20, 20, expect.any(AbortSignal)),
  );
  fireEvent.click(await screen.findByRole("button", { name: /扫描 · failed/ }));
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
  fireEvent.click(await screen.findByRole("button", { name: /扫描 · failed/ }));
  expect(screen.getByTestId("job")).toBeInTheDocument();
  api.sourceSummary.mockRejectedValue(Object.assign(new Error("Access revoked"), { status: 403 }));
  await act(async () => {
    await view.client.invalidateQueries({ queryKey: ["storage-source-summary"] });
  });
  await screen.findByRole("alert");
  expect(screen.queryByRole("link", { name: "Vehicles / Fleet" })).not.toBeInTheDocument();
  expect(screen.queryByTestId("job")).not.toBeInTheDocument();
});

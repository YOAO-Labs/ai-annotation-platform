import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { createTestQueryClient } from "@/test/queryClient";
import { DirectoryReferencePanel } from "./DirectoryReferencePanel";
const state = vi.hoisted(() => ({ session: "session", revision: 1 }));
const api = vi.hoisted(() => ({
  sources: vi.fn(),
  source: vi.fn(),
  locations: vi.fn(),
  scans: vi.fn(),
  scan: vi.fn(),
  registration: vi.fn(),
  activeRegistration: vi.fn(),
  entries: vi.fn(),
}));
vi.mock("@/api/datasetSources", () => ({ datasetSourcesApi: api }));
vi.mock("@/hooks/useSessionGuard", () => ({
  useSessionGuard: () => () => () => true,
  useSessionRevision: () => state.revision,
}));
vi.mock("@/stores/authStore", () => ({
  useAuthStore: (select: (value: unknown) => unknown) =>
    select({ user: { id: "user" }, sessionId: state.session }),
}));
vi.mock("./DirectorySourceControls", () => ({ DirectorySourceControls: () => null }));
vi.mock("./DirectorySyncSchedule", () => ({ DirectorySyncSchedule: () => null }));
vi.mock("./DirectoryRegistrationProgress", () => ({
  DirectoryRegistrationProgress: ({ scanId, readOnly }: { scanId: string; readOnly: boolean }) => (
    <output data-testid="registration">
      {scanId}/{String(readOnly)}
    </output>
  ),
}));
const source = {
  id: "source",
  dataset_id: "dataset",
  name: "Source",
  enabled: true,
  config_revision: 2,
  location_id: "location",
  relative_path: "",
  unregistered_at: null,
};
const failed = {
  id: "failed",
  source_id: "source",
  source_revision: 2,
  created_at: "2026-10-23T01:00:00Z",
  status: "failed",
  complete: false,
  counts: {},
};
const complete = { ...failed, id: "complete", status: "completed", complete: true };
const clients: ReturnType<typeof createTestQueryClient>[] = [];
afterEach(() => clients.splice(0).forEach((client) => client.clear()));
function mount(props = {}) {
  const client = createTestQueryClient();
  clients.push(client);
  const tree = () => (
    <QueryClientProvider client={client}>
      <DirectoryReferencePanel datasetId="dataset" {...props} />
    </QueryClientProvider>
  );
  const view = render(tree());
  return { ...view, refresh: () => view.rerender(tree()) };
}
beforeEach(() => {
  vi.clearAllMocks();
  state.session = "session";
  state.revision = 1;
  api.sources.mockResolvedValue({ items: [source], total: 1 });
  api.source.mockResolvedValue(source);
  api.locations.mockResolvedValue({ items: [], total: 0 });
  api.scans.mockImplementation(async (_id, _signal, options) =>
    options?.complete
      ? { items: [complete], total: 1 }
      : options?.active_only
        ? { items: [], total: 0 }
        : options?.offset
          ? { items: [complete], total: 21 }
          : { items: [failed], total: 21 },
  );
  api.scan.mockResolvedValue(complete);
  api.entries.mockResolvedValue({ items: [], total: 0 });
  api.registration.mockResolvedValue(null);
  api.activeRegistration.mockResolvedValue(null);
});
it("keeps last complete independent of newer failures and paginates retained history", async () => {
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "查看最近完整扫描" }));
  expect(await screen.findByTestId("registration")).toHaveTextContent("complete/true");
  fireEvent.click(screen.getByRole("button", { name: "下一页扫描" }));
  await waitFor(() =>
    expect(api.scans).toHaveBeenCalledWith("source", expect.any(AbortSignal), { offset: 20 }),
  );
  expect(await screen.findByText(/第 2 页 · 共 21 次/)).toBeInTheDocument();
  expect(screen.getByTestId("registration")).toHaveTextContent("complete/true");
});
it("opens a linked source beyond the first200 list rows and its exact historical scan", async () => {
  api.sources.mockResolvedValue({ items: [], total: 201 });
  mount({ initialSourceId: "source", initialScanId: "complete" });
  expect(await screen.findByRole("option", { name: "Source" })).toBeInTheDocument();
  expect(await screen.findByTestId("registration")).toHaveTextContent("complete/true");
  expect(api.source).toHaveBeenCalledWith("source", expect.any(AbortSignal));
});
it("shows resumed older active work while history is selected and blocks starting another scan", async () => {
  api.scans.mockImplementation(async (_id, _signal, options) =>
    options?.active_only
      ? { items: [{ ...failed, id: "older-active", status: "running" }], total: 1 }
      : options?.complete
        ? { items: [complete], total: 1 }
        : { items: [failed], total: 21 },
  );
  mount({ initialSourceId: "source", initialScanId: "complete" });
  expect(await screen.findByText("当前执行：扫描 running")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "只读预扫描" })).toBeDisabled();
  expect(await screen.findByTestId("registration")).toHaveTextContent("complete/true");
});
it("keeps inactive source registration and differences visible in read-only mode", async () => {
  api.sources.mockResolvedValue({ items: [{ ...source, enabled: false }], total: 1 });
  api.scans.mockImplementation(async (_id, _signal, options) =>
    options?.active_only ? { items: [], total: 0 } : { items: [complete], total: 1 },
  );
  mount();
  expect(await screen.findByTestId("registration")).toHaveTextContent("complete/true");
  expect(screen.getByRole("button", { name: "只读预扫描" })).toBeDisabled();
  expect(screen.getByText(/来源已停用或注销/)).toBeInTheDocument();
});
it.each(["source", "scan"])(
  "rejects mismatched %s links without loading unrelated entries",
  async (kind) => {
    if (kind === "source") api.source.mockResolvedValue({ ...source, dataset_id: "other" });
    else api.scan.mockResolvedValue({ ...complete, source_id: "other" });
    mount({ initialSourceId: "source", initialScanId: "complete" });
    expect(await screen.findByRole("alert")).toHaveTextContent("不匹配");
    expect(api.entries).not.toHaveBeenCalled();
    expect(screen.queryByTestId("registration")).not.toBeInTheDocument();
  },
);
it("retires late focused-source data on an authentication transition", async () => {
  let resolve!: (value: unknown) => void;
  api.source.mockReturnValueOnce(
    new Promise((done) => {
      resolve = done;
    }),
  );
  const view = mount({ initialSourceId: "source" });
  await waitFor(() => expect(api.source).toHaveBeenCalledTimes(1));
  api.source.mockResolvedValue({ ...source, name: "New session source" });
  state.session = "next";
  state.revision++;
  view.refresh();
  await screen.findByRole("option", { name: "New session source" });
  await act(async () => resolve({ ...source, name: "Retired source" }));
  expect(screen.queryByRole("option", { name: "Retired source" })).not.toBeInTheDocument();
});

vi.mock("@/components/jobs/AsyncJobDetailModal", () => ({ AsyncJobDetailModal: () => null }));
it("tracks active registration on an older scan independently of the newest scan", async () => {
  api.activeRegistration.mockResolvedValue({ id: "older-registration", status: "running" });
  mount();
  expect(await screen.findByText("当前执行：预检 / 登记 running")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "只读预扫描" })).toBeDisabled();
});
it("refreshes last-complete and history when an older resumed scan finishes", async () => {
  let finished = false;
  api.scans.mockImplementation(async (_id, _signal, options) =>
    options?.active_only
      ? {
          items: finished ? [] : [{ ...failed, id: "older-active", status: "running" }],
          total: finished ? 0 : 1,
        }
      : options?.complete
        ? { items: finished ? [complete] : [], total: finished ? 1 : 0 }
        : { items: [failed], total: 21 },
  );
  const client = createTestQueryClient();
  clients.push(client);
  render(
    <QueryClientProvider client={client}>
      <DirectoryReferencePanel datasetId="dataset" />
    </QueryClientProvider>,
  );
  await screen.findByText("当前执行：扫描 running");
  expect(screen.queryByRole("button", { name: "查看最近完整扫描" })).not.toBeInTheDocument();
  finished = true;
  await act(async () => {
    await client.invalidateQueries({
      queryKey: ["dataset-scans", "user/session/dataset", "source", "active"],
    });
  });
  expect(await screen.findByRole("button", { name: "查看最近完整扫描" })).toBeInTheDocument();
  expect(screen.getByText("当前执行：无")).toBeInTheDocument();
});
it.each(["scan", "registration"])(
  "blocks new work when current %s state cannot be read",
  async (kind) => {
    if (kind === "registration") api.activeRegistration.mockRejectedValue(new Error("offline"));
    else {
      const original = api.scans.getMockImplementation()!;
      api.scans.mockImplementation((id, signal, options) =>
        options?.active_only ? Promise.reject(new Error("offline")) : original(id, signal, options),
      );
    }
    mount();
    expect(await screen.findByText("当前执行：无法确认，请刷新")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "只读预扫描" })).toBeDisabled();
    expect(screen.getByRole("alert")).toBeInTheDocument();
  },
);
it("waits for registration state before enabling a new scan", async () => {
  let resolve!: (value: null) => void;
  api.activeRegistration.mockReturnValue(
    new Promise((done) => {
      resolve = done;
    }),
  );
  mount();
  await waitFor(() => expect(api.activeRegistration).toHaveBeenCalled());
  expect(screen.getByText("当前执行：读取中…")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "只读预扫描" })).toBeDisabled();
  await act(async () => resolve(null));
  await waitFor(() => expect(screen.getByRole("button", { name: "只读预扫描" })).toBeEnabled());
});

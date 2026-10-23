import { expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { DirectoryReferencePanel } from "./DirectoryReferencePanel";
const api = vi.hoisted(() => ({
  sources: vi.fn(),
  locations: vi.fn(),
  scans: vi.fn(),
  syncSchedule: vi.fn(),
  activeRegistration: vi.fn().mockResolvedValue(null),
  previewRules: vi.fn(),
  applyRules: vi.fn(),
  configureSync: vi.fn(),
}));
vi.mock("@/api/datasetSources", () => ({ datasetSourcesApi: api }));
vi.mock("@/hooks/useSessionGuard", () => ({
  useSessionGuard: () => () => () => true,
  useSessionRevision: () => 1,
}));
vi.mock("@/stores/authStore", () => ({
  useAuthStore: (selector: (value: unknown) => unknown) =>
    selector({ user: { id: "user" }, sessionId: "session" }),
}));
it("refreshes a paused schedule revision after saving source rules before re-enabling", async () => {
  let changed = false;
  const source = {
    id: "source",
    cache_policy: "none",
    dataset_id: "dataset",
    enabled: true,
    location_id: "location",
    name: "source",
    node_id: "node",
    ownership_mode: "reference",
    policy_revision: 1,
    relative_path: "root",
    config_revision: 2,
    recursive: true,
    include_globs: ["*"],
    exclude_globs: [],
    unregistered_at: null,
  };
  api.sources.mockImplementation(async () => ({
    items: [{ ...source, config_revision: changed ? 3 : 2 }],
    total: 1,
  }));
  api.locations.mockResolvedValue({ items: [], total: 0 });
  api.scans.mockResolvedValue({ items: [], total: 0 });
  api.syncSchedule.mockImplementation(async () => ({
    revision: changed ? 2 : 1,
    enabled: false,
    state: changed ? "blocked" : "idle",
    interval_minutes: 60,
    new_file_policy: "manual",
    timezone: "UTC",
    last_error: changed ? "configuration_changed" : null,
  }));
  api.previewRules.mockResolvedValue({
    proof: "proof",
    total: 0,
    counts: {},
    active_attempt_count: 0,
    items: [],
  });
  api.applyRules.mockImplementation(async () => {
    changed = true;
    return { ...source, config_revision: 3 };
  });
  api.configureSync.mockResolvedValue({});
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <DirectoryReferencePanel datasetId="dataset" />
    </QueryClientProvider>,
  );
  await screen.findByRole("button", { name: "启用周期扫描" });
  fireEvent.change(screen.getByRole("textbox", { name: "新的排除规则 1" }), {
    target: { value: "hidden" },
  });
  fireEvent.click(screen.getByRole("button", { name: "预览规则影响" }));
  fireEvent.click(await screen.findByRole("checkbox", { name: /已核对影响/ }));
  fireEvent.click(screen.getByRole("button", { name: "确认保存扫描规则" }));
  await waitFor(() => expect(api.syncSchedule.mock.calls.length).toBeGreaterThanOrEqual(2));
  await screen.findByText(/来源或位置配置已变化，请核对后重新启用/);
  fireEvent.click(screen.getByRole("button", { name: "启用周期扫描" }));
  await waitFor(() =>
    expect(api.configureSync).toHaveBeenCalledWith("source", {
      expected_revision: 2,
      enabled: true,
      interval_minutes: 60,
      new_file_policy: "manual",
      timezone: "UTC",
    }),
  );
});

vi.mock("@/components/jobs/AsyncJobDetailModal", () => ({ AsyncJobDetailModal: () => null }));

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type {
  StorageLocationOut,
  StorageMaintenanceImpact as Impact,
} from "@/api/generated/types.gen";
import { createTestQueryClient } from "@/test/queryClient";
import { StorageMaintenanceImpact } from "./StorageMaintenanceImpact";

const auth = vi.hoisted(() => ({ role: "super_admin", session: "one", revision: 1 }));
const api = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock("@/api/client", async (original) => ({
  ...(await original<typeof import("@/api/client")>()),
  apiClient: api,
}));
vi.mock("@/hooks/usePermissions", () => ({ usePermissions: () => ({ role: auth.role }) }));
vi.mock("@/hooks/useSessionGuard", () => ({ useSessionRevision: () => auth.revision }));
vi.mock("@/stores/authStore", () => ({
  useAuthStore: (select: (state: unknown) => unknown) => select({ sessionId: auth.session }),
}));

const location = {
  id: "place",
  name: "受控目录",
  kind: "filesystem",
  config_revision: 1,
} as StorageLocationOut;
const result: Impact = {
  location_id: "place",
  config_revision: 1,
  measured_at: "2026-10-24T02:00:00Z",
  scope: "stored_dependencies",
  frozen_coverage: "recorded_location_lineage_only",
  input_coverage: "current_task_links_only",
  execution_permission: false,
  counts: {
    sources: 23,
    enabled_sources: 21,
    unregistered_sources: 1,
    items: 5,
    reference_items: 3,
    datasets: 2,
    projects: 4,
    tasks: 7,
    annotations: 11,
    active_scans: 1,
    active_registrations: 2,
    enabled_schedules: 1,
    ready_previews: 3,
    failed_previews: 1,
    preview_bytes: 1024,
    cache_entries: 2,
    cache_bytes: 2048,
    active_cache_read_leases: 1,
    active_input_leases: 2,
    cleanup_input_leases: 1,
    ready_lineage_versions: 1,
    unfinished_lineage_versions: 1,
  },
};
const clients: ReturnType<typeof createTestQueryClient>[] = [];
function mount(initial = location) {
  let current = initial;
  const client = createTestQueryClient();
  clients.push(client);
  const tree = () => (
    <QueryClientProvider client={client}>
      <StorageMaintenanceImpact location={current} />
    </QueryClientProvider>
  );
  const view = render(tree());
  return {
    refresh: (next = current) => {
      current = next;
      view.rerender(tree());
    },
  };
}
function expand() {
  fireEvent.click(screen.getByText("维护影响", { selector: "summary" }));
}
beforeEach(() => {
  vi.clearAllMocks();
  auth.role = "super_admin";
  auth.session = "one";
  auth.revision = 1;
  api.get.mockResolvedValue(result);
});
afterEach(() => clients.splice(0).forEach((client) => client.clear()));

it("loads only when expanded and exposes record counts and partial coverage", async () => {
  mount();
  expect(api.get).not.toHaveBeenCalled();
  expand();
  expect(await screen.findByText("23 个 · 启用 21 · 已注销 1")).toBeInTheDocument();
  expect(screen.getByText("2 个数据集 · 4 个项目")).toBeInTheDocument();
  expect(screen.getByText("就绪 1 · 未完成 1")).toBeInTheDocument();
  expect(screen.getByText(/托管和缺少位置血缘的历史未计入/)).toBeInTheDocument();
  expect(screen.getByText(/旧完整缓存将失配/)).toBeInTheDocument();
  expect(api.get).toHaveBeenCalledTimes(1);
  expect(api.get.mock.calls[0][0]).toBe("/storage-locations/place/maintenance-impact");
});

it("refreshes the read query and retains dated evidence after a network failure", async () => {
  mount();
  expand();
  await screen.findByText("7 个任务 · 11 条标注");
  api.get.mockRejectedValue(new Error("temporary outage"));
  fireEvent.click(screen.getByRole("button", { name: "刷新维护盘点" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("刷新失败，保留上次盘点");
  expect(screen.getByText("7 个任务 · 11 条标注")).toBeInTheDocument();
  expect(screen.getByText(/统计于/)).toBeInTheDocument();
  expect(api.get).toHaveBeenCalledTimes(2);
});

it.each([401, 403, 404])(
  "hides retained dependencies after access fails with %i",
  async (status) => {
    mount();
    expand();
    await screen.findByText("7 个任务 · 11 条标注");
    api.get.mockRejectedValue(Object.assign(new Error("Access revoked"), { status }));
    fireEvent.click(screen.getByRole("button", { name: "刷新维护盘点" }));
    await screen.findByRole("alert");
    expect(screen.queryByText("7 个任务 · 11 条标注")).not.toBeInTheDocument();
  },
);

it("does not expose the panel to other roles or platform bindings", () => {
  auth.role = "project_admin";
  const view = mount();
  expect(screen.queryByText("维护影响")).not.toBeInTheDocument();
  auth.role = "super_admin";
  view.refresh({ ...location, kind: "s3" });
  expect(screen.queryByText("维护影响")).not.toBeInTheDocument();
  expect(api.get).not.toHaveBeenCalled();
});

it("drops pending results and expansion when the authenticated session changes", async () => {
  let complete!: (value: Impact) => void;
  api.get.mockReturnValueOnce(
    new Promise<Impact>((resolve) => {
      complete = resolve;
    }),
  );
  const view = mount();
  expand();
  await waitFor(() => expect(api.get).toHaveBeenCalledTimes(1));
  auth.session = "two";
  auth.revision = 2;
  view.refresh();
  await act(async () => complete(result));
  expect(screen.queryByText("23 个 · 启用 21 · 已注销 1")).not.toBeInTheDocument();
  expect(screen.getByText("维护影响").closest("details")).not.toHaveAttribute("open");
});

it("does not carry old evidence into another location or configuration revision", async () => {
  const view = mount();
  expand();
  await screen.findByText("7 个任务 · 11 条标注");
  view.refresh({ ...location, config_revision: 2 });
  expect(screen.queryByText("7 个任务 · 11 条标注")).not.toBeInTheDocument();
  api.get.mockResolvedValue({
    ...result,
    location_id: "next",
    config_revision: 2,
    counts: { ...result.counts, sources: 0, enabled_sources: 0, unregistered_sources: 0 },
  });
  view.refresh({ ...location, id: "next", config_revision: 2 });
  expand();
  expect(await screen.findByText("0 个 · 启用 0 · 已注销 0")).toBeInTheDocument();
  expect(api.get.mock.calls[api.get.mock.calls.length - 1]?.[0]).toBe(
    "/storage-locations/next/maintenance-impact",
  );
});

it("explains a changed configuration instead of showing mismatched evidence", async () => {
  api.get.mockResolvedValue({ ...result, config_revision: 2 });
  mount();
  expand();
  expect(await screen.findByRole("alert")).toHaveTextContent("位置配置已变化");
  expect(screen.queryByText("7 个任务 · 11 条标注")).not.toBeInTheDocument();
});

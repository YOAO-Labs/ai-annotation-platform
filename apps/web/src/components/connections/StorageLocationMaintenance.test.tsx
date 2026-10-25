import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type {
  StorageLocationOut,
  StorageMaintenancePreview,
  StorageMaintenanceStatus,
} from "@/api/generated/types.gen";
import type { StorageMaintenanceOperation } from "@/api/storageMaintenance";
import { createTestQueryClient } from "@/test/queryClient";
import { StorageLocationMaintenance } from "./StorageLocationMaintenance";

const auth = vi.hoisted(() => ({ role: "super_admin", session: "one", revision: 1 }));
const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
const toast = vi.hoisted(() => vi.fn());
vi.mock("@/api/client", async (original) => ({
  ...(await original<typeof import("@/api/client")>()),
  apiClient: api,
}));
vi.mock("@/hooks/usePermissions", () => ({ usePermissions: () => ({ role: auth.role }) }));
vi.mock("@/stores/authSession", () => ({
  getAuthSessionRevision: () => auth.revision,
  subscribeAuthSession: () => () => {},
}));
vi.mock("@/stores/authStore", () => ({
  useAuthStore: (select: (state: unknown) => unknown) => select({ sessionId: auth.session }),
}));
vi.mock("@/components/ui/Toast", () => ({
  useToastStore: (select: (state: unknown) => unknown) => select({ push: toast }),
}));
const location = {
  id: "place",
  name: "受控目录",
  kind: "filesystem",
  config_revision: 4,
} as StorageLocationOut;
function status(overrides: Partial<StorageMaintenanceStatus> = {}): StorageMaintenanceStatus {
  return {
    location_id: location.id,
    state: "active",
    revision: 1,
    changed_at: null,
    changed_by: null,
    measured_at: new Date().toISOString(),
    protocol_enabled: true,
    open_operations: 0,
    operations: [],
    has_more: false,
    coverage: "registered_location_access_only",
    physical_cleanup_permission: false,
    ...overrides,
  };
}
function review(
  operation: StorageMaintenanceOperation = "drain",
  overrides: Partial<StorageMaintenancePreview> = {},
): StorageMaintenancePreview {
  return {
    ...status(),
    operation,
    target_state: { drain: "draining", stop: "stopped", resume: "active" }[
      operation
    ] as StorageMaintenancePreview["target_state"],
    expires_at: new Date(Date.now() + 300_000).toISOString(),
    impact_token: "reviewed-once",
    impact: {
      location_id: location.id,
      config_revision: 4,
      measured_at: new Date().toISOString(),
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
        target_records: 0,
        target_annotations: 0,
        target_datasets: 0,
        target_training: 0,
        target_media_cache: 0,
        target_audit_archive: 0,
        target_import: 0,
        target_export: 0,
        target_avatars: 0,
        target_bug_reports: 0,
        target_related_datasets: 0,
        target_related_projects: 0,
        target_related_tasks: 0,
        target_versions: 0,
        target_runs: 0,
        current_bindings: 0,
        unlocated_targets: 0,
      },
    },
    ...overrides,
  };
}
const clients: ReturnType<typeof createTestQueryClient>[] = [];
function mount(initial = location) {
  let current = initial;
  const client = createTestQueryClient();
  clients.push(client);
  const tree = () => (
    <QueryClientProvider client={client}>
      <StorageLocationMaintenance location={current} />
    </QueryClientProvider>
  );
  const view = render(tree());
  return {
    client,
    refresh: (next = current) => {
      current = next;
      view.rerender(tree());
    },
  };
}
async function expand() {
  fireEvent.click(screen.getByText("位置访问维护", { selector: "summary" }));
  await screen.findByText(/维护修订 1 · 未收尾访问/);
}
async function preview() {
  fireEvent.click(screen.getByRole("button", { name: "开始维护" }));
  return screen.findByRole("dialog", { name: "开始维护前确认" });
}
beforeEach(() => {
  vi.resetAllMocks();
  Object.assign(auth, { role: "super_admin", session: "one", revision: 1 });
  api.get.mockResolvedValue(status());
  api.post.mockImplementation(async (path: string) =>
    path.endsWith("maintenance-preview") ? review() : status({ state: "draining", revision: 2 }),
  );
});
afterEach(() => {
  vi.useRealTimers();
  clients.splice(0).forEach((client) => client.clear());
});

it("loads on demand and distinguishes registered access from physical storage quiet", async () => {
  mount();
  expect(api.get).not.toHaveBeenCalled();
  await expand();
  expect(api.get.mock.calls[0][0]).toBe("/storage-locations/place/maintenance");
  expect(screen.getByText(/不能证明整块挂载/)).toBeInTheDocument();
  expect(api.post).not.toHaveBeenCalled();
});
it("hides maintenance from non-superadmins and platform storage bindings", () => {
  auth.role = "project_admin";
  const view = mount();
  expect(screen.queryByText("位置访问维护")).not.toBeInTheDocument();
  auth.role = "super_admin";
  view.refresh({ ...location, kind: "s3" });
  expect(screen.queryByText("位置访问维护")).not.toBeInTheDocument();
  expect(api.get).not.toHaveBeenCalled();
});
it("requires deployment qualification before offering a control action", async () => {
  api.get.mockResolvedValue(status({ protocol_enabled: false }));
  mount();
  await expand();
  expect(screen.getByText(/空账本不能代替部署核验/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "开始维护" })).toBeDisabled();
});
it("does not offer stop while an unresolved physical access exists", async () => {
  api.get.mockResolvedValue(
    status({
      state: "draining",
      open_operations: 1,
      operations: [
        {
          id: "op",
          purpose: "reference_read",
          node_id: "node",
          revision: 1,
          started_at: new Date().toISOString(),
        },
      ],
    }),
  );
  mount();
  await expand();
  expect(screen.getByRole("button", { name: "停止访问" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "恢复访问" })).toBeEnabled();
  expect(screen.getByText(/超时、作业终态和心跳失联都不能证明线程已结束/)).toBeInTheDocument();
});
it.each(["draining", "stopped"] as const)("offers reviewed resume from %s", async (state) => {
  api.get.mockResolvedValue(status({ state }));
  api.post.mockResolvedValueOnce(review("resume", { state }));
  mount();
  await expand();
  fireEvent.click(screen.getByRole("button", { name: "恢复访问" }));
  const dialog = await screen.findByRole("dialog", { name: "恢复访问前确认" });
  expect(api.post).toHaveBeenCalledWith("/storage-locations/place/maintenance-preview", {
    operation: "resume",
    expected_revision: 1,
  });
  expect(within(dialog).getByText(/不会重放已失效的作业/)).toBeInTheDocument();
});
it("reviews the exact generation and changes nothing until confirmed", async () => {
  mount();
  await expand();
  const dialog = await preview();
  expect(api.post).toHaveBeenCalledTimes(1);
  expect(api.post).toHaveBeenCalledWith("/storage-locations/place/maintenance-preview", {
    operation: "drain",
    expected_revision: 1,
  });
  expect(within(dialog).getByText(/维护修订 1 → 2/)).toBeInTheDocument();
  fireEvent.click(within(dialog).getByText("查看已保存关系的影响盘点", { selector: "summary" }));
  expect(within(dialog).getByText("7 个任务 · 11 条标注")).toBeInTheDocument();
});
it("cancels with Escape, restores focus and sends no mutation", async () => {
  mount();
  await expand();
  await preview();
  fireEvent.keyDown(document, { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(screen.getByRole("button", { name: "开始维护" })).toHaveFocus();
  expect(api.post).toHaveBeenCalledTimes(1);
});
it("submits the reviewed token once and updates the current generation", async () => {
  api.get
    .mockResolvedValueOnce(status())
    .mockResolvedValue(status({ state: "draining", revision: 2 }));
  mount();
  await expand();
  const dialog = await preview();
  const confirm = within(dialog).getByRole("button", { name: "确认开始维护" });
  fireEvent.click(confirm);
  fireEvent.click(confirm);
  await waitFor(() =>
    expect(toast).toHaveBeenCalledWith({ msg: "位置已关闭新访问", kind: "success" }),
  );
  expect(api.post).toHaveBeenCalledTimes(2);
  expect(api.post.mock.calls[1]).toEqual([
    "/storage-locations/place/maintenance",
    { operation: "drain", expected_revision: 1, impact_token: "reviewed-once" },
  ]);
  await screen.findByText(/维护修订 2/);
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});
it("requires a fresh preview after conflict instead of retrying the old token", async () => {
  mount();
  await expand();
  const dialog = await preview();
  api.post.mockRejectedValueOnce(Object.assign(new Error("关系已变化"), { status: 409 }));
  fireEvent.click(within(dialog).getByRole("button", { name: "确认开始维护" }));
  await screen.findByRole("alert");
  expect(screen.getByRole("button", { name: "确认开始维护" })).toBeDisabled();
  expect(api.post).toHaveBeenCalledTimes(2);
  api.post.mockResolvedValueOnce(review("drain", { impact_token: "fresh" }));
  fireEvent.click(screen.getByRole("button", { name: "重新预览" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "确认开始维护" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "确认开始维护" }));
  await waitFor(() => expect(api.post).toHaveBeenCalledTimes(4));
  expect(api.post.mock.calls[3][1].impact_token).toBe("fresh");
});
it("blocks expired previews while the confirmation is open", async () => {
  mount();
  await expand();
  await preview();
  vi.useFakeTimers();
  // Re-preview under the fake clock so the expiry timer belongs to that clock.
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "重新预览" })));
  await act(async () => vi.advanceTimersByTimeAsync(300_001));
  expect(screen.getByRole("button", { name: "确认开始维护" })).toBeDisabled();
  expect(screen.getByRole("status")).toHaveTextContent("预览已过期");
  fireEvent.click(screen.getByRole("button", { name: "确认开始维护" }));
  expect(api.post).toHaveBeenCalledTimes(2);
});
it.each([401, 403, 404])("hides previously loaded evidence on status denial %i", async (code) => {
  mount();
  await expand();
  api.get.mockRejectedValue(Object.assign(new Error("Access revoked"), { status: code }));
  fireEvent.click(screen.getByRole("button", { name: "刷新维护状态" }));
  await screen.findByRole("alert");
  expect(screen.queryByText(/维护修订 1/)).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "开始维护" })).not.toBeInTheDocument();
});
it("removes all loaded evidence after permission is revoked during confirmation", async () => {
  mount();
  await expand();
  await preview();
  api.post.mockRejectedValueOnce(Object.assign(new Error("Access revoked"), { status: 403 }));
  fireEvent.click(screen.getByRole("button", { name: "重新预览" }));
  await screen.findByRole("alert");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.queryByText(/维护修订 1/)).not.toBeInTheDocument();
});
it("keeps dated evidence after a network error but disables every action", async () => {
  mount();
  await expand();
  api.get.mockRejectedValue(new Error("offline"));
  fireEvent.click(screen.getByRole("button", { name: "刷新维护状态" }));
  await screen.findByRole("alert");
  expect(screen.getByText(/维护修订 1/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "开始维护" })).toBeDisabled();
});
it.each(["location", "configuration", "session"])(
  "retires a confirmation after %s changes",
  async (kind) => {
    const view = mount();
    await expand();
    await preview();
    if (kind === "location") view.refresh({ ...location, id: "next" });
    else if (kind === "configuration") view.refresh({ ...location, config_revision: 5 });
    else {
      auth.revision += 1;
      auth.session = "two";
      view.refresh();
    }
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(api.post).toHaveBeenCalledTimes(1);
  },
);
it("discards a pending preview after the owner location changes", async () => {
  let resolve!: (value: StorageMaintenancePreview) => void;
  api.post.mockReturnValueOnce(
    new Promise<StorageMaintenancePreview>((done) => {
      resolve = done;
    }),
  );
  const view = mount();
  await expand();
  fireEvent.click(screen.getByRole("button", { name: "开始维护" }));
  view.refresh({ ...location, id: "next" });
  await act(async () => resolve(review()));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(api.post).toHaveBeenCalledTimes(1);
});
it.each([
  { location_id: "another" },
  { revision: 2 },
  { expires_at: "invalid-date" },
  { physical_cleanup_permission: true },
])("rejects mismatched review evidence %j", async (overrides) => {
  api.post.mockResolvedValueOnce(review("drain", overrides as Partial<StorageMaintenancePreview>));
  mount();
  await expand();
  fireEvent.click(screen.getByRole("button", { name: "开始维护" }));
  await screen.findByRole("alert");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(api.post).toHaveBeenCalledTimes(1);
});

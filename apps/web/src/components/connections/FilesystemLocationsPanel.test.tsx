import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { createTestQueryClient } from "@/test/queryClient";
import { FilesystemLocationsPanel } from "./FilesystemLocationsPanel";
const state = vi.hoisted(() => ({ role: "super_admin" }));
const clientApi = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn() }));
const sources = vi.hoisted(() => ({ location: vi.fn(), probe: vi.fn() }));
vi.mock("@/api/client", async (original) => ({
  ...(await original<typeof import("@/api/client")>()),
  apiClient: clientApi,
}));
vi.mock("@/api/datasetSources", () => ({ datasetSourcesApi: sources }));
vi.mock("@/hooks/usePermissions", () => ({ usePermissions: () => ({ role: state.role }) }));
vi.mock("@/hooks/useSessionGuard", () => ({
  useSessionRevision: () => 1,
  useSessionGuard: () => () => () => true,
}));
vi.mock("@/stores/authStore", () => ({
  useAuthStore: (select: (value: unknown) => unknown) =>
    select({ user: { id: "manager" }, sessionId: "session" }),
}));
vi.mock("./StorageLocationAccessFields", () => ({
  StorageLocationAccessFields: () => <span>Location grants</span>,
}));
const location = {
  id: "location",
  name: "Archive",
  kind: "filesystem",
  enabled: true,
  config_revision: 4,
  root_handle: "ceph",
  relative_path: "archive",
  scope: "restricted",
  use_user_ids: [],
  cache_policy: "preview",
};
const clients: ReturnType<typeof createTestQueryClient>[] = [];
afterEach(() => clients.splice(0).forEach((client) => client.clear()));
function mount(props = {}) {
  const client = createTestQueryClient();
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <FilesystemLocationsPanel {...props} />
    </QueryClientProvider>,
  );
}
beforeEach(() => {
  vi.clearAllMocks();
  state.role = "super_admin";
  clientApi.get.mockResolvedValue(["ceph"]);
  clientApi.post.mockResolvedValue(location);
  clientApi.patch.mockResolvedValue(location);
  sources.location.mockResolvedValue(location);
});
it("registers a controlled directory from the only configuration owner and returns its location identity", async () => {
  const onCreated = vi.fn();
  mount({ onCreated });
  await screen.findByRole("option", { name: "ceph" });
  fireEvent.change(screen.getByRole("textbox", { name: "位置名称" }), {
    target: { value: "Archive" },
  });
  fireEvent.change(screen.getByRole("combobox", { name: "受控根" }), { target: { value: "ceph" } });
  fireEvent.change(screen.getByRole("textbox", { name: "根内相对目录" }), {
    target: { value: "archive" },
  });
  fireEvent.click(screen.getByRole("button", { name: "登记目录位置" }));
  await waitFor(() => expect(onCreated).toHaveBeenCalledWith(location));
  expect(clientApi.post).toHaveBeenCalledWith(
    "/storage-locations",
    expect.objectContaining({ root_handle: "ceph", relative_path: "archive", scope: "restricted" }),
  );
  expect(sources.location).not.toHaveBeenCalled();
});
it("configures the exact detail identity with revision checking and hides registration from the detail", async () => {
  mount({ locationId: "location" });
  fireEvent.click(await screen.findByRole("button", { name: "配置使用范围 Archive" }));
  expect(screen.queryByRole("button", { name: "登记目录位置" })).not.toBeInTheDocument();
  expect(clientApi.get).not.toHaveBeenCalled();
  clientApi.post.mockResolvedValueOnce({
    location_id: "location",
    config_revision: 4,
    measured_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 300_000).toISOString(),
    impact_token: "scope-review",
    changes: [],
    impact: {
      location_id: "location",
      config_revision: 4,
      measured_at: new Date().toISOString(),
      scope: "stored_dependencies",
      frozen_coverage: "recorded_location_lineage_only",
      input_coverage: "current_task_links_only",
      execution_permission: false,
      counts: {
        sources: 0,
        enabled_sources: 0,
        unregistered_sources: 0,
        items: 0,
        reference_items: 0,
        datasets: 0,
        projects: 0,
        tasks: 0,
        annotations: 0,
        active_scans: 0,
        active_registrations: 0,
        enabled_schedules: 0,
        ready_previews: 0,
        failed_previews: 0,
        preview_bytes: 0,
        cache_entries: 0,
        cache_bytes: 0,
        active_cache_read_leases: 0,
        active_input_leases: 0,
        cleanup_input_leases: 0,
        ready_lineage_versions: 0,
        unfinished_lineage_versions: 0,
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
  });
  fireEvent.click(screen.getByRole("button", { name: "预览使用范围变更" }));
  await screen.findByRole("dialog", { name: "确认位置配置变更" });
  expect(clientApi.patch).not.toHaveBeenCalled();
  expect(clientApi.post).toHaveBeenCalledWith("/storage-locations/location/change-preview", {
    expected_revision: 4,
    scope: "restricted",
    use_user_ids: [],
  });
  fireEvent.click(screen.getByRole("button", { name: "确认保存位置配置" }));
  await waitFor(() =>
    expect(clientApi.patch).toHaveBeenCalledWith(
      "/storage-locations/location",
      expect.objectContaining({ expected_revision: 4, impact_token: "scope-review" }),
    ),
  );
});
it("does not expose filesystem registration or private roots to non-super administrators", () => {
  state.role = "project_admin";
  mount({ locationId: "location" });
  expect(clientApi.get).not.toHaveBeenCalled();
  expect(sources.location).not.toHaveBeenCalled();
  expect(screen.queryByRole("region", { name: "受控目录位置" })).not.toBeInTheDocument();
});

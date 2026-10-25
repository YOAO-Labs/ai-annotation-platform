import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { createTestQueryClient } from "@/test/queryClient";
import type { StorageLocationOut } from "@/api/generated/types.gen";
import { StorageOriginalCachePanel } from "./StorageOriginalCachePanel";
import { originalCacheValues } from "@/utils/cachePolicy";

const api = vi.hoisted(() => ({ get: vi.fn(), patch: vi.fn(), post: vi.fn() }));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  apiClient: api,
}));
const location: StorageLocationOut = {
  id: "cache-location",
  name: "Cache",
  kind: "s3",
  enabled: true,
  config_revision: 8,
  declared_capabilities: { reference: true },
  cache_policy: "on_demand",
  original_cache_quota_bytes: 12345,
  original_cache_ttl_seconds: 61,
};
const usage = {
  expected_revision: 8,
  cache_policy: "on_demand",
  quota_bytes: 12345,
  ttl_seconds: 61,
  charged_bytes: 123,
  ready: { count: 1, bytes: 123 },
  uploading: { count: 0, bytes: 0 },
  deleting: { count: 0, bytes: 0 },
  failed_count: 0,
  uncertain_count: 0,
  measured_at: "2026-10-24T00:00:00Z",
};
const clients: ReturnType<typeof createTestQueryClient>[] = [];
afterEach(() => clients.splice(0).forEach((client) => client.clear()));
beforeEach(() => {
  vi.clearAllMocks();
  api.get.mockResolvedValue(usage);
  api.patch.mockResolvedValue(location);
  api.post.mockImplementation(async () => ({
    location_id: "cache-location",
    config_revision: 8,
    measured_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 300_000).toISOString(),
    impact_token: "cache-review",
    changes: [],
    impact: {
      location_id: "cache-location",
      config_revision: 8,
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
  }));
});
function mount() {
  const client = createTestQueryClient();
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <StorageOriginalCachePanel location={location} />
    </QueryClientProvider>,
  );
}

it("preserves exact byte and TTL values and binds saving to location revision", async () => {
  mount();
  await screen.findByText(/已计费/);
  expect(screen.getByRole("spinbutton", { name: "保留时间（秒）" })).toHaveValue(61);
  expect(screen.getByRole("button", { name: "预览缓存配置变更" })).toBeDisabled();
  fireEvent.change(screen.getByRole("combobox", { name: "位置缓存上限" }), {
    target: { value: "preview" },
  });
  fireEvent.click(screen.getByRole("button", { name: "预览缓存配置变更" }));
  await screen.findByRole("dialog", { name: "确认位置配置变更" });
  expect(api.patch).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "确认保存位置配置" }));
  await waitFor(() =>
    expect(api.patch).toHaveBeenCalledWith("/storage-locations/cache-location", {
      expected_revision: 8,
      impact_token: "cache-review",
      cache_policy: "preview",
      original_cache_quota_bytes: 12345,
      original_cache_ttl_seconds: 61,
    }),
  );
});

it("sends the confirmed observation cutoff and reports queued deletion honestly", async () => {
  api.post.mockResolvedValue({
    retired_count: 1,
    skipped_leased_count: 0,
    remaining_ready_count: 0,
    usage,
  });
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "请求清理原图缓存" }));
  expect(api.post).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "确认提交回收" }));
  await waitFor(() =>
    expect(api.post).toHaveBeenCalledWith(
      "/storage-locations/cache-location/original-cache/cleanup",
      {
        expected_revision: 8,
        recorded_before: usage.measured_at,
      },
    ),
  );
  expect(await screen.findByText(/已提交 1 份副本回收/)).toHaveTextContent("实际删除前继续计费");
});

it("retains edits on a revision conflict and hides old accounting after denied mutation until revalidated", async () => {
  mount();
  await screen.findByText(/已计费/);
  const ttlInput = screen.getByRole("spinbutton", { name: "保留时间（秒）" });
  fireEvent.change(ttlInput, {
    target: { value: "62" },
  });
  api.patch.mockRejectedValueOnce({
    status: 409,
    message: "Storage configuration revision changed",
  });
  fireEvent.click(screen.getByRole("button", { name: "预览缓存配置变更" }));
  fireEvent.click(await screen.findByRole("button", { name: "确认保存位置配置" }));
  await screen.findByRole("alert");
  expect(ttlInput).toHaveValue(62);
  api.patch.mockRejectedValueOnce({ status: 403, message: "Forbidden" });
  expect(screen.getByRole("button", { name: "确认保存位置配置" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "重新预览" }));
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "确认保存位置配置" })).toBeEnabled(),
  );
  fireEvent.click(screen.getByRole("button", { name: "确认保存位置配置" }));
  fireEvent.click(await screen.findByRole("button", { name: "重新核对权限与用量" }));
  await screen.findByRole("spinbutton", { name: "保留时间（秒）" });
  expect(api.get).toHaveBeenCalledTimes(2);
});

it("requires a positive integer byte budget for on-demand and never rounds TTL", () => {
  expect(originalCacheValues("on_demand", "0", "86400").valid).toBe(false);
  expect(originalCacheValues("preview", "0", "86400").valid).toBe(true);
  expect(originalCacheValues("on_demand", "0.00000001", "86400").valid).toBe(false);
  expect(originalCacheValues("on_demand", "1", "60.5").valid).toBe(false);
  expect(originalCacheValues("on_demand", String(12345 / 1024 ** 2), "61")).toEqual({
    valid: true,
    original_cache_quota_bytes: 12345,
    original_cache_ttl_seconds: 61,
  });
});

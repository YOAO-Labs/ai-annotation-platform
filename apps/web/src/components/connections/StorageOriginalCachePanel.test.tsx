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
  expect(screen.getByRole("button", { name: "保存缓存配置" })).toBeDisabled();
  fireEvent.change(screen.getByRole("combobox", { name: "位置缓存上限" }), {
    target: { value: "preview" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存缓存配置" }));
  await waitFor(() =>
    expect(api.patch).toHaveBeenCalledWith("/storage-locations/cache-location", {
      expected_revision: 8,
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
  fireEvent.change(screen.getByRole("spinbutton", { name: "保留时间（秒）" }), {
    target: { value: "62" },
  });
  api.patch.mockRejectedValueOnce({
    status: 409,
    message: "Storage configuration revision changed",
  });
  fireEvent.click(screen.getByRole("button", { name: "保存缓存配置" }));
  await screen.findByRole("alert");
  expect(screen.getByRole("spinbutton", { name: "保留时间（秒）" })).toHaveValue(62);
  api.patch.mockRejectedValueOnce({ status: 403, message: "Forbidden" });
  fireEvent.click(screen.getByRole("button", { name: "保存缓存配置" }));
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

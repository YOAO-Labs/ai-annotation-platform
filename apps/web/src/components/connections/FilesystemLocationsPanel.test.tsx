import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { createTestQueryClient } from "@/test/queryClient";
import { FilesystemLocationsPanel } from "./FilesystemLocationsPanel";
const state = vi.hoisted(() => ({ role: "super_admin" }));
const clientApi = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn() }));
const sources = vi.hoisted(() => ({ location: vi.fn(), probe: vi.fn() }));
vi.mock("@/api/client", () => ({ apiClient: clientApi }));
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
  fireEvent.click(screen.getByRole("button", { name: "保存使用范围" }));
  await waitFor(() =>
    expect(clientApi.patch).toHaveBeenCalledWith(
      "/storage-locations/location",
      expect.objectContaining({ expected_revision: 4 }),
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

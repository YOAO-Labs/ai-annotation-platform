import { beforeEach, expect, it, vi } from "vitest";
import { storageMigrationWindowsApi } from "./storageMigrationWindows";
const client = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock("./client", () => ({ apiClient: client }));
beforeEach(() => vi.clearAllMocks());
it("reads bounded history and encoded details with cancellation signals", async () => {
  const signal = new AbortController().signal;
  await storageMigrationWindowsApi.list(40, 20, signal);
  expect(client.get).toHaveBeenCalledWith("/storage/migration-windows?limit=20&offset=40", {
    signal,
  });
  await storageMigrationWindowsApi.get("window/one", signal);
  expect(client.get).toHaveBeenCalledWith("/storage/migration-windows/window%2Fone", { signal });
});
it("opens only from a parent copy and transitions with explicit CAS", async () => {
  await storageMigrationWindowsApi.create("parent");
  expect(client.post).toHaveBeenLastCalledWith("/storage/migration-windows", {
    parent_copy_id: "parent",
  });
  await storageMigrationWindowsApi.transition("window/one", "stop", 7);
  expect(client.post).toHaveBeenLastCalledWith(
    "/storage/migration-windows/window%2Fone/transitions",
    { action: "stop", expected_revision: 7 },
  );
  await storageMigrationWindowsApi.transition("window/one", "close", 8);
  expect(client.post).toHaveBeenLastCalledWith(
    "/storage/migration-windows/window%2Fone/transitions",
    { action: "close", expected_revision: 8 },
  );
});

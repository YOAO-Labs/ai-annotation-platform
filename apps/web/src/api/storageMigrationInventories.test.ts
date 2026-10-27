import { beforeEach, expect, it, vi } from "vitest";
import { storageMigrationInventoriesApi } from "./storageMigrationInventories";

const client = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), silentGetBlob: vi.fn() }));
vi.mock("./client", () => ({ apiClient: client }));
beforeEach(() => vi.clearAllMocks());

it("uses global bounded history, explicit dispatch and authenticated binary adapter", async () => {
  const signal = new AbortController().signal;
  await storageMigrationInventoriesApi.list(40, 20, signal);
  expect(client.get).toHaveBeenCalledWith("/storage/migration-inventories?limit=20&offset=40", {
    signal,
  });
  await storageMigrationInventoriesApi.dispatch("job/one");
  expect(client.post).toHaveBeenCalledWith("/storage/migration-inventories/job%2Fone/dispatch");
  await storageMigrationInventoriesApi.report("job/one", signal);
  expect(client.silentGetBlob).toHaveBeenCalledWith(
    "/storage/migration-inventories/job%2Fone/report",
    { signal },
  );
  expect(client.post).toHaveBeenCalledTimes(1);
});

it("submits only the configured-source limits and leaves handling unreviewed", async () => {
  const limits = {
    max_pages: 3,
    max_entries: 5,
    max_object_bytes: 9,
    max_total_bytes: 20,
    max_seconds: 2,
    read_timeout_seconds: 1,
    page_size: 4,
  };
  await storageMigrationInventoriesApi.start(limits);
  expect(client.post).toHaveBeenCalledWith("/storage/migration-inventories", { limits });
});

it("adds window binding only on explicit selection", async () => {
  const limits = {
    max_pages: 1,
    max_entries: 2,
    max_object_bytes: 3,
    max_total_bytes: 4,
    max_seconds: 5,
    read_timeout_seconds: 6,
    page_size: 7,
  };
  await storageMigrationInventoriesApi.start(limits, "window");
  expect(client.post).toHaveBeenCalledWith("/storage/migration-inventories", {
    limits,
    window_id: "window",
  });
});

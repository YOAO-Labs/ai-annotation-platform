import { beforeEach, expect, it, vi } from "vitest";
import { storageMigrationCopiesApi } from "./storageMigrationCopies";
const client = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), silentGetBlob: vi.fn() }));
vi.mock("./client", () => ({ apiClient: client }));
beforeEach(() => vi.clearAllMocks());
it("uses registered target and persistent copy endpoints with bounded pagination and abort signals", async () => {
  const signal = new AbortController().signal;
  await storageMigrationCopiesApi.targets("source/id", 20, 20, signal);
  expect(client.get).toHaveBeenCalledWith(
    "/storage/migration-copy-targets?inventory_job_id=source%2Fid&limit=20&offset=20",
    { signal },
  );
  await storageMigrationCopiesApi.list(40, 20, signal);
  expect(client.get).toHaveBeenCalledWith("/storage/migration-copies?limit=20&offset=40", {
    signal,
  });
  await storageMigrationCopiesApi.get("copy/id", signal);
  expect(client.get).toHaveBeenCalledWith("/storage/migration-copies/copy%2Fid", { signal });
  await storageMigrationCopiesApi.failedObjects("copy/id", 20, 20, signal);
  expect(client.get).toHaveBeenCalledWith(
    "/storage/migration-copies/copy%2Fid/failed-objects?limit=20&offset=20",
    { signal },
  );
  expect(client.post).not.toHaveBeenCalled();
});
it("keeps selected retry distinct from ordinary resume, preparation and read-only verification", async () => {
  await storageMigrationCopiesApi.attempt("copy", "prepare");
  expect(client.post).toHaveBeenLastCalledWith("/storage/migration-copies/copy/attempts", {
    operation: "prepare",
  });
  await storageMigrationCopiesApi.attempt("copy", "run");
  expect(client.post).toHaveBeenLastCalledWith("/storage/migration-copies/copy/attempts", {
    operation: "run",
    retry_failed: false,
  });
  await storageMigrationCopiesApi.attempt("copy", "run", true, ["a".repeat(64)]);
  expect(client.post).toHaveBeenLastCalledWith("/storage/migration-copies/copy/attempts", {
    operation: "run",
    retry_failed: true,
    retry_object_ids: ["a".repeat(64)],
  });
  await storageMigrationCopiesApi.attempt("copy", "verify");
  expect(client.post).toHaveBeenLastCalledWith("/storage/migration-copies/copy/attempts", {
    operation: "verify",
  });
  await storageMigrationCopiesApi.dispatch("copy");
  expect(client.post).toHaveBeenLastCalledWith("/storage/migration-copies/copy/dispatch");
  await storageMigrationCopiesApi.attempt("copy", "recover");
  expect(client.post).toHaveBeenLastCalledWith("/storage/migration-copies/copy/attempts", {
    operation: "recover",
  });
});
it("downloads a checkpoint through the authenticated binary adapter", async () => {
  const signal = new AbortController().signal;
  await storageMigrationCopiesApi.checkpoint("copy/id", signal);
  expect(client.silentGetBlob).toHaveBeenCalledWith(
    "/storage/migration-copies/copy%2Fid/checkpoint",
    { signal },
  );
});

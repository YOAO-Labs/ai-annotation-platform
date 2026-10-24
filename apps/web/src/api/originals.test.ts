import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { MeResponse } from "./auth";
import type { OriginalIdentity } from "./generated";
import { useAuthStore } from "@/stores/authStore";
import {
  captureOriginalOwner,
  describeOriginal,
  isOriginalDownloadPath,
  isOriginalOwnerCurrent,
  originalOwnerKey,
  originalResourcePath,
  promoteOriginalRead,
  readOriginal,
  readOriginalBytes,
  type OriginalResource,
} from "./originals";

const taskId = "11111111-1111-1111-1111-111111111111";
const itemId = "22222222-2222-2222-2222-222222222222";
const datasetId = "33333333-3333-3333-3333-333333333333";
const contentId = "44444444-4444-4444-4444-444444444444";
const resource: OriginalResource = { kind: "task", taskId };
const digest = "a".repeat(64);
const path = `/api/v1/files/tasks/${taskId}/original?content_id=${contentId}&sha256=${digest}`;
const fetchMock = vi.fn();
const tick = async () => {
  for (let i = 0; i < 12; i++) await Promise.resolve();
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (value: unknown) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}
function identity(overrides: Partial<OriginalIdentity> = {}): OriginalIdentity {
  return {
    owner_kind: "dataset_asset",
    content_id: contentId,
    sha256: digest,
    size_bytes: 3,
    media_type: "image",
    verification_state: "verified",
    original_availability: "unchecked",
    download_path: path,
    ...overrides,
  };
}
function binary(value: Blob | Promise<Blob>) {
  return { ok: true, status: 200, blob: () => Promise.resolve(value) };
}
function json(value: unknown) {
  return { ok: true, status: 200, json: async () => value };
}
function authenticate(token = "private-token") {
  useAuthStore.getState().setAuth(token, { id: "alice" } as MeResponse);
}
beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  authenticate();
});
afterEach(async () => {
  useAuthStore.getState().logout();
  await tick();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it("constructs only the three exact resource families and never puts credentials in a namespace", () => {
  expect(originalResourcePath(resource)).toBe(`/files/tasks/${taskId}/original`);
  expect(originalResourcePath({ kind: "item", datasetId, itemId })).toBe(
    `/datasets/${datasetId}/items/${itemId}/original`,
  );
  expect(originalResourcePath({ kind: "camera", taskId, linkId: itemId })).toBe(
    `/files/tasks/${taskId}/links/${itemId}/original`,
  );
  expect(originalOwnerKey()).not.toContain("private-token");
  expect(() => originalResourcePath({ kind: "task", taskId: "../../auth/me" })).toThrow();
  expect(fetchMock).not.toHaveBeenCalled();
});

it("reads identity then exact full bytes with one API prefix and silent authenticated transport", async () => {
  const blob = new Blob(["abc"]);
  fetchMock.mockResolvedValueOnce(json(identity())).mockResolvedValueOnce(binary(blob));
  expect(await readOriginal(resource)).toEqual({ identity: identity(), blob });
  expect(fetchMock.mock.calls.map((args) => args[0])).toEqual([
    `/api/v1/files/tasks/${taskId}/original-identity`,
    path,
  ]);
  expect(fetchMock.mock.calls[1][1]).toMatchObject({
    cache: "no-store",
    headers: { Authorization: "Bearer private-token" },
  });
});

it.each([
  "https://s3.invalid/asset?token=secret",
  "/api/v1/auth/me?content_id=" + contentId + "&sha256=" + digest,
  path + "&token=secret",
  path + "&content_id=" + contentId,
  path + "#secret",
  path.replace("/files/", "/%66iles/"),
])("never authenticates an arbitrary or ambiguous download path: %s", async (url) => {
  expect(isOriginalDownloadPath(url)).toBe(false);
  await expect(readOriginalBytes(url)).rejects.toMatchObject({ status: 409 });
  expect(fetchMock).not.toHaveBeenCalled();
});

it.each([
  { download_path: "https://s3.invalid/object" },
  { sha256: "f" },
  { verification_state: "hint" },
  { size_bytes: -1 },
  { original_availability: "online" },
])("rejects untrusted identity fields before binary I/O: %j", async (fields) => {
  fetchMock.mockResolvedValueOnce(json(identity(fields as Partial<OriginalIdentity>)));
  await expect(readOriginal(resource)).rejects.toMatchObject({ status: 409 });
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

it("keeps an over-budget identity visible but refuses the full download before binary I/O", async () => {
  const large = identity({ size_bytes: 512 * 1024 ** 2 + 1 });
  fetchMock.mockResolvedValue(json(large));
  expect((await describeOriginal(resource)).size_bytes).toBe(large.size_bytes);
  await expect(readOriginal(resource, { identity: large })).rejects.toMatchObject({ status: 413 });
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

it("holds two slots through complete body reads and starts foreground work before queued prefetch", async () => {
  const bodies = Array.from({ length: 4 }, () => deferred<Blob>());
  fetchMock.mockImplementation(() => binary(bodies[fetchMock.mock.calls.length - 1].promise));
  const first = readOriginalBytes(path);
  const second = readOriginalBytes(path);
  const speculative = readOriginalBytes(path, { priority: "prefetch" });
  const foreground = readOriginalBytes(path.replace(digest, "b".repeat(64)));
  await tick();
  expect(fetchMock).toHaveBeenCalledTimes(2);
  bodies[0].resolve(new Blob(["one"]));
  await first;
  await tick();
  expect(fetchMock).toHaveBeenCalledTimes(3);
  expect(fetchMock.mock.calls[2][0]).toContain("sha256=" + "b".repeat(64));
  bodies[1].resolve(new Blob(["two"]));
  await second;
  await tick();
  expect(fetchMock).toHaveBeenCalledTimes(4);
  bodies[2].resolve(new Blob(["active"]));
  bodies[3].resolve(new Blob(["prefetch"]));
  await Promise.all([foreground, speculative]);
});

it("cancels waiting downloads without consuming a network slot and can promote a shared private fetch", async () => {
  const body = deferred<Blob>();
  fetchMock.mockReturnValue(binary(body.promise));
  const first = readOriginalBytes(path);
  const second = readOriginalBytes(path);
  const cancelled = new AbortController();
  const skipped = readOriginalBytes(path, { signal: cancelled.signal }).catch(
    (error: unknown) => error,
  );
  const controller = new AbortController();
  const promoted = readOriginalBytes(path.replace(digest, "c".repeat(64)), {
    signal: controller.signal,
    priority: "prefetch",
  });
  const ordinary = readOriginalBytes(path.replace(digest, "d".repeat(64)));
  cancelled.abort();
  promoteOriginalRead(controller.signal);
  expect(await skipped).toMatchObject({ name: "AbortError" });
  expect(fetchMock).toHaveBeenCalledTimes(2);
  body.resolve(new Blob(["body"]));
  await Promise.all([first, second, promoted, ordinary]);
  expect(fetchMock).toHaveBeenCalledTimes(4);
  expect(fetchMock.mock.calls[2][0]).toContain("sha256=" + "c".repeat(64));
});

it.each(["account", "session"])(
  "discards late successful bytes after %s retirement",
  async (change) => {
    const body = deferred<Blob>();
    fetchMock.mockReturnValue(binary(body.promise));
    const owner = captureOriginalOwner()!;
    const pending = readOriginalBytes(path, { owner }).catch((error: unknown) => error);
    if (change === "account")
      useAuthStore.getState().setAuth("bob-token", { id: "bob" } as MeResponse);
    else useAuthStore.setState({ sessionId: crypto.randomUUID() });
    expect(isOriginalOwnerCurrent(owner)).toBe(false);
    body.resolve(new Blob(["late"]));
    expect(await pending).toMatchObject({ name: "AbortError" });
  },
);

it("keeps the session namespace on token refresh and retries old credential rejection once", async () => {
  const old = deferred<ReturnType<typeof json>>();
  fetchMock.mockReturnValueOnce(old.promise).mockResolvedValueOnce(json(identity()));
  const owner = captureOriginalOwner()!;
  const key = originalOwnerKey();
  const pending = describeOriginal(resource, { owner });
  useAuthStore.getState().setToken("new-token");
  expect(originalOwnerKey()).toBe(key);
  old.resolve({ ok: false, status: 401, json: async () => ({ detail: "expired" }) } as ReturnType<
    typeof json
  >);
  expect(await pending).toEqual(identity());
  expect(fetchMock).toHaveBeenCalledTimes(2);
  expect(fetchMock.mock.calls[1][1].headers.Authorization).toBe("Bearer new-token");
  expect(useAuthStore.getState().token).toBe("new-token");
});

it("rejects cross-tab partial credentials before any request", async () => {
  localStorage.setItem("token", "other-tab-token");
  expect(captureOriginalOwner()).toBeNull();
  await expect(describeOriginal(resource)).rejects.toMatchObject({ name: "AbortError" });
  expect(fetchMock).not.toHaveBeenCalled();
});

it("checks the returned byte count and never downgrades an identity error to a signed URL", async () => {
  fetchMock
    .mockResolvedValueOnce(json(identity()))
    .mockResolvedValueOnce(binary(new Blob(["bad-size"])));
  await expect(readOriginal(resource)).rejects.toMatchObject({
    status: 409,
    detailRaw: { code: "content_mismatch" },
  });
  fetchMock.mockResolvedValueOnce({
    ok: false,
    status: 409,
    headers: new Headers(),
    json: async () => ({ detail: { code: "no_readable_original" } }),
  });
  await expect(readOriginal(resource)).rejects.toMatchObject({ status: 409 });
  expect(fetchMock).toHaveBeenCalledTimes(3);
});

it("only retries the explicit capacity response and stops after a finite number of attempts", async () => {
  vi.useFakeTimers();
  fetchMock.mockResolvedValue({
    ok: false,
    status: 429,
    headers: new Headers(),
    json: async () => ({ detail: { code: "snapshot_capacity_exceeded" } }),
  });
  const pending = readOriginalBytes(path).catch((error: unknown) => error);
  await vi.runAllTimersAsync();
  expect(await pending).toMatchObject({ status: 429 });
  expect(fetchMock).toHaveBeenCalledTimes(4);
  fetchMock.mockResolvedValueOnce({
    ok: false,
    status: 429,
    headers: new Headers(),
    json: async () => ({ detail: { code: "other_limit" } }),
  });
  await expect(readOriginalBytes(path)).rejects.toMatchObject({ status: 429 });
  expect(fetchMock).toHaveBeenCalledTimes(5);
});

import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { MeResponse } from "./auth";
import type { OriginalIdentity } from "./originals";
import { captureOriginalOwner } from "./originals";
import {
  prepareOriginalVideo,
  probeOriginalVideo,
  type VideoOriginalResource,
} from "./originalVideo";
import { useAuthStore } from "@/stores/authStore";

const taskId = "11111111-1111-1111-1111-111111111111";
const itemId = "22222222-2222-2222-2222-222222222222";
const datasetId = "33333333-3333-3333-3333-333333333333";
const contentId = "44444444-4444-4444-4444-444444444444";
const leaseId = "55555555-5555-5555-5555-555555555555";
const digest = "a".repeat(64);
const path = `/api/v1/files/tasks/${taskId}/original`;
const mediaPath = `/api/v1/media/original-playback/${leaseId}`;
const resource: VideoOriginalResource = { kind: "task", taskId };
const fetchMock = vi.fn();
function identity(overrides: Partial<OriginalIdentity> = {}): OriginalIdentity {
  return {
    owner_kind: "dataset_asset",
    content_id: contentId,
    sha256: digest,
    size_bytes: 3,
    media_type: "video",
    verification_state: "verified",
    original_availability: "unchecked",
    download_path: `${path}?content_id=${contentId}&sha256=${digest}`,
    ...overrides,
  };
}
function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
function prepared(overrides = {}) {
  return json({
    lease_id: leaseId,
    media_path: mediaPath,
    expires_at: new Date(Date.now() + 300_000).toISOString(),
    ...overrides,
  });
}
function head(status = 200, headers = {}) {
  return new Response(null, {
    status,
    headers: {
      "Content-Length": "3",
      "X-Content-SHA256": digest,
      ETag: `"sha256:${digest}"`,
      ...headers,
    },
  });
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}
const tick = async () => {
  for (let i = 0; i < 20; i++) await Promise.resolve();
};
beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  useAuthStore.getState().setAuth("private-token", { id: "alice" } as MeResponse);
});
afterEach(() => {
  useAuthStore.getState().logout();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it("describes then prepares with current credentials; native HEAD has only the path-scoped cookie", async () => {
  fetchMock
    .mockResolvedValueOnce(json(identity()))
    .mockResolvedValueOnce(prepared())
    .mockResolvedValueOnce(head());
  const lease = await prepareOriginalVideo(resource);
  expect(lease).toMatchObject({
    leaseId,
    mediaPath,
    identity: identity(),
    owner: captureOriginalOwner(),
  });
  expect(lease.expiresAt).toBeGreaterThan(Date.now());
  expect(fetchMock.mock.calls[0][0]).toBe(`${path}-identity`);
  expect(fetchMock.mock.calls[1]).toEqual([
    `${path}-playback`,
    expect.objectContaining({
      method: "POST",
      credentials: "same-origin",
      redirect: "error",
      cache: "no-store",
      body: JSON.stringify({
        content_id: contentId,
        sha256: digest,
        session_id: lease.owner.sessionId,
      }),
      headers: expect.objectContaining({ Authorization: "Bearer private-token" }),
    }),
  ]);
  expect(await probeOriginalVideo(lease)).toBe("valid");
  expect(fetchMock.mock.calls[2]).toEqual([
    mediaPath,
    expect.objectContaining({
      method: "HEAD",
      credentials: "same-origin",
      redirect: "error",
      cache: "no-store",
    }),
  ]);
  expect(fetchMock.mock.calls[2][1].headers).toBeUndefined();
  expect(Object.isFrozen(lease.identity)).toBe(true);
});

it("supports the exact dataset item route with a supplied, equivalently verified identity", async () => {
  const itemPath = `/api/v1/datasets/${datasetId}/items/${itemId}/original`;
  fetchMock
    .mockResolvedValueOnce(prepared())
    .mockResolvedValueOnce(new Response(null, { status: 204 }));
  const lease = await prepareOriginalVideo(
    { kind: "item", datasetId, itemId },
    {
      identity: identity({ download_path: `${itemPath}?content_id=${contentId}&sha256=${digest}` }),
    },
  );
  await Promise.all([lease.release(), lease.release()]);
  expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
    `${itemPath}-playback`,
    `${itemPath}-playback/${leaseId}`,
  ]);
});

it.each([
  { download_path: "https://evil.invalid/file" },
  { download_path: `${path}?content_id=${contentId}&sha256=${digest}&token=secret` },
  { download_path: `${path.replace(taskId, itemId)}?content_id=${contentId}&sha256=${digest}` },
  { sha256: "f" },
  { content_id: "../auth/me" },
  { size_bytes: -1 },
  { verification_state: "hint" },
  { original_availability: "online" },
  { owner_kind: "unknown" },
])("rejects a supplied invalid identity before any authenticated request: %j", async (fields) => {
  await expect(
    prepareOriginalVideo(resource, { identity: identity(fields as Partial<OriginalIdentity>) }),
  ).rejects.toMatchObject({ status: 409 });
  expect(fetchMock).not.toHaveBeenCalled();
});
it.each([
  { media_type: "image", status: 415 },
  { size_bytes: 5 * 1024 ** 3 + 1, status: 413 },
])(
  "rejects nonvideo or overbudget originals before preparation: %j",
  async ({ status, ...fields }) => {
    await expect(
      prepareOriginalVideo(resource, { identity: identity(fields) }),
    ).rejects.toMatchObject({ status });
    expect(fetchMock).not.toHaveBeenCalled();
  },
);
it.each([
  { kind: "task", taskId: "../auth/me" },
  { kind: "camera", taskId, linkId: itemId },
])("does not authenticate arbitrary resource families: %j", async (value) => {
  await expect(
    prepareOriginalVideo(value as VideoOriginalResource, { identity: identity() }),
  ).rejects.toMatchObject({ status: 409 });
  expect(fetchMock).not.toHaveBeenCalled();
});
it("requires a UUID session and a complete current owner/token pair", async () => {
  useAuthStore.setState({ sessionId: "legacy-string" });
  await expect(prepareOriginalVideo(resource, { identity: identity() })).rejects.toMatchObject({
    name: "AbortError",
  });
  useAuthStore.setState({ sessionId: crypto.randomUUID() });
  localStorage.setItem("token", "another-tab");
  await expect(prepareOriginalVideo(resource, { identity: identity() })).rejects.toMatchObject({
    name: "AbortError",
  });
  expect(fetchMock).not.toHaveBeenCalled();
});

it.each([
  "https://evil.invalid/file",
  mediaPath + "?token=secret",
  mediaPath + "#secret",
  mediaPath.replace(leaseId, contentId),
  mediaPath.replace("/media/", "/%6dedia/"),
])("never publishes or probes an arbitrary native path: %s", async (value) => {
  fetchMock
    .mockResolvedValueOnce(prepared({ media_path: value }))
    .mockResolvedValueOnce(new Response(null, { status: 204 }));
  await expect(prepareOriginalVideo(resource, { identity: identity() })).rejects.toMatchObject({
    status: 409,
  });
  await tick();
  expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
    `${path}-playback`,
    `${path}-playback/${leaseId}`,
  ]);
});
it.each(["invalid", new Date(0).toISOString()])(
  "rejects and retires invalid/expired response deadlines: %s",
  async (expires_at) => {
    fetchMock
      .mockResolvedValueOnce(prepared({ expires_at }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    await expect(prepareOriginalVideo(resource, { identity: identity() })).rejects.toMatchObject({
      status: 409,
    });
    await tick();
    expect(fetchMock.mock.calls[1][1].method).toBe("DELETE");
  },
);
it.each(["account", "session", "abort"])(
  "retires a late prepared lease after %s instead of publishing it",
  async (change) => {
    const pendingResponse = deferred<Response>();
    fetchMock
      .mockReturnValueOnce(pendingResponse.promise)
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    const controller = new AbortController();
    const pending = prepareOriginalVideo(resource, {
      identity: identity(),
      signal: controller.signal,
    }).catch((error: unknown) => error);
    if (change === "account")
      useAuthStore.getState().setAuth("bob-token", { id: "bob" } as MeResponse);
    else if (change === "session") useAuthStore.setState({ sessionId: crypto.randomUUID() });
    else controller.abort();
    pendingResponse.resolve(prepared());
    expect(await pending).toMatchObject({ name: "AbortError" });
    await tick();
    expect(fetchMock.mock.calls[1]).toEqual([
      `${path}-playback/${leaseId}`,
      expect.objectContaining({
        method: "DELETE",
        headers: expect.objectContaining({ Authorization: "Bearer private-token" }),
      }),
    ]);
  },
);
it("retires an old-token success and retries once with refreshed credentials", async () => {
  const pendingResponse = deferred<Response>();
  fetchMock
    .mockReturnValueOnce(pendingResponse.promise)
    .mockResolvedValueOnce(new Response(null, { status: 204 }))
    .mockResolvedValueOnce(
      prepared({ lease_id: contentId, media_path: mediaPath.replace(leaseId, contentId) }),
    );
  const pending = prepareOriginalVideo(resource, { identity: identity() });
  useAuthStore.getState().setToken("new-token");
  pendingResponse.resolve(prepared());
  const lease = await pending;
  expect(lease.leaseId).toBe(contentId);
  expect(fetchMock.mock.calls.map((call) => call[1].headers.Authorization)).toEqual([
    "Bearer private-token",
    "Bearer private-token",
    "Bearer new-token",
  ]);
});
it("retries an old-token rejection without logging out the renewed session", async () => {
  const pendingResponse = deferred<Response>();
  fetchMock.mockReturnValueOnce(pendingResponse.promise).mockResolvedValueOnce(prepared());
  const pending = prepareOriginalVideo(resource, { identity: identity() });
  useAuthStore.getState().setToken("new-token");
  pendingResponse.resolve(json({ detail: "expired" }, 401));
  expect((await pending).leaseId).toBe(leaseId);
  expect(useAuthStore.getState().token).toBe("new-token");
});
it("caps repeated credential rotation and retires both successful old leases", async () => {
  const first = deferred<Response>();
  const second = deferred<Response>();
  fetchMock
    .mockReturnValueOnce(first.promise)
    .mockResolvedValueOnce(head(204))
    .mockReturnValueOnce(second.promise)
    .mockResolvedValueOnce(head(204));
  const pending = prepareOriginalVideo(resource, { identity: identity() }).catch(
    (error: unknown) => error,
  );
  useAuthStore.getState().setToken("new-token");
  first.resolve(prepared());
  await tick();
  useAuthStore.getState().setToken("newer-token");
  second.resolve(prepared());
  expect(await pending).toMatchObject({ name: "AbortError" });
  expect(fetchMock.mock.calls.filter((call) => call[1].method === "POST")).toHaveLength(2);
  expect(fetchMock.mock.calls.filter((call) => call[1].method === "DELETE")).toHaveLength(2);
});
it("release is idempotent, bounded and an old 401 cannot log out a new account", async () => {
  vi.useFakeTimers();
  fetchMock.mockResolvedValueOnce(prepared());
  const lease = await prepareOriginalVideo(resource, { identity: identity() });
  useAuthStore.getState().setAuth("bob-token", { id: "bob" } as MeResponse);
  const response = deferred<Response>();
  fetchMock.mockReturnValueOnce(response.promise);
  const release = lease.release();
  expect(lease.release()).toBe(release);
  await vi.advanceTimersByTimeAsync(5000);
  await release;
  expect(fetchMock.mock.calls[1][1].signal.aborted).toBe(true);
  response.resolve(json({ detail: "expired" }, 401));
  await tick();
  expect(useAuthStore.getState().token).toBe("bob-token");
  expect(fetchMock).toHaveBeenCalledTimes(2);
});
it.each([
  "video_playback_preparing",
  "video_playback_capacity_exceeded",
  "video_playback_lease_capacity_exceeded",
  "snapshot_capacity_exceeded",
])("bounds retries for explicit %s admission responses", async (code) => {
  vi.useFakeTimers();
  fetchMock.mockImplementation(() => Promise.resolve(json({ detail: { code } }, 429)));
  const pending = prepareOriginalVideo(resource, { identity: identity() }).catch(
    (error: unknown) => error,
  );
  await vi.advanceTimersByTimeAsync(7000);
  expect(await pending).toMatchObject({ status: 429 });
  expect(fetchMock).toHaveBeenCalledTimes(4);
});
it("does not retry arbitrary 429 responses and aborts a pending bounded backoff", async () => {
  fetchMock.mockResolvedValueOnce(json({ detail: { code: "unrelated" } }, 429));
  await expect(prepareOriginalVideo(resource, { identity: identity() })).rejects.toMatchObject({
    status: 429,
  });
  expect(fetchMock).toHaveBeenCalledTimes(1);
  vi.useFakeTimers();
  fetchMock.mockResolvedValueOnce(json({ detail: { code: "video_playback_preparing" } }, 429));
  const controller = new AbortController();
  const pending = prepareOriginalVideo(resource, {
    identity: identity(),
    signal: controller.signal,
  }).catch((error: unknown) => error);
  await tick();
  controller.abort();
  expect(await pending).toMatchObject({ name: "AbortError" });
  expect(vi.getTimerCount()).toBe(0);
});
it.each([403, 409, 500])(
  "surfaces HEAD %s without an automatic new preparation",
  async (status) => {
    fetchMock.mockResolvedValueOnce(prepared()).mockResolvedValueOnce(head(status));
    const lease = await prepareOriginalVideo(resource, { identity: identity() });
    await expect(probeOriginalVideo(lease)).rejects.toMatchObject({ status });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  },
);
it("distinguishes expiry/restart and rejects changed content headers", async () => {
  fetchMock
    .mockResolvedValueOnce(prepared())
    .mockResolvedValueOnce(head(410))
    .mockResolvedValueOnce(head(200, { "Content-Length": "4" }));
  const lease = await prepareOriginalVideo(resource, { identity: identity() });
  expect(await probeOriginalVideo(lease)).toBe("expired");
  await expect(probeOriginalVideo(lease)).rejects.toMatchObject({ status: 409 });
});
it("discards late native probes after owner replacement", async () => {
  const response = deferred<Response>();
  fetchMock.mockResolvedValueOnce(prepared()).mockReturnValueOnce(response.promise);
  const lease = await prepareOriginalVideo(resource, { identity: identity() });
  const pending = probeOriginalVideo(lease).catch((error: unknown) => error);
  useAuthStore.getState().setAuth("bob-token", { id: "bob" } as MeResponse);
  response.resolve(head());
  expect(await pending).toMatchObject({ name: "AbortError" });
  expect(useAuthStore.getState().token).toBe("bob-token");
});

it("treats an installed old credential proof as expired only during an explicit same-owner probe", async () => {
  fetchMock.mockResolvedValueOnce(prepared());
  const lease = await prepareOriginalVideo(resource, { identity: identity() });
  const owner = captureOriginalOwner();
  useAuthStore.getState().setToken("new-token");
  expect(captureOriginalOwner()).toEqual(owner);
  expect(lease.mediaPath).toBe(mediaPath);
  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(await probeOriginalVideo(lease)).toBe("expired");
  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(useAuthStore.getState().token).toBe("new-token");
  const controller = new AbortController();
  controller.abort();
  await expect(probeOriginalVideo(lease, { signal: controller.signal })).rejects.toMatchObject({
    name: "AbortError",
  });
  localStorage.setItem("token", "incomplete-storage-transition");
  await expect(probeOriginalVideo(lease)).rejects.toMatchObject({ name: "AbortError" });
  useAuthStore.getState().setAuth("bob-token", { id: "bob" } as MeResponse);
  await expect(probeOriginalVideo(lease)).rejects.toMatchObject({ name: "AbortError" });
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

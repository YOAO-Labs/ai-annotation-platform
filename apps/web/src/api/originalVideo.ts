import { apiClient, ApiError } from "./client";
import type { OriginalPlaybackLease } from "./generated";
import {
  captureOriginalOwner,
  describeOriginal,
  isOriginalOwnerCurrent,
  originalResourcePath,
  type OriginalIdentity,
  type OriginalOwner,
  type OriginalResource,
} from "./originals";
import { useAuthStore } from "@/stores/authStore";

export type VideoOriginalResource = Exclude<OriginalResource, { kind: "camera" }>;
export interface OriginalVideoLease {
  leaseId: string;
  mediaPath: string;
  expiresAt: number;
  identity: OriginalIdentity;
  owner: OriginalOwner;
  /** Idempotent in-page retirement; failures leave lease expiry as the final owner. */
  release(): Promise<void>;
  /** Best-effort retirement when the document leaves, where nothing can be awaited. */
  retireOnUnload(): void;
}
interface PrepareOptions {
  owner?: OriginalOwner;
  signal?: AbortSignal;
  identity?: OriginalIdentity;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const SHA = /^[0-9a-f]{64}$/;
const MAX_BYTES = 5 * 1024 ** 3;
const RETRY_CODES = new Set([
  "video_playback_preparing",
  "video_playback_capacity_exceeded",
  "video_playback_lease_capacity_exceeded",
  "snapshot_capacity_exceeded",
]);
const leases = new WeakMap<OriginalVideoLease, { released: boolean; token: string }>();

function invalid(): never {
  throw new ApiError(409, "原视频身份或播放路径无效，请刷新后重试");
}
function aborted(): DOMException {
  return new DOMException("原视频请求已取消或登录会话已变更", "AbortError");
}
function current(owner: OriginalOwner, signal?: AbortSignal): string {
  signal?.throwIfAborted();
  if (!isOriginalOwnerCurrent(owner)) throw aborted();
  return useAuthStore.getState().token!;
}
function resourcePath(resource: VideoOriginalResource): string {
  if (resource?.kind !== "task" && resource?.kind !== "item") invalid();
  const identifiers =
    resource.kind === "task" ? [resource.taskId] : [resource.datasetId, resource.itemId];
  if (identifiers.some((value) => typeof value !== "string" || !UUID.test(value))) invalid();
  return originalResourcePath(resource);
}
function validateIdentity(path: string, value: OriginalIdentity): OriginalIdentity {
  if (
    !value ||
    typeof value.content_id !== "string" ||
    !UUID.test(value.content_id) ||
    typeof value.sha256 !== "string" ||
    !SHA.test(value.sha256) ||
    !Number.isSafeInteger(value.size_bytes) ||
    value.size_bytes < 0 ||
    !["dataset_asset", "task_original"].includes(value.owner_kind) ||
    (!path.startsWith("/files/tasks/") && value.owner_kind !== "dataset_asset") ||
    value.verification_state !== "verified" ||
    value.original_availability !== "unchecked" ||
    typeof value.media_type !== "string" ||
    value.download_path !== `/api/v1${path}?content_id=${value.content_id}&sha256=${value.sha256}`
  )
    invalid();
  if (value.media_type !== "video") throw new ApiError(415, "仅视频原件支持原生播放");
  if (value.size_bytes > MAX_BYTES) throw new ApiError(413, "原视频超过 5 GiB 播放上限");
  return Object.freeze({ ...value });
}
function leaseId(value: unknown): string | null {
  return typeof value === "string" && UUID.test(value) ? value : null;
}
function delay(signal: AbortSignal | undefined, milliseconds: number): Promise<void> {
  return new Promise((resolve, reject) => {
    const abort = () => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
      reject(aborted());
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", abort);
      resolve();
    }, milliseconds);
    signal?.addEventListener("abort", abort, { once: true });
    if (signal?.aborted) abort();
  });
}

/**
 * In-page retirement. Chromium reports a completed `fetch()` whose response has
 * no body as `net::ERR_ABORTED` (crbug.com/371021302), so the API's 204 lease
 * release would look like a cancelled request to fail-closed observers. XHR
 * sends the same DELETE, credentials and cookie clearing without that false
 * abort; the browser bounds it with `timeout`.
 */
function retireLease(url: string, token: string, body: string): Promise<void> {
  return new Promise<void>((resolve) => {
    try {
      const request = new XMLHttpRequest();
      request.open("DELETE", url);
      request.setRequestHeader("Content-Type", "application/json");
      request.setRequestHeader("Authorization", `Bearer ${token}`);
      request.timeout = 5_000;
      request.onloadend = () => resolve();
      request.send(body);
    } catch {
      // A revoked JWT, restart or offline browser leaves expiry as the final cleanup owner.
      resolve();
    }
  });
}

/**
 * The document is leaving: only a keepalive request outlives it, and nothing can
 * await the answer. A revoked JWT, restart or offline browser still leaves lease
 * expiry as the final cleanup owner.
 */
function retireLeaseOnUnload(url: string, token: string, body: string): void {
  try {
    void fetch(url, {
      method: "DELETE",
      keepalive: true,
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body,
      credentials: "same-origin",
      redirect: "error",
      cache: "no-store",
    }).catch(() => undefined);
  } catch {
    // Nothing can be delivered from a document that is already gone.
  }
}

interface Releaser {
  release(): Promise<void>;
  retireOnUnload(): void;
}

/** Cleanup uses the credentials that created this lease, without global auth side effects. */
function releaser(path: string, id: string, token: string, sessionId: string): Releaser {
  const url = `/api/v1${path}-playback/${id}`;
  const body = JSON.stringify({ session_id: sessionId });
  let pending: Promise<void> | undefined;
  let settled = false;
  return {
    release: () => {
      pending ??= retireLease(url, token, body).finally(() => {
        settled = true;
      });
      return pending;
    },
    retireOnUnload: () => {
      // A settled release already revoked this lease; an in-flight or unstarted
      // one is re-sent because its document can no longer deliver it.
      if (!settled) retireLeaseOnUnload(url, token, body);
    },
  };
}

export async function prepareOriginalVideo(
  resource: VideoOriginalResource,
  options: PrepareOptions = {},
): Promise<OriginalVideoLease> {
  const path = resourcePath(resource);
  const captured = options.owner ?? captureOriginalOwner();
  if (!captured || !UUID.test(captured.sessionId)) throw aborted();
  const owner = Object.freeze({ ...captured });
  current(owner, options.signal);
  const identity = validateIdentity(
    path,
    options.identity ?? (await describeOriginal(resource, { owner, signal: options.signal })),
  );
  current(owner, options.signal);
  let tokenRetries = 0;
  let capacityRetries = 0;
  for (;;) {
    const token = current(owner, options.signal);
    let result: OriginalPlaybackLease | undefined;
    let failure: unknown;
    try {
      result = await apiClient.silentPost<OriginalPlaybackLease>(
        `${path}-playback`,
        { content_id: identity.content_id, sha256: identity.sha256, session_id: owner.sessionId },
        {
          signal: options.signal,
          cache: "no-store",
          redirect: "error",
          credentials: "same-origin",
        },
      );
    } catch (error) {
      failure = error;
    }
    const id = leaseId(result?.lease_id);
    const release = id ? releaser(path, id, token, owner.sessionId) : undefined;
    let latest: string;
    try {
      latest = current(owner, options.signal);
    } catch (error) {
      // Never publish a late cookie/path into the next task, account or session.
      if (release) void release.release();
      throw error;
    }
    if (latest !== token) {
      if (release) void release.release();
      if (tokenRetries++ === 0) continue;
      throw aborted();
    }
    if (failure !== undefined) {
      const code =
        failure instanceof ApiError && failure.detailRaw && typeof failure.detailRaw === "object"
          ? (failure.detailRaw as { code?: unknown }).code
          : null;
      if (
        failure instanceof ApiError &&
        failure.status === 429 &&
        typeof code === "string" &&
        RETRY_CODES.has(code) &&
        capacityRetries < 3
      ) {
        await delay(options.signal, 1000 * 2 ** capacityRetries++);
        continue;
      }
      throw failure;
    }
    const expiresAt = typeof result?.expires_at === "string" ? Date.parse(result.expires_at) : NaN;
    if (
      !id ||
      result?.media_path !== `/api/v1/media/original-playback/${id}` ||
      !Number.isFinite(expiresAt) ||
      expiresAt <= Date.now()
    ) {
      if (release) void release.release();
      invalid();
    }
    const state = { released: false, token };
    const lease: OriginalVideoLease = Object.freeze({
      leaseId: id,
      mediaPath: result.media_path,
      expiresAt,
      identity,
      owner,
      release: () => {
        state.released = true;
        return release!.release();
      },
      retireOnUnload: () => {
        state.released = true;
        release!.retireOnUnload();
      },
    });
    leases.set(lease, state);
    return lease;
  }
}

/** Native media errors carry no HTTP status; this scoped HEAD distinguishes an expired lease. */
export async function probeOriginalVideo(
  lease: OriginalVideoLease,
  options: { signal?: AbortSignal } = {},
): Promise<"valid" | "expired"> {
  const state = leases.get(lease);
  if (!state) invalid();
  const token = current(lease.owner, options.signal);
  // Recover may need a fresh authority proof even when the logical login continues.
  // Keep this receipt private: the lease URL and public owner never carry credentials.
  if (state.released || state.token !== token) return "expired";
  const response = await fetch(lease.mediaPath, {
    method: "HEAD",
    credentials: "same-origin",
    redirect: "error",
    cache: "no-store",
    signal: options.signal,
  });
  if (current(lease.owner, options.signal) !== token) throw aborted();
  if (state.released || response.status === 410) return "expired";
  if (response.status !== 200)
    throw new ApiError(response.status, "原视频播放校验失败，请检查访问权限后重试");
  if (
    response.headers.get("Content-Length") !== String(lease.identity.size_bytes) ||
    response.headers.get("X-Content-SHA256") !== lease.identity.sha256 ||
    response.headers.get("ETag") !== `"sha256:${lease.identity.sha256}"`
  )
    throw new ApiError(409, "原视频播放内容身份不匹配，请刷新后重试");
  return "valid";
}

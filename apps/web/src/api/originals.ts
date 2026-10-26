import { apiClient, ApiError } from "./client";
import type { OriginalIdentity } from "./generated";
import { useAuthStore } from "@/stores/authStore";

export type { OriginalIdentity } from "./generated";
export type OriginalResource =
  | { kind: "task"; taskId: string }
  | { kind: "item"; datasetId: string; itemId: string }
  | { kind: "camera"; taskId: string; linkId: string };
export interface OriginalOwner {
  userId: string;
  sessionId: string;
}
interface ReadOptions {
  signal?: AbortSignal;
  owner?: OriginalOwner;
  priority?: "active" | "prefetch";
}

const UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";
const uuid = new RegExp(`^${UUID}$`, "i");
const sha = /^[0-9a-f]{64}$/;
const MAX_BYTES = 512 * 1024 ** 2;

function invalidPath(): never {
  throw new ApiError(409, "原件身份或读取路径无效，请刷新后重试");
}
function aborted(): DOMException {
  return new DOMException("原件读取已取消或登录会话已变更", "AbortError");
}
function identifier(value: string): string {
  if (!uuid.test(value)) invalidPath();
  return value.toLowerCase();
}

export function originalResourcePath(resource: OriginalResource): string {
  switch (resource.kind) {
    case "task":
      return `/files/tasks/${identifier(resource.taskId)}/original`;
    case "camera":
      return `/files/tasks/${identifier(resource.taskId)}/links/${identifier(resource.linkId)}/original`;
    case "item":
      return `/datasets/${identifier(resource.datasetId)}/items/${identifier(resource.itemId)}/original`;
    default:
      return invalidPath();
  }
}

export function captureOriginalOwner(): OriginalOwner | null {
  const auth = useAuthStore.getState();
  return auth.user?.id &&
    auth.sessionId &&
    auth.token &&
    auth.token === localStorage.getItem("token")
    ? { userId: auth.user.id, sessionId: auth.sessionId }
    : null;
}

export function isOriginalOwnerCurrent(owner: OriginalOwner | null): boolean {
  const current = captureOriginalOwner();
  return !!owner && current?.userId === owner.userId && current?.sessionId === owner.sessionId;
}

/** Public cache namespace; never includes a bearer token or credential receipt. */
export function originalOwnerKey(): string {
  const auth = useAuthStore.getState();
  return JSON.stringify([auth.user?.id ?? null, auth.sessionId]);
}

function requireOwner(options: ReadOptions): OriginalOwner {
  const owner = options.owner ?? captureOriginalOwner();
  if (!owner || !isOriginalOwnerCurrent(owner)) throw aborted();
  options.signal?.throwIfAborted();
  return owner;
}

async function requestForOwner<T>(
  owner: OriginalOwner,
  signal: AbortSignal | undefined,
  request: () => Promise<T>,
): Promise<T> {
  for (let attempt = 0; ; attempt++) {
    if (!isOriginalOwnerCurrent(owner)) throw aborted();
    signal?.throwIfAborted();
    const token = useAuthStore.getState().token;
    let value: T | undefined;
    let failure: unknown;
    let failed = false;
    try {
      value = await request();
    } catch (error) {
      failed = true;
      failure = error;
    }
    if (!isOriginalOwnerCurrent(owner)) throw aborted();
    signal?.throwIfAborted();
    if (useAuthStore.getState().token !== token) {
      if (attempt === 0) continue;
      throw aborted();
    }
    if (failed) throw failure;
    return value as T;
  }
}

function validateIdentity(
  resource: OriginalResource,
  identity: OriginalIdentity,
): OriginalIdentity {
  if (
    !identity ||
    !uuid.test(identity.content_id) ||
    !sha.test(identity.sha256) ||
    !Number.isSafeInteger(identity.size_bytes) ||
    identity.size_bytes < 0 ||
    !["dataset_asset", "task_original"].includes(identity.owner_kind) ||
    (resource.kind !== "task" && identity.owner_kind !== "dataset_asset") ||
    identity.verification_state !== "verified" ||
    identity.original_availability !== "unchecked" ||
    typeof identity.media_type !== "string" ||
    !identity.media_type
  )
    invalidPath();
  const expected = `/api/v1${originalResourcePath(resource)}?content_id=${identity.content_id}&sha256=${identity.sha256}`;
  if (identity.download_path !== expected) invalidPath();
  return identity;
}

export async function describeOriginal(
  resource: OriginalResource,
  options: ReadOptions = {},
): Promise<OriginalIdentity> {
  const owner = requireOwner(options);
  const identity = await requestForOwner(owner, options.signal, () =>
    apiClient.silentGet<OriginalIdentity>(`${originalResourcePath(resource)}-identity`, {
      signal: options.signal,
      cache: "no-store",
    }),
  );
  return validateIdentity(resource, identity);
}

function parseDownloadPath(path: string): { path: string; resource: OriginalResource } {
  if (typeof path !== "string" || !path.startsWith("/api/v1/")) invalidPath();
  const url = new URL(path, "https://original.invalid");
  if (url.origin !== "https://original.invalid" || url.hash) invalidPath();
  const keys = [...url.searchParams.keys()];
  const contentId = url.searchParams.get("content_id") ?? "";
  const digest = url.searchParams.get("sha256") ?? "";
  if (
    keys.length !== 2 ||
    !keys.includes("content_id") ||
    !keys.includes("sha256") ||
    !uuid.test(contentId) ||
    !sha.test(digest)
  )
    invalidPath();
  const task = url.pathname.match(new RegExp(`^/api/v1/files/tasks/(${UUID})/original$`, "i"));
  const camera = url.pathname.match(
    new RegExp(`^/api/v1/files/tasks/(${UUID})/links/(${UUID})/original$`, "i"),
  );
  const item = url.pathname.match(
    new RegExp(`^/api/v1/datasets/(${UUID})/items/(${UUID})/original$`, "i"),
  );
  const resource: OriginalResource = task
    ? { kind: "task", taskId: task[1] }
    : camera
      ? { kind: "camera", taskId: camera[1], linkId: camera[2] }
      : item
        ? { kind: "item", datasetId: item[1], itemId: item[2] }
        : invalidPath();
  const expected = `/api/v1${originalResourcePath(resource)}?content_id=${contentId}&sha256=${digest}`;
  if (path !== expected) invalidPath();
  return { path: expected.slice("/api/v1".length), resource };
}

export function isOriginalDownloadPath(path: string): boolean {
  try {
    parseDownloadPath(path);
    return true;
  } catch {
    return false;
  }
}

interface DownloadJob {
  owner: OriginalOwner;
  parent?: AbortSignal;
  controller: AbortController;
  priority: "active" | "prefetch";
  running: boolean;
  canceled: boolean;
  draining: boolean;
  start(): void;
  cancel(): void;
}
const downloads = new Set<DownloadJob>();
let active = 0;

function pump(): void {
  while (active < 2) {
    const waiting = [...downloads].filter((job) => !job.running);
    const next = waiting.find((job) => job.priority === "active") ?? waiting[0];
    if (!next) return;
    next.start();
  }
}

/** A shared consumer may promote its queued private fetch without duplicating it. */
export function promoteOriginalRead(signal: AbortSignal): void {
  for (const job of downloads) if (job.parent === signal) job.priority = "active";
  pump();
}

const unbindOwner = useAuthStore.subscribe(() => {
  for (const job of downloads) if (!isOriginalOwnerCurrent(job.owner)) job.cancel();
});
if (import.meta.hot) {
  import.meta.hot.dispose(() => {
    unbindOwner();
    for (const job of downloads) {
      job.controller.abort();
      job.cancel();
    }
  });
}

function delay(signal: AbortSignal, milliseconds: number): Promise<void> {
  return new Promise((resolve, reject) => {
    const abort = () => {
      clearTimeout(timer);
      signal.removeEventListener("abort", abort);
      reject(aborted());
    };
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", abort);
      resolve();
    }, milliseconds);
    signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) abort();
  });
}

async function fetchBinary(
  path: string,
  owner: OriginalOwner,
  signal: AbortSignal,
  abandoned: () => boolean,
): Promise<Blob> {
  for (let attempt = 0; ; attempt++) {
    if (abandoned()) throw aborted();
    try {
      return await requestForOwner(owner, signal, () =>
        apiClient.silentGetBlob(path, { signal, cache: "no-store" }),
      );
    } catch (error) {
      const detail = error instanceof ApiError ? error.detailRaw : null;
      const code = detail && typeof detail === "object" && "code" in detail ? detail.code : null;
      if (
        attempt >= 3 ||
        abandoned() ||
        !(error instanceof ApiError) ||
        error.status !== 429 ||
        code !== "snapshot_capacity_exceeded"
      )
        throw error;
      await delay(signal, 250 * 2 ** attempt);
    }
  }
}

export async function readOriginalBytes(
  downloadPath: string,
  options: ReadOptions = {},
): Promise<Blob> {
  const { path } = parseDownloadPath(downloadPath);
  const owner = requireOwner(options);
  return new Promise((resolve, reject) => {
    const controller = new AbortController();
    const cleanup = () => {
      downloads.delete(job);
      options.signal?.removeEventListener("abort", job.cancel);
    };
    const job: DownloadJob = {
      owner,
      parent: options.signal,
      controller,
      priority: options.priority ?? "active",
      running: false,
      canceled: false,
      draining: false,
      cancel() {
        if (job.canceled) {
          // Owner retirement must never leave a stale credential in flight.
          if (job.draining && !isOriginalOwnerCurrent(owner)) controller.abort();
          return;
        }
        job.canceled = true;
        if (job.running && job.priority === "prefetch" && isOriginalOwnerCurrent(owner)) {
          // The server admits at most two physical original reads. An aborted
          // prefetch can still hold its admission until the transport settles,
          // so keep the client slot instead of racing a replacement into it.
          job.draining = true;
          options.signal?.removeEventListener("abort", job.cancel);
          reject(aborted());
          return;
        }
        controller.abort();
        if (!job.running) {
          cleanup();
          reject(aborted());
          queueMicrotask(pump);
        }
      },
      start() {
        if (!isOriginalOwnerCurrent(owner) || options.signal?.aborted) return job.cancel();
        job.running = true;
        active++;
        void fetchBinary(path, owner, controller.signal, () => job.draining)
          .then(resolve, reject)
          .finally(() => {
            active--;
            cleanup();
            pump();
          });
      },
    };
    downloads.add(job);
    options.signal?.addEventListener("abort", job.cancel, { once: true });
    if (options.signal?.aborted) job.cancel();
    else pump();
  });
}

export async function readOriginal(
  resource: OriginalResource,
  options: ReadOptions & { identity?: OriginalIdentity } = {},
): Promise<{ identity: OriginalIdentity; blob: Blob }> {
  const owner = requireOwner(options);
  const identity = options.identity
    ? validateIdentity(resource, options.identity)
    : await describeOriginal(resource, { ...options, owner });
  if (identity.size_bytes > MAX_BYTES) {
    throw new ApiError(413, "原件超过当前完整下载的512MiB限制", { code: "content_too_large" });
  }
  const blob = await readOriginalBytes(identity.download_path, { ...options, owner });
  if (!isOriginalOwnerCurrent(owner)) throw aborted();
  options.signal?.throwIfAborted();
  if (blob.size !== identity.size_bytes) {
    throw new ApiError(409, "原件响应大小与身份不符，请刷新后重试", { code: "content_mismatch" });
  }
  return { identity, blob };
}

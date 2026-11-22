import { createStore, get, promisifyRequest } from "idb-keyval";
import { useAuthStore } from "@/stores/authStore";

const KEY = "anno.offline-queue.v1";
const CHANNEL = "anno.offline-queue.v1";
const REPLAY_IDS_KEY = `${KEY}.replay-ids`;
const REPLAY_ID_TTL_MS = 24 * 60 * 60 * 1000;

export type OfflineOp =
  | {
      kind: "create";
      id: string;
      taskId: string;
      userId?: string;
      projectId?: string;
      videoSegmentId?: string | null;
      tmpId?: string;
      replayStarted?: boolean;
      payload: unknown;
      ts: number;
      retry_count?: number;
    }
  | {
      kind: "update";
      id: string;
      taskId: string;
      userId?: string;
      projectId?: string;
      videoSegmentId?: string | null;
      annotationId: string;
      payload: unknown;
      etag?: string;
      ts: number;
      retry_count?: number;
    }
  | {
      kind: "delete";
      id: string;
      taskId: string;
      userId?: string;
      projectId?: string;
      videoSegmentId?: string | null;
      annotationId: string;
      ts: number;
      retry_count?: number;
    };

let memCache: OfflineOp[] | null = null;
export interface OfflineQueueScope {
  videoSegmentId?: string | null;
  userId: string;
  taskId?: string;
  projectId?: string;
  operationId?: string;
  isCurrent?: () => boolean;
}

type ReplayedIds = Record<string, { id: string; expiresAt: number }>;

function replayIdKey(scope: OfflineQueueScope, tmpId: string): string {
  return JSON.stringify([scope.userId, scope.taskId, tmpId]);
}
type QueueScopeInput = OfflineQueueScope | string | null | undefined;

interface QueueSubscription {
  cb: (count: number) => void;
  scope?: QueueScopeInput;
}

const subs = new Set<QueueSubscription>();
const queueStore = createStore("keyval-store", "keyval");
let accessTail: Promise<unknown> = Promise.resolve();
const activeDrains = new Map<string, Promise<{ ok: number; failed: number }>>();
let drainTail: Promise<unknown> = Promise.resolve();

function withDrainLock<T>(work: () => Promise<T>): Promise<T> {
  if (typeof navigator !== "undefined" && navigator.locks) {
    return navigator.locks.request(`${KEY}.drain`, work);
  }
  const pending = drainTail.then(work);
  drainTail = pending.catch(() => undefined);
  return pending;
}

function serialize<T>(work: () => Promise<T>): Promise<T> {
  const result = accessTail.then(work);
  accessTail = result.catch(() => undefined);
  return result;
}

function decodeQueue(raw: unknown): OfflineOp[] {
  if (raw === undefined) return [];
  if (!Array.isArray(raw)) throw new Error("Stored offline queue is not an array");
  return raw;
}

function normalizeScope(scope: QueueScopeInput): OfflineQueueScope | undefined {
  if (!scope) return undefined;
  return typeof scope === "string" ? { userId: scope } : scope;
}

function scopeKey(scope: QueueScopeInput): string {
  const normalized = normalizeScope(scope);
  if (!normalized) return "*";
  return JSON.stringify([
    normalized.userId,
    normalized.projectId,
    normalized.taskId,
    normalized.videoSegmentId === null ? "" : normalized.videoSegmentId,
    normalized.operationId,
  ]);
}

function currentUserId(): string | undefined {
  try {
    return useAuthStore.getState().user?.id ?? undefined;
  } catch {
    return undefined;
  }
}

function isOwnedBy(op: OfflineOp, scope: QueueScopeInput): boolean {
  const normalized = normalizeScope(scope);
  if (!normalized) return true;
  if (!op.userId || op.userId !== normalized.userId) return false;
  if (normalized.taskId && op.taskId !== normalized.taskId) return false;
  if (normalized.projectId && op.projectId !== normalized.projectId) return false;
  if (normalized.videoSegmentId !== undefined) {
    const payload = op.kind === "create" ? op.payload : undefined;
    const legacySegment =
      payload && typeof payload === "object" && "video_segment_id" in payload
        ? payload.video_segment_id
        : null;
    if ((op.videoSegmentId ?? legacySegment ?? null) !== normalized.videoSegmentId) return false;
  }
  if (normalized.operationId && op.id !== normalized.operationId) return false;
  return true;
}

function scopedQueue(queue: OfflineOp[], scope: QueueScopeInput): OfflineOp[] {
  return scope ? queue.filter((op) => isOwnedBy(op, scope)) : queue;
}

function ownerForOp(op: OfflineOp, scope: QueueScopeInput): OfflineQueueScope | undefined {
  const normalized = normalizeScope(scope);
  const userId = normalized?.userId ?? op.userId ?? currentUserId();
  if (!userId) return normalized;
  if (op.userId && op.userId !== userId) {
    throw new Error("Offline operation owner does not match the active account");
  }
  return { ...normalized, userId };
}

function stampOwner(op: OfflineOp, scope: QueueScopeInput): OfflineOp {
  const owner = ownerForOp(op, scope);
  if (!owner?.userId) return structuredClone(op);
  const snapshot = structuredClone({
    ...op,
    userId: owner.userId,
    projectId: op.projectId ?? owner.projectId,
  });
  if (!isOwnedBy(snapshot, owner)) throw new Error("Offline operation scope does not match");
  return snapshot;
}

let bc: BroadcastChannel | null = null;
try {
  bc = typeof BroadcastChannel !== "undefined" ? new BroadcastChannel(CHANNEL) : null;
} catch {
  bc = null;
}
if (bc) {
  bc.onmessage = (ev) => {
    if (ev.data?.type === "changed") {
      readStored().then(notify, () => undefined);
    }
  };
}

function broadcast() {
  try {
    bc?.postMessage({ type: "changed", ts: Date.now() });
  } catch {
    /* ignore */
  }
}

function readStored(): Promise<OfflineOp[]> {
  return serialize(async () => {
    const queue = decodeQueue(await get(KEY, queueStore));
    memCache = queue;
    return queue;
  });
}

async function load(): Promise<OfflineOp[]> {
  try {
    return await readStored();
  } catch {
    return memCache ?? [];
  }
}

function mutateQueue(
  reducer: (queue: OfflineOp[]) => OfflineOp[] | null,
  discardStoredValue = false,
  recordRemap?: { key: string; realId: string },
): Promise<void> {
  return serialize(async () => {
    let committed: OfflineOp[] | null = null;
    await queueStore("readwrite", (store) => {
      const completion = promisifyRequest(store.transaction);
      let failure: unknown;
      const abort = (error: unknown) => {
        failure = error;
        try {
          store.transaction.abort();
        } catch (abortError) {
          failure ??= abortError;
        }
      };
      try {
        const request = store.get(KEY);
        request.onsuccess = () => {
          try {
            committed = reducer(discardStoredValue ? [] : decodeQueue(request.result));
            if (committed !== null) store.put(committed, KEY);
            if (recordRemap) {
              const readIds = store.get(REPLAY_IDS_KEY);
              readIds.onsuccess = () => {
                try {
                  const now = Date.now();
                  const ids = Object.fromEntries(
                    Object.entries((readIds.result ?? {}) as ReplayedIds).filter(
                      ([, entry]) => entry.expiresAt > now,
                    ),
                  );
                  ids[recordRemap.key] = {
                    id: recordRemap.realId,
                    expiresAt: now + REPLAY_ID_TTL_MS,
                  };
                  store.put(ids, REPLAY_IDS_KEY);
                } catch (error) {
                  abort(error);
                }
              };
            }
          } catch (error) {
            abort(error);
          }
        };
      } catch (error) {
        abort(error);
      }
      return completion.catch((error) => {
        throw failure ?? error ?? new Error("Offline queue transaction aborted");
      });
    });
    if (committed !== null) {
      memCache = committed;
      notify();
      broadcast();
    }
  });
}

function notify() {
  const queue = memCache ?? [];
  subs.forEach(({ cb, scope }) => {
    try {
      cb(scopedQueue(queue, scope).length);
    } catch {
      /* ignore */
    }
  });
}

export async function enqueueDurably(op: OfflineOp, scope?: QueueScopeInput): Promise<void> {
  const snapshot = stampOwner(op, scope);
  await mutateQueue((queue) => [...queue, snapshot]);
}

export async function count(scope?: QueueScopeInput): Promise<number> {
  const q = await load();
  return scopedQueue(q, scope).length;
}

export async function countDurably(scope?: QueueScopeInput): Promise<number> {
  return scopedQueue(await readStored(), scope).length;
}

export async function getAll(scope?: QueueScopeInput): Promise<OfflineOp[]> {
  const q = await load();
  return structuredClone(scopedQueue(q, scope));
}

export async function removeDurablyById(id: string, scope?: QueueScopeInput): Promise<void> {
  await mutateQueue((queue) =>
    queue.some((op) => op.id === id && isOwnedBy(op, scope))
      ? queue.filter((op) => !(op.id === id && isOwnedBy(op, scope)))
      : null,
  );
}

export function cancelOfflineCreate(
  tmpId: string,
  scope: OfflineQueueScope & { taskId: string },
): Promise<boolean> {
  return withDrainLock(async () => {
    let found = false;
    await mutateQueue((queue) => {
      if (scope.isCurrent && !scope.isCurrent()) throw new Error("离线操作所属账号已切换");
      const pending = queue.find(
        (op) => op.kind === "create" && op.tmpId === tmpId && isOwnedBy(op, scope),
      );
      if (pending?.kind !== "create") return null;
      if (pending.replayStarted) throw new Error("该创建已开始同步，请联网确认后重试撤销");
      found = true;
      const taskScope = { userId: scope.userId, taskId: scope.taskId };
      return queue.filter((op) => {
        if (!isOwnedBy(op, taskScope)) return true;
        if (op.videoSegmentId != null && op.videoSegmentId !== scope.videoSegmentId) return true;
        return op.kind === "create" ? op.tmpId !== tmpId : op.annotationId !== tmpId;
      });
    });
    return found;
  });
}

export async function resolveOfflineAnnotationId(
  tmpId: string,
  scope: OfflineQueueScope,
): Promise<string | undefined> {
  const ids = await get<ReplayedIds>(REPLAY_IDS_KEY, queueStore);
  const receipt = ids?.[replayIdKey(scope, tmpId)];
  if (receipt && receipt.expiresAt > Date.now()) return receipt.id;
}

export async function replaceAnnotationId(
  oldId: string,
  newId: string,
  scope?: QueueScopeInput,
  replayedCreateId?: string,
): Promise<void> {
  const owner = normalizeScope(scope);
  const recordRemap =
    replayedCreateId && owner?.taskId
      ? { key: replayIdKey(owner, oldId), realId: newId }
      : undefined;
  await mutateQueue(
    (queue) => {
      let changed = false;
      const next: OfflineOp[] = [];
      for (const op of queue) {
        if (!isOwnedBy(op, scope)) {
          next.push(op);
        } else if (op.kind === "create") {
          if (op.id === replayedCreateId && op.tmpId === oldId) changed = true;
          else next.push(op);
        } else if (op.annotationId === oldId) {
          changed = true;
          next.push({ ...op, annotationId: newId });
        } else {
          next.push(op);
        }
      }
      return changed ? next : null;
    },
    false,
    recordRemap,
  );
}

export interface DrainOptions {
  shouldProcess?: (op: OfflineOp) => boolean | "indeterminate" | Promise<boolean | "indeterminate">;
  classifyError?: (error: unknown, op: OfflineOp) => "denied" | "missing" | "retry";
  /**
   * @deprecated Legacy boolean classifier kept for compatibility; prefer
   * {@link classifyError}.  True is equivalent to "denied".
   */
  isAuthorityDenial?: (error: unknown) => boolean;
}

export function drain(
  handler: (op: OfflineOp) => Promise<void>,
  scope?: QueueScopeInput,
  options?: DrainOptions,
): Promise<{ ok: number; failed: number; denied?: number; deferred?: number }> {
  const key = scopeKey(scope);
  const existing = activeDrains.get(key);
  if (existing) return existing;
  const running = withDrainLock(() => runDrain(handler, scope, options)).finally(() => {
    if (activeDrains.get(key) === running) activeDrains.delete(key);
  });
  activeDrains.set(key, running);
  return running;
}

async function runDrain(
  handler: (op: OfflineOp) => Promise<void>,
  scope?: QueueScopeInput,
  options?: DrainOptions,
) {
  let ok = 0;
  let failed = 0;
  let denied = 0;
  let deferred = 0;
  const skipped = new Set<string>();
  while (true) {
    const normalizedScope = normalizeScope(scope);
    if (normalizedScope?.isCurrent && !normalizedScope.isCurrent()) break;
    let op: OfflineOp | undefined;
    try {
      const queue = await readStored();
      op = queue.find((item) => isOwnedBy(item, scope) && !skipped.has(item.id));
    } catch {
      failed++;
      break;
    }
    if (!op) break;
    if (normalizedScope?.isCurrent && !normalizedScope.isCurrent()) break;
    if (options?.shouldProcess) {
      let allowed: boolean | "indeterminate" = false;
      try {
        allowed = await options.shouldProcess(structuredClone(op));
      } catch {
        allowed = "indeterminate";
      }
      if (allowed === "indeterminate") {
        deferred++;
        const deferredId = op.id;
        await mutateQueue((queue) =>
          queue.some((item) => item.id === deferredId && isOwnedBy(item, scope))
            ? queue.map((item) =>
                item.id === deferredId && isOwnedBy(item, scope)
                  ? { ...item, retry_count: (item.retry_count ?? 0) + 1 }
                  : item,
              )
            : null,
        ).catch(() => undefined);
        break;
      }
      if (!allowed) {
        denied++;
        skipped.add(op.id);
        continue;
      }
    }
    try {
      if (normalizedScope && op.kind === "create" && op.tmpId && !op.replayStarted) {
        const submittedId = op.id;
        let present = false;
        await mutateQueue((queue) => {
          const index = queue.findIndex(
            (item) => item.kind === "create" && item.id === submittedId && isOwnedBy(item, scope),
          );
          const item = queue[index];
          if (!item || item.kind !== "create") return null;
          present = true;
          if (item.replayStarted) return null;
          const next = [...queue];
          next[index] = { ...item, replayStarted: true };
          return next;
        });
        if (!present) continue;
        if (normalizedScope.isCurrent && !normalizedScope.isCurrent()) break;
      }
      await handler(structuredClone(op));
    } catch (error) {
      if (normalizedScope?.isCurrent && !normalizedScope.isCurrent()) break;
      const outcome = options?.classifyError
        ? options.classifyError(error, op)
        : options?.isAuthorityDenial?.(error)
          ? ("denied" as const)
          : ("retry" as const);
      if (outcome === "denied") {
        denied++;
        skipped.add(op.id);
        continue;
      }
      if (outcome === "missing" && op.kind === "delete") {
        try {
          await removeDurablyById(op.id, scope);
          ok++;
        } catch {
          failed++;
          break;
        }
        continue;
      }
      failed++;
      const failedId = op.id;
      await mutateQueue((queue) =>
        queue.some((item) => item.id === failedId && isOwnedBy(item, scope))
          ? queue.map((item) =>
              item.id === failedId && isOwnedBy(item, scope)
                ? { ...item, retry_count: (item.retry_count ?? 0) + 1 }
                : item,
            )
          : null,
      ).catch(() => undefined);
      break;
    }
    try {
      await removeDurablyById(op.id, scope);
      ok++;
    } catch {
      failed++;
      break;
    }
  }
  const result = options ? { ok, failed, denied, deferred } : { ok, failed };
  return result;
}

export async function clearAll(scope?: QueueScopeInput): Promise<void> {
  await mutateQueue((queue) => {
    if (!scope) return [];
    const next = queue.filter((op) => !isOwnedBy(op, scope));
    return next.length === queue.length ? null : next;
  }, !scope);
}

export function subscribe(cb: (count: number) => void, scope?: QueueScopeInput): () => void {
  const subscription = { cb, scope };
  subs.add(subscription);
  readStored().then(
    (queue) => {
      if (subs.has(subscription)) {
        try {
          cb(scopedQueue(queue, scope).length);
        } catch {
          /* ignore */
        }
      }
    },
    () => undefined,
  );
  return () => {
    subs.delete(subscription);
  };
}

export function isOfflineCandidate(err: unknown): boolean {
  if (err instanceof TypeError) return true;
  if (err && typeof err === "object" && "status" in err) {
    const s = (err as { status?: number }).status;
    return typeof s === "number" && s >= 500;
  }
  return false;
}

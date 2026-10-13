// 标注工作台离线队列（v0.5.4 Phase 1，v0.5.5 phase 2 加多 tab 同步）。
// idb-keyval 持久化；网络抖动 / 后端 5xx 时由 WorkbenchShell 调用 enqueue。
// 恢复在线后由 useOnlineStatus 触发 drain。

import { createStore, get, promisifyRequest } from "idb-keyval";
import { useAuthStore } from "@/stores/authStore";

const KEY = "anno.offline-queue.v1";
const CHANNEL = "anno.offline-queue.v1";

export type OfflineOp =
  | {
      kind: "create";
      id: string;
      taskId: string;
      /** Owning account. Missing on legacy entries; those entries are retained but never claimed by a scoped account. */
      userId?: string;
      projectId?: string;
      tmpId?: string;
      payload: unknown;
      ts: number;
      retry_count?: number;
    }
  | {
      kind: "update";
      id: string;
      taskId: string;
      /** Owning account. Missing on legacy entries; those entries are retained but never claimed by a scoped account. */
      userId?: string;
      projectId?: string;
      annotationId: string;
      payload: unknown;
      /** Preserve the original version check for conditional geometry updates. */
      etag?: string;
      ts: number;
      retry_count?: number;
    }
  | {
      kind: "delete";
      id: string;
      taskId: string;
      /** Owning account. Missing on legacy entries; those entries are retained but never claimed by a scoped account. */
      userId?: string;
      projectId?: string;
      annotationId: string;
      ts: number;
      retry_count?: number;
    };

let memCache: OfflineOp[] | null = null;
export interface OfflineQueueScope {
  userId: string;
  /** Optional task/project narrowing for drawer actions and targeted cleanup. */
  taskId?: string;
  projectId?: string;
  operationId?: string;
  /** Stop a stale drain before it can use a new account's credentials or UI owner. */
  isCurrent?: () => boolean;
}

type QueueScopeInput = OfflineQueueScope | string | null | undefined;

interface QueueSubscription {
  cb: (count: number) => void;
  scope?: QueueScopeInput;
}

const subs = new Set<QueueSubscription>();
// Keep the existing idb-keyval database/store so already queued operations survive.
const queueStore = createStore("keyval-store", "keyval");
let accessTail: Promise<unknown> = Promise.resolve();
const activeDrains = new Map<string, Promise<{ ok: number; failed: number }>>();
let drainTail: Promise<unknown> = Promise.resolve();

function withDrainLock<T>(work: () => Promise<T>): Promise<T> {
  // The entire request + durable acknowledgement must share one lock across
  // tabs and overlapping user/task/single-item scopes. The browser releases it
  // if a tab closes. Server idempotency also covers a lost response/acknowledgement.
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
  return `${normalized.userId}|${normalized.projectId ?? "*"}|${normalized.taskId ?? "*"}|${normalized.operationId ?? "*"}`;
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
  // Legacy rows deliberately remain invisible to a scoped account. They are
  // retained until their original account is restored or an explicit migration
  // tool handles them; never silently assign them to the current account.
  if (!op.userId || op.userId !== normalized.userId) return false;
  if (normalized.taskId && op.taskId !== normalized.taskId) return false;
  if (normalized.projectId && op.projectId !== normalized.projectId) return false;
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
  return structuredClone({ ...op, userId: owner.userId });
}

// 多 tab 同步：BroadcastChannel 广播队列变更事件
let bc: BroadcastChannel | null = null;
try {
  bc = typeof BroadcastChannel !== "undefined" ? new BroadcastChannel(CHANNEL) : null;
} catch {
  bc = null;
}
if (bc) {
  bc.onmessage = (ev) => {
    if (ev.data?.type === "changed") {
      // 其它 tab 改了 idb；重读，再通知本 tab 订阅者
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
    // Existing count/drawer callers are best-effort readers. Never cache a failed
    // read as an empty queue, and never use this fallback as input to a write.
    return memCache ?? [];
  }
}

/** Atomic read/modify/write; reducer returns null when no write is needed. */
function mutateQueue(
  reducer: (queue: OfflineOp[]) => OfflineOp[] | null,
  discardStoredValue = false,
): Promise<void> {
  return serialize(async () => {
    let committed: OfflineOp[] | null = null;
    await queueStore("readwrite", (store) => {
      // idb-keyval 6.2.2 update() only installs get.onsuccess. Listen to the
      // transaction before reading so a failed read also rejects, rather than hangs.
      const completion = promisifyRequest(store.transaction);
      let failure: unknown;
      const abort = (error: unknown) => {
        failure = error;
        store.transaction.abort();
      };
      try {
        const request = store.get(KEY);
        request.onsuccess = () => {
          try {
            committed = reducer(discardStoredValue ? [] : decodeQueue(request.result));
            if (committed !== null) store.put(committed, KEY);
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

/** Resolves only after IndexedDB commits the operation; failure does not accept it. */
export async function enqueueDurably(op: OfflineOp, scope?: QueueScopeInput): Promise<void> {
  // Capture caller-owned payloads before waiting behind another queue operation.
  const snapshot = stampOwner(op, scope);
  await mutateQueue((queue) => [...queue, snapshot]);
}

/** Legacy fire-and-forget compatibility. Do not use this as a save acknowledgement. */
export async function enqueue(op: OfflineOp, scope?: QueueScopeInput): Promise<void> {
  try {
    await enqueueDurably(op, scope);
  } catch {
    // Existing callers do not handle rejected promises. New acceptance-sensitive
    // flows must await enqueueDurably and retain their draft when it rejects.
  }
}

export async function count(scope?: QueueScopeInput): Promise<number> {
  const q = await load();
  return scopedQueue(q, scope).length;
}

/** Authoritative read for submission/logout gates; storage failure is unknown, not zero. */
export async function countDurably(scope?: QueueScopeInput): Promise<number> {
  return scopedQueue(await readStored(), scope).length;
}

/** 返回当前队列快照（拷贝，外部修改不会反向影响）。供 OfflineQueueDrawer 渲染。 */
export async function getAll(scope?: QueueScopeInput): Promise<OfflineOp[]> {
  const q = await load();
  return structuredClone(scopedQueue(q, scope));
}

/** 按 op.id 删除单条；用于 OfflineQueueDrawer 的「重试单条成功后弹出 / 删除单条」。 */
export async function removeById(id: string, scope?: QueueScopeInput): Promise<void> {
  // Preserve the legacy best-effort API used by event handlers and undo.
  await removeDurablyById(id, scope).catch(() => undefined);
}

export async function removeDurablyById(id: string, scope?: QueueScopeInput): Promise<void> {
  await mutateQueue((queue) =>
    queue.some((op) => op.id === id && isOwnedBy(op, scope))
      ? queue.filter((op) => !(op.id === id && isOwnedBy(op, scope)))
      : null,
  );
}

/** v0.6.3 P0：离线 create 成功拿到 realId 后，把队列中后续 update/delete op 的 annotationId 同步替换。
 *  否则那些 op 仍带 tmpId 上送，server 必 404。调用方：WorkbenchShell.executeOp 的 create 分支。 */
export async function replaceAnnotationId(
  oldId: string,
  newId: string,
  scope?: QueueScopeInput,
): Promise<void> {
  await mutateQueue((queue) => {
    let changed = false;
    const next = queue.map((op) => {
      if (
        isOwnedBy(op, scope) &&
        (op.kind === "update" || op.kind === "delete") &&
        op.annotationId === oldId
      ) {
        changed = true;
        return { ...op, annotationId: newId };
      }
      return op;
    });
    return changed ? next : null;
  });
}

/**
 * 顺序消费队列。默认 handler 抛错时停止 drain（保留剩余项），返回成功条数。
 * 提供 `shouldProcess` 时，返回 false 的 op 会被跳过并保留（不计失败），
 * 让同一队列中其它已授权账号/项目的操作继续同步；返回 "indeterminate" 表示
 * 授权状态未知（网络/服务器故障），op 保留计入 deferred 并停止本次 pass。
 */
export interface DrainOptions {
  shouldProcess?: (op: OfflineOp) => boolean | "indeterminate" | Promise<boolean | "indeterminate">;
  /**
   * Classify a handler error:
   * - "denied":  affirmative authority revocation (403 from a replay).  The op
   *   is retained and skipped without stopping the pass so unrelated authorized
   *   projects still sync; no retry_count is incremented.
   * - "missing": the operation's target no longer exists (task/annotation
   *   deleted elsewhere).  For a delete the desired end state already exists
   *   server-side, so the op is dequeued and counted as ok; create/update are
   *   permanent local failures (retry_count++, stop).
   * - "retry":   transport/server or unknown failures keep the existing
   *   retry_count + stop behavior.
   */
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
  // Denied ops are retained and skipped for the rest of this drain pass so a
  // revoked project cannot block an unrelated authorized project's operations.
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
    // IndexedDB access yields: logout may have switched credentials while the
    // stored operation was being read. Check again before starting the request.
    if (normalizedScope?.isCurrent && !normalizedScope.isCurrent()) break;
    if (options?.shouldProcess) {
      let allowed: boolean | "indeterminate" = false;
      try {
        allowed = await options.shouldProcess(structuredClone(op));
      } catch {
        // An authorizer that throws cannot answer; treat as indeterminate so
        // the op is retried instead of permanently denied.
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
      // The network handler may enqueue or replace IDs: never hold accessTail here.
      await handler(structuredClone(op));
    } catch (error) {
      if (normalizedScope?.isCurrent && !normalizedScope.isCurrent()) break;
      const outcome = options?.classifyError
        ? options.classifyError(error, op)
        : options?.isAuthorityDenial?.(error)
          ? ("denied" as const)
          : ("retry" as const);
      if (outcome === "denied") {
        // Permanent authority denial: retain the draft but skip it so a later
        // authorized op in the same account queue can still sync.
        denied++;
        skipped.add(op.id);
        continue;
      }
      if (outcome === "missing" && op.kind === "delete") {
        // The annotation is already gone server-side: the delete's desired end
        // state exists, so the op leaves the queue and counts as synced.
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
      // Re-read within the write transaction; concurrent enqueues/replacements
      // must not be overwritten by the snapshot passed to the network handler.
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

/** 清空队列（仅用于测试 / 手动 reset）。 */
export async function clearAll(scope?: QueueScopeInput): Promise<void> {
  await mutateQueue((queue) => {
    if (!scope) return [];
    const next = queue.filter((op) => !isOwnedBy(op, scope));
    return next.length === queue.length ? null : next;
  }, !scope).catch(() => undefined);
}

export function subscribe(cb: (count: number) => void, scope?: QueueScopeInput): () => void {
  const subscription = { cb, scope };
  subs.add(subscription);
  // 初始通知
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

/**
 * 判断错误是否应进队列（网络断 / 5xx）。
 * 网络层 fetch reject 会丢一个 TypeError；ApiError 带 status >= 500 也算。
 */
export function isOfflineCandidate(err: unknown): boolean {
  if (err instanceof TypeError) return true;
  if (err && typeof err === "object" && "status" in err) {
    const s = (err as { status?: number }).status;
    return typeof s === "number" && s >= 500;
  }
  return false;
}

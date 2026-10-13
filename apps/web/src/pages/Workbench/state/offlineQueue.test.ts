// Model IndexedDB's structured clones and transaction completion separately from
// put(), including failures before get.onsuccess and after a successful put().

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/api/client";

const storage = vi.hoisted(() => ({
  values: new Map<string, unknown>(),
  readError: null as Error | null,
  readThrow: null as Error | null,
  writeError: null as Error | null,
  commitError: null as Error | null,
  commitGate: null as Promise<void> | null,
  writes: vi.fn(),
}));

vi.mock("idb-keyval", async (importOriginal) => {
  const actual = await importOriginal<typeof import("idb-keyval")>();
  return {
    ...actual,
    createStore: vi.fn(
      () => (_mode: IDBTransactionMode, callback: (store: IDBObjectStore) => unknown) => {
        let settled = false;
        let pending: { key: string; value: unknown } | null = null;
        const transaction = {
          error: null as Error | null,
          oncomplete: null as (() => void) | null,
          onabort: null as (() => void) | null,
          onerror: null as (() => void) | null,
          abort() {
            if (settled) return;
            settled = true;
            transaction.error ??= new Error("Transaction aborted");
            queueMicrotask(() => transaction.onabort?.());
          },
        };
        const finish = async () => {
          if (pending && storage.commitGate) {
            const gate = storage.commitGate;
            storage.commitGate = null;
            await gate;
          }
          if (settled) return;
          if (pending && storage.commitError) {
            transaction.error = storage.commitError;
            storage.commitError = null;
            transaction.abort();
            return;
          }
          if (pending) storage.values.set(pending.key, structuredClone(pending.value));
          settled = true;
          transaction.oncomplete?.();
        };
        const store = {
          transaction,
          get(key: string) {
            if (storage.readThrow) {
              const error = storage.readThrow;
              storage.readThrow = null;
              throw error;
            }
            const request = {
              result: undefined as unknown,
              error: null as Error | null,
              onsuccess: null as (() => void) | null,
              onerror: null as (() => void) | null,
            };
            queueMicrotask(() => {
              if (storage.readError) {
                request.error = transaction.error = storage.readError;
                storage.readError = null;
                request.onerror?.();
                transaction.abort();
                return;
              }
              request.result = structuredClone(storage.values.get(key));
              request.onsuccess?.();
              queueMicrotask(() => void finish());
            });
            return request;
          },
          put(value: unknown, key: string) {
            storage.writes(key, value);
            if (storage.writeError) {
              const error = storage.writeError;
              storage.writeError = null;
              throw error;
            }
            pending = { key, value: structuredClone(value) };
          },
        };
        return Promise.resolve(callback(store as unknown as IDBObjectStore));
      },
    ),
  };
});

import {
  clearAll,
  drain,
  enqueue,
  enqueueDurably,
  getAll,
  count,
  removeById,
  replaceAnnotationId,
  subscribe,
  type OfflineOp,
  type OfflineQueueScope,
} from "./offlineQueue";

beforeEach(async () => {
  storage.values.clear();
  storage.readError = storage.readThrow = storage.writeError = storage.commitError = null;
  storage.commitGate = null;
  await clearAll();
  storage.writes.mockClear();
});

afterEach(() => {
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

const KEY = "anno.offline-queue.v1";

function deleteOp(id: string, annotationId = id): OfflineOp {
  return { kind: "delete", id, taskId: "task", annotationId, ts: 1 };
}

function updateOp(id: string, annotationId = id): OfflineOp {
  return {
    kind: "update",
    id,
    taskId: "task",
    annotationId,
    payload: { attributes: {} },
    etag: 'W/"1"',
    ts: 1,
  };
}

function ownedDeleteOp(id: string, userId: string): OfflineOp {
  return { ...deleteOp(id), userId };
}

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

it("serializes overlapping drains from independent tabs with one browser lock", async () => {
  let tail = Promise.resolve();
  const request = vi.fn((_name: string, work: () => Promise<unknown>) => {
    const result = tail.then(work);
    tail = result.then(
      () => undefined,
      () => undefined,
    );
    return result;
  });
  vi.stubGlobal("navigator", { locks: { request } });
  vi.resetModules();
  const otherTab = await import("./offlineQueue");
  await enqueueDurably(ownedDeleteOp("shared", "alice"));
  const gate = deferred();
  const handler = vi.fn(async () => {
    await gate.promise;
  });
  const first = drain(handler, { userId: "alice", taskId: "task" });
  const second = otherTab.drain(handler, { userId: "alice" });
  await vi.waitFor(() => expect(handler).toHaveBeenCalledTimes(1));
  expect(request).toHaveBeenCalledTimes(2);
  expect(request.mock.calls[0][0]).toBe(request.mock.calls[1][0]);
  gate.resolve();
  await Promise.all([first, second]);
  expect(handler).toHaveBeenCalledTimes(1);
  expect(await getAll({ userId: "alice" })).toEqual([]);
});

describe("offlineQueue durable acceptance", () => {
  it("waits for transaction completion before resolving or notifying subscribers", async () => {
    const subscriber = vi.fn();
    const unsubscribe = subscribe(subscriber);
    await count();
    subscriber.mockClear();
    const gate = deferred();
    storage.commitGate = gate.promise;
    const accepted = vi.fn();
    const pending = enqueueDurably(deleteOp("new")).then(accepted);
    try {
      await vi.waitFor(() => expect(storage.writes).toHaveBeenCalledOnce());
      expect(accepted).not.toHaveBeenCalled();
      expect(subscriber).not.toHaveBeenCalled();
      expect(storage.values.get(KEY)).toEqual([]);
    } finally {
      gate.resolve();
      await pending;
      unsubscribe();
    }
    expect(accepted).toHaveBeenCalledOnce();
    expect(subscriber).toHaveBeenCalledWith(1);
    expect(await getAll()).toEqual([deleteOp("new")]);
  });

  it.each(["readError", "readThrow", "writeError", "commitError"] as const)(
    "%s rejects durable enqueue without publishing acceptance; a later retry preserves saved data",
    async (failure) => {
      await enqueueDurably(deleteOp("saved"));
      const subscriber = vi.fn();
      const unsubscribe = subscribe(subscriber);
      await count();
      subscriber.mockClear();
      const error = new Error(failure);
      storage[failure] = error;
      try {
        await expect(enqueueDurably(deleteOp("new"))).rejects.toBe(error);
        expect(subscriber).not.toHaveBeenCalled();
        expect(await getAll()).toEqual([deleteOp("saved")]);
        expect(storage.values.get(KEY)).toEqual([deleteOp("saved")]);
        await enqueueDurably(deleteOp("new"));
        expect(await getAll()).toEqual([deleteOp("saved"), deleteOp("new")]);
      } finally {
        unsubscribe();
      }
    },
  );

  it("a failed display read cannot overwrite saved operations with an empty fallback", async () => {
    storage.values.set(KEY, [deleteOp("other-tab")]);
    storage.readError = new Error("Read unavailable");
    expect(await getAll()).toEqual([]);
    await enqueueDurably(deleteOp("new"));
    expect(await getAll()).toEqual([deleteOp("other-tab"), deleteOp("new")]);
  });

  it("legacy fire-and-forget enqueue does not reject or accept an uncommitted operation", async () => {
    storage.commitError = new Error("Quota exceeded");
    await expect(enqueue(deleteOp("legacy"))).resolves.toBeUndefined();
    expect(await getAll()).toEqual([]);
    await enqueueDurably(deleteOp("next"));
    expect(await getAll()).toEqual([deleteOp("next")]);
  });

  it("captures input payloads and returns independent snapshots", async () => {
    const op: OfflineOp = {
      kind: "create",
      id: "create",
      taskId: "task",
      payload: { value: 1 },
      ts: 1,
    };
    const pending = enqueueDurably(op);
    (op.payload as { value: number }).value = 2;
    await pending;
    const [snapshot] = await getAll();
    (snapshot as Extract<OfflineOp, { kind: "create" }>).payload = { value: 3 };
    expect((await getAll())[0]).toMatchObject({ payload: { value: 1 } });
  });

  it("rejects corrupt stored data without replacing it", async () => {
    storage.values.set(KEY, { corrupt: true });
    await expect(enqueueDurably(deleteOp("new"))).rejects.toThrow("not an array");
    expect(storage.values.get(KEY)).toEqual({ corrupt: true });
    expect(storage.writes).not.toHaveBeenCalled();
    await clearAll();
    expect(storage.values.get(KEY)).toEqual([]);
  });

  it("keeps invocation order across concurrent durable and legacy enqueues", async () => {
    await Promise.all([
      enqueueDurably(deleteOp("one")),
      enqueue(deleteOp("two")),
      enqueueDurably(deleteOp("three")),
    ]);
    expect((await getAll()).map((op) => op.id)).toEqual(["one", "two", "three"]);
  });
});

describe("offlineQueue.replaceAnnotationId", () => {
  it("把队列中后续 update / delete op 的 annotationId 同步替换", async () => {
    const tmp = "tmp_abc";
    await enqueue({ kind: "create", id: "op1", taskId: "t1", tmpId: tmp, payload: {}, ts: 1 });
    await enqueue({
      kind: "update",
      id: "op2",
      taskId: "t1",
      annotationId: tmp,
      payload: { foo: 1 },
      ts: 2,
    });
    await enqueue({ kind: "delete", id: "op3", taskId: "t1", annotationId: tmp, ts: 3 });
    await enqueue({
      kind: "update",
      id: "op4",
      taskId: "t1",
      annotationId: "other",
      payload: {},
      ts: 4,
    });

    await replaceAnnotationId(tmp, "real-1");

    const all = await getAll();
    expect(all).toHaveLength(4);
    // create 的 tmpId 字段不动（执行 create 时调用方自己换 cache，drain 自然消费）
    expect((all[0] as Extract<OfflineOp, { kind: "create" }>).tmpId).toBe(tmp);
    // update / delete 的 annotationId 已替换
    expect((all[1] as Extract<OfflineOp, { kind: "update" }>).annotationId).toBe("real-1");
    expect((all[2] as Extract<OfflineOp, { kind: "delete" }>).annotationId).toBe("real-1");
    // 不是目标 tmpId 的 op 不动
    expect((all[3] as Extract<OfflineOp, { kind: "update" }>).annotationId).toBe("other");
  });

  it("无匹配项时不写盘", async () => {
    await enqueue({
      kind: "update",
      id: "op1",
      taskId: "t1",
      annotationId: "x",
      payload: {},
      ts: 1,
    });
    storage.writes.mockClear();
    await replaceAnnotationId("not-in-queue", "anything");
    const all = await getAll();
    expect((all[0] as Extract<OfflineOp, { kind: "update" }>).annotationId).toBe("x");
    expect(storage.writes).not.toHaveBeenCalled();
  });
});

describe("offlineQueue.drain · retry_count 累计 + 失败时停止", () => {
  it("handler 抛错 → op.retry_count +1，op 仍留队列", async () => {
    await enqueue({ kind: "delete", id: "op1", taskId: "t1", annotationId: "a", ts: 1 });
    const handler = vi.fn(async () => {
      throw new Error("network");
    });

    const result = await drain(handler);
    expect(result).toEqual({ ok: 0, failed: 1 });

    const all = await getAll();
    expect(all).toHaveLength(1);
    expect(all[0].retry_count).toBe(1);

    // 再 drain 一次应继续累加
    const result2 = await drain(handler);
    expect(result2).toEqual({ ok: 0, failed: 1 });
    const all2 = await getAll();
    expect(all2[0].retry_count).toBe(2);
  });

  it("成功的 op 出队，不写 retry_count", async () => {
    await enqueue({ kind: "delete", id: "op1", taskId: "t1", annotationId: "a", ts: 1 });
    await enqueue({ kind: "delete", id: "op2", taskId: "t1", annotationId: "b", ts: 2 });
    const result = await drain(async () => {
      /* ok */
    });
    expect(result).toEqual({ ok: 2, failed: 0 });
    const all = await getAll();
    expect(all).toHaveLength(0);
  });

  it("半路失败 → 已成功部分出队，失败 op 累计 retry_count，后续保留", async () => {
    await enqueue({ kind: "delete", id: "ok1", taskId: "t1", annotationId: "a", ts: 1 });
    await enqueue({ kind: "delete", id: "fail", taskId: "t1", annotationId: "b", ts: 2 });
    await enqueue({ kind: "delete", id: "later", taskId: "t1", annotationId: "c", ts: 3 });
    let calls = 0;
    const handler = vi.fn(async () => {
      calls++;
      if (calls === 2) throw new Error("boom");
    });

    const result = await drain(handler);
    expect(result).toEqual({ ok: 1, failed: 1 });
    const all = await getAll();
    expect(all.map((o) => o.id)).toEqual(["fail", "later"]);
    expect(all[0].retry_count).toBe(1);
    expect(all[1].retry_count).toBeUndefined();
  });
});

describe("offlineQueue concurrent mutations", () => {
  it("a drain handler can durably enqueue and replace IDs without taking a nested lock", async () => {
    await enqueueDurably({
      kind: "create",
      id: "create",
      taskId: "task",
      tmpId: "tmp",
      payload: {},
      ts: 1,
    });
    await enqueueDurably(deleteOp("delete", "tmp"));
    const observed: OfflineOp[] = [];
    const result = await drain(async (op) => {
      observed.push(op);
      if (op.kind === "create") {
        await enqueueDurably({
          kind: "update",
          id: "update",
          taskId: "task",
          annotationId: "tmp",
          payload: {},
          ts: 2,
        });
        await replaceAnnotationId("tmp", "real");
      }
    });
    expect(result).toEqual({ ok: 3, failed: 0 });
    expect(observed.slice(1)).toMatchObject([
      { id: "delete", annotationId: "real" },
      { id: "update", annotationId: "real" },
    ]);
    expect(await getAll()).toEqual([]);
  });

  it("removes only the processed ID when another operation removes the head during a drain", async () => {
    await enqueueDurably(deleteOp("in-flight"));
    const entered = deferred();
    const release = deferred();
    const observed: string[] = [];
    const running = drain(async (op) => {
      observed.push(op.id);
      if (op.id === "in-flight") {
        entered.resolve();
        await release.promise;
      }
    });
    await entered.promise;
    try {
      await enqueueDurably(deleteOp("later"));
      await removeById("in-flight");
    } finally {
      release.resolve();
    }
    expect(await running).toEqual({ ok: 2, failed: 0 });
    expect(observed).toEqual(["in-flight", "later"]);
    expect(await getAll()).toEqual([]);
  });

  it("a failed handler preserves concurrent enqueues while incrementing only its own retry count", async () => {
    await enqueueDurably(deleteOp("failed"));
    const result = await drain(async () => {
      await enqueueDurably(deleteOp("later"));
      throw new Error("Network unavailable");
    });
    expect(result).toEqual({ ok: 0, failed: 1 });
    expect(await getAll()).toEqual([{ ...deleteOp("failed"), retry_count: 1 }, deleteOp("later")]);
  });

  it("shares concurrent drains within this tab so each operation is sent once", async () => {
    await enqueueDurably(deleteOp("one"));
    const gate = deferred();
    const handler = vi.fn(async () => gate.promise);
    const first = drain(handler);
    const otherHandler = vi.fn(async () => {});
    const second = drain(otherHandler);
    try {
      expect(second).toBe(first);
    } finally {
      gate.resolve();
    }
    expect(await first).toEqual({ ok: 1, failed: 0 });
    expect(handler).toHaveBeenCalledOnce();
    expect(otherHandler).not.toHaveBeenCalled();
  });

  it("keeps the persisted operation when dequeue commit fails after a successful handler", async () => {
    await enqueueDurably(deleteOp("saved"));
    const result = await drain(async () => {
      storage.commitError = new Error("Commit failed");
    });
    expect(result).toEqual({ ok: 0, failed: 1 });
    expect(await getAll()).toEqual([deleteOp("saved")]);
  });

  it("does not execute cached operations when the drain read fails", async () => {
    await enqueueDurably(deleteOp("saved"));
    storage.readError = new Error("Read failed");
    const handler = vi.fn(async () => {});
    expect(await drain(handler)).toEqual({ ok: 0, failed: 1 });
    expect(handler).not.toHaveBeenCalled();
    expect(await getAll()).toEqual([deleteOp("saved")]);
  });

  it("remove and replace mutate the latest stored queue instead of the last displayed snapshot", async () => {
    await enqueueDurably(deleteOp("local", "tmp"));
    storage.values.set(KEY, [deleteOp("local", "tmp"), deleteOp("other-tab", "tmp")]);
    await replaceAnnotationId("tmp", "real");
    await removeById("local");
    expect(await getAll()).toEqual([deleteOp("other-tab", "real")]);
  });

  it.each(["remove", "replace", "clear"] as const)(
    "%s failure leaves persisted and displayed operations unchanged",
    async (mutation) => {
      await enqueueDurably(deleteOp("saved", "tmp"));
      storage.commitError = new Error("Commit failed");
      const pending =
        mutation === "remove"
          ? removeById("saved")
          : mutation === "replace"
            ? replaceAnnotationId("tmp", "real")
            : clearAll();
      if (mutation === "replace") {
        await expect(pending).rejects.toThrow("Commit failed");
      } else {
        await expect(pending).resolves.toBeUndefined();
      }
      expect(await getAll()).toEqual([deleteOp("saved", "tmp")]);
    },
  );
});

describe("offlineQueue account ownership", () => {
  it("scoped count/getAll/drain only expose the owning account", async () => {
    const alice: OfflineQueueScope = { userId: "alice" };
    const bob: OfflineQueueScope = { userId: "bob" };
    await enqueueDurably(ownedDeleteOp("alice-op", "alice"));
    await enqueueDurably(ownedDeleteOp("bob-op", "bob"));

    expect(await count(alice)).toBe(1);
    expect((await getAll(alice)).map((op) => op.id)).toEqual(["alice-op"]);
    const handled: string[] = [];
    expect(
      await drain(async (op) => {
        handled.push(op.id);
      }, alice),
    ).toEqual({ ok: 1, failed: 0 });
    expect(handled).toEqual(["alice-op"]);
    expect((await getAll(bob)).map((op) => op.id)).toEqual(["bob-op"]);
  });

  it("keeps legacy rows unclaimed and stops a stale drain before handler or dequeue", async () => {
    await enqueueDurably(deleteOp("legacy"));
    await enqueueDurably(ownedDeleteOp("alice-op", "alice"));
    const scope: OfflineQueueScope = { userId: "alice", isCurrent: () => false };
    const handler = vi.fn(async () => {});

    expect(await count({ userId: "alice" })).toBe(1);
    expect(await drain(handler, scope)).toEqual({ ok: 0, failed: 0 });
    expect(handler).not.toHaveBeenCalled();
    expect((await getAll()).map((op) => op.id)).toEqual(["legacy", "alice-op"]);
  });

  it("dequeues a successful old-owner operation after the account switches during its handler", async () => {
    let currentUser = "alice";
    const scope: OfflineQueueScope = {
      userId: "alice",
      isCurrent: () => currentUser === "alice",
    };
    await enqueueDurably(ownedDeleteOp("alice-op", "alice"));
    await enqueueDurably(ownedDeleteOp("bob-op", "bob"));
    const entered = deferred();
    const release = deferred();

    const running = drain(async () => {
      entered.resolve();
      currentUser = "bob";
      await release.promise;
    }, scope);
    await entered.promise;
    release.resolve();

    expect(await running).toEqual({ ok: 1, failed: 0 });
    expect((await getAll()).map((op) => op.id)).toEqual(["bob-op"]);
  });

  it("rechecks the account after awaiting the stored queue", async () => {
    await enqueueDurably(ownedDeleteOp("alice-op", "alice"));
    let currentUser = "alice";
    const handler = vi.fn(async () => {});
    const running = drain(handler, {
      userId: "alice",
      isCurrent: () => currentUser === "alice",
    });
    currentUser = "bob";
    await running;
    expect(handler).not.toHaveBeenCalled();
    expect((await getAll({ userId: "alice" })).map((op) => op.id)).toEqual(["alice-op"]);
  });

  it("scoped clear leaves another account and legacy rows intact", async () => {
    await enqueueDurably(deleteOp("legacy"));
    await enqueueDurably(ownedDeleteOp("alice-op", "alice"));
    await enqueueDurably(ownedDeleteOp("bob-op", "bob"));

    await clearAll({ userId: "alice" });
    expect((await getAll()).map((op) => op.id)).toEqual(["legacy", "bob-op"]);
  });
});

describe("offlineQueue.drain · per-operation authorization", () => {
  it("retains a denied project's op and continues with an authorized project", async () => {
    await enqueueDurably({ ...deleteOp("a"), projectId: "A" });
    await enqueueDurably({ ...deleteOp("b"), projectId: "B" });
    const processed: string[] = [];
    const result = await drain(
      async (op) => {
        processed.push(op.id);
      },
      undefined,
      { shouldProcess: async (op) => op.projectId !== "A" },
    );
    expect(result).toEqual({ ok: 1, failed: 0, denied: 1, deferred: 0 });
    expect(processed).toEqual(["b"]);
    const all = await getAll();
    expect(all.map((op) => op.id)).toEqual(["a"]);
    expect(all[0].retry_count).toBeUndefined();
  });

  it("treats an authorizer error as indeterminate: deferred, retained, retry_count bumped", async () => {
    await enqueueDurably({ ...deleteOp("a"), projectId: "A" });
    const handler = vi.fn(async () => {});
    const result = await drain(handler, undefined, {
      shouldProcess: () => {
        throw new Error("boom");
      },
    });
    expect(result).toEqual({ ok: 0, failed: 0, denied: 0, deferred: 1 });
    expect(handler).not.toHaveBeenCalled();
    const all = await getAll();
    expect(all.map((op) => op.id)).toEqual(["a"]);
    expect(all[0].retry_count).toBe(1);
  });
});

describe("offlineQueue.drain · permanent authority denial", () => {
  it("retains a 403-denied op and continues with the next authorized op", async () => {
    await enqueueDurably({ ...deleteOp("a"), projectId: "A" });
    await enqueueDurably({ ...deleteOp("b"), projectId: "B" });
    const processed: string[] = [];
    const result = await drain(
      async (op) => {
        if (op.id === "a") throw new ApiError(403, "forbidden");
        processed.push(op.id);
      },
      undefined,
      {
        classifyError: (error) =>
          error instanceof ApiError && error.status === 403 ? "denied" : "retry",
      },
    );
    expect(result).toEqual({ ok: 1, failed: 0, denied: 1, deferred: 0 });
    expect(processed).toEqual(["b"]);
    const all = await getAll();
    expect(all.map((op) => op.id)).toEqual(["a"]);
    expect(all[0].retry_count).toBeUndefined();
  });

  it("dequeues a delete whose 404 target is already gone and counts it synced", async () => {
    await enqueueDurably({ ...deleteOp("a"), projectId: "A" });
    await enqueueDurably({ ...deleteOp("b"), projectId: "B" });
    const processed: string[] = [];
    const result = await drain(
      async (op) => {
        if (op.id === "a") throw new ApiError(404, "Annotation not found");
        processed.push(op.id);
      },
      undefined,
      {
        classifyError: (error) =>
          error instanceof ApiError && error.status === 404 ? "missing" : "retry",
      },
    );
    expect(result).toEqual({ ok: 2, failed: 0, denied: 0, deferred: 0 });
    expect(processed).toEqual(["b"]);
    // The satisfied delete left the queue and the second op synced cleanly.
    expect(await getAll()).toEqual([]);
  });

  it("keeps a create/update whose 404 target is gone as a retryable failure", async () => {
    await enqueueDurably({ ...updateOp("a") });
    const handler = vi.fn(async () => {
      throw new ApiError(404, "Annotation not found");
    });
    const result = await drain(handler, undefined, {
      classifyError: () => "missing",
    });
    expect(result).toEqual({ ok: 0, failed: 1, denied: 0, deferred: 0 });
    const all = await getAll();
    expect(all.map((op) => op.id)).toEqual(["a"]);
    expect(all[0].retry_count).toBe(1);
  });

  it("stops on a non-authority error as before", async () => {
    await enqueueDurably({ ...deleteOp("a"), projectId: "A" });
    await enqueueDurably({ ...deleteOp("b"), projectId: "B" });
    const result = await drain(
      async () => {
        throw new Error("network");
      },
      undefined,
      { isAuthorityDenial: (error) => error instanceof ApiError && error.status === 403 },
    );
    expect(result).toEqual({ ok: 0, failed: 1, denied: 0, deferred: 0 });
    expect((await getAll()).map((op) => op.id)).toEqual(["a", "b"]);
  });
});

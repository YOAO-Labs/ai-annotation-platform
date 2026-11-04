import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { webcrypto } from "node:crypto";
import type { TrainingDatasetCreate } from "@/api/trainingDatasets";
import { instanceMaskBridge } from "./trainingDatasetBridges";
import {
  discardTrainingDatasetDraft,
  readTrainingDatasetDraft,
  removeMatchingTrainingDatasetDraft,
  saveTrainingDatasetDraft,
  trainingDatasetDraftKey,
  trainingDatasetDraftSnapshot,
  type TrainingDatasetDraftOwner,
  upgradeTrainingDatasetDraft,
} from "./trainingDatasetDraftStorage";

const owner = {
  userId: "user-a",
  sessionId: "session-a",
  projectId: "project-a",
  task: "detect" as const,
};
const body: TrainingDatasetCreate = {
  name: "冻结首轮",
  request_id: "11111111-1111-4111-8111-111111111111",
  preflight_digest: "a".repeat(64),
  task: "detect",
  batch_id: null,
  parent_id: null,
  mode: "reviewed",
  seed: 42,
  validation_fraction: 0.2,
  dataset_id: "11111111-2222-4333-8444-555555555555",
  dataset_filter: { extension: "png", min_width: 100 },
  task_ids: [],
  confirmed_negative_ids: [],
};
const legacyKey = (o: TrainingDatasetDraftOwner = owner) =>
  trainingDatasetDraftKey(o).replace(
    "training:dataset-request:1:",
    "aap-training-dataset-request:1:",
  );
const legacy = (o: TrainingDatasetDraftOwner = owner, b = body) =>
  JSON.stringify({ version: 1, ownerKey: legacyKey(o), body: b });
const guard = () => {};

beforeEach(() => {
  vi.stubGlobal("crypto", webcrypto);
  const queues = new Map<string, Promise<unknown>>();
  vi.stubGlobal("navigator", {
    locks: {
      request: (key: string, callback: () => unknown) => {
        const next = (queues.get(key) ?? Promise.resolve()).then(callback);
        queues.set(
          key,
          next.catch(() => {}),
        );
        return next;
      },
    },
  });
});
afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("training dataset pending request storage", () => {
  it("explicitly upgrades the exact legacy request without sending or changing its suffix", async () => {
    const key = trainingDatasetDraftKey(owner);
    const old = legacyKey();
    expect(key.slice("training:dataset-request:1:".length)).toBe(
      old.slice("aap-training-dataset-request:1:".length),
    );
    localStorage.setItem(old, legacy());
    expect(await readTrainingDatasetDraft(owner)).toMatchObject({ body: null, legacy: true });
    expect(await upgradeTrainingDatasetDraft(owner, guard)).toBeNull();
    expect(localStorage.getItem(old)).toBe(legacy());
    expect(JSON.parse(localStorage.getItem(key)!)).toEqual({ version: 1, ownerKey: key, body });
    expect(await readTrainingDatasetDraft(owner)).toEqual({ body, error: null });
  });

  it("preserves exact requests, rejects replacements and removes only the complete matching body", async () => {
    expect(await saveTrainingDatasetDraft(owner, body)).toBeNull();
    const changed = { ...body, name: "changed" };
    expect(await saveTrainingDatasetDraft(owner, changed)).toContain("另一个页面");
    expect(await removeMatchingTrainingDatasetDraft(owner, changed)).toBeNull();
    expect((await readTrainingDatasetDraft(owner)).body).toEqual(body);
    expect(await removeMatchingTrainingDatasetDraft(owner, body)).toBeNull();
    expect((await readTrainingDatasetDraft(owner)).body).toBeNull();
  });

  it.each([
    { userId: "user-b" },
    { sessionId: "session-b" },
    { projectId: "project-b" },
    { task: "segment" as const },
  ])("isolates another owner %j", async (change) => {
    await saveTrainingDatasetDraft(owner, body);
    expect((await readTrainingDatasetDraft({ ...owner, ...change })).body).toBeNull();
  });

  it("isolates polygon and instance-mask bridge requests", async () => {
    const polygon = { ...owner, task: "segment" as const };
    const mask = { ...polygon, bridge: instanceMaskBridge } as const;
    const polygonBody = { ...body, task: "segment" as const };
    const maskBody: TrainingDatasetCreate = {
      ...polygonBody,
      bridge: instanceMaskBridge,
      request_id: "22222222-2222-4222-8222-222222222222",
    };
    localStorage.setItem(legacyKey(polygon), legacy(polygon, polygonBody));
    expect((await readTrainingDatasetDraft(mask)).body).toBeNull();
    expect(await upgradeTrainingDatasetDraft(polygon, guard)).toBeNull();
    expect(await saveTrainingDatasetDraft(mask, maskBody)).toBeNull();
    expect((await readTrainingDatasetDraft(polygon)).body).toEqual(polygonBody);
    expect(await removeMatchingTrainingDatasetDraft(mask, maskBody)).toBeNull();
    expect((await readTrainingDatasetDraft(polygon)).body).toEqual(polygonBody);
  });

  it.each([
    "not-json",
    "",
    "null",
    "[]",
    JSON.stringify({ version: 1, ownerKey: "wrong", body }),
    " ".repeat(2 * 1024 * 1024 + 1),
  ])("retains unreadable legacy data without overwriting or sending (%#)", async (source) => {
    localStorage.setItem(legacyKey(), source);
    expect(await upgradeTrainingDatasetDraft(owner, guard)).toBeTruthy();
    expect(await saveTrainingDatasetDraft(owner, body)).toBeTruthy();
    expect(localStorage.getItem(legacyKey())).toBe(source);
    expect(localStorage.getItem(trainingDatasetDraftKey(owner))).toBeNull();
  });

  it.each([
    { ...body, task: "segment" },
    { ...body, request_id: "invalid" },
    { ...body, seed: -1 },
    { ...body, task_ids: [null] },
    { ...body, extra: true },
    { ...body, dataset_filter: { page: 9 } },
  ])("retains invalid current records instead of replacing them", async (invalid) => {
    const key = trainingDatasetDraftKey(owner);
    const source = JSON.stringify({ version: 1, ownerKey: key, body: invalid });
    localStorage.setItem(key, source);
    expect((await readTrainingDatasetDraft(owner)).error).toBeTruthy();
    expect(await saveTrainingDatasetDraft(owner, body)).toBeTruthy();
    expect(await removeMatchingTrainingDatasetDraft(owner, body)).toBeTruthy();
    expect(localStorage.getItem(key)).toBe(source);
  });

  it("does not overwrite a corrupt target during legacy upgrade", async () => {
    const key = trainingDatasetDraftKey(owner);
    localStorage.setItem(key, "corrupt");
    localStorage.setItem(legacyKey(), legacy());
    expect(await upgradeTrainingDatasetDraft(owner, guard)).toBeTruthy();
    expect(localStorage.getItem(key)).toBe("corrupt");
    expect(localStorage.getItem(legacyKey())).toBe(legacy());
  });

  it("retains both conflicting copies until an explicit exact-copy discard", async () => {
    await saveTrainingDatasetDraft(owner, body);
    const old = legacy(owner, { ...body, name: "旧请求" });
    localStorage.setItem(legacyKey(), old);
    expect(await upgradeTrainingDatasetDraft(owner, guard)).toContain("冲突");
    expect(await removeMatchingTrainingDatasetDraft(owner, body)).toBeTruthy();
    const snapshot = trainingDatasetDraftSnapshot(owner);
    expect(await discardTrainingDatasetDraft(owner, "legacy", "stale", guard)).toContain("变化");
    expect(localStorage.getItem(legacyKey())).toBe(old);
    expect(await discardTrainingDatasetDraft(owner, "legacy", snapshot.legacy!, guard)).toBeNull();
    expect((await readTrainingDatasetDraft(owner)).body).toEqual(body);
  });

  it("retains a matching legacy copy after explicit upgrade", async () => {
    await saveTrainingDatasetDraft(owner, body);
    localStorage.setItem(legacyKey(), legacy());
    expect((await readTrainingDatasetDraft(owner)).body).toBeNull();
    expect(await upgradeTrainingDatasetDraft(owner, guard)).toBeNull();
    expect((await readTrainingDatasetDraft(owner)).body).toEqual(body);
  });

  it("settles the recovered request without reviving retained data and detects later legacy writes", async () => {
    localStorage.setItem(legacyKey(), legacy());
    expect(await upgradeTrainingDatasetDraft(owner, guard)).toBeNull();
    expect(await removeMatchingTrainingDatasetDraft(owner, body)).toBeNull();
    expect(localStorage.getItem(legacyKey())).toBe(legacy());
    expect((await readTrainingDatasetDraft(owner)).body).toBeNull();
    expect(await saveTrainingDatasetDraft(owner, body)).toContain("已确认");
    const later = { ...body, request_id: "22222222-2222-4222-8222-222222222222" };
    expect(await saveTrainingDatasetDraft(owner, later)).toBeNull();
    localStorage.setItem(legacyKey(), legacy(owner, { ...body, name: "旧标签页修改" }));
    expect((await readTrainingDatasetDraft(owner)).error).toContain("冲突");
    expect(await removeMatchingTrainingDatasetDraft(owner, later)).toContain("冲突");
    expect(localStorage.getItem(trainingDatasetDraftKey(owner))).not.toBeNull();
  });

  it("retains legacy data if target storage fails or a late old writer changes it", async () => {
    const old = legacy();
    localStorage.setItem(legacyKey(), old);
    const full = {
      getItem: localStorage.getItem.bind(localStorage),
      setItem: () => {
        throw new DOMException("Quota exceeded", "QuotaExceededError");
      },
      removeItem: localStorage.removeItem.bind(localStorage),
    };
    expect(await upgradeTrainingDatasetDraft(owner, guard, full)).toBeTruthy();
    expect(localStorage.getItem(legacyKey())).toBe(old);
    const late = legacy(owner, { ...body, name: "late writer" });
    const racing = {
      ...full,
      setItem: (key: string, value: string) => {
        localStorage.setItem(key, value);
        localStorage.setItem(legacyKey(), late);
      },
    };
    expect(await upgradeTrainingDatasetDraft(owner, guard, racing)).toContain("冲突");
    expect(localStorage.getItem(legacyKey())).toBe(late);
    expect(await saveTrainingDatasetDraft(owner, body)).toBeTruthy();
  });

  it("serializes competing writes and fails closed without Web Locks", async () => {
    const other = { ...body, request_id: "22222222-2222-4222-8222-222222222222" };
    const [first, second] = await Promise.all([
      saveTrainingDatasetDraft(owner, body),
      saveTrainingDatasetDraft(owner, other),
    ]);
    expect(first).toBeNull();
    expect(second).toContain("另一个页面");
    expect((await readTrainingDatasetDraft(owner)).body).toEqual(body);
    vi.stubGlobal("navigator", {});
    expect(await saveTrainingDatasetDraft(owner, other)).toContain("Web Locks");
    expect(await upgradeTrainingDatasetDraft(owner, guard)).toContain("Web Locks");
    expect(await removeMatchingTrainingDatasetDraft(owner, body)).toContain("Web Locks");
    expect((await readTrainingDatasetDraft(owner)).body).toEqual(body);
  });

  it("serializes an explicit upgrade against another tab's conflicting save", async () => {
    localStorage.setItem(legacyKey(), legacy());
    const other = { ...body, name: "另一标签页" };
    const [upgrade, save] = await Promise.all([
      upgradeTrainingDatasetDraft(owner, guard),
      saveTrainingDatasetDraft(owner, other),
    ]);
    expect(upgrade).toBeNull();
    expect(save).toContain("另一个页面");
    expect((await readTrainingDatasetDraft(owner)).body).toEqual(body);
    expect(localStorage.getItem(legacyKey())).toBe(legacy());
  });

  it("checks the owner again after the lock is acquired", async () => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const realLocks = navigator.locks;
    const held = realLocks.request(trainingDatasetDraftKey(owner), async () => gate);
    let active = true;
    const pending = saveTrainingDatasetDraft(owner, body, localStorage, () => {
      if (!active) throw new Error("stale");
    });
    active = false;
    release();
    await held;
    expect(await pending).toBeTruthy();
    expect((await readTrainingDatasetDraft(owner)).body).toBeNull();
  });
});

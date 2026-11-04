import { webcrypto } from "node:crypto";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  emptyTrainingDraft,
  inspectTrainingDraftRecords,
  legacyTrainingDraftKey,
  readTrainingDraft,
  removeMatchingTrainingDraft,
  saveTrainingDraft,
  settledTrainingDraft,
  trainingDraftKey,
  trainingDraftOwner,
  upgradeTrainingDraft,
  withTrainingDraftLock,
} from "./trainingDraftStorage";

beforeEach(() => {
  localStorage.clear();
  vi.stubGlobal("crypto", webcrypto);
});
afterEach(() => vi.unstubAllGlobals());
const owner = trainingDraftOwner("user-1", "project-1");

it("persists task selection and rejects a pending body for another task", () => {
  const draft = { ...pendingDraft(), task: "segment" as const };
  expect(saveTrainingDraft({ ...draft, pending: undefined })).toBeNull();
  expect(readTrainingDraft(owner).draft?.task).toBe("segment");
  expect(saveTrainingDraft(draft)).not.toBeNull();
  expect(
    saveTrainingDraft({ ...draft, pending: { body: { ...draft.pending.body, task: "segment" } } }),
  ).toBeNull();
});

it("restores an exact pending study and rejects two pending actions or another request owner", () => {
  const ordinary = pendingDraft();
  const pendingStudy = {
    base: ordinary.pending.body,
    grid: { lr0: [0.01, 0.02] },
    objective: "metrics/mAP50-95" as const,
    direction: "maximize" as const,
    budgets: { max_trials: 2, max_planned_epochs: 100, max_native_seconds: 20 },
    preview_digest: "b".repeat(64),
  };
  const draft = { ...ordinary, pending: undefined, pendingStudy };
  expect(saveTrainingDraft(draft)).toBeNull();
  expect(readTrainingDraft(owner).draft).toEqual(draft);
  for (const changed of [
    { ...draft, pending: ordinary.pending },
    {
      ...draft,
      pendingStudy: {
        ...pendingStudy,
        base: { ...pendingStudy.base, request_id: crypto.randomUUID() },
      },
    },
    {
      ...draft,
      pendingStudy: {
        ...pendingStudy,
        budgets: { ...pendingStudy.budgets, max_native_seconds: -1 },
      },
    },
  ]) {
    localStorage.setItem(trainingDraftKey(owner), JSON.stringify(changed));
    expect(readTrainingDraft(owner).draft).toBeNull();
  }
});

it("preserves a pinned initialization in exact pending bodies and rejects hidden or mismatched sources", () => {
  const old = pendingDraft();
  const initialization = {
    kind: "trained_model" as const,
    model_version_id: crypto.randomUUID(),
    source_digest: "c".repeat(64),
  };
  const draft = {
    ...old,
    initialization,
    pending: { body: { ...old.pending.body, initialization } },
  };
  expect(saveTrainingDraft(draft)).toBeNull();
  expect(readTrainingDraft(owner).draft).toEqual(draft);
  for (const changed of [
    { ...draft, initialization: undefined },
    { ...draft, initialization: { ...initialization, source_digest: "d".repeat(64) } },
    { ...draft, initialization: { ...initialization, url: "https://arbitrary.invalid" } },
    {
      ...draft,
      initialization: { kind: "catalog", model_version_id: initialization.model_version_id },
    },
  ]) {
    localStorage.setItem(trainingDraftKey(owner), JSON.stringify(changed));
    expect(readTrainingDraft(owner).draft).toBeNull();
  }
  expect(
    saveTrainingDraft({
      ...old,
      pending: undefined,
      initialization: { kind: "trained_model", model_version_id: initialization.model_version_id },
    }),
  ).toBeNull();
});
function pendingDraft() {
  const draft = {
    ...emptyTrainingDraft(owner),
    name: "实验",
    datasetId: "dataset-1",
    baseModel: "yolo11n",
    schemaId: "schema-1",
    schemaDigest: "a".repeat(64),
    fields: { lr0: { kind: "scalar" as const, text: "1e-6" } },
  };
  return {
    ...draft,
    pending: {
      body: {
        name: draft.name,
        dataset_id: draft.datasetId,
        framework: "ultralytics" as const,
        base_model: draft.baseModel,
        request_id: draft.requestId,
        configuration: {
          schema_id: draft.schemaId,
          schema_digest: draft.schemaDigest,
          values: { lr0: 0.000001 },
        },
        preflight_digest: "b".repeat(64),
      },
    },
  };
}

it("restores exact typed submission with owner/model/schema identity and no credentials", () => {
  const draft = pendingDraft();
  expect(saveTrainingDraft(draft)).toBeNull();
  expect(readTrainingDraft(owner)).toEqual({ draft, error: null });
  expect(readTrainingDraft(trainingDraftOwner("user-2", "project-1")).draft).toBeNull();
  expect(readTrainingDraft(trainingDraftOwner("user-1", "project-2")).draft).toBeNull();
  expect(localStorage.getItem(trainingDraftKey(owner))).not.toContain("token");
});

it("preserves malformed stored data and surfaces failures", () => {
  const key = trainingDraftKey(owner);
  localStorage.setItem(key, "{broken");
  expect(readTrainingDraft(owner).error).toMatch(/原记录未被自动覆盖/);
  expect(localStorage.getItem(key)).toBe("{broken");
  const draft = pendingDraft();
  expect(
    saveTrainingDraft(draft, {
      setItem: () => {
        throw new Error("quota");
      },
    }),
  ).toMatch(/无法保存/);
  for (const patch of [
    { schemaDigest: "other" },
    { ownerKey: "foreign" },
    { requestId: crypto.randomUUID() },
    { name: "other" },
  ]) {
    localStorage.setItem(key, JSON.stringify({ ...draft, ...patch }));
    expect(readTrainingDraft(owner).draft).toBeNull();
  }
  localStorage.setItem(
    key,
    JSON.stringify({
      ...draft,
      pending: { body: { ...draft.pending.body, parameters: { batch: 2 } } },
    }),
  );
  expect(readTrainingDraft(owner).draft).toBeNull();
});

it("does not clear another tab's newer or different draft", () => {
  const draft = pendingDraft();
  const newer = { ...draft, revision: draft.revision + 1 };
  saveTrainingDraft(newer);
  removeMatchingTrainingDraft(draft);
  expect(readTrainingDraft(owner).draft).toEqual(newer);
  removeMatchingTrainingDraft(newer);
  expect(readTrainingDraft(owner).draft).toBeNull();
});

it("restores exact source identity and pending origin, rejects mismatched or executable metadata", () => {
  const draft = pendingDraft();
  const reference = {
    kind: "recipe" as const,
    recipe_id: crypto.randomUUID(),
    revision_id: crypto.randomUUID(),
    digest: "d".repeat(64),
  };
  const source = {
    reference,
    label: "已固定配方",
    baseSha256: "e".repeat(64),
    baseFilename: "yolo11n.pt",
    imageDigest: `sha256:${"f".repeat(64)}`,
    frameworkVersion: "8.4.177",
    userValues: { lr0: 1e-6 },
    schemaId: draft.schemaId,
    schemaDigest: draft.schemaDigest,
  };
  const next = {
    ...draft,
    source,
    pending: { body: { ...draft.pending.body, origin: reference } },
  };
  expect(saveTrainingDraft(next)).toBeNull();
  expect(readTrainingDraft(owner).draft).toEqual(next);
  const key = trainingDraftKey(owner);
  for (const changed of [
    { ...next, source: { ...source, script: "execute" } },
    { ...next, source: { ...source, baseSha256: "other" } },
    {
      ...next,
      pending: {
        body: { ...next.pending.body, origin: { ...reference, revision_id: crypto.randomUUID() } },
      },
    },
    { ...next, source: { ...source, reference: { ...reference, run_id: crypto.randomUUID() } } },
  ]) {
    localStorage.setItem(key, JSON.stringify(changed));
    expect(readTrainingDraft(owner).draft).toBeNull();
  }
});

it("restores the exact canonical task for a request whose creation response was lost", () => {
  const old = pendingDraft();
  const draft = { ...old, pending: { body: { ...old.pending.body, task: "segment" as const } } };
  expect(saveTrainingDraft(draft)).toBeNull();
  expect(readTrainingDraft(owner).draft?.pending?.body.task).toBe("segment");
  localStorage.setItem(
    trainingDraftKey(owner),
    JSON.stringify({ ...draft, pending: { body: { ...draft.pending.body, task: "aap" } } }),
  );
  expect(readTrainingDraft(owner).draft).toBeNull();
});

it.each(["editable", "pending run", "pending study"])(
  "upgrades the exact %s record only after explicit choice",
  async (kind) => {
    const run = pendingDraft();
    const draft =
      kind === "editable"
        ? { ...run, pending: undefined }
        : kind === "pending study"
          ? {
              ...run,
              pending: undefined,
              pendingStudy: {
                base: run.pending.body,
                grid: { lr0: [0.01, 0.005] },
                budgets: { max_trials: 2, max_planned_epochs: 10, max_native_seconds: 100 },
                objective: "metrics/mAP50-95",
                direction: "maximize",
                preview_digest: "c".repeat(64),
              },
            }
          : run;
    const raw = JSON.stringify(draft);
    localStorage.setItem(legacyTrainingDraftKey(owner), raw);
    const seen = inspectTrainingDraftRecords(owner);
    expect(localStorage.getItem(trainingDraftKey(owner))).toBeNull();
    await upgradeTrainingDraft(owner, seen, "legacy", () => true);
    expect(localStorage.getItem(trainingDraftKey(owner))).toBe(raw);
    expect(localStorage.getItem(legacyTrainingDraftKey(owner))).toBe(raw);
    expect(readTrainingDraft(owner).draft).toEqual(draft);
  },
);

it("keeps the current target and retained legacy source after explicit conflict resolution", async () => {
  const old = JSON.stringify(pendingDraft());
  const current = JSON.stringify({ ...pendingDraft(), pending: undefined, name: "current" });
  localStorage.setItem(legacyTrainingDraftKey(owner), old);
  localStorage.setItem(trainingDraftKey(owner), current);
  await upgradeTrainingDraft(owner, inspectTrainingDraftRecords(owner), "target", () => true);
  expect(localStorage.getItem(legacyTrainingDraftKey(owner))).toBe(old);
  expect(localStorage.getItem(trainingDraftKey(owner))).toBe(current);
  expect(await settledTrainingDraft(inspectTrainingDraftRecords(owner))).toBe(true);
});

it("refuses stale, corrupt, foreign, or unavailable source records without overwriting them", async () => {
  const old = JSON.stringify(pendingDraft());
  localStorage.setItem(legacyTrainingDraftKey(owner), old);
  const seen = inspectTrainingDraftRecords(owner);
  localStorage.setItem(legacyTrainingDraftKey(owner), "{broken");
  await expect(upgradeTrainingDraft(owner, seen, "legacy", () => true)).rejects.toThrow();
  expect(localStorage.getItem(legacyTrainingDraftKey(owner))).toBe("{broken");
  expect(localStorage.getItem(trainingDraftKey(owner))).toBeNull();
  localStorage.setItem(
    legacyTrainingDraftKey(owner),
    JSON.stringify({ ...pendingDraft(), ownerKey: "foreign" }),
  );
  expect(inspectTrainingDraftRecords(owner).legacyInvalid).toBe(true);
  await expect(
    upgradeTrainingDraft(owner, inspectTrainingDraftRecords(owner), "legacy", () => true),
  ).rejects.toThrow();
  expect(localStorage.getItem(trainingDraftKey(owner))).toBeNull();
});

it("leaves the legacy record intact when the target write fails", async () => {
  const raw = JSON.stringify(pendingDraft());
  localStorage.setItem(legacyTrainingDraftKey(owner), raw);
  const seen = inspectTrainingDraftRecords(owner);
  const spy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new DOMException("quota", "QuotaExceededError");
  });
  try {
    await expect(upgradeTrainingDraft(owner, seen, "legacy", () => true)).rejects.toThrow();
    expect(localStorage.getItem(legacyTrainingDraftKey(owner))).toBe(raw);
    expect(localStorage.getItem(trainingDraftKey(owner))).toBeNull();
  } finally {
    spy.mockRestore();
  }
});

it("recognizes a settled source and exposes a late old-tab write without changing either record", async () => {
  const original = JSON.stringify(pendingDraft());
  localStorage.setItem(legacyTrainingDraftKey(owner), original);
  await upgradeTrainingDraft(owner, inspectTrainingDraftRecords(owner), "legacy", () => true);
  expect(await settledTrainingDraft(inspectTrainingDraftRecords(owner))).toBe(true);
  const later = JSON.stringify({ ...pendingDraft(), pending: undefined, name: "late" });
  localStorage.setItem(legacyTrainingDraftKey(owner), later);
  expect(await settledTrainingDraft(inspectTrainingDraftRecords(owner))).toBe(false);
  expect(localStorage.getItem(legacyTrainingDraftKey(owner))).toBe(later);
  expect(localStorage.getItem(trainingDraftKey(owner))).toBe(original);
});

it("does not treat an unreadable record as a missing draft", () => {
  const spy = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
    throw new DOMException("denied", "SecurityError");
  });
  try {
    expect(() => inspectTrainingDraftRecords(owner)).toThrow();
  } finally {
    spy.mockRestore();
  }
});

it("fails closed when Web Locks are unavailable", async () => {
  const original = Object.getOwnPropertyDescriptor(navigator, "locks");
  Object.defineProperty(navigator, "locks", { configurable: true, value: undefined });
  try {
    await expect(withTrainingDraftLock(owner, () => "unsafe")).rejects.toThrow(/Web Locks/);
  } finally {
    if (original) Object.defineProperty(navigator, "locks", original);
    else Reflect.deleteProperty(navigator, "locks");
  }
});

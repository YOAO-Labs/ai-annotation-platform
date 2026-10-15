import { beforeEach, expect, it } from "vitest";
import {
  emptyTrainingDraft,
  readTrainingDraft,
  removeMatchingTrainingDraft,
  saveTrainingDraft,
  trainingDraftKey,
  trainingDraftOwner,
} from "./trainingDraftStorage";

beforeEach(() => localStorage.clear());
const owner = trainingDraftOwner("user-1", "project-1");
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

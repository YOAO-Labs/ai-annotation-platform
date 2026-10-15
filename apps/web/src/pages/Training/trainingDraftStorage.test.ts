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

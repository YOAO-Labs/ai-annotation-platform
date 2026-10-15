import type { TrainingRunCreate } from "@/api/trainingRuns";
import type { ParameterDraft, ParameterDrafts, ParameterValue } from "./trainingConfiguration";

export type TrainingDraft = {
  version: 1;
  ownerKey: string;
  draftId: string;
  revision: number;
  requestId: string;
  name: string;
  datasetId: string;
  framework: string;
  baseModel: string;
  schemaId: string;
  schemaDigest: string;
  fields: ParameterDrafts;
  pending?: { body: TrainingRunCreate };
};

const prefix = "aap-training-draft:1";
const unsafeKeys = new Set(["__proto__", "prototype", "constructor"]);
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const sha = /^[0-9a-f]{64}$/;
export const trainingDraftOwner = (userId: string, projectId: string) => `${userId}:${projectId}`;
export const trainingDraftKey = (ownerKey: string) => `${prefix}:${ownerKey}`;

export function emptyTrainingDraft(ownerKey: string, framework = "ultralytics"): TrainingDraft {
  return {
    version: 1,
    ownerKey,
    draftId: crypto.randomUUID(),
    revision: 0,
    requestId: crypto.randomUUID(),
    name: "",
    datasetId: "",
    framework,
    baseModel: "",
    schemaId: "",
    schemaDigest: "",
    fields: {},
  };
}

function object(value: unknown): value is Record<string, unknown> {
  return (
    !!value &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    Object.getPrototypeOf(value) === Object.prototype
  );
}

function fieldDraft(value: unknown): value is ParameterDraft {
  if (!object(value)) return false;
  if (value.kind === "null") return Object.keys(value).length === 1;
  if (value.kind === "scalar")
    return (
      Object.keys(value).length === 2 && typeof value.text === "string" && value.text.length <= 1000
    );
  return (
    value.kind === "array" &&
    Object.keys(value).length === 2 &&
    Array.isArray(value.items) &&
    value.items.length <= 100 &&
    value.items.every((item) => typeof item === "string" && item.length <= 100)
  );
}

function parameterValue(value: unknown, depth = 0): value is ParameterValue {
  if (depth > 3) return false;
  if (value === null || typeof value === "boolean" || typeof value === "string") return true;
  if (typeof value === "number") return Number.isFinite(value);
  return (
    Array.isArray(value) &&
    value.length <= 100 &&
    value.every((item) => parameterValue(item, depth + 1))
  );
}

function pendingBody(value: unknown): value is TrainingRunCreate {
  if (!object(value) || !object(value.configuration)) return false;
  if (
    Object.keys(value).some(
      (key) =>
        ![
          "name",
          "dataset_id",
          "framework",
          "base_model",
          "request_id",
          "configuration",
          "preflight_digest",
        ].includes(key),
    )
  )
    return false;
  const config = value.configuration;
  return (
    Object.keys(config).every((key) => ["schema_id", "schema_digest", "values"].includes(key)) &&
    typeof value.name === "string" &&
    value.name.length <= 120 &&
    typeof value.dataset_id === "string" &&
    value.dataset_id.length <= 64 &&
    ["ultralytics", "libreyolo"].includes(String(value.framework)) &&
    typeof value.base_model === "string" &&
    value.base_model.length <= 64 &&
    typeof value.request_id === "string" &&
    uuid.test(value.request_id) &&
    typeof value.preflight_digest === "string" &&
    sha.test(value.preflight_digest) &&
    typeof config.schema_id === "string" &&
    config.schema_id.length <= 150 &&
    typeof config.schema_digest === "string" &&
    sha.test(config.schema_digest) &&
    object(config.values) &&
    Object.keys(config.values).length <= 150 &&
    Object.entries(config.values).every(
      ([key, item]) => !unsafeKeys.has(key) && parameterValue(item),
    )
  );
}

function validTrainingDraft(value: unknown, ownerKey: string): value is TrainingDraft {
  if (!object(value) || value.version !== 1 || value.ownerKey !== ownerKey) return false;
  if (
    Object.keys(value).some(
      (key) =>
        ![
          "version",
          "ownerKey",
          "draftId",
          "revision",
          "requestId",
          "name",
          "datasetId",
          "framework",
          "baseModel",
          "schemaId",
          "schemaDigest",
          "fields",
          "pending",
        ].includes(key),
    )
  )
    return false;
  if (
    typeof value.draftId !== "string" ||
    !uuid.test(value.draftId) ||
    typeof value.requestId !== "string" ||
    !uuid.test(value.requestId)
  )
    return false;
  if (!Number.isSafeInteger(value.revision) || Number(value.revision) < 0) return false;
  for (const key of ["name", "datasetId", "framework", "baseModel", "schemaId", "schemaDigest"]) {
    if (typeof value[key] !== "string" || value[key].length > 150) return false;
  }
  if (value.schemaDigest && !sha.test(String(value.schemaDigest))) return false;
  if (
    !object(value.fields) ||
    Object.keys(value.fields).length > 150 ||
    !Object.entries(value.fields).every(
      ([key, item]) => key.length <= 100 && !unsafeKeys.has(key) && fieldDraft(item),
    )
  )
    return false;
  if (value.pending !== undefined) {
    if (
      !object(value.pending) ||
      Object.keys(value.pending).length !== 1 ||
      !pendingBody(value.pending.body)
    )
      return false;
    const body = value.pending.body;
    if (
      body.request_id !== value.requestId ||
      body.framework !== value.framework ||
      body.base_model !== value.baseModel ||
      body.dataset_id !== value.datasetId ||
      body.name !== String(value.name).trim() ||
      body.configuration?.schema_id !== value.schemaId ||
      body.configuration?.schema_digest !== value.schemaDigest
    )
      return false;
  }
  return true;
}

export function readTrainingDraft(
  ownerKey: string,
  storage: Pick<Storage, "getItem"> = localStorage,
): { draft: TrainingDraft | null; error: string | null } {
  try {
    const source = storage.getItem(trainingDraftKey(ownerKey));
    if (!source) return { draft: null, error: null };
    if (source.length > 128 * 1024) throw new Error("草稿超出大小限制");
    const value: unknown = JSON.parse(source);
    if (!validTrainingDraft(value, ownerKey)) throw new Error("草稿格式或归属不匹配");
    return { draft: value, error: null };
  } catch {
    return { draft: null, error: "无法恢复本地草稿，请检查浏览器存储。原记录未被自动覆盖。" };
  }
}

export function saveTrainingDraft(
  draft: TrainingDraft,
  storage: Pick<Storage, "setItem"> = localStorage,
): string | null {
  try {
    if (!validTrainingDraft(draft, draft.ownerKey)) throw new Error("Invalid draft");
    const source = JSON.stringify(draft);
    if (source.length > 128 * 1024) throw new Error("Draft too large");
    storage.setItem(trainingDraftKey(draft.ownerKey), source);
    return null;
  } catch {
    return "浏览器无法保存草稿。请保持此页面，以便重试尚未确认的创建请求。";
  }
}

export function removeMatchingTrainingDraft(
  draft: TrainingDraft,
  storage: Pick<Storage, "getItem" | "removeItem"> = localStorage,
): void {
  const stored = readTrainingDraft(draft.ownerKey, storage).draft;
  if (
    stored?.draftId === draft.draftId &&
    stored.revision === draft.revision &&
    stored.requestId === draft.requestId
  ) {
    storage.removeItem(trainingDraftKey(draft.ownerKey));
  }
}

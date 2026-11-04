import { randomId } from "@/utils/id";
import type { TrainingRunCreate, TrainingInitialization } from "@/api/trainingRuns";
import type { TrainingOrigin } from "@/api/trainingRecipes";
import type { TrainingStudyCreate } from "@/api/trainingStudies";
import type { ParameterDraft, ParameterDrafts, ParameterValue } from "./trainingConfiguration";

export type TrainingDraft = {
  version: 1;
  ownerKey: string;
  draftId: string;
  revision: number;
  requestId: string;
  name: string;
  task?: "detect" | "segment";
  datasetId: string;
  framework: string;
  baseModel: string;
  schemaId: string;
  schemaDigest: string;
  fields: ParameterDrafts;
  source?: TrainingDraftSource;
  initialization?: TrainingInitialization;
  pending?: { body: TrainingRunCreate };
  pendingStudy?: TrainingStudyCreate;
};

export type TrainingDraftSource = {
  reference: TrainingOrigin;
  label: string;
  baseSha256: string;
  baseFilename: string;
  imageDigest: string;
  frameworkVersion: string;
  userValues: Record<string, ParameterValue>;
  schemaId: string;
  schemaDigest: string;
};

const prefix = "training:draft:1";
const legacyPrefix = "aap-training-draft:1";
const unsafeKeys = new Set(["__proto__", "prototype", "constructor"]);
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const sha = /^[0-9a-f]{64}$/;
export const trainingDraftOwner = (userId: string, projectId: string) => `${userId}:${projectId}`;
export const trainingDraftKey = (ownerKey: string) => `${prefix}:${ownerKey}`;
export const legacyTrainingDraftKey = (ownerKey: string) => `${legacyPrefix}:${ownerKey}`;
export const trainingDraftSettlementKey = (ownerKey: string) =>
  `${trainingDraftKey(ownerKey)}:legacy-settled`;

export function emptyTrainingDraft(ownerKey: string, framework = "ultralytics"): TrainingDraft {
  return {
    version: 1,
    ownerKey,
    draftId: randomId(),
    revision: 0,
    requestId: randomId(),
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

function validOrigin(value: unknown): value is TrainingOrigin {
  if (
    !object(value) ||
    Object.keys(value).some(
      (key) =>
        !["kind", "recipe_id", "revision_id", "run_id", "attempt_id", "digest"].includes(key),
    ) ||
    typeof value.digest !== "string" ||
    !sha.test(value.digest)
  )
    return false;
  for (const key of ["recipe_id", "revision_id", "run_id", "attempt_id"])
    if (value[key] != null && (typeof value[key] !== "string" || !uuid.test(value[key])))
      return false;
  return value.kind === "recipe"
    ? !!value.recipe_id && !!value.revision_id && !value.run_id && !value.attempt_id
    : value.kind === "clone" && !!value.run_id && !value.recipe_id && !value.revision_id;
}

export function validInitialization(value: unknown): value is TrainingInitialization {
  if (
    !object(value) ||
    Object.keys(value).some((key) => !["kind", "model_version_id", "source_digest"].includes(key))
  )
    return false;
  if (value.kind === "catalog") return Object.keys(value).length === 1;
  return (
    value.kind === "trained_model" &&
    typeof value.model_version_id === "string" &&
    uuid.test(value.model_version_id) &&
    (value.source_digest === undefined ||
      (typeof value.source_digest === "string" && sha.test(value.source_digest)))
  );
}

function validSource(value: unknown): value is TrainingDraftSource {
  if (!object(value) || Object.keys(value).length !== 9 || !validOrigin(value.reference))
    return false;
  if (
    ![
      "label",
      "baseSha256",
      "baseFilename",
      "imageDigest",
      "frameworkVersion",
      "schemaId",
      "schemaDigest",
    ].every((key) => typeof value[key] === "string" && value[key].length <= 150)
  )
    return false;
  return (
    sha.test(String(value.baseSha256)) &&
    sha.test(String(value.schemaDigest)) &&
    /^sha256:[0-9a-f]{64}$/.test(String(value.imageDigest)) &&
    object(value.userValues) &&
    Object.keys(value.userValues).length <= 150 &&
    Object.entries(value.userValues).every(
      ([key, item]) => !unsafeKeys.has(key) && parameterValue(item),
    )
  );
}

function pendingBody(value: unknown): value is TrainingRunCreate {
  if (!object(value) || !object(value.configuration)) return false;
  if (
    Object.keys(value).some(
      (key) =>
        ![
          "name",
          "task",
          "dataset_id",
          "framework",
          "base_model",
          "request_id",
          "configuration",
          "preflight_digest",
          "origin",
          "initialization",
        ].includes(key),
    )
  )
    return false;
  const config = value.configuration;
  return (
    Object.keys(config).every((key) => ["schema_id", "schema_digest", "values"].includes(key)) &&
    typeof value.name === "string" &&
    (value.task == null || value.task === "detect" || value.task === "segment") &&
    value.name.length <= 120 &&
    typeof value.dataset_id === "string" &&
    value.dataset_id.length <= 64 &&
    ["ultralytics", "libreyolo"].includes(String(value.framework)) &&
    typeof value.base_model === "string" &&
    value.base_model.length <= 64 &&
    (value.origin === undefined || validOrigin(value.origin)) &&
    (value.initialization === undefined ||
      (validInitialization(value.initialization) &&
        (value.initialization.kind === "catalog" || !!value.initialization.source_digest))) &&
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
          "task",
          "datasetId",
          "framework",
          "baseModel",
          "schemaId",
          "schemaDigest",
          "fields",
          "pending",
          "pendingStudy",
          "source",
          "initialization",
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
  if (value.task !== undefined && value.task !== "detect" && value.task !== "segment") return false;
  if (value.source !== undefined && !validSource(value.source)) return false;
  if (value.initialization !== undefined && !validInitialization(value.initialization))
    return false;
  if (
    !object(value.fields) ||
    Object.keys(value.fields).length > 150 ||
    !Object.entries(value.fields).every(
      ([key, item]) => key.length <= 100 && !unsafeKeys.has(key) && fieldDraft(item),
    )
  )
    return false;
  if (value.pending !== undefined || value.pendingStudy !== undefined) {
    if (value.pending !== undefined && value.pendingStudy !== undefined) return false;
    if (value.pendingStudy !== undefined && !pendingStudy(value.pendingStudy)) return false;
    if (
      value.pending !== undefined &&
      (!object(value.pending) ||
        Object.keys(value.pending).length !== 1 ||
        !pendingBody(value.pending.body))
    )
      return false;
    const body =
      value.pendingStudy !== undefined
        ? (value.pendingStudy as TrainingStudyCreate).base
        : (value.pending as { body: TrainingRunCreate }).body;
    if (
      body.request_id !== value.requestId ||
      body.framework !== value.framework ||
      body.base_model !== value.baseModel ||
      body.dataset_id !== value.datasetId ||
      body.name !== String(value.name).trim() ||
      (value.task !== undefined && (body.task ?? "detect") !== value.task) ||
      body.configuration?.schema_id !== value.schemaId ||
      body.configuration?.schema_digest !== value.schemaDigest
    )
      return false;
    if (
      JSON.stringify(body.origin) !==
      JSON.stringify((value.source as TrainingDraftSource | undefined)?.reference)
    )
      return false;
    if (JSON.stringify(body.initialization) !== JSON.stringify(value.initialization)) return false;
  }
  return true;
}

function pendingStudy(value: unknown): value is TrainingStudyCreate {
  if (
    !object(value) ||
    Object.keys(value).some(
      (key) =>
        !["base", "grid", "objective", "direction", "budgets", "preview_digest"].includes(key),
    ) ||
    !pendingBody(value.base) ||
    !object(value.grid) ||
    !object(value.budgets)
  )
    return false;
  const count = Object.keys(value.grid).length;
  if (
    count < 1 ||
    count > 10 ||
    Object.entries(value.grid).some(
      ([key, values]) =>
        unsafeKeys.has(key) ||
        !Array.isArray(values) ||
        values.length < 1 ||
        values.length > 20 ||
        !values.every((item) => parameterValue(item)),
    )
  )
    return false;
  const budgets = value.budgets;
  return (
    Object.keys(budgets).length === 3 &&
    Number.isInteger(budgets.max_trials) &&
    Number(budgets.max_trials) >= 1 &&
    Number(budgets.max_trials) <= 20 &&
    Number.isInteger(budgets.max_planned_epochs) &&
    Number(budgets.max_planned_epochs) >= 1 &&
    Number(budgets.max_planned_epochs) <= 20000 &&
    typeof budgets.max_native_seconds === "number" &&
    Number.isFinite(budgets.max_native_seconds) &&
    budgets.max_native_seconds > 0 &&
    budgets.max_native_seconds <= 604800 &&
    [
      "metrics/mAP50-95",
      "metrics/mAP50",
      "metrics/mAP75",
      "metrics/precision",
      "metrics/recall",
      "metrics/AR100",
    ].includes(String(value.objective)) &&
    value.direction === "maximize" &&
    typeof value.preview_digest === "string" &&
    sha.test(value.preview_digest)
  );
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

export type TrainingDraftRecords = {
  legacy: string | null;
  target: string | null;
  settlement: string | null;
  legacyDraft: TrainingDraft | null;
  legacyInvalid: boolean;
  targetInvalid: boolean;
  targetDraft: TrainingDraft | null;
};

function parseRecord(raw: string | null, owner: string): TrainingDraft | null {
  if (raw === null) return null;
  if (raw.length > 128 * 1024) throw new Error("草稿超出大小限制");
  const value: unknown = JSON.parse(raw);
  if (!validTrainingDraft(value, owner)) throw new Error("草稿格式或归属不匹配");
  return value;
}

export async function withTrainingDraftLock<T>(
  owner: string,
  action: () => T | Promise<T>,
): Promise<T> {
  if (!navigator.locks?.request) {
    throw new Error("浏览器不支持草稿协调锁。请使用支持 Web Locks 的浏览器，保留原记录后重试。");
  }
  return navigator.locks.request(`training:draft:1:${owner}`, action);
}

export function inspectTrainingDraftRecords(owner: string): TrainingDraftRecords {
  const legacy = localStorage.getItem(legacyTrainingDraftKey(owner));
  const target = localStorage.getItem(trainingDraftKey(owner));
  const settlement = localStorage.getItem(trainingDraftSettlementKey(owner));
  let legacyDraft: TrainingDraft | null = null;
  let legacyInvalid = false;
  try {
    legacyDraft = parseRecord(legacy, owner);
  } catch {
    legacyInvalid = true;
  }
  let targetDraft: TrainingDraft | null = null;
  let targetInvalid = false;
  try {
    targetDraft = parseRecord(target, owner);
  } catch {
    targetInvalid = true;
  }
  return {
    legacy,
    target,
    settlement,
    legacyDraft,
    legacyInvalid,
    targetDraft,
    targetInvalid,
  };
}

async function legacyFingerprint(raw: string): Promise<string> {
  if (!crypto.subtle) throw new Error("浏览器不支持核验旧草稿，请使用支持 Web Crypto 的浏览器。");
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(raw));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export async function settledTrainingDraft(records: TrainingDraftRecords): Promise<boolean> {
  if (records.legacy === null) return true;
  return (
    records.settlement !== null && (await legacyFingerprint(records.legacy)) === records.settlement
  );
}

// Called only while holding the owner's Web Lock after explicit user choice.
export async function upgradeTrainingDraft(
  owner: string,
  seen: TrainingDraftRecords,
  choice: "legacy" | "target" | "ignore",
  owns: () => boolean,
): Promise<void> {
  if (seen.legacy === null) throw new Error("旧草稿不存在，请重新核对。");
  const fingerprint = await legacyFingerprint(seen.legacy);
  if (!owns()) throw new Error("账号或项目已变化。");
  const now = inspectTrainingDraftRecords(owner);
  if (
    now.legacy !== seen.legacy ||
    now.target !== seen.target ||
    now.settlement !== seen.settlement
  )
    throw new Error("草稿已在其他标签页更改，请重新核对。");
  if (choice === "legacy" && !now.legacyDraft)
    throw new Error("旧草稿格式或归属不匹配，不能升级。");
  if (choice === "ignore" && !now.legacyInvalid) throw new Error("旧草稿可读取，请重新核对。");
  if (choice !== "legacy" && now.targetInvalid)
    throw new Error("当前草稿格式或归属不匹配，不能直接使用。");
  const selected = choice === "legacy" ? now.legacy : now.target;
  if (choice !== "ignore" && selected === null) throw new Error("所选草稿不存在，请重新核对。");
  if (choice === "legacy") {
    localStorage.setItem(trainingDraftKey(owner), selected!);
    if (localStorage.getItem(trainingDraftKey(owner)) !== selected)
      throw new Error("无法验证升级后的草稿。");
  }
  if (localStorage.getItem(legacyTrainingDraftKey(owner)) !== now.legacy)
    throw new Error("旧草稿已更改，请重新核对。");
  localStorage.setItem(trainingDraftSettlementKey(owner), fingerprint);
  if (
    localStorage.getItem(trainingDraftSettlementKey(owner)) !== fingerprint ||
    localStorage.getItem(legacyTrainingDraftKey(owner)) !== now.legacy
  )
    throw new Error("旧草稿已更改或无法验证，请重新核对。");
}

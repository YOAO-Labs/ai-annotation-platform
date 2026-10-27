import { validDatasetTrainingFilter } from "../Datasets/datasetTrainingSelection";
import type { TrainingDatasetCreate } from "@/api/trainingDatasets";
import { instanceMaskBridge } from "./trainingDatasetBridges";
import { trainingDraftOwner } from "./trainingDraftStorage";

export type TrainingDatasetDraftOwner = {
  userId: string;
  sessionId: string;
  projectId: string;
  task: "detect" | "segment";
  bridge?: typeof instanceMaskBridge;
};

const prefix = "aap-training-dataset-request:1:";
const maxLength = 2 * 1024 * 1024;
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const restoreError = "无法恢复本地创建请求，请检查浏览器存储。原记录未被自动覆盖。";

export const trainingDatasetDraftKey = (owner: TrainingDatasetDraftOwner) =>
  prefix +
  JSON.stringify([
    owner.sessionId,
    trainingDraftOwner(owner.userId, owner.projectId),
    owner.task,
    ...(owner.bridge ? [owner.bridge] : []),
  ]);

function object(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function identifiers(value: unknown): boolean {
  return (
    value === undefined ||
    (Array.isArray(value) &&
      value.length <= 20000 &&
      value.every((id) => typeof id === "string" && id.length > 0 && id.length <= 100))
  );
}

function validBody(
  value: unknown,
  owner: TrainingDatasetDraftOwner,
): value is TrainingDatasetCreate {
  if (
    !object(value) ||
    Object.keys(value).some(
      (key) =>
        ![
          "name",
          "request_id",
          "preflight_digest",
          "task",
          "bridge",
          "batch_id",
          "dataset_id",
          "dataset_filter",
          "parent_id",
          "mode",
          "seed",
          "validation_fraction",
          "task_ids",
          "confirmed_negative_ids",
        ].includes(key),
    )
  )
    return false;
  return (
    typeof value.name === "string" &&
    !!value.name.trim() &&
    value.name.length <= 120 &&
    typeof value.request_id === "string" &&
    uuid.test(value.request_id) &&
    typeof value.preflight_digest === "string" &&
    /^[0-9a-f]{64}$/.test(value.preflight_digest) &&
    (value.task ?? "detect") === owner.task &&
    (value.bridge ?? undefined) === owner.bridge &&
    (value.bridge === undefined ||
      (owner.task === "segment" && value.bridge === instanceMaskBridge)) &&
    ["batch_id", "parent_id", "dataset_id"].every(
      (key) =>
        value[key] == null ||
        (typeof value[key] === "string" && value[key].length > 0 && value[key].length <= 100),
    ) &&
    (value.mode === undefined || value.mode === "reviewed" || value.mode === "manager_confirmed") &&
    (value.seed === undefined ||
      (Number.isInteger(value.seed) &&
        Number(value.seed) >= 0 &&
        Number(value.seed) <= 2147483647)) &&
    (value.validation_fraction === undefined ||
      (typeof value.validation_fraction === "number" &&
        value.validation_fraction >= 0.05 &&
        value.validation_fraction <= 0.5)) &&
    (value.dataset_filter == null ||
      (typeof value.dataset_id === "string" && validDatasetTrainingFilter(value.dataset_filter))) &&
    identifiers(value.task_ids) &&
    identifiers(value.confirmed_negative_ids)
  );
}

export function readTrainingDatasetDraft(
  owner: TrainingDatasetDraftOwner,
  storage?: Pick<Storage, "getItem">,
): { body: TrainingDatasetCreate | null; error: string | null } {
  try {
    storage ??= localStorage;
    const key = trainingDatasetDraftKey(owner);
    const source = storage.getItem(key);
    if (source === null) return { body: null, error: null };
    if (source.length > maxLength) throw new Error("Request too large");
    const value: unknown = JSON.parse(source);
    if (
      !object(value) ||
      Object.keys(value).length !== 3 ||
      value.version !== 1 ||
      value.ownerKey !== key ||
      !validBody(value.body, owner)
    )
      throw new Error("Invalid request owner or body");
    return { body: value.body, error: null };
  } catch {
    return { body: null, error: restoreError };
  }
}

export function saveTrainingDatasetDraft(
  owner: TrainingDatasetDraftOwner,
  body: TrainingDatasetCreate,
  storage?: Pick<Storage, "getItem" | "setItem">,
): string | null {
  try {
    storage ??= localStorage;
    const existing = readTrainingDatasetDraft(owner, storage);
    if (existing.error) return existing.error;
    if (existing.body && JSON.stringify(existing.body) !== JSON.stringify(body))
      return "另一个页面保留了尚未确认的创建请求，请刷新后确认原请求。";
    if (!validBody(body, owner)) throw new Error("Invalid request");
    const key = trainingDatasetDraftKey(owner);
    const source = JSON.stringify({ version: 1, ownerKey: key, body });
    if (source.length > maxLength) throw new Error("Request too large");
    storage.setItem(key, source);
    return null;
  } catch {
    return "浏览器无法保存创建请求。请检查浏览器存储后重试；请求尚未发送。";
  }
}

export function removeMatchingTrainingDatasetDraft(
  owner: TrainingDatasetDraftOwner,
  body: TrainingDatasetCreate,
  storage?: Pick<Storage, "getItem" | "removeItem">,
): string | null {
  try {
    storage ??= localStorage;
    const existing = readTrainingDatasetDraft(owner, storage);
    if (existing.error) return existing.error;
    if (existing.body?.request_id === body.request_id)
      storage.removeItem(trainingDatasetDraftKey(owner));
    return null;
  } catch {
    return "创建结果已确认，但浏览器无法清理本地记录。请检查浏览器存储后再次确认原请求。";
  }
}

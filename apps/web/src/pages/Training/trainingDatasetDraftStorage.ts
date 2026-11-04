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

const prefix = "training:dataset-request:1:";
const legacyPrefix = "aap-training-dataset-request:1:";
const maxLength = 2 * 1024 * 1024;
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const restoreError = "无法恢复本地创建请求，请检查浏览器存储及安全上下文。原记录未被自动覆盖。";

export const trainingDatasetDraftKey = (owner: TrainingDatasetDraftOwner) =>
  prefix +
  JSON.stringify([
    owner.sessionId,
    trainingDraftOwner(owner.userId, owner.projectId),
    owner.task,
    ...(owner.bridge ? [owner.bridge] : []),
  ]);

const legacyKey = (owner: TrainingDatasetDraftOwner) =>
  legacyPrefix + trainingDatasetDraftKey(owner).slice(prefix.length);

const legacyError = "发现旧版创建请求。请先升级或明确忽略旧记录；不会自动发送请求。";
const conflictError = "新旧创建请求冲突或记录损坏，原记录均已保留。请核对后明确处理。";
const lockError = "浏览器不支持协调本地请求，请使用支持 Web Locks 的浏览器恢复；原记录未更改。";

type LegacyReceipt = { fingerprint: string; settledRequestId: string | null };
const receiptKey = (owner: TrainingDatasetDraftOwner) => `${trainingDatasetDraftKey(owner)}:legacy`;

async function fingerprint(source: string): Promise<string> {
  const bytes = new TextEncoder().encode(JSON.stringify(source));
  const hash = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(hash), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function readReceipt(
  owner: TrainingDatasetDraftOwner,
  storage: Pick<Storage, "getItem">,
): LegacyReceipt | null {
  const source = storage.getItem(receiptKey(owner));
  if (source === null) return null;
  const value: unknown = JSON.parse(source);
  if (
    !object(value) ||
    Object.keys(value).length !== 2 ||
    typeof value.fingerprint !== "string" ||
    !/^[0-9a-f]{64}$/.test(value.fingerprint) ||
    !(
      value.settledRequestId === null ||
      (typeof value.settledRequestId === "string" && uuid.test(value.settledRequestId))
    )
  )
    throw new Error("Invalid legacy receipt");
  return value as LegacyReceipt;
}

function parseDraft(
  source: string,
  key: string,
  owner: TrainingDatasetDraftOwner,
): TrainingDatasetCreate {
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
  return value.body;
}

async function locked(
  owner: TrainingDatasetDraftOwner,
  guard: () => void,
  action: () => string | null | Promise<string | null>,
): Promise<string | null> {
  if (typeof navigator === "undefined" || !navigator.locks?.request) return lockError;
  try {
    return await navigator.locks.request(trainingDatasetDraftKey(owner), () => {
      guard();
      return action();
    });
  } catch {
    return lockError;
  }
}

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

export async function readTrainingDatasetDraft(
  owner: TrainingDatasetDraftOwner,
  storage?: Pick<Storage, "getItem">,
): Promise<{ body: TrainingDatasetCreate | null; error: string | null; legacy?: boolean }> {
  try {
    storage ??= localStorage;
    const key = trainingDatasetDraftKey(owner);
    const old = storage.getItem(legacyKey(owner));
    const receipt = readReceipt(owner, storage);
    if (old !== null && !receipt) return { body: null, error: legacyError, legacy: true };
    if (receipt) {
      if (
        old === null ||
        (await fingerprint(old)) !== receipt.fingerprint ||
        storage.getItem(legacyKey(owner)) !== old
      )
        return { body: null, error: conflictError, legacy: true };
    }
    const source = storage.getItem(key);
    if (source === null) return { body: null, error: null };
    const body = parseDraft(source, key, owner);
    return { body: body.request_id === receipt?.settledRequestId ? null : body, error: null };
  } catch {
    return { body: null, error: restoreError };
  }
}

export async function saveTrainingDatasetDraft(
  owner: TrainingDatasetDraftOwner,
  body: TrainingDatasetCreate,
  storage: Pick<Storage, "getItem" | "setItem"> = localStorage,
  guard: () => void = () => {},
): Promise<string | null> {
  return locked(owner, guard, async () => {
    try {
      const existing = await readTrainingDatasetDraft(owner, storage);
      guard();
      if (existing.error) return existing.error;
      if (existing.body && JSON.stringify(existing.body) !== JSON.stringify(body))
        return "另一个页面保留了尚未确认的创建请求，请刷新后确认原请求。";
      if (!validBody(body, owner)) throw new Error("Invalid request");
      const receipt = readReceipt(owner, storage);
      if (receipt?.settledRequestId === body.request_id)
        return "此创建请求已确认，请检查数据版本。";
      const key = trainingDatasetDraftKey(owner);
      const source = JSON.stringify({ version: 1, ownerKey: key, body });
      if (source.length > maxLength) throw new Error("Request too large");
      storage.setItem(key, source);
      if (storage.getItem(key) !== source) return conflictError;
      const checked = await readTrainingDatasetDraft(owner, storage);
      guard();
      return checked.error;
    } catch {
      return "浏览器无法保存创建请求。请检查浏览器存储后重试；请求尚未发送。";
    }
  });
}

export async function removeMatchingTrainingDatasetDraft(
  owner: TrainingDatasetDraftOwner,
  body: TrainingDatasetCreate,
  storage: Pick<Storage, "getItem" | "setItem" | "removeItem"> = localStorage,
  guard: () => void = () => {},
): Promise<string | null> {
  return locked(owner, guard, async () => {
    try {
      const existing = await readTrainingDatasetDraft(owner, storage);
      guard();
      if (existing.error) return existing.error;
      if (existing.body && JSON.stringify(existing.body) === JSON.stringify(body)) {
        const receipt = readReceipt(owner, storage);
        if (receipt) {
          const settled = JSON.stringify({ ...receipt, settledRequestId: body.request_id });
          storage.setItem(receiptKey(owner), settled);
          if (storage.getItem(receiptKey(owner)) !== settled) return conflictError;
        }
        storage.removeItem(trainingDatasetDraftKey(owner));
      }
      return null;
    } catch {
      return "创建结果已确认，但浏览器无法清理本地记录。请检查浏览器存储后再次确认原请求。";
    }
  });
}

export async function upgradeTrainingDatasetDraft(
  owner: TrainingDatasetDraftOwner,
  guard: () => void,
  storage: Pick<Storage, "getItem" | "setItem"> = localStorage,
): Promise<string | null> {
  return locked(owner, guard, async () => {
    try {
      const oldKey = legacyKey(owner);
      const key = trainingDatasetDraftKey(owner);
      const oldSource = storage.getItem(oldKey);
      if (oldSource === null) return "旧版创建请求已变化，请刷新后检查。";
      const body = parseDraft(oldSource, oldKey, owner);
      const digest = await fingerprint(oldSource);
      guard();
      if (storage.getItem(oldKey) !== oldSource) return conflictError;
      const prior = readReceipt(owner, storage);
      if (prior && (prior.fingerprint !== digest || prior.settledRequestId)) return conflictError;
      const source = JSON.stringify({ version: 1, ownerKey: key, body });
      const target = storage.getItem(key);
      if (
        target !== null &&
        JSON.stringify(parseDraft(target, key, owner)) !== JSON.stringify(body)
      )
        return conflictError;
      if (target === null) storage.setItem(key, source);
      if (storage.getItem(key) !== (target ?? source) || storage.getItem(oldKey) !== oldSource)
        return conflictError;
      const receipt = JSON.stringify({ fingerprint: digest, settledRequestId: null });
      storage.setItem(receiptKey(owner), receipt);
      if (storage.getItem(receiptKey(owner)) !== receipt || storage.getItem(oldKey) !== oldSource)
        return conflictError;
      return null;
    } catch {
      return conflictError;
    }
  });
}

export async function discardTrainingDatasetDraft(
  owner: TrainingDatasetDraftOwner,
  keyKind: "legacy" | "current",
  expected: string,
  guard: () => void,
  storage: Pick<Storage, "getItem" | "setItem" | "removeItem"> = localStorage,
): Promise<string | null> {
  return locked(owner, guard, async () => {
    try {
      if (keyKind === "legacy") {
        if (storage.getItem(legacyKey(owner)) !== expected) return "记录已变化，请刷新后重新核对。";
        const digest = await fingerprint(expected);
        guard();
        if (storage.getItem(legacyKey(owner)) !== expected) return conflictError;
        const receipt = JSON.stringify({ fingerprint: digest, settledRequestId: null });
        storage.setItem(receiptKey(owner), receipt);
        return storage.getItem(receiptKey(owner)) === receipt ? null : conflictError;
      }
      const key = trainingDatasetDraftKey(owner);
      if (storage.getItem(key) !== expected) return "记录已变化，请刷新后重新核对。";
      storage.removeItem(key);
      return null;
    } catch {
      return "无法处理指定记录，请检查浏览器存储。";
    }
  });
}

export function trainingDatasetDraftSnapshot(owner: TrainingDatasetDraftOwner) {
  return {
    legacy: localStorage.getItem(legacyKey(owner)),
    current: localStorage.getItem(trainingDatasetDraftKey(owner)),
  };
}

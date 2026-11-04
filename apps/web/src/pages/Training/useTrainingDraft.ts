import { randomId } from "@/utils/id";
import { useEffect, useRef, useState } from "react";
import { isCurrentAuthOwner, useAuthStore } from "@/stores/authStore";
import { getAuthSessionRevision } from "@/stores/authSession";
import {
  emptyTrainingDraft,
  inspectTrainingDraftRecords,
  legacyTrainingDraftKey,
  saveTrainingDraft,
  trainingDraftKey,
  trainingDraftSettlementKey,
  trainingDraftOwner,
  withTrainingDraftLock,
  type TrainingDraft,
} from "./trainingDraftStorage";

export function useTrainingDraft(
  userId: string,
  projectId: string,
  sessionToken: string,
  frameworkHint: string,
  initial: {
    draft: TrainingDraft | null;
    raw: string | null;
    legacy: string | null;
    settlement: string | null;
  },
) {
  const ownerKey = trainingDraftOwner(userId, projectId);
  const [sessionRevision] = useState(getAuthSessionRevision);
  const [draft, setDraft] = useState(
    () => initial.draft ?? emptyTrainingDraft(ownerKey, frameworkHint || "ultralytics"),
  );
  const [storageError, setStorageError] = useState<string | null>(null);
  const current = useRef(draft);
  const expected = useRef(initial.raw);
  const alive = useRef(true);
  const queue = useRef<Promise<unknown>>(Promise.resolve());
  useEffect(() => {
    alive.current = true;
    const changed = (event: StorageEvent) => {
      if (
        alive.current &&
        getAuthSessionRevision() === sessionRevision &&
        isCurrentAuthOwner(userId) &&
        useAuthStore.getState().token === sessionToken &&
        (event.key === null ||
          (event.key === legacyTrainingDraftKey(ownerKey) && event.newValue !== initial.legacy) ||
          (event.key === trainingDraftSettlementKey(ownerKey) &&
            event.newValue !== initial.settlement) ||
          (event.key === trainingDraftKey(ownerKey) && event.newValue !== expected.current))
      )
        setStorageError("草稿在其他标签页发生变化。请保留本页内容，重新打开以核对冲突。");
    };
    window.addEventListener("storage", changed);
    return () => {
      alive.current = false;
      window.removeEventListener("storage", changed);
    };
  }, [initial.legacy, initial.settlement, ownerKey, sessionRevision, sessionToken, userId]);
  const ownsSession = () =>
    alive.current &&
    getAuthSessionRevision() === sessionRevision &&
    isCurrentAuthOwner(userId) &&
    useAuthStore.getState().token === sessionToken;
  const matches = (snapshot: TrainingDraft) =>
    ownsSession() &&
    snapshot.ownerKey === ownerKey &&
    current.current.ownerKey === ownerKey &&
    current.current.draftId === snapshot.draftId &&
    current.current.revision === snapshot.revision &&
    current.current.schemaId === snapshot.schemaId &&
    current.current.schemaDigest === snapshot.schemaDigest;
  const schedule = <T>(operation: () => Promise<T>): Promise<T> => {
    const result = queue.current.then(operation, operation);
    queue.current = result;
    return result;
  };
  const commit = (next: TrainingDraft): Promise<boolean> => {
    if (!ownsSession() || next.ownerKey !== ownerKey) return Promise.resolve(false);
    current.current = next;
    setDraft(next);
    return schedule(async () => {
      try {
        const saved = await withTrainingDraftLock(ownerKey, () => {
          if (!ownsSession()) return false;
          const records = inspectTrainingDraftRecords(ownerKey);
          if (
            records.targetInvalid ||
            records.legacy !== initial.legacy ||
            records.settlement !== initial.settlement ||
            records.target !== expected.current
          )
            throw new Error("草稿在其他标签页发生变化。请保留本页内容，重新打开以核对冲突。");
          const error = saveTrainingDraft(next);
          if (error) throw new Error(error);
          const raw = JSON.stringify(next);
          if (localStorage.getItem(trainingDraftKey(ownerKey)) !== raw)
            throw new Error("无法验证已保存的草稿。");
          expected.current = raw;
          return true;
        });
        if (!ownsSession()) return false;
        setStorageError(null);
        return saved && current.current === next;
      } catch (reason) {
        if (ownsSession())
          setStorageError(reason instanceof Error ? reason.message : "无法保存本地草稿。");
        return false;
      }
    });
  };
  return {
    draft,
    current,
    storageError,
    restored: !!initial.draft,
    ownsSession,
    matches,
    commit,
    update: (patch: Partial<TrainingDraft>) =>
      commit({
        ...current.current,
        ...patch,
        revision: current.current.revision + 1,
        requestId: randomId(),
        pending: undefined,
        pendingStudy: undefined,
      }),
    remove: (snapshot: TrainingDraft) =>
      schedule(async () => {
        try {
          return await withTrainingDraftLock(ownerKey, () => {
            if (!matches(snapshot)) return false;
            const records = inspectTrainingDraftRecords(ownerKey);
            if (
              records.targetInvalid ||
              records.legacy !== initial.legacy ||
              records.settlement !== initial.settlement ||
              records.target !== expected.current
            )
              throw new Error("草稿在其他标签页发生变化，原记录已保留，请重新核对。");
            localStorage.removeItem(trainingDraftKey(ownerKey));
            expected.current = null;
            return true;
          });
        } catch (reason) {
          if (ownsSession())
            setStorageError(reason instanceof Error ? reason.message : "无法清理本地草稿。");
          return false;
        }
      }),
  };
}

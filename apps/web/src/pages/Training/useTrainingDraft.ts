import { randomId } from "@/utils/id";
import { useEffect, useRef, useState } from "react";
import { isCurrentAuthOwner, useAuthStore } from "@/stores/authStore";
import { getAuthSessionRevision } from "@/stores/authSession";
import {
  emptyTrainingDraft,
  readTrainingDraft,
  removeMatchingTrainingDraft,
  saveTrainingDraft,
  trainingDraftOwner,
  type TrainingDraft,
} from "./trainingDraftStorage";

export function useTrainingDraft(
  userId: string,
  projectId: string,
  sessionToken: string,
  frameworkHint: string,
) {
  const ownerKey = trainingDraftOwner(userId, projectId);
  const [initial] = useState(() => readTrainingDraft(ownerKey));
  const [sessionRevision] = useState(getAuthSessionRevision);
  const [draft, setDraft] = useState(
    () => initial.draft ?? emptyTrainingDraft(ownerKey, frameworkHint || "ultralytics"),
  );
  const [storageError, setStorageError] = useState(initial.error);
  const current = useRef(draft);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
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
  const commit = (next: TrainingDraft) => {
    if (!ownsSession() || next.ownerKey !== ownerKey) return false;
    current.current = next;
    setDraft(next);
    setStorageError(saveTrainingDraft(next));
    return true;
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
    remove: (snapshot: TrainingDraft) => {
      if (matches(snapshot)) {
        try {
          removeMatchingTrainingDraft(snapshot);
        } catch {
          /* A confirmed run remains valid if local storage is unavailable. */
        }
      }
    },
  };
}

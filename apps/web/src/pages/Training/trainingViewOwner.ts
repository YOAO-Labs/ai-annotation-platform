import { useEffect, useMemo, useRef, useSyncExternalStore } from "react";
import { isCurrentAuthOwner, useAuthStore } from "@/stores/authStore";
import { getAuthSessionRevision, subscribeAuthSession } from "@/stores/authSession";

export type AssertTrainingOwner = (signal?: AbortSignal) => void;

export function useTrainingSessionRevision() {
  return useSyncExternalStore(subscribeAuthSession, getAuthSessionRevision, getAuthSessionRevision);
}

export function useTrainingViewOwner(projectId: string, scope: string) {
  const revision = useTrainingSessionRevision();
  const owner = useMemo(
    () => ({
      projectId,
      scope,
      userId: useAuthStore.getState().user?.id,
      token: useAuthStore.getState().token,
      revision,
    }),
    [projectId, scope, revision],
  );
  const current = useRef(owner);
  const alive = useRef(true);
  current.current = owner;
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  return useMemo(() => {
    const owns = () =>
      alive.current &&
      current.current === owner &&
      getAuthSessionRevision() === owner.revision &&
      !!owner.userId &&
      isCurrentAuthOwner(owner.userId) &&
      useAuthStore.getState().token === owner.token;
    const assert: AssertTrainingOwner = (signal) => {
      signal?.throwIfAborted();
      if (!owns()) throw new DOMException("训练查看会话已变化", "AbortError");
    };
    return { owner, owns, assert };
  }, [owner]);
}

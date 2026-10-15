import { useEffect, useMemo, useRef } from "react";
import { isCurrentAuthOwner, useAuthStore } from "@/stores/authStore";

export type AssertTrainingOwner = (signal?: AbortSignal) => void;

export function useTrainingViewOwner(projectId: string, scope: string) {
  const owner = useMemo(
    () => ({
      projectId,
      scope,
      userId: useAuthStore.getState().user?.id,
      token: useAuthStore.getState().token,
    }),
    [projectId, scope],
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

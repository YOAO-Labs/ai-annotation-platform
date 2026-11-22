import { useCallback, useLayoutEffect, useRef, useState } from "react";
import { isCurrentAuthOwner, useAuthStore } from "@/stores/authStore";
import { randomId } from "@/utils/id";
import { clearUserCanvasDraftRecovery } from "./discussionCanvasRecovery";
import type { DiscussionSessionOwner } from "./discussionTypes";

type AuthState = ReturnType<typeof useAuthStore.getState>;
interface AuthLease {
  userId: string | null;
  sessionId: string;
}

const authUserId = (auth: AuthState) => (auth.token ? (auth.user?.id ?? null) : null);
const newLease = (auth: AuthState): AuthLease => ({
  userId: authUserId(auth),
  sessionId: randomId(),
});

export function useAuthenticatedDiscussionSession() {
  const [lease, setLease] = useState(() => newLease(useAuthStore.getState()));
  const leaseRef = useRef(lease);
  const activeRef = useRef(true);

  useLayoutEffect(() => {
    activeRef.current = true;
    const synchronize = (auth: AuthState, previous: AuthState) => {
      if (authUserId(auth) === authUserId(previous) && !(!auth.token && previous.token)) return;
      if (previous.user?.id) clearUserCanvasDraftRecovery(previous.user.id);
      const next = newLease(auth);
      leaseRef.current = next;
      setLease(next);
    };
    const unsubscribe = useAuthStore.subscribe(synchronize);
    const current = useAuthStore.getState();
    if (authUserId(current) !== leaseRef.current.userId) {
      const next = newLease(current);
      leaseRef.current = next;
      setLease(next);
    }
    return () => {
      activeRef.current = false;
      unsubscribe();
    };
  }, []);

  const isOwnerCurrent = useCallback((owner: DiscussionSessionOwner): boolean => {
    const current = leaseRef.current;
    if (
      !activeRef.current ||
      current.userId !== owner.userId ||
      current.sessionId !== owner.sessionId
    )
      return false;
    try {
      return isCurrentAuthOwner(owner.userId);
    } catch {
      return false;
    }
  }, []);

  return { ...lease, isOwnerCurrent };
}

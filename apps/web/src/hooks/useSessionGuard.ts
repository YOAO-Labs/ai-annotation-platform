import { useEffect, useRef, useSyncExternalStore } from "react";
import { getAuthSessionRevision, subscribeAuthSession } from "@/stores/authSession";

/** Retire multi-request actions on unmount or any authentication transition. */
export function useSessionGuard() {
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  return () => {
    const revision = getAuthSessionRevision();
    return () => alive.current && getAuthSessionRevision() === revision;
  };
}

export function useSessionRevision() {
  return useSyncExternalStore(subscribeAuthSession, getAuthSessionRevision);
}

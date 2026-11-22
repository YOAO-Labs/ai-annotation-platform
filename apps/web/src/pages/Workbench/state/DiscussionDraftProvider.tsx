import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import {
  createDiscussionDraftStore,
  createDiscussionSessionId,
  type DiscussionDraft,
  type DiscussionDraftStore,
  type DiscussionDraftStoreSnapshot,
} from "./useDiscussionDraftStore";
import {
  discussionTargetKey,
  type DiscussionTarget,
  type DiscussionSessionOwner,
} from "./discussionTypes";

export interface DiscussionDraftProviderProps {
  userId: string | null | undefined;
  sessionId?: string | null;
  onDispose?: (owner: DiscussionSessionOwner) => void;
  isOwnerCurrent?: (owner: DiscussionSessionOwner) => boolean;
  beforeUnload?: boolean;
  children: ReactNode;
}

const DiscussionDraftContext = createContext<DiscussionDraftStore | null>(null);

const EMPTY_OWNER: DiscussionSessionOwner = { sessionId: "", userId: "" };
const EMPTY_SNAPSHOT: DiscussionDraftStoreSnapshot = {
  owner: EMPTY_OWNER,
  drafts: {},
  sendTargets: {},
  disposed: true,
  dirty: false,
  dirtyText: false,
};

export function DiscussionDraftProvider({
  userId,
  sessionId,
  onDispose,
  isOwnerCurrent,
  beforeUnload = true,
  children,
}: DiscussionDraftProviderProps) {
  const disposeCallbackRef = useRef(onDispose);
  const ownerCurrentRef = useRef(isOwnerCurrent);
  const activeStoreRef = useRef<DiscussionDraftStore | null>(null);
  const pendingDisposalsRef = useRef(
    new Map<DiscussionDraftStore, ReturnType<typeof setTimeout>>(),
  );
  disposeCallbackRef.current = onDispose;
  ownerCurrentRef.current = isOwnerCurrent;

  const generatedSessionId = useMemo(() => (userId ? createDiscussionSessionId() : null), [userId]);
  const ownerSessionId = sessionId ?? generatedSessionId;
  const store = useMemo(() => {
    if (!userId || !ownerSessionId) return null;
    return createDiscussionDraftStore({
      owner: { userId, sessionId: ownerSessionId },
      onDispose: (owner) => disposeCallbackRef.current?.(owner),
      isOwnerCurrent: (owner) => ownerCurrentRef.current?.(owner) ?? true,
    });
  }, [ownerSessionId, userId]);

  useEffect(() => {
    if (!store) return undefined;
    const pendingDisposals = pendingDisposalsRef.current;
    const pending = pendingDisposals.get(store);
    if (pending !== undefined) {
      clearTimeout(pending);
      pendingDisposals.delete(store);
    }
    activeStoreRef.current = store;
    return () => {
      if (activeStoreRef.current === store) activeStoreRef.current = null;
      const timer = setTimeout(() => {
        pendingDisposals.delete(store);
        if (activeStoreRef.current !== store) store.dispose();
      }, 0);
      pendingDisposals.set(store, timer);
    };
  }, [store]);

  const subscribe = useCallback(
    (listener: () => void) => store?.subscribe(listener) ?? (() => undefined),
    [store],
  );
  const getSnapshot = useCallback(() => store?.getSnapshot() ?? EMPTY_SNAPSHOT, [store]);
  const snapshot = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);

  useEffect(() => {
    if (!beforeUnload || !store || !snapshot.dirty || typeof window === "undefined") {
      return undefined;
    }
    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", handleBeforeUnload);
    return () => window.removeEventListener("beforeunload", handleBeforeUnload);
  }, [beforeUnload, snapshot.dirty, store]);

  return (
    <DiscussionDraftContext.Provider value={store}>{children}</DiscussionDraftContext.Provider>
  );
}

export function useDiscussionDraftStore(): DiscussionDraftStore | null {
  return useContext(DiscussionDraftContext);
}

export const useDiscussionDraftSession = useDiscussionDraftStore;

export function useDiscussionDraft(
  target: DiscussionTarget | null | undefined,
): DiscussionDraft | undefined {
  const store = useDiscussionDraftStore();
  const targetKey = target ? discussionTargetKey(target) : null;
  const subscribe = useCallback(
    (listener: () => void) => store?.subscribe(listener) ?? (() => undefined),
    [store],
  );
  const getDraft = useCallback(
    () => (store && target ? store.getDraft(target) : undefined),
    [store, target],
  );

  const draft = useSyncExternalStore(subscribe, getDraft, getDraft);

  useEffect(() => {
    if (store && target && !store.getDraft(target)) {
      try {
        store.ensureDraft(target);
      } catch {
        // Auth lease replacement can retire the store between render and the
        // effect. The next authenticated provider owns the new draft scope.
      }
    }
  }, [store, target, targetKey]);

  return draft;
}

export function useDiscussionDraftSnapshot(): DiscussionDraftStoreSnapshot | null {
  const store = useDiscussionDraftStore();
  const subscribe = useCallback(
    (listener: () => void) => store?.subscribe(listener) ?? (() => undefined),
    [store],
  );
  const getSnapshot = useCallback(() => store?.getSnapshot() ?? null, [store]);
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}

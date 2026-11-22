import { useCallback, useEffect, useMemo, useReducer } from "react";
import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { authApi, type UserPreferences, type UserPreferencesPatch } from "@/api/auth";
import { isCurrentAuthOwner } from "@/stores/authStore";
import {
  parseStoredShortcutOverrides,
  resolveEffectiveCommands,
  type ShortcutBinding,
  type ShortcutDomain,
} from "./hotkeyBindings";
import { useUserPreferences, userPreferencesQueryKey } from "./useUserPreferences";

export interface ShortcutWriteRequest {
  domain: ShortcutDomain;
  commandId: string;
  bindings: ShortcutBinding[] | null;
}

const writeKey = (req: Pick<ShortcutWriteRequest, "domain" | "commandId">) =>
  `${req.domain}:${req.commandId}`;

export interface ShortcutSaveFailure {
  request: ShortcutWriteRequest;
}

const writeChains = new WeakMap<QueryClient, Map<string, Promise<void>>>();

export function useWorkbenchShortcutPreferences() {
  const { prefs, loaded, error, refetch, userId } = useUserPreferences();
  const queryClient = useQueryClient();
  const [revision, refresh] = useReducer((value: number) => value + 1, 0);

  const parsed = useMemo(() => parseStoredShortcutOverrides(prefs?.workbench?.shortcuts), [prefs]);
  const effective = useMemo(() => resolveEffectiveCommands(parsed.overrides), [parsed.overrides]);
  const loadFailed = prefs === undefined && !!error;

  const session = useMemo(
    () => ({
      userId,
      active: true,
      latest: new Map<string, ShortcutWriteRequest>(),
      pending: new Set<string>(),
      failures: new Map<string, ShortcutSaveFailure>(),
    }),
    [userId],
  );

  useEffect(() => {
    session.active = true;
    return () => {
      session.active = false;
      session.latest.clear();
      session.pending.clear();
      session.failures.clear();
    };
  }, [session]);

  const ownsSession = useCallback(
    () => session.active && !!session.userId && isCurrentAuthOwner(session.userId),
    [session],
  );

  const runWrite = useCallback(
    async (request: ShortcutWriteRequest) => {
      if (!ownsSession() || !session.userId) return;
      const payload: UserPreferencesPatch = {
        workbench: {
          shortcuts: {
            [request.domain]: { [request.commandId]: request.bindings },
          },
        },
      };
      const response = await authApi.updatePreferences(payload);
      if (!isCurrentAuthOwner(session.userId)) return;
      const queryKey = userPreferencesQueryKey(session.userId);
      await queryClient.cancelQueries({ queryKey, exact: true });
      if (!isCurrentAuthOwner(session.userId)) return;
      queryClient.setQueryData<UserPreferences>(queryKey, (previous) => {
        if (!previous) return response;
        const returned = response.workbench.shortcuts;
        const shortcuts = previous.workbench.shortcuts ?? returned;
        if (!shortcuts) return previous;
        const bucket = { ...shortcuts[request.domain] };
        const saved = returned?.[request.domain]?.[request.commandId];
        if (saved === undefined) delete bucket[request.commandId];
        else bucket[request.commandId] = saved;
        return {
          ...previous,
          workbench: {
            ...previous.workbench,
            shortcuts: { ...shortcuts, [request.domain]: bucket },
          },
        };
      });
    },
    [ownsSession, queryClient, session],
  );

  const saveCommand = useCallback(
    (input: ShortcutWriteRequest) => {
      if (!loaded || loadFailed || !ownsSession()) return;
      const request = {
        ...input,
        bindings:
          input.bindings?.map((binding) => ({
            ...binding,
            modifiers: [...binding.modifiers],
          })) ?? null,
      };
      const key = writeKey(request);
      session.latest.set(key, request);
      session.failures.delete(key);
      session.pending.add(key);
      refresh();
      let chains = writeChains.get(queryClient);
      if (!chains) {
        chains = new Map();
        writeChains.set(queryClient, chains);
      }
      const chainKey = `${session.userId}:${key}`;
      const previous = chains.get(chainKey) ?? Promise.resolve();
      const chain = previous
        .then(async () => {
          if (!ownsSession() || session.latest.get(key) !== request) return;
          try {
            await runWrite(request);
            if (!ownsSession() || session.latest.get(key) !== request) return;
            session.latest.delete(key);
            session.pending.delete(key);
            refresh();
          } catch {
            if (!ownsSession() || session.latest.get(key) !== request) return;
            session.pending.delete(key);
            session.failures.set(key, { request });
            refresh();
          }
        })
        .finally(() => {
          if (chains.get(chainKey) === chain) chains.delete(chainKey);
        });
      chains.set(chainKey, chain);
    },
    [loaded, loadFailed, ownsSession, queryClient, runWrite, session],
  );

  const retrySave = useCallback(
    (domain: ShortcutDomain, commandId: string) => {
      const failure = session.failures.get(writeKey({ domain, commandId }));
      if (failure) saveCommand(failure.request);
    },
    [session, saveCommand],
  );

  const discardPending = useCallback(
    (domain: ShortcutDomain, commandId: string) => {
      if (!ownsSession()) return;
      const key = writeKey({ domain, commandId });
      session.latest.delete(key);
      session.pending.delete(key);
      session.failures.delete(key);
      refresh();
    },
    [ownsSession, session],
  );

  const { previewEffective, pendingKeys, failedMap } = useMemo(() => {
    const overrides = {
      common: { ...parsed.overrides.common },
      image: { ...parsed.overrides.image },
      video: { ...parsed.overrides.video },
    };
    for (const request of session.latest.values()) {
      overrides[request.domain][request.commandId] = request.bindings;
    }
    return {
      previewEffective: resolveEffectiveCommands(overrides),
      pendingKeys: new Set(session.pending) as ReadonlySet<string>,
      failedMap: new Map(session.failures) as ReadonlyMap<string, ShortcutSaveFailure>,
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [parsed.overrides, session, revision]);

  return {
    overrides: parsed.overrides,
    issues: parsed.issues,
    opaque: parsed.opaque,
    effective,
    previewEffective,
    loaded: loaded && !loadFailed,
    loadError: loadFailed ? error : null,
    retryLoad: refetch,
    userId,
    saveCommand,
    retrySave,
    discardPending,
    pendingKeys,
    failedMap,
  };
}

export type WorkbenchShortcutPreferencesState = ReturnType<typeof useWorkbenchShortcutPreferences>;

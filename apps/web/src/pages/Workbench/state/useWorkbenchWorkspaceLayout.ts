import { useCallback, useEffect, useReducer, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { authApi, type UserPreferences } from "@/api/auth";
import { ApiError } from "@/api/client";
import { useAuthStore } from "@/stores/authStore";
import {
  readWorkspaceEnvelope,
  sanitizeWorkspaceSnapshot,
  WORKSPACE_SCHEMA_VERSION,
  WORKSPACE_CONTEXTS,
  type WorkspaceContext,
  type WorkspaceSnapshot,
} from "../layout/workbenchLayoutSnapshot";
import { userPreferencesQueryKey } from "./useUserPreferences";
import { migrateThreeDWorkspace } from "../layout/workbenchLayoutPresets";

type ReadOnlyReason = "invalid" | "newer-schema" | "restore-failed" | null;

interface WorkspaceSession {
  userId: string | undefined;
  context: WorkspaceContext;
  snapshot: WorkspaceSnapshot;
  standard: WorkspaceSnapshot;
  initialized: boolean;
  readOnlyReason: ReadOnlyReason;
  error: string | null;
  restoreRevision: number;
  dirty: WorkspaceSnapshot | null;
  saving: boolean;
  active: boolean;
  paused: boolean;
  timer: number | null;
  needsThreeDMigration: boolean;
}

let activeWorkspaceRequest: { userId: string; promise: Promise<UserPreferences> } | null = null;

export function workspaceStorageKey(userId: string, context: WorkspaceContext): string {
  return `workbench.${userId}.workspace.${context}`;
}

function readLocal(userId: string | undefined, context: WorkspaceContext): unknown {
  if (!userId) return undefined;
  try {
    const raw = window.localStorage.getItem(workspaceStorageKey(userId, context));
    if (raw === null) return undefined;
    try {
      return JSON.parse(raw);
    } catch {
      return false;
    }
  } catch {
    return undefined;
  }
}

function writeLocal(session: WorkspaceSession, snapshot: WorkspaceSnapshot): void {
  if (!session.userId) return;
  try {
    window.localStorage.setItem(
      workspaceStorageKey(session.userId, session.context),
      JSON.stringify({ schemaVersion: WORKSPACE_SCHEMA_VERSION, snapshot }),
    );
  } catch {
    // Server persistence still works when browser storage is unavailable.
  }
}

function contextEnvelope(workspace: unknown, context: WorkspaceContext): unknown {
  if (workspace === undefined) return undefined;
  if (!workspace || typeof workspace !== "object" || Array.isArray(workspace)) return false;
  const value = workspace as Record<string, unknown>;
  const contexts = value.contexts === undefined ? {} : value.contexts;
  if (
    value.engine !== "dockview@8" ||
    !contexts ||
    typeof contexts !== "object" ||
    Array.isArray(contexts) ||
    Object.keys(contexts).some((key) => !WORKSPACE_CONTEXTS.includes(key as WorkspaceContext))
  )
    return false;
  if (!Object.prototype.hasOwnProperty.call(contexts, context)) return undefined;
  return (contexts as Record<string, unknown>)[context] ?? false;
}

function createSession(
  userId: string | undefined,
  context: WorkspaceContext,
  fallback: WorkspaceSnapshot,
  standard: WorkspaceSnapshot,
): WorkspaceSession {
  const cached = readLocal(userId, context);
  const local = readWorkspaceEnvelope(cached);
  return {
    userId,
    context,
    snapshot: local.snapshot ?? (local.readOnlyReason ? standard : fallback),
    standard,
    initialized: false,
    readOnlyReason: local.readOnlyReason,
    error: null,
    restoreRevision: 0,
    dirty: null,
    saving: false,
    active: false,
    paused: false,
    timer: null,
    needsThreeDMigration:
      !cached ||
      typeof cached !== "object" ||
      !("schemaVersion" in cached) ||
      cached.schemaVersion !== WORKSPACE_SCHEMA_VERSION,
  };
}

function isSchemaDowngrade(error: unknown): boolean {
  if (!(error instanceof ApiError) || error.status !== 409) return false;
  const detail = error.detailRaw;
  return (
    detail === "layout_schema_downgrade" ||
    (typeof detail === "object" &&
      detail !== null &&
      ("reason" in detail ? detail.reason : "code" in detail ? detail.code : undefined) ===
        "layout_schema_downgrade")
  );
}

export function useWorkbenchWorkspaceLayout(
  context: WorkspaceContext,
  fallbackSnapshot: WorkspaceSnapshot,
  standardSnapshot: WorkspaceSnapshot = fallbackSnapshot,
  paused = false,
) {
  const userId = useAuthStore((state) => state.user?.id);
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: userPreferencesQueryKey(userId),
    queryFn: () => authApi.getPreferences(),
    enabled: !!userId,
    staleTime: 5 * 60 * 1000,
  });
  const [revision, refresh] = useReducer((value: number) => value + 1, 0);
  const sessionRef = useRef<WorkspaceSession | null>(null);
  if (sessionRef.current?.userId !== userId || sessionRef.current?.context !== context) {
    sessionRef.current = createSession(userId, context, fallbackSnapshot, standardSnapshot);
  }
  const session = sessionRef.current!;
  session.paused = paused;

  const isCurrent = useCallback(
    (candidate: WorkspaceSession) =>
      candidate.active &&
      sessionRef.current === candidate &&
      candidate.userId === useAuthStore.getState().user?.id,
    [],
  );

  const flush = useCallback(
    async function flush(candidate: WorkspaceSession, retiring = false): Promise<void> {
      if (
        (retiring
          ? candidate.userId !== useAuthStore.getState().user?.id
          : !isCurrent(candidate)) ||
        !candidate.userId ||
        !candidate.initialized ||
        (!retiring && candidate.paused) ||
        candidate.readOnlyReason ||
        (!retiring && candidate.saving) ||
        !candidate.dirty
      )
        return;
      candidate.saving = true;
      refresh();
      while (activeWorkspaceRequest) {
        try {
          await activeWorkspaceRequest.promise;
        } catch {
          // The owner of that request reports its error; waiting does not retry it.
        }
      }
      if (
        (retiring
          ? candidate.userId !== useAuthStore.getState().user?.id
          : !isCurrent(candidate)) ||
        candidate.readOnlyReason ||
        (!retiring && candidate.paused) ||
        candidate.timer !== null
      ) {
        candidate.saving = false;
        if (isCurrent(candidate)) refresh();
        return;
      }
      const sent = candidate.dirty;
      const request = authApi.updatePreferences({
        workbench: {
          layout: {
            workspace: {
              engine: "dockview@8",
              contexts: {
                [candidate.context]: { schemaVersion: WORKSPACE_SCHEMA_VERSION, snapshot: sent },
              },
            },
          },
        },
      });
      activeWorkspaceRequest = { userId: candidate.userId, promise: request };
      try {
        const response = await request;
        if (candidate.userId !== useAuthStore.getState().user?.id) return;
        queryClient.setQueryData<UserPreferences>(
          userPreferencesQueryKey(candidate.userId),
          (previous) => {
            if (!previous) return response;
            const saved = readWorkspaceEnvelope(
              contextEnvelope(response.workbench?.layout?.workspace, candidate.context),
            );
            if (!saved.snapshot) return previous;
            return {
              ...previous,
              workbench: {
                ...previous.workbench,
                layout: {
                  ...previous.workbench.layout,
                  workspace: {
                    ...previous.workbench.layout.workspace,
                    engine: "dockview@8",
                    contexts: {
                      ...previous.workbench.layout.workspace?.contexts,
                      [candidate.context]: {
                        schemaVersion: WORKSPACE_SCHEMA_VERSION,
                        snapshot: saved.snapshot,
                      },
                    },
                  },
                },
              },
            };
          },
        );
        if (!isCurrent(candidate)) return;
        if (candidate.dirty === sent) candidate.dirty = null;
        candidate.error = null;
      } catch (error) {
        if (!isCurrent(candidate)) return;
        if (isSchemaDowngrade(error)) {
          candidate.dirty = null;
          candidate.readOnlyReason = "newer-schema";
          candidate.snapshot = candidate.standard;
          candidate.restoreRevision += 1;
          candidate.error = "布局已由新版更新，请刷新到新版后继续调整。";
        } else {
          candidate.error = "布局暂未保存，已保留本地调整；再次调整时会重试保存。";
        }
      } finally {
        activeWorkspaceRequest = null;
        candidate.saving = false;
        if (isCurrent(candidate)) {
          refresh();
          if (candidate.dirty && candidate.dirty !== sent && candidate.timer === null) {
            void flush(candidate);
          }
        }
      }
    },
    [isCurrent, queryClient],
  );

  const schedule = useCallback(
    (candidate: WorkspaceSession) => {
      if (candidate.timer !== null) window.clearTimeout(candidate.timer);
      candidate.timer = null;
      if (candidate.paused) return;
      candidate.timer = window.setTimeout(() => {
        candidate.timer = null;
        void flush(candidate);
      }, 300);
    },
    [flush],
  );

  useEffect(() => {
    session.active = true;
    return () => {
      if (session.timer !== null) window.clearTimeout(session.timer);
      session.timer = null;
      if (session.dirty && !session.readOnlyReason) void flush(session, true);
      session.active = false;
    };
  }, [flush, session]);

  useEffect(() => {
    if (!userId || session.initialized || query.isPending || query.isFetching) return;
    if (activeWorkspaceRequest?.userId === userId) {
      let cancelled = false;
      void activeWorkspaceRequest.promise
        .catch(() => undefined)
        .then(() => {
          if (!cancelled) refresh();
        });
      return () => {
        cancelled = true;
      };
    }
    session.initialized = true;
    if (query.isError) {
      session.error = "暂时无法读取账号布局，当前使用本地布局。";
    } else {
      const workspace = query.data?.workbench?.layout?.workspace;
      const envelope = contextEnvelope(workspace, context);
      const remote = readWorkspaceEnvelope(envelope);
      const needsMigration =
        envelope === undefined ||
        (typeof envelope === "object" &&
          envelope !== null &&
          "schemaVersion" in envelope &&
          typeof envelope.schemaVersion === "number" &&
          envelope.schemaVersion < WORKSPACE_SCHEMA_VERSION);
      if (remote.readOnlyReason) {
        session.readOnlyReason = remote.readOnlyReason;
        session.snapshot = session.standard;
        session.restoreRevision += 1;
      } else if (remote.snapshot) {
        session.readOnlyReason = null;
        const snapshot = needsMigration
          ? migrateThreeDWorkspace(remote.snapshot, query.data?.workbench ?? {}, context)
          : remote.snapshot;
        if (JSON.stringify(snapshot) !== JSON.stringify(session.snapshot)) {
          session.snapshot = snapshot;
          session.restoreRevision += 1;
        }
        writeLocal(session, snapshot);
        if (needsMigration && context.endsWith(":3d")) {
          session.dirty = snapshot;
          schedule(session);
        }
      } else if (!session.readOnlyReason) {
        const snapshot = session.needsThreeDMigration
          ? migrateThreeDWorkspace(session.snapshot, query.data?.workbench ?? {}, context)
          : session.snapshot;
        if (JSON.stringify(snapshot) !== JSON.stringify(session.snapshot)) {
          session.snapshot = snapshot;
          session.restoreRevision += 1;
        }
        session.dirty = session.snapshot;
        writeLocal(session, session.snapshot);
        schedule(session);
      }
    }
    refresh();
  }, [
    context,
    query.data,
    query.isError,
    query.isFetching,
    query.isPending,
    revision,
    schedule,
    session,
    userId,
  ]);

  useEffect(() => {
    if (paused && session.timer !== null) {
      window.clearTimeout(session.timer);
      session.timer = null;
    } else if (!paused && session.initialized && session.dirty && !session.readOnlyReason) {
      schedule(session);
    }
  }, [paused, schedule, session]);

  const failRestore = useCallback(() => {
    if (
      !isCurrent(session) ||
      session.readOnlyReason === "newer-schema" ||
      session.readOnlyReason === "restore-failed"
    )
      return;
    session.readOnlyReason = "restore-failed";
    session.dirty = null;
    session.snapshot = session.standard;
    session.restoreRevision += 1;
    session.error = "布局恢复失败，已使用标准布局；回到桌面宽度后可重置。";
    refresh();
  }, [isCurrent, session]);

  const save = useCallback(
    (snapshot: WorkspaceSnapshot, reset = false): boolean => {
      if (
        !isCurrent(session) ||
        !session.userId ||
        !session.initialized ||
        session.readOnlyReason === "newer-schema" ||
        (session.readOnlyReason && !reset)
      )
        return false;
      let clean: WorkspaceSnapshot;
      try {
        clean = sanitizeWorkspaceSnapshot(snapshot);
      } catch {
        failRestore();
        return false;
      }
      if (reset) session.readOnlyReason = null;
      session.dirty = clean;
      writeLocal(session, clean);
      schedule(session);
      refresh();
      return true;
    },
    [failRestore, isCurrent, schedule, session],
  );
  const reset = useCallback((snapshot: WorkspaceSnapshot) => save(snapshot, true), [save]);

  return {
    snapshot: session.snapshot,
    initialized: session.initialized,
    readOnly: !userId || !session.initialized || session.readOnlyReason !== null,
    readOnlyReason: session.readOnlyReason,
    restoreRevision: session.restoreRevision,
    dirty: session.dirty !== null,
    saving: session.saving,
    error: session.error,
    save,
    reset,
    failRestore,
  };
}

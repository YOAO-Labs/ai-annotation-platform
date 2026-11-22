import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { QueryClient } from "@tanstack/react-query";

import { tasksApi } from "@/api/tasks";
import { ApiError } from "@/api/client";
import { useOnlineStatus } from "@/hooks/useOnlineStatus";
import { isCurrentAuthOwner } from "@/stores/authStore";
import type { AnnotationResponse } from "@/types";

import {
  drain,
  isOfflineCandidate,
  replaceAnnotationId as offlineQueueReplaceAnnotationId,
  type DrainOptions,
  type OfflineQueueScope,
  type OfflineOp,
} from "./offlineQueue";

interface ToastInput {
  msg: string;
  sub?: string;
  kind?: "success" | "warning" | "error" | "";
}

interface HistoryLike {
  replaceAnnotationId: (
    oldId: string,
    newId: string,
    taskId?: string,
    videoSegmentId?: string | null,
  ) => void;
}

const OFFLINE_REVOKED_MESSAGE =
  "项目权限已变更，已暂停离线同步；本机草稿仍保留，可切换账号或恢复权限后重试";

const OFFLINE_INDETERMINATE_MESSAGE = "网络不稳定，离线同步已暂停，将在恢复后自动重试";

export interface FlushAuthorizationOutcome {
  outcome: "authorized" | "denied" | "indeterminate";
}

export type FlushAuthorization =
  | boolean
  | FlushAuthorizationOutcome["outcome"]
  | FlushAuthorizationOutcome;

export function normalizeFlushAuthorization(value: FlushAuthorization): boolean | "indeterminate" {
  if (typeof value === "boolean") return value;
  const outcome = typeof value === "string" ? value : value.outcome;
  if (outcome === "indeterminate") return "indeterminate";
  return outcome === "authorized";
}

export function classifyReplayError(error: unknown): "denied" | "missing" | "retry" {
  if (!(error instanceof ApiError)) return "retry";
  if (error.status === 403) return "denied";
  if (error.status === 404) return "missing";
  return "retry";
}

const drainOptions = (shouldProcess: NonNullable<DrainOptions["shouldProcess"]>): DrainOptions => ({
  shouldProcess,
  classifyError: classifyReplayError,
});

export interface UseWorkbenchOfflineQueueArgs {
  history: HistoryLike;
  queryClient: QueryClient;
  pushToast: (toast: ToastInput) => void;
  userId?: string | null;
  taskId?: string;
  authorizeFlush?: (op: OfflineOp) => FlushAuthorization | Promise<FlushAuthorization>;
}

export interface UseWorkbenchOfflineQueueReturn {
  online: boolean;
  queueCount: number;
  queueReady: boolean;
  queueScope?: OfflineQueueScope;
  syncError: string | null;
  enqueueOnError: (err: unknown, fallback: () => void | Promise<void>) => Promise<void>;
  flushOne: (op: OfflineOp) => Promise<void>;
  flushAll: () => Promise<void>;
  classifyError: DrainOptions["classifyError"];
  drawerOpen: boolean;
  openDrawer: () => void;
  closeDrawer: () => void;
}

export function useWorkbenchOfflineQueue({
  history,
  queryClient,
  pushToast,
  userId,
  taskId,
  authorizeFlush,
}: UseWorkbenchOfflineQueueArgs): UseWorkbenchOfflineQueueReturn {
  const queueScope = useMemo<OfflineQueueScope | undefined>(() => {
    return {
      userId: userId ?? "",
      isCurrent: () => !!userId && isCurrentAuthOwner(userId),
    };
  }, [userId]);
  const { online, queueCount, queueReady, queueReadError } = useOnlineStatus(queueScope);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [syncError, setSyncError] = useState<string | null>(null);
  const currentTaskRef = useRef<string | undefined>(taskId);
  currentTaskRef.current = taskId;
  const authorizeFlushRef = useRef(authorizeFlush);
  authorizeFlushRef.current = authorizeFlush;

  useEffect(() => {
    setSyncError(null);
  }, [userId]);
  useEffect(() => {
    if (queueCount === 0) setSyncError(null);
  }, [queueCount]);

  const assertCurrentOwner = useCallback(() => {
    if (!queueScope || !isCurrentAuthOwner(queueScope.userId)) {
      throw new Error("离线队列所属账号已切换，已暂停同步");
    }
  }, [queueScope]);

  const enqueueOnError = useCallback(
    async (err: unknown, fallback: () => void | Promise<void>) => {
      const ownsUi = () =>
        !!userId && isCurrentAuthOwner(userId) && currentTaskRef.current === taskId;
      if (isOfflineCandidate(err)) {
        try {
          await fallback();
          if (!ownsUi()) return;
          pushToast({ msg: "已暂存到离线队列", sub: "恢复连接后将自动同步", kind: "warning" });
        } catch {
          if (!ownsUi()) return;
          setSyncError("无法保存到本机，请保留当前页面并检查浏览器存储");
          pushToast({ msg: "本机保存失败，修改尚未保存", kind: "error" });
        }
      } else {
        if (!ownsUi()) return;
        pushToast({ msg: "操作失败", sub: String(err), kind: "error" });
      }
    },
    [pushToast, taskId, userId],
  );

  const flushOne = useCallback(
    async (op: OfflineOp) => {
      try {
        assertCurrentOwner();
        if (!op.userId || op.userId !== userId) {
          throw new Error("离线操作没有当前账号归属，已保留待原账号处理");
        }
        if (op.kind === "create") {
          const payload = op.payload as Parameters<typeof tasksApi.createAnnotation>[1];
          const real = await tasksApi.createAnnotation(op.taskId, payload, op.id);
          const canWriteCurrentTask = () =>
            !!userId && isCurrentAuthOwner(userId) && currentTaskRef.current === op.taskId;
          if (op.tmpId) {
            const scope = { userId: op.userId, taskId: op.taskId };
            await offlineQueueReplaceAnnotationId(op.tmpId, real.id, scope, op.id);
            const segmentId = op.videoSegmentId ?? payload.video_segment_id ?? null;
            if (userId && isCurrentAuthOwner(userId))
              history.replaceAnnotationId(op.tmpId, real.id, op.taskId, segmentId);
            if (canWriteCurrentTask()) {
              const key = segmentId
                ? ["annotations", op.taskId, segmentId]
                : ["annotations", op.taskId];
              queryClient.setQueryData<AnnotationResponse[]>(key, (prev) =>
                prev?.map((a) =>
                  a.id === op.tmpId ? { ...real, render_key: a.render_key ?? op.tmpId } : a,
                ),
              );
            }
          } else if (canWriteCurrentTask()) {
            queryClient.invalidateQueries({ queryKey: ["annotations", op.taskId] });
          }
        } else if (op.kind === "update") {
          await tasksApi.updateAnnotation(
            op.taskId,
            op.annotationId,
            op.payload as Parameters<typeof tasksApi.updateAnnotation>[2],
            op.etag,
          );
        } else {
          await tasksApi.deleteAnnotation(op.taskId, op.annotationId);
        }
      } catch (error) {
        if (
          !(error instanceof ApiError && (error.status === 403 || error.status === 404)) &&
          userId &&
          isCurrentAuthOwner(userId)
        ) {
          setSyncError("离线操作同步失败，请检查网络或任务权限后重试");
        }
        throw error;
      }
    },
    [assertCurrentOwner, currentTaskRef, history, queryClient, queueScope, userId],
  );

  const runFlushAll = useCallback(async (): Promise<{
    ok: number;
    failed: number;
    denied?: number;
    deferred?: number;
  } | null> => {
    if (!queueScope) return null;
    const result = await drain(
      flushOne,
      queueScope,
      drainOptions(async (op) => {
        const authorize = authorizeFlushRef.current;
        if (!authorize) return true;
        return normalizeFlushAuthorization(await authorize(op));
      }),
    );
    if (!queueScope.isCurrent?.()) return null;
    if (result.ok > 0) {
      setSyncError(null);
      queryClient.invalidateQueries({ queryKey: ["annotations"] });
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
      pushToast({ msg: `已同步 ${result.ok} 条离线操作`, kind: "success" });
    }
    if ((result.denied ?? 0) > 0) {
      setSyncError(OFFLINE_REVOKED_MESSAGE);
    } else if ((result.deferred ?? 0) > 0) {
      setSyncError(OFFLINE_INDETERMINATE_MESSAGE);
    } else if (result.failed > 0) {
      setSyncError("部分离线操作同步失败");
      pushToast({ msg: "部分操作仍未能同步", sub: "请检查网络后重试", kind: "warning" });
    }
    return result;
  }, [flushOne, pushToast, queryClient, queueScope]);

  const runFlushAllRef = useRef(runFlushAll);
  runFlushAllRef.current = runFlushAll;
  const [retryNonce, bumpRetryNonce] = useState(0);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const retryAttemptRef = useRef(0);
  const clearRetryTimer = useCallback(() => {
    if (retryTimerRef.current !== null) {
      clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }
  }, []);
  useEffect(() => {
    if (!online || queueCount === 0) {
      clearRetryTimer();
      retryAttemptRef.current = 0;
    }
  }, [online, queueCount, clearRetryTimer]);
  useEffect(() => clearRetryTimer, [clearRetryTimer]);

  const flushAllWithRetry = useCallback(async () => {
    let result: Awaited<ReturnType<typeof runFlushAllRef.current>> = null;
    try {
      result = await runFlushAllRef.current();
    } catch {
      return;
    }
    const transient = result !== null && ((result.deferred ?? 0) > 0 || (result.failed ?? 0) > 0);
    clearRetryTimer();
    if (transient) {
      const attempt = Math.min(retryAttemptRef.current, 5);
      retryAttemptRef.current = attempt + 1;
      const delay = Math.min(60_000, 2_000 * 2 ** attempt);
      retryTimerRef.current = setTimeout(() => {
        retryTimerRef.current = null;
        bumpRetryNonce((value) => value + 1);
      }, delay);
    } else {
      retryAttemptRef.current = 0;
    }
  }, [clearRetryTimer]);

  const flushAllWithRetryRef = useRef(flushAllWithRetry);
  flushAllWithRetryRef.current = flushAllWithRetry;
  useEffect(() => {
    if (online && queueReady && queueCount > 0) void flushAllWithRetryRef.current();
  }, [online, queueCount, queueReady, userId, authorizeFlush, retryNonce]);

  const openDrawer = useCallback(() => setDrawerOpen(true), []);
  const closeDrawer = useCallback(() => setDrawerOpen(false), []);

  return {
    online,
    queueCount,
    queueReady,
    queueScope,
    syncError: queueReadError ?? syncError,
    enqueueOnError,
    flushOne,
    flushAll: flushAllWithRetry,
    classifyError: classifyReplayError,
    drawerOpen,
    openDrawer,
    closeDrawer,
  };
}

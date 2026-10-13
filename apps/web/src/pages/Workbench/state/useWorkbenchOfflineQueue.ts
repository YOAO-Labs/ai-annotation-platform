// v0.6.3 P1：从 WorkbenchShell 拆出的离线队列接线层。
// 集中管理：online 状态 + 单条 op 远端执行 + drain 全量同步 + drawer 开关
// + 错误归类（网络抖动入队 / 业务错 toast）。
//
// 不在这里管的：乐观 cache 写入（依赖 taskId / projectId / meUserId / s.setSelectedId 太多 shell 上下文，
// 仍由 WorkbenchShell 持有 `optimisticEnqueueCreate` helper），以及 history 的 push 行为本身。

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
  replaceAnnotationId: (oldId: string, newId: string) => void;
}

/** Shown when the account no longer has project write access. */
const OFFLINE_REVOKED_MESSAGE =
  "项目权限已变更，已暂停离线同步；本机草稿仍保留，可切换账号或恢复权限后重试";

/** Shown when a drain could not determine authority (transport/server error). */
const OFFLINE_INDETERMINATE_MESSAGE = "网络不稳定，离线同步已暂停，将在恢复后自动重试";

export interface FlushAuthorizationOutcome {
  /** "indeterminate": transport/server failure — retryable, never a denial. */
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

/**
 * Classify a replay error.  Only an affirmative 403 is an authority denial:
 * after the project-access preflight succeeds, a 404 means the task or
 * annotation was deleted elsewhere (a satisfied delete leaves the queue), and
 * transport/5xx errors stay retryable.
 */
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
  /** The account that owns the current Workbench queue. */
  userId?: string | null;
  /** Current task owner for history/UI writebacks; queue syncing may include other tasks. */
  taskId?: string;
  /**
   * Authorize one queued operation by its actual task/project.  Returning false
   * (or { outcome: "denied" }) retains the op (draft preserved) while unrelated
   * authorized projects keep syncing.  Return "indeterminate" on a
   * transport/server failure so the drain defers and retries instead of
   * reporting a permission change.  Omit to allow everything (tests/legacy).
   */
  authorizeFlush?: (op: OfflineOp) => FlushAuthorization | Promise<FlushAuthorization>;
}

export interface UseWorkbenchOfflineQueueReturn {
  online: boolean;
  queueCount: number;
  queueReady: boolean;
  queueScope?: OfflineQueueScope;
  syncError: string | null;
  /** 网络抖动 / 5xx → fallback() 入队；业务错（4xx 等）→ 直接 toast */
  enqueueOnError: (err: unknown, fallback: () => void | Promise<void>) => Promise<void>;
  /** 单条 op 的远端执行；create 成功时调 history.replaceAnnotationId + 改 cache + 跨队列替换 tmpId */
  flushOne: (op: OfflineOp) => Promise<void>;
  /** 顺序消费整个队列；带 toast 通知与 invalidate，内部带暂态退避重试。 */
  flushAll: () => Promise<void>;
  /** drain 错误分类器，供 drawer 单条重试复用同一语义。 */
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
  // Read through a ref inside async drains so an authorization change flips the
  // gate without restarting the queue.
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
          const real = await tasksApi.createAnnotation(
            op.taskId,
            op.payload as Parameters<typeof tasksApi.createAnnotation>[1],
            op.id,
          );
          const canWriteCurrentTask =
            !!userId && isCurrentAuthOwner(userId) && currentTaskRef.current === op.taskId;
          if (op.tmpId) {
            // A queue can contain another task from the same account. Its server
            // result is safe to cache, but the current task's history owner is not.
            if (canWriteCurrentTask) {
              history.replaceAnnotationId(op.tmpId, real.id);
              queryClient.setQueryData<AnnotationResponse[]>(["annotations", op.taskId], (prev) =>
                (prev ?? []).map((a) =>
                  a.id === op.tmpId ? { ...real, render_key: a.render_key ?? op.tmpId } : a,
                ),
              );
            }
            // v0.6.3 P0：跨队列替换 tmpId → realId，保后续 update/delete 不 404
            await offlineQueueReplaceAnnotationId(op.tmpId, real.id, queueScope);
          } else if (canWriteCurrentTask) {
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
        // The drain classifies the error; a missing target or a denial must not
        // be surfaced as a generic network failure here.
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
        // Read the latest authorizer for every op: a project/account switch while
        // a drain is in flight must not keep using a captured decision.
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
      // Transport/server failure during the authorization preflight: this is
      // retryable, not a permission change.  The retry effect below schedules
      // the next attempt.
      setSyncError(OFFLINE_INDETERMINATE_MESSAGE);
    } else if (result.failed > 0) {
      setSyncError("部分离线操作同步失败");
      pushToast({ msg: "部分操作仍未能同步", sub: "请检查网络后重试", kind: "warning" });
    }
    return result;
  }, [flushOne, pushToast, queryClient, queueScope]);

  const runFlushAllRef = useRef(runFlushAll);
  runFlushAllRef.current = runFlushAll;
  // A deferred/failed drain schedules one bounded backoff retry: a transient
  // authorization failure must not strand the queue until connectivity
  // changes — the queue count and online state are unchanged in that case.
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
  // Retry on connection/queue/authorization changes and on scheduled retries,
  // not on every render.
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

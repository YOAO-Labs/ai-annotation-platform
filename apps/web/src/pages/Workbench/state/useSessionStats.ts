import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../../../api/client";
import { meApi, type TaskEventIn } from "../../../api/me";

export const RING_SIZE = 20;
export const MIN_SAMPLES = 10;
export const MIN_INTERVAL_MS = 1_500;
export const MAX_INTERVAL_MS = 30 * 60 * 1_000;
export const IDLE_TIMEOUT_MS = 5 * 60 * 1_000;
export const IDLE_CHECK_INTERVAL_MS = 15 * 1_000;
export const FLUSH_THRESHOLD = 20;
export const MAX_FLUSH_BATCH_SIZE = 200;
// ponytail: cap offline telemetry at 1,000 intervals; a durable local queue is
// the upgrade path if outages need lossless collection.
export const MAX_PENDING_EVENTS = 1_000;
export const SESSION_COLLECTOR_VERSION = "session-v2";
const MAX_FLUSH_RETRIES = 2;
const RETRY_DELAYS_MS = [1_000, 2_000] as const;
const PERMANENT_TASK_EVENT_STATUS_CODES = new Set([400, 403, 404, 409, 410, 422]);

function isPermanentTaskEventError(error: unknown): error is ApiError {
  return error instanceof ApiError && PERMANENT_TASK_EVENT_STATUS_CODES.has(error.status);
}

type SessionContext = {
  taskId: string;
  projectId: string | null;
  kind: "annotate" | "review";
  accountId: string | null;
  startedAt: number | null;
  lastActivityAt: number;
  qualifiedMs: number;
};

type SessionOwner = Pick<SessionContext, "projectId" | "kind" | "accountId">;

function clientEventId(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }
  const bytes = new Uint8Array(16);
  if (typeof globalThis.crypto?.getRandomValues === "function") {
    globalThis.crypto.getRandomValues(bytes);
  } else {
    for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((value) => value.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function visibleDocument(): boolean {
  return typeof document === "undefined" || document.visibilityState !== "hidden";
}

function sameContext(left: SessionContext, right: SessionContext): boolean {
  return (
    left.taskId === right.taskId &&
    left.projectId === right.projectId &&
    left.kind === right.kind &&
    left.accountId === right.accountId
  );
}

function sameOwner(left: SessionOwner, right: SessionOwner): boolean {
  return (
    left.projectId === right.projectId &&
    left.kind === right.kind &&
    left.accountId === right.accountId
  );
}

/**
 * Record bounded, visible Workbench intervals for ETA and the project
 * performance source. The active interval is always owned by its captured
 * task/project/account/work type; hidden and idle time is closed before a new
 * interval can start.
 */
export function useSessionStats(
  currentTaskId: string | null,
  projectId?: string | null,
  kind: "annotate" | "review" = "annotate",
  accountId?: string | null,
) {
  const activeRef = useRef<SessionContext | null>(null);
  const pendingRef = useRef<TaskEventIn[]>([]);
  const pendingAccountRef = useRef<string | null>(null);
  const pendingCoverageRef = useRef(false);
  const ownerRef = useRef<SessionOwner | null>(null);
  const queueGenerationRef = useRef(0);
  const accountRef = useRef<string | null>(accountId ?? null);
  accountRef.current = accountId ?? null;
  const mountedRef = useRef(true);
  // React StrictMode mounts effects twice; render/effect setup must restore it.
  mountedRef.current = true;
  const retryAttemptRef = useRef(0);
  const retryTimerRef = useRef<number | null>(null);
  const flushInFlightRef = useRef<Promise<void> | null>(null);
  const flushPendingRef = useRef<(keepalive?: boolean) => Promise<void>>(async () => {});
  const [samples, setSamples] = useState<number[]>([]);

  const clearPending = useCallback(() => {
    // In-flight requests cannot be cancelled. Advancing the generation makes
    // their eventual success/failure a no-op instead of touching a new
    // account's queue.
    queueGenerationRef.current += 1;
    pendingRef.current = [];
    pendingAccountRef.current = null;
    pendingCoverageRef.current = false;
    flushInFlightRef.current = null;
    retryAttemptRef.current = 0;
    if (retryTimerRef.current !== null) {
      window.clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }
  }, []);

  const scheduleRetry = useCallback((generation: number) => {
    if (queueGenerationRef.current !== generation) return;
    if (!mountedRef.current || retryTimerRef.current !== null) return;
    const attempt = retryAttemptRef.current;
    if (attempt >= MAX_FLUSH_RETRIES) return;
    retryAttemptRef.current += 1;
    retryTimerRef.current = window.setTimeout(
      () => {
        retryTimerRef.current = null;
        if (queueGenerationRef.current !== generation) return;
        void flushPendingRef.current();
      },
      RETRY_DELAYS_MS[attempt] ?? RETRY_DELAYS_MS[RETRY_DELAYS_MS.length - 1],
    );
  }, []);

  const flushPending = useCallback(
    async (keepalive = false) => {
      if (flushInFlightRef.current) return flushInFlightRef.current;
      const generation = queueGenerationRef.current;
      const batch = pendingRef.current.slice(0, MAX_FLUSH_BATCH_SIZE);
      const batchAccount = pendingAccountRef.current;
      if (batch.length === 0 || !batchAccount || accountRef.current !== batchAccount) {
        return;
      }

      const request = (async () => {
        let hasMore = false;
        const coverageMarker = pendingCoverageRef.current ? batch[0] : undefined;
        if (coverageMarker) coverageMarker.collection_coverage = "partial";
        try {
          const response = await meApi.submitTaskEvents(
            batch,
            keepalive ? { keepalive: true } : undefined,
          );
          if (queueGenerationRef.current !== generation) return;
          const markerDiscarded = Boolean(
            coverageMarker &&
            response.discarded?.some(
              (item) =>
                item.index === 0 &&
                (!coverageMarker.client_id || item.client_id === coverageMarker.client_id),
            ),
          );
          const ids = new Set(batch.map((event) => event.client_id));
          const batchRefs = new Set(batch);
          pendingRef.current = pendingRef.current.filter((event) =>
            event.client_id ? !ids.has(event.client_id) : !batchRefs.has(event),
          );
          if (pendingRef.current.length === 0) {
            pendingAccountRef.current = null;
            pendingCoverageRef.current = false;
          } else if (coverageMarker && !markerDiscarded) {
            pendingCoverageRef.current = false;
          }
          retryAttemptRef.current = 0;
          hasMore = pendingRef.current.length > 0;
        } catch (error) {
          if (queueGenerationRef.current === generation) {
            // A closed interval can become permanently invalid after a task is
            // deleted or reassigned. Drop only a single such row; mixed batches
            // receive per-row discard details from the API and resolve here.
            if (batch.length === 1 && isPermanentTaskEventError(error)) {
              const rejected = batch[0];
              pendingRef.current = pendingRef.current.filter((event) =>
                rejected?.client_id ? event.client_id !== rejected.client_id : event !== rejected,
              );
              if (pendingRef.current.length === 0) {
                pendingAccountRef.current = null;
                pendingCoverageRef.current = false;
              }
              retryAttemptRef.current = 0;
              hasMore = pendingRef.current.length > 0;
            } else {
              // Network, auth, throttling and server failures stay queued for
              // a bounded retry. A later account switch clears the old queue.
              scheduleRetry(generation);
            }
          }
        } finally {
          if (queueGenerationRef.current === generation) {
            flushInFlightRef.current = null;
            if (hasMore) void flushPendingRef.current(keepalive);
          }
        }
      })();
      flushInFlightRef.current = request;
      return request;
    },
    [scheduleRetry],
  );
  flushPendingRef.current = flushPending;

  const closeActive = useCallback(
    (endedAt: number, restartAt: number | null = null, finalizeTask = true) => {
      const active = activeRef.current;
      if (!active) return;

      // Never turn an idle gap into productive time, even if a browser event
      // was delayed. A long live interval is split into bounded DB rows.
      const effectiveEndedAt = Math.min(endedAt, active.lastActivityAt + IDLE_TIMEOUT_MS);
      const events: TaskEventIn[] = [];
      let startedAt: number | null = active.startedAt;
      while (startedAt !== null && startedAt < effectiveEndedAt) {
        const chunkEndedAt = Math.min(effectiveEndedAt, startedAt + MAX_INTERVAL_MS);
        const durationMs = chunkEndedAt - startedAt;
        if (durationMs > MIN_INTERVAL_MS && durationMs <= MAX_INTERVAL_MS) {
          active.qualifiedMs += durationMs;
          if (active.projectId && active.accountId && active.accountId === accountRef.current) {
            events.push({
              client_id: clientEventId(),
              task_id: active.taskId,
              project_id: active.projectId,
              kind: active.kind,
              collector_version: SESSION_COLLECTOR_VERSION,
              started_at: new Date(startedAt).toISOString(),
              ended_at: new Date(chunkEndedAt).toISOString(),
              duration_ms: durationMs,
            });
          }
        }
        if (chunkEndedAt >= effectiveEndedAt) {
          startedAt = null;
        } else {
          startedAt = chunkEndedAt;
        }
      }

      const shouldRestart =
        restartAt !== null && restartAt <= effectiveEndedAt && effectiveEndedAt === restartAt;
      active.startedAt = shouldRestart ? restartAt : null;
      if (shouldRestart) active.lastActivityAt = Math.max(active.lastActivityAt, restartAt);

      if (mountedRef.current && finalizeTask && active.qualifiedMs > MIN_INTERVAL_MS) {
        const taskDurationMs = active.qualifiedMs;
        active.qualifiedMs = 0;
        setSamples((previous) => {
          const next = [...previous, taskDurationMs];
          return next.length > RING_SIZE ? next.slice(-RING_SIZE) : next;
        });
      }
      if (finalizeTask) active.qualifiedMs = 0;
      if (events.length === 0) return;
      if (pendingAccountRef.current && pendingAccountRef.current !== active.accountId) {
        clearPending();
      }
      pendingAccountRef.current = active.accountId;
      pendingRef.current.push(...events);
      if (pendingRef.current.length > MAX_PENDING_EVENTS) {
        pendingRef.current.splice(0, pendingRef.current.length - MAX_PENDING_EVENTS);
        pendingCoverageRef.current = true;
      }
      if (pendingRef.current.length >= FLUSH_THRESHOLD) void flushPending();
    },
    [clearPending, flushPending],
  );

  useEffect(() => {
    const startedAt = visibleDocument() ? Date.now() : null;
    const nextOwner: SessionOwner = {
      projectId: projectId ?? null,
      kind,
      accountId: accountId ?? null,
    };
    const previousOwner = ownerRef.current;
    const ownerChanged = previousOwner !== null && !sameOwner(previousOwner, nextOwner);
    const next = currentTaskId
      ? {
          taskId: currentTaskId,
          projectId: projectId ?? null,
          kind,
          accountId: accountId ?? null,
          startedAt,
          lastActivityAt: startedAt ?? Date.now(),
          qualifiedMs: 0,
        }
      : null;
    const previous = activeRef.current;
    if (previous && (!next || !sameContext(previous, next))) {
      closeActive(Date.now(), null, !ownerChanged);
    }
    if (ownerChanged) {
      setSamples([]);
      if (previousOwner?.accountId !== nextOwner.accountId) clearPending();
    }
    ownerRef.current = nextOwner;
    activeRef.current = next;
  }, [accountId, clearPending, closeActive, currentTaskId, kind, projectId]);

  useEffect(() => {
    mountedRef.current = true;
    const onActivity = () => {
      const active = activeRef.current;
      if (!active || !visibleDocument()) return;
      const now = Date.now();
      if (active.startedAt === null) {
        active.startedAt = now;
        active.lastActivityAt = now;
        return;
      }
      if (now - active.lastActivityAt >= IDLE_TIMEOUT_MS) {
        closeActive(active.lastActivityAt + IDLE_TIMEOUT_MS, null, false);
        active.startedAt = now;
      }
      active.lastActivityAt = now;
    };
    const checkIdleAndSplit = () => {
      const active = activeRef.current;
      if (!active || active.startedAt === null || !visibleDocument()) return;
      const now = Date.now();
      if (now - active.lastActivityAt >= IDLE_TIMEOUT_MS) {
        closeActive(active.lastActivityAt + IDLE_TIMEOUT_MS, null, false);
      } else if (now - active.startedAt >= MAX_INTERVAL_MS) {
        const splitAt = active.startedAt + MAX_INTERVAL_MS;
        closeActive(splitAt, splitAt, false);
      }
    };
    const onVisibilityChange = () => {
      const active = activeRef.current;
      if (!active) return;
      if (document.visibilityState === "hidden") {
        closeActive(Date.now(), null, false);
        void flushPendingRef.current(true);
      } else if (active.startedAt === null) {
        active.startedAt = Date.now();
        active.lastActivityAt = active.startedAt;
      }
    };
    const onPageHide = () => {
      closeActive(Date.now());
      void flushPendingRef.current(true);
    };
    const activityEvents = [
      "pointerdown",
      "pointermove",
      "keydown",
      "wheel",
      "touchstart",
    ] as const;
    for (const eventName of activityEvents) document.addEventListener(eventName, onActivity);
    document.addEventListener("visibilitychange", onVisibilityChange);
    const idleTimer = window.setInterval(checkIdleAndSplit, IDLE_CHECK_INTERVAL_MS);
    window.addEventListener("pagehide", onPageHide);
    window.addEventListener("beforeunload", onPageHide);
    return () => {
      for (const eventName of activityEvents) document.removeEventListener(eventName, onActivity);
      document.removeEventListener("visibilitychange", onVisibilityChange);
      window.clearInterval(idleTimer);
      window.removeEventListener("pagehide", onPageHide);
      window.removeEventListener("beforeunload", onPageHide);
    };
  }, [closeActive]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (retryTimerRef.current !== null) window.clearTimeout(retryTimerRef.current);
      closeActive(Date.now());
      void flushPendingRef.current(true);
    };
  }, [closeActive]);

  const avgMs =
    samples.length >= MIN_SAMPLES
      ? samples.reduce((total, sample) => total + sample, 0) / samples.length
      : null;

  function etaMs(remainingCount: number): number | null {
    if (avgMs === null || remainingCount <= 0) return null;
    return Math.round(avgMs * remainingCount);
  }

  return { avgMs, samplesCount: samples.length, etaMs };
}

/** Format milliseconds as mm:ss / h:mm. */
export function formatDuration(ms: number): string {
  if (ms < 0) return "—";
  const totalSec = Math.round(ms / 1000);
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  if (h > 0) return `${h}:${m.toString().padStart(2, "0")}`;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

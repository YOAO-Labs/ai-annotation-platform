import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError } from "@/api";
import { tasksApi } from "@/api/tasks";
import type { TaskLockConflictDetail, TaskLockResponse } from "@/types";

const HEARTBEAT_INTERVAL_MS = 60_000;

// DELETE targets (task, user), not an acquisition token. Serialize even across
// hook remounts so an old cleanup can never delete a newer same-task lock.
const taskLockOperations = new Map<string, Promise<void>>();

function enqueueLockOperation(taskId: string, operation: () => Promise<void>) {
  const pending = (taskLockOperations.get(taskId) ?? Promise.resolve()).then(operation);
  const settled = pending.catch(() => undefined);
  taskLockOperations.set(taskId, settled);
  void settled.then(() => {
    if (taskLockOperations.get(taskId) === settled) taskLockOperations.delete(taskId);
  });
  return settled;
}

function parseLockConflict(err: unknown): TaskLockConflictDetail | null {
  if (!(err instanceof ApiError) || err.status !== 409) return null;
  const detail = err.detailRaw;
  if (!detail || typeof detail !== "object") return null;
  const typed = detail as TaskLockConflictDetail;
  return typed.reason === "task_locked_by_other" ? typed : null;
}

export function useTaskLock(taskId: string | undefined, enabled = true) {
  const [pausedTaskId, setPausedTaskId] = useState<string | null>(null);
  const sessionEnabled = enabled && taskId !== undefined && pausedTaskId !== taskId;
  const session = useMemo(
    () => ({ taskId: sessionEnabled ? taskId : undefined }),
    [taskId, sessionEnabled],
  );
  const activeSessionRef = useRef<{
    taskId: string;
    session: typeof session;
    stop: () => Promise<void>;
  } | null>(null);
  const [state, setState] = useState<{
    session: typeof session;
    lock: TaskLockResponse | null;
    lockError: string | null;
    lockConflict: TaskLockConflictDetail | null;
  }>({ session, lock: null, lockError: null, lockConflict: null });
  const [now, setNow] = useState(Date.now);

  useEffect(() => {
    setPausedTaskId((paused) => (paused && paused !== taskId ? null : paused));
  }, [taskId]);

  useEffect(() => {
    const tid = session.taskId;
    setState({ session, lock: null, lockError: null, lockConflict: null });
    if (!tid) return;

    let cancelled = false;
    let requestPending = true;
    let heartbeatTimer = 0;
    let countdownTimer = 0;
    let releasePromise: Promise<void> | null = null;

    const stop = () => {
      if (releasePromise) return releasePromise;
      cancelled = true;
      clearInterval(heartbeatTimer);
      clearInterval(countdownTimer);
      // A cancelled POST may still acquire on the server. Its DELETE must finish
      // after that POST and before a resumed session's POST, including unmount.
      releasePromise = enqueueLockOperation(tid, async () => {
        await tasksApi.releaseLockKeepalive(tid);
      });
      return releasePromise;
    };
    activeSessionRef.current = { taskId: tid, session, stop };

    async function acquire(allowRetry = true, recovering = false): Promise<void> {
      if (cancelled) return;
      try {
        const lock = await tasksApi.acquireLock(tid);
        if (!cancelled) {
          setState({ session, lock, lockError: null, lockConflict: null });
          setNow(Date.now());
        }
      } catch (err: unknown) {
        if (cancelled) return;
        const lockConflict = parseLockConflict(err);
        if (!lockConflict && allowRetry) {
          await acquire(false, recovering);
          return;
        }
        setState({
          session,
          lock: null,
          lockConflict,
          lockError: lockConflict
            ? "Task is locked by another user"
            : recovering
              ? "Lock expired"
              : null,
        });
      }
    }

    void enqueueLockOperation(tid, async () => {
      await acquire();
      requestPending = false;
    });

    heartbeatTimer = window.setInterval(() => {
      if (cancelled || requestPending) return;
      requestPending = true;
      void enqueueLockOperation(tid, async () => {
        try {
          if (cancelled) return;
          await tasksApi.heartbeatLock(tid);
        } catch {
          if (cancelled) return;
          setState({ session, lock: null, lockError: null, lockConflict: null });
          await acquire(false, true);
        } finally {
          requestPending = false;
        }
      });
    }, HEARTBEAT_INTERVAL_MS);

    countdownTimer = window.setInterval(() => setNow(Date.now()), 1000);

    return () => {
      if (activeSessionRef.current?.session === session) activeSessionRef.current = null;
      void stop();
    };
  }, [session]);

  const releaseForPhaseTransition = useCallback(async (ownerTaskId: string) => {
    const active = activeSessionRef.current;
    if (!active || active.taskId !== ownerTaskId) return;
    setPausedTaskId(ownerTaskId);
    setState((current) =>
      current.session === active.session
        ? { session: current.session, lock: null, lockError: null, lockConflict: null }
        : current,
    );
    await active.stop();
  }, []);
  const resumeAfterPhaseTransitionFailure = useCallback((ownerTaskId: string) => {
    setPausedTaskId((paused) => (paused === ownerTaskId ? null : paused));
  }, []);

  const current = state.session === session && !!session.taskId;
  const lock = current ? state.lock : null;
  return {
    lock,
    lockError: current ? state.lockError : null,
    lockConflict: current ? state.lockConflict : null,
    remainingMs: lock ? Math.max(0, new Date(lock.expire_at).getTime() - now) : 0,
    isLocked: !!lock,
    releaseForPhaseTransition,
    resumeAfterPhaseTransitionFailure,
  };
}

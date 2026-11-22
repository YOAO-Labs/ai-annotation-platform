import { useCallback, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { authApi, type LoginPayload } from "../api/auth";
import { tasksApi } from "../api/tasks";
import {
  countDurably as offlineQueueCount,
  drain as drainOfflineQueue,
  replaceAnnotationId as replaceOfflineAnnotationId,
  type OfflineOp,
  type OfflineQueueScope,
} from "../pages/Workbench/state/offlineQueue";
import { isCurrentAuthOwner, useAuthStore } from "../stores/authStore";
import { clearProactiveLogout, markProactiveLogout } from "../utils/authRedirect";

export function useLogin() {
  const setAuth = useAuthStore((s) => s.setAuth);

  return useMutation({
    mutationFn: async (payload: LoginPayload) => {
      const { access_token } = await authApi.login(payload);
      localStorage.setItem("token", access_token);
      const user = await authApi.me().catch((err) => {
        localStorage.removeItem("token");
        throw err;
      });
      setAuth(access_token, user);
      clearProactiveLogout();
      return user;
    },
  });
}

export type LogoutDecision = "sync" | "keep";

export interface LogoutPrompt {
  userId: string;
  pendingCount: number | null;
}

export interface LogoutController {
  requestLogout: () => Promise<void>;
  confirmLogout: (decision: LogoutDecision) => Promise<void>;
  cancelLogout: () => void;
  prompt: LogoutPrompt | null;
  syncError: string | null;
  isLoggingOut: boolean;
}

function scopeForUser(userId: string): OfflineQueueScope {
  return {
    userId,
    isCurrent: () => isCurrentAuthOwner(userId),
  };
}

async function flushLogoutOperation(op: OfflineOp, scope: OfflineQueueScope): Promise<void> {
  if (!scope.isCurrent?.() || op.userId !== scope.userId) {
    throw new Error("登录状态已变化，请刷新页面后使用原账号同步");
  }
  if (op.kind === "create") {
    const real = await tasksApi.createAnnotation(
      op.taskId,
      op.payload as Parameters<typeof tasksApi.createAnnotation>[1],
      op.id,
    );
    if (op.tmpId)
      await replaceOfflineAnnotationId(op.tmpId, real.id, { ...scope, taskId: op.taskId }, op.id);
    return;
  }
  if (op.kind === "update") {
    await tasksApi.updateAnnotation(
      op.taskId,
      op.annotationId,
      op.payload as Parameters<typeof tasksApi.updateAnnotation>[2],
      op.etag,
    );
    return;
  }
  await tasksApi.deleteAnnotation(op.taskId, op.annotationId);
}

export function useLogout(): LogoutController {
  const clearLocal = useAuthStore((s) => s.logout);
  const navigate = useNavigate();
  const [prompt, setPrompt] = useState<LogoutPrompt | null>(null);
  const [syncError, setSyncError] = useState<string | null>(null);
  const [isLoggingOut, setIsLoggingOut] = useState(false);

  const completeLogout = useCallback(
    async (ownerId?: string) => {
      const currentUserId = useAuthStore.getState().user?.id;
      if (ownerId && !isCurrentAuthOwner(ownerId)) return;
      markProactiveLogout();
      if (currentUserId) await authApi.logout().catch(() => {});
      const next = useAuthStore.getState();
      if (ownerId && next.token !== null && !isCurrentAuthOwner(ownerId)) return;
      if (!ownerId && next.token !== null && next.user?.id !== currentUserId) return;
      clearLocal();
      navigate("/login", { replace: true });
    },
    [clearLocal, navigate],
  );

  const requestLogout = useCallback(async () => {
    const ownerId = useAuthStore.getState().user?.id;
    if (!ownerId) {
      markProactiveLogout();
      clearLocal();
      navigate("/login", { replace: true });
      return;
    }
    markProactiveLogout();
    setIsLoggingOut(true);
    try {
      const pendingCount = await offlineQueueCount(scopeForUser(ownerId));
      if (useAuthStore.getState().user?.id !== ownerId) {
        return;
      }
      if (pendingCount > 0) {
        setSyncError(null);
        setPrompt({ userId: ownerId, pendingCount });
        return;
      }
      await completeLogout(ownerId);
    } catch {
      if (isCurrentAuthOwner(ownerId)) {
        setPrompt({ userId: ownerId, pendingCount: null });
        setSyncError("无法读取本机待同步记录，请重试，或明确选择保留记录并退出");
      }
    } finally {
      setIsLoggingOut(false);
    }
  }, [clearLocal, completeLogout, navigate]);

  const confirmLogout = useCallback(
    async (decision: LogoutDecision) => {
      const pending = prompt;
      if (!pending) return;
      setIsLoggingOut(true);
      try {
        if (decision === "keep") {
          setPrompt(null);
          setSyncError(null);
          await completeLogout(pending.userId);
          return;
        }
        if (!isCurrentAuthOwner(pending.userId)) {
          setPrompt(null);
          setSyncError(null);
          return;
        }
        let result: { ok: number; failed: number };
        try {
          const scope = scopeForUser(pending.userId);
          result = await drainOfflineQueue((op) => flushLogoutOperation(op, scope), scope);
        } catch (error) {
          if (!isCurrentAuthOwner(pending.userId)) {
            setPrompt(null);
            setSyncError(null);
            return;
          }
          setSyncError(`同步失败：${error instanceof Error ? error.message : String(error)}`);
          return;
        }
        if (!isCurrentAuthOwner(pending.userId)) {
          setPrompt(null);
          setSyncError(null);
          return;
        }
        const remaining = await offlineQueueCount(scopeForUser(pending.userId));
        if (result.failed > 0 || remaining > 0) {
          setSyncError("仍有操作未同步，请重试或保留记录并退出");
          return;
        }
        setPrompt(null);
        setSyncError(null);
        await completeLogout(pending.userId);
      } catch {
        if (isCurrentAuthOwner(pending.userId)) {
          setSyncError("无法确认本机记录已同步，请重试或保留记录并退出");
        }
      } finally {
        setIsLoggingOut(false);
      }
    },
    [completeLogout, prompt],
  );

  const cancelLogout = useCallback(() => {
    if (!isLoggingOut) {
      setPrompt(null);
      setSyncError(null);
      clearProactiveLogout();
    }
  }, [isLoggingOut]);

  return { requestLogout, confirmLogout, cancelLogout, prompt, syncError, isLoggingOut };
}

export function useLogoutAll() {
  const { setToken } = useAuthStore();
  return useMutation({
    mutationFn: async () => {
      const owner = useAuthStore.getState();
      const { access_token } = await authApi.logoutAll();
      const current = useAuthStore.getState();
      if (
        current.user?.id !== owner.user?.id ||
        current.token !== owner.token ||
        localStorage.getItem("token") !== owner.token
      )
        return;
      setToken(access_token);
    },
  });
}

export function useCurrentUser() {
  return useAuthStore((s) => s.user);
}

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
      // Issue #123 · 新会话建立,结束主动退出窗口,之后的过期跳转恢复正常 from。
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
    if (op.tmpId) await replaceOfflineAnnotationId(op.tmpId, real.id, scope);
    return;
  }
  if (op.kind === "update") {
    await tasksApi.updateAnnotation(
      op.taskId,
      op.annotationId,
      op.payload as Parameters<typeof tasksApi.updateAnnotation>[2],
    );
    return;
  }
  await tasksApi.deleteAnnotation(op.taskId, op.annotationId);
}

/**
 * Logout is deliberately owned here so every caller clears the local auth state
 * only after the best-effort server logout, while the offline queue stays intact.
 */
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
      // Issue #123 · 主动退出不沿用旧会话的登录返回地址:置粘性标记,让 RequireAuth
      // 的兜底跳转(可能在 navigate 生效前后多次发生)始终不带 state.from,并显式
      // 回到 /login。标记由登录成功 / 登录页挂载 / 恢复认证时清除;会话过期仍由
      // RequireAuth 携带 from,保留登录后返回原业务页的能力。
      // 必须在等待服务端退出前置位:退出请求可能因令牌已过期返回 401,API 客户端
      // 随即清空本地凭证,之后的令牌校验不再通过;此时仍要完成干净跳转,不能让
      // RequireAuth 把当前受限页写回 state.from。
      markProactiveLogout();
      if (currentUserId) await authApi.logout().catch(() => {});
      // 等待期间被其它标签页/会话换成新账号则放弃清理;凭证被本次退出清空
      // (token 变为 null)时继续完成跳转。
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
    // Issue #123 · 读取离线队列期间也可能收到 401 清空凭证;提前置位,保证任何
    // 时序下的兜底跳转都不带 from。取消退出时清除标记。
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
      // 放弃退出:撤销 requestLogout 提前置位的主动退出标记。
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

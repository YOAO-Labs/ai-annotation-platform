import { useEffect, useRef } from "react";
import { meApi } from "@/api/me";
import { useAuthStore } from "@/stores/authStore";

const HEARTBEAT_INTERVAL_MS = 30_000;

/**
 * 在线状态心跳。
 *
 * - 已登录（store.token 非空）+ document.visibilityState === 'visible' 时每 30s
 *   POST /auth/me/heartbeat 一次，刷新后端 last_seen_at + status='online'。
 * - 切到后台 / 隐藏 → 暂停；返回 → 立即打一次再恢复。
 * - 401 由 apiClient 自动 logout；其它错误静默忽略，不打扰用户。
 *
 * 后端 Celery beat 任务 mark_inactive_offline 每 2 分钟扫描，把超过
 * OFFLINE_THRESHOLD_MINUTES (默认 5min) 未刷新的 online 用户置 offline。
 * UsersPage 顶部「本周活跃」基于 last_seen_at >= now-7d 聚合（GET /users/stats）。
 */
export function useHeartbeat(intervalMs: number = HEARTBEAT_INTERVAL_MS): void {
  const token = useAuthStore((s) => s.token);
  const timerRef = useRef<number | null>(null);

  useEffect(() => {
    if (!token) return;

    const send = () => {
      if (typeof document !== "undefined" && document.visibilityState !== "visible") return;
      meApi.heartbeat().catch(() => {
        // 静默：401 已由 apiClient 触发 logout；其它错误（5xx / 网络）不打扰用户
      });
    };

    const start = () => {
      if (timerRef.current != null) return;
      send();
      timerRef.current = window.setInterval(send, intervalMs);
    };

    const stop = () => {
      if (timerRef.current != null) {
        window.clearInterval(timerRef.current);
        timerRef.current = null;
      }
    };

    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") {
        start();
      } else {
        stop();
      }
    };

    if (typeof document !== "undefined" && document.visibilityState === "visible") {
      start();
    }
    document.addEventListener("visibilitychange", onVisibilityChange);

    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [token, intervalMs]);
}

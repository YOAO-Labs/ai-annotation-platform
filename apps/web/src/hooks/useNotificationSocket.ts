import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useAuthStore } from "@/stores/authStore";
import { getAuthSessionRevision } from "@/stores/authSession";
import { useSessionRevision } from "@/hooks/useSessionGuard";
import { authApi } from "@/api/auth";
import { buildWsUrl } from "@/lib/wsHost";
import { useToastStore } from "@/components/ui/Toast";
import { jobNotificationVerb } from "@/components/jobs/asyncJobPresentation";
import {
  notificationPreferencesKey,
  useNotificationPreferences,
} from "@/hooks/useNotificationPreferences";

const REAUTH_CLOSE_CODES = new Set([1008, 4001]);

const ALERT_BATCH_WINDOW_MS = 1_000;
const ALERT_DEDUP_LIMIT = 200;

interface IncomingNotification {
  id?: unknown;
  type?: unknown;
  reason?: unknown;
  payload?: Record<string, unknown>;
  [extension: string]: unknown;
}

interface QueuedAlert {
  id: string;
  type: string;
  payload: Record<string, unknown>;
}

function alertKind(type: string): "error" | "warning" {
  return type === "task.rejected" ||
    type === "batch.rejected" ||
    type === "job.failed" ||
    type === "export.failed"
    ? "error"
    : "warning";
}

function alertText(type: string, payload: Record<string, unknown>): string {
  const str = (key: string) => (typeof payload[key] === "string" ? payload[key] : "");
  const jobVerb = jobNotificationVerb(type, payload.kind, payload);
  if (jobVerb) return `${jobVerb}${str("error_message") ? `：${str("error_message")}` : ""}`;
  switch (type) {
    case "task.rejected":
      return `任务 ${str("task_display_id")} 被审核员退回${str("reject_reason") ? `：${str("reject_reason")}` : ""}`;
    case "batch.rejected":
      return `批次 ${str("batch_display_id") || str("batch_name")} 被驳回${str("feedback") ? `：${str("feedback")}` : ""}`;
    case "batch.review_reopened":
      return `批次 ${str("batch_display_id")} 重新进入审核`;
    case "batch.admin_locked":
      return `批次 ${str("batch_display_id")} 被管理员锁定`;
    case "annotation.comment_mentioned":
      return `${str("actor_name") || "有人"} 在标注评论中提到了你`;
    case "feedback.comment_mentioned":
      return `${str("actor_name") || "有人"} 在任务留言中提到了你`;
    case "feedback.reply_created":
      return `${str("actor_name") || "有人"} 回复了你关注的问题`;
    case "export.failed":
      return `导出失败${str("error") ? `：${str("error")}` : ""}`;
    default:
      return "收到重要通知，请查看通知中心";
  }
}

export function useNotificationSocket() {
  const token = useAuthStore((s) => s.token);
  const setToken = useAuthStore((s) => s.setToken);
  const userId = useAuthStore((s) => s.user?.id);
  const revision = useSessionRevision();
  const qc = useQueryClient();
  const pushToast = useToastStore((s) => s.push);
  const preferencesQ = useNotificationPreferences();

  const preferencesRef = useRef(preferencesQ);
  preferencesRef.current = preferencesQ;
  const userIdRef = useRef(userId);
  userIdRef.current = userId;
  const preferencesStaleSinceRef = useRef(0);

  useEffect(() => {
    if (!token) return;

    let ws: WebSocket | null = null;
    let closedManually = false;
    let retryTimer: number | null = null;
    let backoff = 1000;
    let refreshing = false;
    const sessionToken = token;

    const migrationAlertAllowed = (payload: Record<string, unknown>): boolean => {
      if (!["storage_migration_inventory", "storage_migration_copy"].includes(String(payload.kind)))
        return true;
      const current = useAuthStore.getState();
      return (
        current.user?.role === "super_admin" &&
        current.user.id === userId &&
        current.token === sessionToken &&
        getAuthSessionRevision() === revision
      );
    };

    const seenAlertIds = new Set<string>();
    const seenAlertOrder: string[] = [];
    let pendingAlerts: QueuedAlert[] = [];
    let alertFlushTimer: number | null = null;

    const rememberAlertId = (id: string) => {
      if (seenAlertIds.has(id)) return false;
      seenAlertIds.add(id);
      seenAlertOrder.push(id);
      while (seenAlertOrder.length > ALERT_DEDUP_LIMIT) {
        const dropped = seenAlertOrder.shift();
        if (dropped !== undefined) seenAlertIds.delete(dropped);
      }
      return true;
    };

    const flushAlerts = () => {
      alertFlushTimer = null;
      const batch = pendingAlerts.filter((alert) => migrationAlertAllowed(alert.payload));
      pendingAlerts = [];
      if (batch.length === 0) return;
      if (typeof document !== "undefined" && document.visibilityState !== "visible") return;
      if (batch.length === 1) {
        pushToast({
          kind: alertKind(batch[0].type),
          msg: alertText(batch[0].type, batch[0].payload),
        });
        return;
      }
      pushToast({
        kind: "warning",
        msg: `收到 ${batch.length} 条重要通知，请查看通知中心`,
      });
    };

    const alertAllowedFor = (type: string): boolean => {
      const snapshot = preferencesRef.current;
      if (!userIdRef.current || !snapshot.isSuccess || snapshot.isError) return false;
      if (snapshot.dataUpdatedAt <= preferencesStaleSinceRef.current) return false;
      const item = snapshot.data?.items.find((entry) => entry.type === type);
      if (!item) return false;
      return item.in_app && item.toast;
    };

    const maybeQueueAlert = (message: IncomingNotification) => {
      if (typeof document !== "undefined" && document.visibilityState !== "visible") return;
      const id = typeof message.id === "string" ? message.id : null;
      const type = typeof message.type === "string" ? message.type : null;
      if (!id || !type) return;
      if (!migrationAlertAllowed(message.payload ?? {})) return;
      if (!alertAllowedFor(type)) return;
      if (!rememberAlertId(id)) return;
      pendingAlerts.push({ id, type, payload: message.payload ?? {} });
      if (alertFlushTimer === null) {
        alertFlushTimer = window.setTimeout(flushAlerts, ALERT_BATCH_WINDOW_MS);
      }
    };

    function currentToken(): string | null {
      return useAuthStore.getState().token;
    }

    function connect() {
      const t = currentToken();
      if (!t) return;

      const url = buildWsUrl("/ws/notifications", { token: t });
      try {
        ws = new WebSocket(url);
      } catch {
        scheduleRetry();
        return;
      }
      ws.onopen = () => {
        backoff = 1000;
        refreshing = false;
        qc.invalidateQueries({ queryKey: ["notifications"] });
      };
      ws.onmessage = (e) => {
        let parsed: IncomingNotification | null = null;
        try {
          parsed = JSON.parse(e.data as string) as IncomingNotification;
          if (parsed && parsed.type === "ping") return;
        } catch {
          return;
        }
        if (!parsed || typeof parsed.type !== "string") return;
        if (parsed.type === "notifications.sync") {
          const reason = typeof parsed.reason === "string" ? parsed.reason : null;
          qc.invalidateQueries({ queryKey: ["notifications"] });
          if (reason === "preferences") {
            const owner = userIdRef.current;
            preferencesStaleSinceRef.current = Date.now();
            if (owner) qc.invalidateQueries({ queryKey: notificationPreferencesKey(owner) });
          }
          return;
        }
        if (parsed.type.startsWith("failed_prediction.retry.")) {
          qc.invalidateQueries({ queryKey: ["admin", "failed-predictions"] });
        }
        if (parsed.type.startsWith("job.")) {
          qc.invalidateQueries({ queryKey: ["async-jobs"] });
          if (parsed.payload?.kind === "prediction_retry") {
            qc.invalidateQueries({ queryKey: ["admin", "failed-predictions"] });
          }
        }
        qc.invalidateQueries({ queryKey: ["notifications"] });
        maybeQueueAlert(parsed);
      };
      ws.onclose = (event) => {
        if (closedManually) return;
        if (REAUTH_CLOSE_CODES.has(event.code) && !refreshing) {
          refreshing = true;
          authApi
            .refresh()
            .then((resp) => {
              if (useAuthStore.getState().token !== sessionToken) {
                closedManually = true;
                return;
              }
              setToken(resp.access_token);
              backoff = 1000;
              scheduleRetry();
            })
            .catch(() => {
              closedManually = true;
            });
          return;
        }
        scheduleRetry();
      };
      ws.onerror = () => {
        try {
          ws?.close();
        } catch {
          /* noop */
        }
      };
    }

    function scheduleRetry() {
      if (closedManually) return;
      if (retryTimer) window.clearTimeout(retryTimer);
      retryTimer = window.setTimeout(connect, backoff);
      backoff = Math.min(backoff * 2, 30_000);
    }

    connect();

    return () => {
      closedManually = true;
      if (retryTimer) window.clearTimeout(retryTimer);
      if (alertFlushTimer !== null) window.clearTimeout(alertFlushTimer);
      pendingAlerts = [];
      try {
        ws?.close();
      } catch {
        /* noop */
      }
    };
  }, [token, userId, revision, qc, setToken, pushToast]);
}

import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useAuthStore } from "@/stores/authStore";
import { authApi } from "@/api/auth";
import { buildWsUrl } from "@/lib/wsHost";
import { useToastStore } from "@/components/ui/Toast";
import {
  notificationPreferencesKey,
  useNotificationPreferences,
} from "@/hooks/useNotificationPreferences";

// v0.8.8 · WS 鉴权过期重连。
// 后端 /ws/notifications 鉴权失败用 1008（policy violation）关闭。
// 标注员长会话（开着标注页面 24h+）token 过期后无需手动刷页：
//   1) onclose code 1008/4001 → 调 /auth/refresh 拿新 token
//   2) 成功 → 写 authStore + 用新 token 重连
//   3) 失败 → ApiError 401 → client.ts 已自动 logout，路由层会跳 /login
const REAUTH_CLOSE_CODES = new Set([1008, 4001]);

/** 重要事件 1 秒内合并为一条 toast 的窗口。 */
const ALERT_BATCH_WINDOW_MS = 1_000;
/** 每账号最多记住的告警去重 ID 数。 */
const ALERT_DEDUP_LIMIT = 200;

interface IncomingNotification {
  id?: unknown;
  type?: unknown;
  payload?: Record<string, unknown>;
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
    case "job.failed":
      return `后台任务失败${str("error_message") ? `：${str("error_message")}` : ""}`;
    case "export.failed":
      return `导出失败${str("error") ? `：${str("error")}` : ""}`;
    default:
      return "收到重要通知，请查看通知中心";
  }
}

/**
 * v0.6.9 · 单用户通知 WS：订阅 /ws/notifications。
 * v0.9 起挂在 App 生命周期（路由切换不拆连接），进入标注/审核工作台
 * 仍有未读角标与实时刷新；WS 断线时 useNotifications 30s 轮询兜底。
 *
 * 职责：
 * - 连接打开/重开、真实通知与 notifications.sync 事件 → invalidate 通知查询
 * - notifications.sync reason=preferences → 额外刷新偏好查询
 * - 可见标签页的瞬时重要提醒：偏好（接收+弹出）开启才弹，1s 内合并，
 *   按通知 ID 去重（最多 200 条），隐藏标签页不弹、只计数。
 */
export function useNotificationSocket() {
  const token = useAuthStore((s) => s.token);
  const setToken = useAuthStore((s) => s.setToken);
  const userId = useAuthStore((s) => s.user?.id);
  const qc = useQueryClient();
  const pushToast = useToastStore((s) => s.push);
  // 该查询在此 hook 保持活跃（认证会话内）；设置入口复用同一缓存。
  const preferencesQ = useNotificationPreferences();

  // 事件回调在 ws.onmessage 闭包里运行，读 ref 拿到最新偏好快照。
  const preferencesRef = useRef(preferencesQ);
  preferencesRef.current = preferencesQ;
  const userIdRef = useRef(userId);
  userIdRef.current = userId;
  // 偏好同步事件之后、新一轮拉取完成之前，缓存可能是别处已改的旧值。
  const preferencesStaleSinceRef = useRef(0);

  useEffect(() => {
    if (!token) return;

    let ws: WebSocket | null = null;
    let closedManually = false;
    let retryTimer: number | null = null;
    let backoff = 1000;
    // 已对当前过期 token 触发过 refresh，避免单次过期触发多次 /auth/refresh
    let refreshing = false;
    // 本次连接绑定的会话（token）；refresh 迟到的结果不得写回后续会话。
    const sessionToken = token;

    // ── 告警去重 + 1s 批量合并（当前账号） ──────────────────────────
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
      const batch = pendingAlerts;
      pendingAlerts = [];
      if (batch.length === 0) return;
      // 批量窗口内标签页可能已切到后台：此时丢弃这批提醒，避免回到前台
      // 才看到不合时宜的 toast，与「隐藏标签页不弹」保持一致。
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

    /** 偏好可用且该类型接收+弹出都开启时才允许瞬时提醒。 */
    const alertAllowedFor = (type: string): boolean => {
      const snapshot = preferencesRef.current;
      if (!userIdRef.current || !snapshot.isSuccess || snapshot.isError) return false;
      // 偏好刚被同步事件标记失效（含本会话自己保存后的 invalidate）：
      // 在新一轮数据落地前抑制提醒，避免用旧偏好弹出。
      if (snapshot.dataUpdatedAt <= preferencesStaleSinceRef.current) return false;
      const item = snapshot.data?.items.find((entry) => entry.type === type);
      if (!item) return false; // 未知/未来类型默认不弹
      return item.in_app && item.toast;
    };

    const maybeQueueAlert = (message: IncomingNotification) => {
      // 只有可见标签页产生瞬时提醒；隐藏会话仅通过计数同步。
      if (typeof document !== "undefined" && document.visibilityState !== "visible") return;
      const id = typeof message.id === "string" ? message.id : null;
      const type = typeof message.type === "string" ? message.type : null;
      if (!id || !type) return;
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

      // 后端 ws_router 在 main.py:108 无 prefix 注册, 路径是 /ws/notifications
      // (历史 v0.6.9 写错为 /api/v1/ws/...). v0.9.13 host/proto 拼接迁到 buildWsUrl helper.
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
        // 重连/重开都先取一次持久化状态，避免错过断线期间的事件。
        qc.invalidateQueries({ queryKey: ["notifications"] });
      };
      ws.onmessage = (e) => {
        // v0.7.0：服务端 30s 心跳 ping 帧不应触发 invalidate；非 JSON 忽略。
        let parsed: IncomingNotification | null = null;
        try {
          parsed = JSON.parse(e.data as string) as IncomingNotification;
          if (parsed && parsed.type === "ping") return;
        } catch {
          return;
        }
        if (!parsed || typeof parsed.type !== "string") return;
        if (parsed.type === "notifications.sync") {
          const reason =
            typeof (parsed as { reason?: unknown }).reason === "string"
              ? (parsed as { reason?: string }).reason
              : null;
          qc.invalidateQueries({ queryKey: ["notifications"] });
          if (reason === "preferences") {
            const owner = userIdRef.current;
            // 偏好已变化：旧快照在重新拉取完成前不得用于弹窗判断。
            preferencesStaleSinceRef.current = Date.now();
            if (owner) qc.invalidateQueries({ queryKey: notificationPreferencesKey(owner) });
          }
          return; // sync 事件绝不产生 toast
        }
        // v0.8.6 F6 · retry.* 进度事件触发失败预测列表 invalidate
        if (parsed.type.startsWith("failed_prediction.retry.")) {
          qc.invalidateQueries({ queryKey: ["admin", "failed-predictions"] });
        }
        if (parsed.type.startsWith("job.")) {
          qc.invalidateQueries({ queryKey: ["async-jobs"] });
          if (parsed.payload?.kind === "prediction_retry") {
            qc.invalidateQueries({ queryKey: ["admin", "failed-predictions"] });
          }
        }
        // 真实通知：刷新计数/列表；可见标签页按偏好决定瞬时提醒。
        qc.invalidateQueries({ queryKey: ["notifications"] });
        maybeQueueAlert(parsed);
      };
      ws.onclose = (event) => {
        if (closedManually) return;
        // v0.8.8 · 鉴权过期：尝试 refresh token 后再重连
        if (REAUTH_CLOSE_CODES.has(event.code) && !refreshing) {
          refreshing = true;
          authApi
            .refresh()
            .then((resp) => {
              // 会话已被登出/替换（token 变化）：迟到的 refresh 结果不得写回。
              if (useAuthStore.getState().token !== sessionToken) {
                closedManually = true;
                return;
              }
              setToken(resp.access_token);
              backoff = 1000;
              scheduleRetry();
            })
            .catch(() => {
              // refresh 失败 → client.ts 401 拦截已 logout → 不再 retry
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
    // token/账号变化都会重开连接并清空待发提醒批次。
  }, [token, userId, qc, setToken, pushToast]);
}

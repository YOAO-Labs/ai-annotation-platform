import { useEffect, useRef, useState } from "react";
import { bumpWsReconnectCount } from "./_wsMetrics";

export type ReconnectState = "connecting" | "open" | "reconnecting" | "closed" | "failed";

interface Options {
  /** 是否启用此连接（false 时不连/不重连）。 */
  enabled?: boolean;
  /** 最大重试次数；用尽后停留在 `failed`。默认 8。 */
  maxRetries?: number;
  /** 退避序列（毫秒）；超出长度则保持最后一项。 */
  backoffMs?: number[];
  /** 服务端发来的消息回调。 */
  onMessage?: (event: MessageEvent) => void;
  /** 连接打开时回调，可用于发送订阅消息。 */
  onOpen?: (ws: WebSocket) => void;
}

const DEFAULT_BACKOFF = [1_000, 2_000, 4_000, 8_000, 15_000, 30_000];

/**
 * 带指数退避自动重连的 WebSocket hook。
 *
 * - 主动 close（unmount / enabled=false）不会触发重连；
 * - 服务端断开 / 网络错误才会进入 `reconnecting` 状态；
 * - 重试用尽后停留在 `failed`，直到外部条件变化（url/enabled 改变）触发重连。
 */
export function useReconnectingWebSocket(
  url: string | null,
  options: Options = {},
): { state: ReconnectState; retries: number } {
  const {
    enabled = true,
    maxRetries = 8,
    backoffMs = DEFAULT_BACKOFF,
    onMessage,
    onOpen,
  } = options;
  const [state, setState] = useState<ReconnectState>("closed");
  const [retries, setRetries] = useState(0);
  const onMessageRef = useRef(onMessage);
  const onOpenRef = useRef(onOpen);
  onMessageRef.current = onMessage;
  onOpenRef.current = onOpen;

  useEffect(() => {
    if (!url || !enabled) {
      setState("closed");
      setRetries(0);
      return;
    }

    let attempt = 0;
    let cancelled = false;
    let ws: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

    const connect = () => {
      if (cancelled) return;
      setState(attempt === 0 ? "connecting" : "reconnecting");
      try {
        ws = new WebSocket(url);
      } catch {
        scheduleReconnect();
        return;
      }

      ws.onopen = () => {
        if (cancelled) return;
        attempt = 0;
        setRetries(0);
        setState("open");
        if (ws && onOpenRef.current) onOpenRef.current(ws);
      };
      ws.onmessage = (e) => onMessageRef.current?.(e);
      ws.onerror = () => {
        // onclose 也会触发，由其统一处理退避
      };
      ws.onclose = () => {
        if (cancelled) return;
        scheduleReconnect();
      };
    };

    const scheduleReconnect = () => {
      if (cancelled) return;
      if (attempt >= maxRetries) {
        setState("failed");
        return;
      }
      const delay = backoffMs[Math.min(attempt, backoffMs.length - 1)];
      attempt += 1;
      setRetries(attempt);
      setState("reconnecting");
      // PerfHud 浏览器侧累计 WS 重连数
      bumpWsReconnectCount();
      reconnectTimer = setTimeout(connect, delay);
    };

    connect();

    return () => {
      cancelled = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (ws) {
        ws.onmessage = ws.onerror = ws.onclose = null;
        if (ws.readyState === WebSocket.OPEN) {
          ws.onopen = null;
          ws.close();
        } else if (ws.readyState === WebSocket.CONNECTING) {
          // 关闭仍在握手中的 socket 浏览器会告警；改为握手完成后再静默关闭。
          const w = ws;
          w.onopen = () => w.close();
        }
      }
    };
  }, [url, enabled, maxRetries, backoffMs]);

  return { state, retries };
}

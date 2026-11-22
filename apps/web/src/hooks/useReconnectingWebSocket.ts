import { useEffect, useRef, useState } from "react";
import { bumpWsReconnectCount } from "./_wsMetrics";

export type ReconnectState = "connecting" | "open" | "reconnecting" | "closed" | "failed";

interface Options {
  enabled?: boolean;
  maxRetries?: number;
  backoffMs?: number[];
  onMessage?: (event: MessageEvent) => void;
  onOpen?: (ws: WebSocket) => void;
}

const DEFAULT_BACKOFF = [1_000, 2_000, 4_000, 8_000, 15_000, 30_000];

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
      ws.onerror = () => {};
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
          const w = ws;
          w.onopen = () => w.close();
        }
      }
    };
  }, [url, enabled, maxRetries, backoffMs]);

  return { state, retries };
}

import { useEffect, useRef } from "react";
import { meApi } from "@/api/me";
import { useAuthStore } from "@/stores/authStore";

const HEARTBEAT_INTERVAL_MS = 30_000;

export function useHeartbeat(intervalMs: number = HEARTBEAT_INTERVAL_MS): void {
  const token = useAuthStore((s) => s.token);
  const timerRef = useRef<number | null>(null);

  useEffect(() => {
    if (!token) return;

    const send = () => {
      if (typeof document !== "undefined" && document.visibilityState !== "visible") return;
      meApi.heartbeat().catch(() => {});
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

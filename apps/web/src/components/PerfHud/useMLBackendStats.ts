import { useEffect, useRef, useState } from "react";
import { useAuthStore } from "@/stores/authStore";
import { buildWsUrl } from "@/lib/wsHost";
import type { MLBackendCompute } from "@/utils/mlBackendCompute";
import { usePerfHudStore } from "./usePerfHudStore";

export interface BackendSnapshot {
  physical_key?: string | null;
  url_host?: string | null;
  backend_id: string;
  backend_name?: string | null;
  bindings?: Array<{
    backend_id: string;
    backend_name: string;
    project_id?: string | null;
    project_display_id?: string | null;
    project_name?: string | null;
  }> | null;
  state: string;
  gpu_info?: {
    device_name?: string | null;
    memory_used_mb?: number | null;
    memory_total_mb?: number | null;
    memory_free_mb?: number | null;
    gpu_utilization_percent?: number | null;
    gpu_temperature_celsius?: number | null;
    gpu_power_watts?: number | null;
    image_pool_loaded_variants?: unknown[] | null;
    video_pool_loaded_variants?: unknown[] | null;
  } | null;
  host?: {
    container_cpu_percent?: number | null;
    container_memory_percent?: number | null;
  } | null;
  cache?: {
    hits?: number | null;
    misses?: number | null;
    size?: number | null;
    capacity?: number | null;
    hit_rate?: number | null;
  } | null;
  model_version?: string | null;
  loaded?: boolean | null;
  idle_unload_seconds?: number | null;
  last_request_age_seconds?: number | null;
  pool?: {
    cap?: number | null;
    current_size?: number | null;
    loaded_keys?: unknown[] | null;
    loaded_variants?: unknown[] | null;
  } | null;
  video_pool?: {
    cap?: number | null;
    current_size?: number | null;
    loaded_keys?: unknown[] | null;
    loaded_variants?: unknown[] | null;
    active_sessions?: number | null;
    idle_seconds?: number | null;
  } | null;
  compute?: MLBackendCompute | null;
  timestamp?: string;
}

export interface BackendHistory {
  gpuUtil: number[];
  vramPercent: number[];
  cpu: number[];
  mem: number[];
}

const RING_BUFFER_SIZE = 60;

function pushRing(arr: number[], v: number): number[] {
  const next = arr.length >= RING_BUFFER_SIZE ? arr.slice(1) : arr.slice();
  next.push(v);
  return next;
}

function snapshotKey(snap: BackendSnapshot): string {
  return snap.physical_key || snap.backend_id;
}

export function useMLBackendStats() {
  const token = useAuthStore((s) => s.token);
  const visible = usePerfHudStore((s) => s.visible);
  const [snapshots, setSnapshots] = useState<Record<string, BackendSnapshot>>({});
  const [history, setHistory] = useState<Record<string, BackendHistory>>({});
  const [status, setStatus] = useState<
    "idle" | "connecting" | "connected" | "closed" | "auth_failed"
  >("idle");
  const lastTickRef = useRef<number>(0);

  useEffect(() => {
    if (!visible || !token) {
      setStatus("idle");
      return;
    }
    const url = buildWsUrl("/ws/ml-backend-stats", { token });
    let ws: WebSocket | null;
    setStatus("connecting");
    try {
      ws = new WebSocket(url);
    } catch {
      setStatus("closed");
      return;
    }
    ws.onopen = () => setStatus("connected");
    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        if (msg?.type === "ping") return;
        if (!Array.isArray(msg?.backends)) return;
        const now = Date.now();
        if (now - lastTickRef.current < 500) return;
        lastTickRef.current = now;

        const nextSnap: Record<string, BackendSnapshot> = {};
        for (const b of msg.backends as BackendSnapshot[]) {
          nextSnap[snapshotKey(b)] = b;
        }
        setSnapshots(nextSnap);
        setHistory((prev) => {
          const next: Record<string, BackendHistory> = { ...prev };
          for (const b of msg.backends as BackendSnapshot[]) {
            const key = snapshotKey(b);
            const cur = next[key] ?? {
              gpuUtil: [],
              vramPercent: [],
              cpu: [],
              mem: [],
            };
            const vramPct =
              b.gpu_info?.memory_used_mb != null && b.gpu_info?.memory_total_mb
                ? (b.gpu_info.memory_used_mb / b.gpu_info.memory_total_mb) * 100
                : 0;
            next[key] = {
              gpuUtil: pushRing(cur.gpuUtil, b.gpu_info?.gpu_utilization_percent ?? 0),
              vramPercent: pushRing(cur.vramPercent, vramPct),
              cpu: pushRing(cur.cpu, b.host?.container_cpu_percent ?? 0),
              mem: pushRing(cur.mem, b.host?.container_memory_percent ?? 0),
            };
          }
          return next;
        });
      } catch {
        // 非 JSON 帧, 忽略
      }
    };
    ws.onclose = (event) => {
      setStatus((s) => {
        if (event.code === 1008) return "auth_failed";
        if (s === "connecting") return "auth_failed";
        return "closed";
      });
    };
    ws.onerror = () => setStatus("closed");

    return () => {
      ws?.close();
      ws = null;
    };
  }, [visible, token]);

  return { snapshots, history, status, connected: status === "connected" };
}

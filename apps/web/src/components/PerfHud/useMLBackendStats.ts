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
    // 老 gsam2 backend 把 image/video pool loaded 数量注入 gpu_info 兜底;
    // 优先使用 pool/video_pool.current_size；缺失时回退到 gpu_info。
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
  /** 有效计算设备观测 (GPU 静默退回 CPU 告警用)。 */
  compute?: MLBackendCompute | null;
  timestamp?: string;
}

export interface BackendHistory {
  gpuUtil: number[];
  vramPercent: number[];
  cpu: number[];
  mem: number[];
}

const RING_BUFFER_SIZE = 60; // 60 帧 * 1s = 60s window

function pushRing(arr: number[], v: number): number[] {
  const next = arr.length >= RING_BUFFER_SIZE ? arr.slice(1) : arr.slice();
  next.push(v);
  return next;
}

function snapshotKey(snap: BackendSnapshot): string {
  return snap.physical_key || snap.backend_id;
}

/**
 * PerfHud · 订阅 /ws/ml-backend-stats, 维护 60s ring buffer.
 * 仅在 PerfHud 浮窗 visible 时建连; 关闭即断 (节省后端 1s pull).
 */
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
    // ws_router 在 main.py 无 prefix 注册, 路径直接是 /ws/ml-backend-stats (与 /ws/prediction-jobs 一致).
    // host/proto 拼接迁到 buildWsUrl helper.
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
        // 节流: 同一秒内多帧只取首条 (后端 1s 触发 1 次, 这层是兜底)
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
      // starlette WebSocket 在 accept 前 close() 会以 HTTP 403 拒绝握手 (浏览器看到 1006).
      // 1008 = policy violation (accept 后 close); 1006 abnormal closure (常见鉴权失败).
      // 用 status === connecting 时收到 close 当作 auth_failed (没握手成功过).
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

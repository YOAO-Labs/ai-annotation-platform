import { apiClient } from "./client";

export type SystemHealthStatus = "ok" | "degraded" | "down";

export interface HealthComponent {
  name: string;
  label: string;
  status: SystemHealthStatus;
  latency_ms: number | null;
  detail: string | null;
}

export interface CeleryWorkerHealth {
  name: string;
  last_heartbeat_seconds_ago: number | null;
  pool_max: number | null;
  status: SystemHealthStatus;
}

export interface CeleryQueueHealth {
  name: string;
  length: number;
  status: SystemHealthStatus;
}

export interface SystemHealthResponse {
  status: SystemHealthStatus;
  version: string;
  components: HealthComponent[];
  celery: {
    active_count: number;
    workers: CeleryWorkerHealth[];
    queues: CeleryQueueHealth[];
  };
}

export const adminSystemHealthApi = {
  get: () => apiClient.get<SystemHealthResponse>("/admin/system-health"),
};

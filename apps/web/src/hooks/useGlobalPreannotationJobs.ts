/**
 * 全局预标 job 订阅 — Topbar 徽章 / 切项目 toast 共用.
 *
 * 与 `usePreannotationProgress(projectId)` 的差异:
 * - 后者单项目高频帧 (current/total)
 * - 本 hook 全局, 仅在 job 开始 / 结束 / 失败 3 时点接消息, 维护一份当前
 *   in-progress jobs Map.
 *
 * WS 端点: `/ws/prediction-jobs?token=<jwt>` (后端 ws.py).
 */

import { useCallback, useMemo, useRef, useState } from "react";

import { useAuthStore } from "@/stores/authStore";
import { useReconnectingWebSocket } from "@/hooks/useReconnectingWebSocket";
import { buildWsUrl } from "@/lib/wsHost";

export interface GlobalJobProgress {
  job_id: string;
  project_id: string;
  project_name?: string | null;
  batch_id?: string | null;
  status: "running" | "completed" | "failed" | "error";
  current: number;
  total: number;
  success_count?: number;
  failed_count?: number;
  duration_ms?: number;
  error?: string | null;
  /** 客户端落库时间, 用于完成 / 失败后的延迟移除 */
  receivedAt: number;
}

const REMOVE_AFTER_DONE_MS = 1500;

export function useGlobalPreannotationJobs(): {
  runningJobs: GlobalJobProgress[];
  byProject: Record<string, GlobalJobProgress>;
  connected: boolean;
} {
  const token = useAuthStore((s) => s.token);
  const role = useAuthStore((s) => s.user?.role);
  const isAdmin = role === "super_admin" || role === "project_admin";

  const [jobs, setJobs] = useState<Record<string, GlobalJobProgress>>({});
  const removalTimersRef = useRef<Record<string, ReturnType<typeof setTimeout>>>({});

  const url = useMemo(() => {
    if (!token || !isAdmin) return null;
    return buildWsUrl("/ws/prediction-jobs", { token });
  }, [token, isAdmin]);

  const onMessage = useCallback((e: MessageEvent) => {
    let payload: GlobalJobProgress | null = null;
    try {
      const parsed = JSON.parse(e.data);
      // ping 帧来自 _heartbeat_loop
      if (parsed?.type === "ping") return;
      if (!parsed?.job_id || !parsed?.project_id) return;
      payload = { ...parsed, receivedAt: Date.now() };
    } catch {
      return;
    }
    if (!payload) return;
    const job: GlobalJobProgress = payload;

    setJobs((prev) => {
      const next = { ...prev, [job.job_id]: job };
      return next;
    });

    if (job.status !== "running") {
      const existing = removalTimersRef.current[job.job_id];
      if (existing) clearTimeout(existing);
      removalTimersRef.current[job.job_id] = setTimeout(() => {
        setJobs((prev) => {
          const { [job.job_id]: _drop, ...rest } = prev;
          return rest;
        });
        delete removalTimersRef.current[job.job_id];
      }, REMOVE_AFTER_DONE_MS);
    }
  }, []);

  const { state } = useReconnectingWebSocket(url, { onMessage, enabled: !!url });

  const runningJobs = useMemo(
    () => Object.values(jobs).filter((j) => j.status === "running"),
    [jobs],
  );

  const byProject = useMemo(() => {
    const out: Record<string, GlobalJobProgress> = {};
    for (const j of runningJobs) {
      // 同项目多 job: 取最新 (按 receivedAt)
      const cur = out[j.project_id];
      if (!cur || cur.receivedAt < j.receivedAt) out[j.project_id] = j;
    }
    return out;
  }, [runningJobs]);

  return {
    runningJobs,
    byProject,
    connected: state === "open",
  };
}

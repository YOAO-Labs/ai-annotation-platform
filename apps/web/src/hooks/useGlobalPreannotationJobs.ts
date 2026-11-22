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

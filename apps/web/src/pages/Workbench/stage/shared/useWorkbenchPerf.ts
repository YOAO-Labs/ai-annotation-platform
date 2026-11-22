import { useEffect, useRef, useState } from "react";

export interface WorkbenchPerfStats {
  longTaskCount: number;
  longTaskMaxMs: number;
  lastLongTaskAt: number | null;
}

const EMPTY: WorkbenchPerfStats = { longTaskCount: 0, longTaskMaxMs: 0, lastLongTaskAt: null };

const W = typeof window === "undefined" ? null : (window as unknown as Record<string, unknown>);

/**
 * 监听 PerformanceObserver longtask 入口，聚合统计；同时把最新结果挂在
 * `window.__workbenchPerf` 便于 BugReport / 浏览器控制台调取。
 *
 * @param sampleRate 0..1。默认 1（DEV 全采）或 0.05（PROD 5%）。0 表示禁用。
 */
export function useWorkbenchPerf(sampleRate?: number): WorkbenchPerfStats {
  const [stats, setStats] = useState<WorkbenchPerfStats>(EMPTY);
  const statsRef = useRef(stats);
  statsRef.current = stats;

  useEffect(() => {
    if (typeof PerformanceObserver === "undefined") return;
    const supported =
      (PerformanceObserver as unknown as { supportedEntryTypes?: string[] }).supportedEntryTypes ??
      [];
    if (!supported.includes("longtask")) return;

    const isDev =
      typeof import.meta !== "undefined" &&
      (import.meta as unknown as { env?: { DEV?: boolean } }).env?.DEV;
    const rate = typeof sampleRate === "number" ? sampleRate : isDev ? 1 : 0.05;
    if (rate <= 0) return;

    if (W && W.__workbenchPerf === undefined) W.__workbenchPerf = EMPTY;

    if (rate < 1 && Math.random() > rate) return;

    const observer = new PerformanceObserver((list) => {
      const entries = list.getEntries();
      if (entries.length === 0) return;
      let maxMs = statsRef.current.longTaskMaxMs;
      let last = statsRef.current.lastLongTaskAt;
      for (const e of entries) {
        if (e.duration > maxMs) maxMs = e.duration;
        last = e.startTime || Date.now();
      }
      const next: WorkbenchPerfStats = {
        longTaskCount: statsRef.current.longTaskCount + entries.length,
        longTaskMaxMs: maxMs,
        lastLongTaskAt: last,
      };
      statsRef.current = next;
      setStats(next);
      if (W) W.__workbenchPerf = next;
    });
    observer.observe({ entryTypes: ["longtask"] });
    return () => observer.disconnect();
  }, [sampleRate]);

  return stats;
}

export function readWorkbenchPerfSnapshot(): WorkbenchPerfStats {
  if (!W) return EMPTY;
  const v = W.__workbenchPerf as WorkbenchPerfStats | undefined;
  return v ?? EMPTY;
}

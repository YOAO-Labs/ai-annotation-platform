import { useEffect, useState } from "react";
import { getApiP95Ms } from "@/api/_metrics";
import { useWsMetricsStore } from "@/hooks/_wsMetrics";
import { useTaskBoxCountStore } from "./useTaskBoxCount";

const LONGTASK_WINDOW_MS = 60_000;
const SAMPLE_INTERVAL_MS = 1_000;

interface BrowserStats {
  fps: number | null;
  jsHeapMB: number | null;
  longtaskCount60s: number;
  longtaskLastMs: number | null;
  apiP95Ms: number | null;
  wsReconnects: number;
  taskBoxCount: number;
}

interface PerformanceMemory {
  usedJSHeapSize: number;
}
type PerformanceWithMemory = Performance & { memory?: PerformanceMemory };

export function useBrowserStats(enabled: boolean): BrowserStats {
  const wsReconnects = useWsMetricsStore((s) => s.reconnects);
  const taskBoxCount = useTaskBoxCountStore((s) => s.count);

  const [fps, setFps] = useState<number | null>(null);
  const [jsHeapMB, setJsHeapMB] = useState<number | null>(null);
  const [apiP95Ms, setApiP95Ms] = useState<number | null>(null);
  const [longtaskCount60s, setLongtaskCount60s] = useState(0);
  const [longtaskLastMs, setLongtaskLastMs] = useState<number | null>(null);

  useEffect(() => {
    if (!enabled) return;
    let raf = 0;
    let frames = 0;
    let lastSample = performance.now();
    const tick = (now: number) => {
      frames += 1;
      if (now - lastSample >= SAMPLE_INTERVAL_MS) {
        setFps(Math.round((frames * 1000) / (now - lastSample)));
        frames = 0;
        lastSample = now;
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [enabled]);

  useEffect(() => {
    if (!enabled) return;
    const id = setInterval(() => {
      const mem = (performance as PerformanceWithMemory).memory;
      if (mem && typeof mem.usedJSHeapSize === "number") {
        setJsHeapMB(Math.round(mem.usedJSHeapSize / (1024 * 1024)));
      }
      setApiP95Ms(getApiP95Ms());
    }, SAMPLE_INTERVAL_MS);
    return () => clearInterval(id);
  }, [enabled]);

  useEffect(() => {
    if (!enabled) return;
    if (typeof PerformanceObserver === "undefined") return;
    const supportedTypes = (
      PerformanceObserver as unknown as {
        supportedEntryTypes?: string[];
      }
    ).supportedEntryTypes;
    if (supportedTypes && !supportedTypes.includes("longtask")) return;

    type Entry = { t: number; d: number };
    const entries: Entry[] = [];
    let observer: PerformanceObserver | null = null;
    try {
      observer = new PerformanceObserver((list) => {
        const cutoff = Date.now() - LONGTASK_WINDOW_MS;
        for (const e of list.getEntries()) {
          entries.push({ t: Date.now(), d: e.duration });
          setLongtaskLastMs(Math.round(e.duration));
        }
        while (entries.length > 0 && entries[0].t < cutoff) entries.shift();
        setLongtaskCount60s(entries.length);
      });
      observer.observe({ type: "longtask", buffered: true });
    } catch {
      // 浏览器不支持 — silently skip
    }
    return () => observer?.disconnect();
  }, [enabled]);

  useEffect(() => {
    if (!enabled) return;
    const id = setInterval(() => {
      setLongtaskCount60s((c) => c);
    }, 5_000);
    return () => clearInterval(id);
  }, [enabled]);

  return {
    fps,
    jsHeapMB,
    longtaskCount60s,
    longtaskLastMs,
    apiP95Ms,
    wsReconnects,
    taskBoxCount,
  };
}

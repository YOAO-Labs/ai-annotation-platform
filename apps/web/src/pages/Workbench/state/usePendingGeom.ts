import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { AnnotationResponse, Geometry } from "@/types";

interface PendingEntry {
  geom: Geometry;
  ts: number;
}

const MAX_AGE_MS = 10_000;

function geomEquals(a: Geometry | undefined, b: Geometry): boolean {
  if (!a) return false;
  if (a === (b as unknown)) return true;
  if (a.type !== b.type) return false;
  return JSON.stringify(a) === JSON.stringify(b);
}

export interface PendingGeomApi {
  pendingGeomMap: Map<string, Geometry>;
  markPendingGeom: (id: string, geom: Geometry) => void;
  clearPendingGeom: (id: string) => void;
}

export function usePendingGeom(annotations: AnnotationResponse[] | undefined): PendingGeomApi {
  const [entries, setEntries] = useState<Map<string, PendingEntry>>(() => new Map());
  const entriesRef = useRef(entries);
  entriesRef.current = entries;

  useEffect(() => {
    if (entriesRef.current.size === 0) return;
    const byId = new Map((annotations ?? []).map((a) => [a.id, a] as const));
    let changed = false;
    const next = new Map(entriesRef.current);
    for (const [id, entry] of entriesRef.current) {
      const ann = byId.get(id);
      if (!ann) {
        next.delete(id);
        changed = true;
        continue;
      }
      if (geomEquals(ann.geometry as Geometry, entry.geom)) {
        next.delete(id);
        changed = true;
      }
    }
    if (changed) setEntries(next);
  }, [annotations]);

  useEffect(() => {
    if (entries.size === 0) return;
    const oldest = Math.min(...Array.from(entries.values(), (e) => e.ts));
    const delay = Math.max(50, MAX_AGE_MS - (Date.now() - oldest));
    const timer = setTimeout(() => {
      const now = Date.now();
      let changed = false;
      const next = new Map(entriesRef.current);
      for (const [id, entry] of entriesRef.current) {
        if (now - entry.ts >= MAX_AGE_MS) {
          next.delete(id);
          changed = true;
        }
      }
      if (changed) setEntries(next);
    }, delay);
    return () => clearTimeout(timer);
  }, [entries]);

  const markPendingGeom = useCallback((id: string, geom: Geometry) => {
    setEntries((prev) => {
      const next = new Map(prev);
      next.set(id, { geom, ts: Date.now() });
      return next;
    });
  }, []);

  const clearPendingGeom = useCallback((id: string) => {
    setEntries((prev) => {
      if (!prev.has(id)) return prev;
      const next = new Map(prev);
      next.delete(id);
      return next;
    });
  }, []);

  const pendingGeomMap = useMemo(() => {
    const m = new Map<string, Geometry>();
    for (const [id, entry] of entries) m.set(id, entry.geom);
    return m;
  }, [entries]);

  return { pendingGeomMap, markPendingGeom, clearPendingGeom };
}

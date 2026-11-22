import { useCallback, useRef, useState } from "react";
import { clamp01 } from "./videoStageGeometry";

export type PointsDraft = { closed: boolean; points: [number, number][] };

export function draftMinPoints(closed: boolean): number {
  return closed ? 3 : 2;
}

export function draftCanCommit(draft: PointsDraft | null): boolean {
  if (!draft) return false;
  return draft.points.length >= draftMinPoints(draft.closed);
}

export interface VideoPolygonDraft {
  draft: PointsDraft | null;
  addPoint: (pt: { x: number; y: number }, closed: boolean) => void;
  removeLastPoint: () => void;
  commit: () => [number, number][] | null;
  cancel: () => void;
}

export function useVideoPolygonDraft(): VideoPolygonDraft {
  const [draft, setDraft] = useState<PointsDraft | null>(null);
  const draftRef = useRef<PointsDraft | null>(draft);
  draftRef.current = draft;

  const addPoint = useCallback((pt: { x: number; y: number }, closed: boolean) => {
    const p: [number, number] = [clamp01(pt.x), clamp01(pt.y)];
    setDraft((cur) =>
      cur && cur.closed === closed
        ? { closed, points: [...cur.points, p] }
        : { closed, points: [p] },
    );
  }, []);

  const removeLastPoint = useCallback(() => {
    setDraft((cur) => {
      if (!cur) return null;
      const rest = cur.points.slice(0, -1);
      return rest.length > 0 ? { closed: cur.closed, points: rest } : null;
    });
  }, []);

  const commit = useCallback((): [number, number][] | null => {
    const cur = draftRef.current;
    const out = draftCanCommit(cur) ? cur!.points : null;
    setDraft(null);
    return out;
  }, []);

  const cancel = useCallback(() => setDraft(null), []);

  return { draft, addPoint, removeLastPoint, commit, cancel };
}

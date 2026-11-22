import { useCallback, useRef, useState, type Dispatch, type SetStateAction } from "react";
import type { Pt } from "../stage/polygonGeom";

export function usePolygonDraftPoints() {
  const [points, publish] = useState<Pt[]>([]);
  const current = useRef(points);
  const setPoints = useCallback<Dispatch<SetStateAction<Pt[]>>>((update) => {
    const next = typeof update === "function" ? update(current.current) : update;
    current.current = next;
    publish(next);
  }, []);
  const getPoints = useCallback(() => current.current, []);
  return { points, setPoints, getPoints };
}

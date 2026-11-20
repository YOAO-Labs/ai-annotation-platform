import { useCallback, useRef, useState } from "react";
import { clamp01 } from "./videoStageGeometry";

/**
 * 视频 polygon/polyline track 绘制草稿状态机 (点击累加顶点)。
 *
 * 与拖拽式 bbox 绘制 (useVideoKonvaInteraction) 正交: 本 hook 只管「多次点击落点 →
 * 满足最小顶点数后可闭合提交」的折线/多边形草稿。closed=true (polygon) 需 ≥3 点、
 * closed=false (polyline) 需 ≥2 点。坐标归一化 [0,1] (与 videoKonvaCoordinates 一致)。
 */

export type PointsDraft = { closed: boolean; points: [number, number][] };

/** 最小可提交顶点数: polygon(闭合) 3, polyline(开) 2。 */
export function draftMinPoints(closed: boolean): number {
  return closed ? 3 : 2;
}

export function draftCanCommit(draft: PointsDraft | null): boolean {
  if (!draft) return false;
  return draft.points.length >= draftMinPoints(draft.closed);
}

export interface VideoPolygonDraft {
  draft: PointsDraft | null;
  /** 起草 / 落点: 归一化点入草稿 (草稿类型由 closed 决定, 中途不变)。 */
  addPoint: (pt: { x: number; y: number }, closed: boolean) => void;
  /** 撤销最后一个落点 (Backspace); 撤空后草稿清为 null。 */
  removeLastPoint: () => void;
  /** 提交并清空: 顶点足够时返回 points, 否则 null (不提交)。 */
  commit: () => [number, number][] | null;
  /** 取消并清空。 */
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

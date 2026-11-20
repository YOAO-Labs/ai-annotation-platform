// 「提交在途」几何 override 桥 —— 防松手闪回原尺寸。
//
// 竞态背景：resize/move/polyVertex/kpNode/rotateBox 松手后, ImageStage 会同步
// `setDrag(null)` (React state) 并触发 `mutations.update.mutate(...)`; 但 tanstack-query
// v5 的 `onMutate` 挂在 `mutation.execute()` 的 `await` 之后, 在 microtask 才执行。
// 于是 React 提交本次 pointerup 更新时:drag=null 已生效, 但 annotations cache 还没被
// 乐观回填 → shape 用旧 b.geometry 画一帧 → 微任务落地后再落到新几何 → **一帧闪回**。
//
// 修法:各 handleCommit* 在 mutate 之前同步 mark 一个 pending 目标几何, ImageStage 的
// override 优先级里插到 drag 与 nudgeMap 之间。等到 annotations cache 反映了新几何
// (乐观 or 网络回) 时, effect 自动 clear; 兜底 800ms 超时防挂死。
//
// 为什么不是「mutate 前先手写缓存」:那样 `useUpdateAnnotation.onMutate` 里捕获的
// `prev` 就是**已改后**的状态, onError 回滚会滚到新值, 隐性 bug。此 hook 完全独立
// 于 mutation 库时序, 即使换 mutation 实现也不需要重接兜底。

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { AnnotationResponse, Geometry } from "@/types";

interface PendingEntry {
  geom: Geometry;
  ts: number;
}

// 兜底超时: 主收敛路径是 (a) annotations cache 命中新几何; (b) useUpdateAnnotation.onSettled
// 主动 clear。此超时仅在两路径均未触发时 (前端逻辑漏挂 / mutation 未走 useUpdateAnnotation)
// 防挂死。挑 10s 是让它显著长于典型 mutation 耗时 —— 慢网 mutation error 之前 pending 保住,
// 不会像原 800ms 让画面提前闪到 (即将被回滚的) 旧几何。
const MAX_AGE_MS = 10_000;

function geomEquals(a: Geometry | undefined, b: Geometry): boolean {
  if (!a) return false;
  if (a === (b as unknown)) return true;
  if (a.type !== b.type) return false;
  return JSON.stringify(a) === JSON.stringify(b);
}

export interface PendingGeomApi {
  /** id → 目标几何; 消费方(ImageStage)按 geometry.type 挑对应分量渲染。 */
  pendingGeomMap: Map<string, Geometry>;
  /** 同步登记一条提交在途 override; 在 `mutations.update.mutate()` 之前调。 */
  markPendingGeom: (id: string, geom: Geometry) => void;
  /** 显式清除(如撤销/删除路径可选调, 一般不需要)。 */
  clearPendingGeom: (id: string) => void;
}

/**
 * 需要拿到当前 annotations 数据以判断「已落地」的收敛条件。
 * 传 undefined 也可(此时仅靠 MAX_AGE_MS 超时兜底)。
 */
export function usePendingGeom(annotations: AnnotationResponse[] | undefined): PendingGeomApi {
  const [entries, setEntries] = useState<Map<string, PendingEntry>>(() => new Map());
  const entriesRef = useRef(entries);
  entriesRef.current = entries;

  // 收敛 1: annotations 变化 → 命中目标几何或标注消失即清。
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

  // 收敛 2: 网络失败/回滚场景 annotations 永远不会命中 → MAX_AGE_MS 超时兜底 drop,
  // 防 override 挂死盖住真实几何。
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

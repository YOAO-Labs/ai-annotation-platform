import { useCallback, useRef, useState } from "react";
import type { AnnotationUpdatePayload } from "@/api/tasks";

export type DirtyField = keyof AnnotationUpdatePayload;

export function useDirtyTracker() {
  const mapRef = useRef<Map<string, Set<DirtyField>>>(new Map());
  const listenersRef = useRef<Set<() => void>>(new Set());
  const [revision, setRevision] = useState(0);

  const notify = useCallback(() => {
    setRevision((n) => n + 1);
    listenersRef.current.forEach((l) => {
      try {
        l();
      } catch {
        /* listeners must not throw */
      }
    });
  }, []);

  const markDirty = useCallback(
    (id: string, field: DirtyField) => {
      let s = mapRef.current.get(id);
      if (!s) {
        s = new Set();
        mapRef.current.set(id, s);
      }
      if (s.has(field)) return;
      s.add(field);
      notify();
    },
    [notify],
  );

  const getDirtyFields = useCallback((id: string): DirtyField[] => {
    const s = mapRef.current.get(id);
    return s ? [...s] : [];
  }, []);

  const clear = useCallback(
    (id: string) => {
      if (!mapRef.current.delete(id)) return;
      notify();
    },
    [notify],
  );

  const clearAll = useCallback(() => {
    if (mapRef.current.size === 0) return;
    mapRef.current.clear();
    notify();
  }, [notify]);

  const subscribe = useCallback((listener: () => void) => {
    listenersRef.current.add(listener);
    return () => {
      listenersRef.current.delete(listener);
    };
  }, []);

  /**
   * 取出并清空指定 annotation 的脏字段。
   *
   * - 若 commit 抛出异常，dirty 不清空，下次 flush 重试；
   * - commit 返回 Promise / async 时，错误回滚也保留 dirty（外部 catch 重试）；
   * - 同一帧多次 flush 时第二次返回 [] 且不再调 commit。
   *
   * @returns 实际 flush 的字段列表（拷贝，外部可读）；空数组表示无 dirty。
   */
  const flush = useCallback(
    (id: string, commit?: (fields: DirtyField[]) => void | Promise<void>): DirtyField[] => {
      const s = mapRef.current.get(id);
      if (!s || s.size === 0) return [];
      const fields = [...s];
      mapRef.current.delete(id);
      notify();
      if (!commit) return fields;
      try {
        const ret = commit(fields);
        if (ret && typeof (ret as Promise<void>).then === "function") {
          (ret as Promise<void>).catch(() => {
            let again = mapRef.current.get(id);
            if (!again) {
              again = new Set();
              mapRef.current.set(id, again);
            }
            fields.forEach((f) => again!.add(f));
            notify();
          });
        }
      } catch {
        let again = mapRef.current.get(id);
        if (!again) {
          again = new Set();
          mapRef.current.set(id, again);
        }
        fields.forEach((f) => again!.add(f));
        notify();
      }
      return fields;
    },
    [notify],
  );

  return { markDirty, getDirtyFields, clear, clearAll, subscribe, flush, revision };
}

export type DirtyTracker = ReturnType<typeof useDirtyTracker>;

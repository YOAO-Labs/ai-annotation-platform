import { useCallback, useRef, useState } from "react";
import type { AnnotationUpdatePayload } from "@/api/tasks";

export type DirtyField = keyof AnnotationUpdatePayload;

/**
 * Per-annotation dirty-field tracker.
 *
 * - dirty bits 通过 markDirty / getDirtyFields / clear / subscribe 管理。
 * - AttributeForm 在批改 mutable 字段时累积 dirty bits，
 *   blur 或 pointerup 调用 {@link flush} 合并一次 PATCH（避免逐字段请求风暴）。
 *
 * API:
 *   markDirty(id, field)        标记某条 annotation 的某字段已脏
 *   getDirtyFields(id)          读取脏字段集合（拷贝，避免外部 mutate）
 *   clear(id)                   清空某条
 *   clearAll()                  清空所有
 *   subscribe(listener)         订阅变更，便于自动 flush
 *   flush(id, commit)           取出脏字段并清空：commit(fields) 由调用方组装 PATCH
 *                               返回值是脏字段列表；若为空表示无可 flush 项。
 */
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
      // 先清空再 commit；commit 失败时 catch 后重新 markDirty 回滚
      mapRef.current.delete(id);
      notify();
      if (!commit) return fields;
      try {
        const ret = commit(fields);
        if (ret && typeof (ret as Promise<void>).then === "function") {
          (ret as Promise<void>).catch(() => {
            // 失败回滚：把字段重新放进 dirty set
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

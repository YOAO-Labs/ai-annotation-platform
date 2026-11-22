import { useEffect, useMemo, useRef } from "react";
import { RasterMaskWorkerPool } from "./rasterMaskWorkerPool";
import type { RasterResourceCoordinator } from "./rasterResourceCoordinator";

export function useRasterMaskWorkerPool(
  taskId: string | null | undefined,
  resourceCoordinator?: RasterResourceCoordinator,
): RasterMaskWorkerPool | undefined {
  const pool = useMemo(
    () =>
      typeof Worker === "undefined" || !taskId
        ? undefined
        : new RasterMaskWorkerPool({ diagnosticsTaskId: taskId, resourceCoordinator }),
    [resourceCoordinator, taskId],
  );
  const activePoolRef = useRef<RasterMaskWorkerPool | undefined>(pool);

  useEffect(() => {
    activePoolRef.current = pool;
    return () => {
      if (activePoolRef.current === pool) activePoolRef.current = undefined;
      queueMicrotask(() => {
        if (pool && activePoolRef.current !== pool) pool.dispose();
      });
    };
  }, [pool]);

  return pool;
}

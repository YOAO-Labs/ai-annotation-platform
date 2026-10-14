import { useEffect, useMemo, useRef, useState } from "react";
import type { Viewport } from "../state/useViewportTransform";
import { imageTileDeviceBudget, visibleImageRect, type WorkbenchImageSource } from "./imagePyramid";
import { ImageTileScheduler, type ImageTileResourceSnapshot } from "./imageTileScheduler";
import {
  clearImageTileDiagnostics,
  publishImageTileDiagnostics,
} from "@/utils/imageTileDiagnostics";
import type { RasterResourceCoordinator } from "./shared/rasterResourceCoordinator";

interface UseImageTileSchedulerOptions {
  source: WorkbenchImageSource | null;
  viewport: Viewport;
  viewportSize: { w: number; h: number };
  pausePrefetch?: boolean;
  deviceMemory?: number | null;
  devicePixelRatio?: number;
  resourceCoordinator?: RasterResourceCoordinator;
}

interface ImageTileSchedulerState {
  scheduler: ImageTileScheduler | null;
  revision: number;
  snapshot: ImageTileResourceSnapshot | null;
}

interface ImageTileSchedulerHandle {
  sourceIdentity: string;
  scheduler: ImageTileScheduler;
}

function navigatorDeviceMemory(): number | null {
  if (typeof navigator === "undefined") return null;
  const value = "deviceMemory" in navigator ? navigator.deviceMemory : undefined;
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function useImageTileScheduler(
  options: UseImageTileSchedulerOptions,
): ImageTileSchedulerState {
  const schedulerRef = useRef<ImageTileSchedulerHandle | null>(null);
  const [revision, setRevision] = useState(0);
  const viewport = options.viewport;
  const [runtimeDpr, setRuntimeDpr] = useState(() =>
    typeof window === "undefined" ? 1 : Math.max(1, window.devicePixelRatio || 1),
  );
  const source = options.source?.kind === "pyramid" ? options.source : null;
  const budget = useMemo(
    () =>
      imageTileDeviceBudget(
        options.deviceMemory === undefined ? navigatorDeviceMemory() : options.deviceMemory,
      ),
    [options.deviceMemory],
  );

  useEffect(() => {
    if (options.devicePixelRatio !== undefined || typeof window === "undefined") return;
    const update = () => setRuntimeDpr(Math.max(1, window.devicePixelRatio || 1));
    window.addEventListener("resize", update);
    return () => window.removeEventListener("resize", update);
  }, [options.devicePixelRatio]);

  useEffect(() => {
    if (!source) {
      schedulerRef.current = null;
      setRevision((value) => value + 1);
      return;
    }
    const scheduler = new ImageTileScheduler({
      taskId: source.taskId,
      sourceIdentity: source.identity,
      generation: source.generation,
      manifest: source.manifest,
      budget,
      ...(options.resourceCoordinator
        ? { resourceCoordinator: options.resourceCoordinator, resourceOwner: "background" }
        : {}),
    });
    schedulerRef.current = { sourceIdentity: source.identity, scheduler };
    const unsubscribe = scheduler.subscribe(() => setRevision((value) => value + 1));
    setRevision((value) => value + 1);
    return () => {
      unsubscribe();
      scheduler.dispose();
      clearImageTileDiagnostics(source.generation);
      if (schedulerRef.current?.scheduler === scheduler) schedulerRef.current = null;
    };
    // A pyramid identity is immutable. Presigned overview URL refreshes must not
    // recreate the scheduler or discard already-decoded tiles.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    budget.concurrency,
    budget.overscanTiles,
    budget.retainedBytes,
    options.resourceCoordinator,
    source?.generation,
    source?.identity,
  ]);

  useEffect(() => {
    const handle = schedulerRef.current;
    if (!handle || !source || handle.sourceIdentity !== source.identity) return;
    const scheduler = handle.scheduler;
    scheduler.setPrefetchPaused(options.pausePrefetch ?? false);
    const rect = visibleImageRect(
      { width: options.viewportSize.w, height: options.viewportSize.h },
      { scale: viewport.scale, tx: viewport.tx, ty: viewport.ty },
      { width: source.manifest.width, height: source.manifest.height },
    );
    const dpr = options.devicePixelRatio ?? runtimeDpr;
    scheduler.update(rect, viewport.scale, dpr);
  }, [
    options.devicePixelRatio,
    options.pausePrefetch,
    viewport.scale,
    viewport.tx,
    viewport.ty,
    options.viewportSize.h,
    options.viewportSize.w,
    runtimeDpr,
    source,
  ]);

  const state = useMemo(() => {
    const handle = schedulerRef.current;
    const scheduler =
      source && handle?.sourceIdentity === source.identity ? handle.scheduler : null;
    return {
      scheduler,
      revision,
      snapshot: scheduler?.getSnapshot() ?? null,
    };
  }, [revision, source]);
  useEffect(() => {
    if (state.snapshot) publishImageTileDiagnostics(state.snapshot);
  }, [state.snapshot]);
  return state;
}

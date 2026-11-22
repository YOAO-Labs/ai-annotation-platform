import { useCallback, useEffect, useRef, useState } from "react";
import { MaskBuffer, type MaskBrushShape } from "../stage/shared/geometry/maskBuffer";
import { maskToPolygon } from "../stage/shared/geometry/maskToPolygon";
import {
  MAX_DENSE_MASK_PIXELS,
  MAX_VIDEO_MASK_DIMENSION,
  type CocoRle,
} from "../stage/shared/geometry/maskRle";
import type {
  MaskConnectivity,
  MaskOperationReport,
  MaskOperationResult,
  MaskOperationSpec,
} from "../stage/shared/geometry/maskOperations";
import { applyMaskOperation } from "../stage/shared/geometry/maskOperations";
import {
  applyMaskInstanceOperation,
  type MaskInstanceOperationPlan,
  type MaskInstanceOperationSpec,
} from "../stage/shared/geometry/maskInstanceOperations";
import {
  executeRasterMaskInstanceOperationAsync,
  executeRasterMaskOperationAsync,
  RasterMaskWorkerCancelledError,
} from "../stage/shared/rasterMaskCompute";
import type { RasterMaskWorkerPool } from "../stage/shared/rasterMaskWorkerPool";
import type {
  RasterResourceCoordinator,
  RasterResourceReservation,
} from "../stage/shared/rasterResourceCoordinator";
import {
  createDenseMaskHistoryCommand,
  MaskHistoryCheckpoint,
  MaskHistoryStore,
  MASK_HISTORY_TILE_SIZE,
  maskHistoryBudgetBytes,
  navigatorMaskHistoryBudgetBytes,
  type MaskHistoryCommand,
  type MaskHistoryResources,
} from "../stage/shared/maskHistory";
import type { MaskEditorPhase } from "./canEditMask";
import {
  LargeMaskFullScanRequiredError,
  sparseMaskCpuComputeBudgetBytes,
  sparseMaskGpuBufferBudgetBytes,
  SparseMaskTileStore,
  type SparseMaskRenderableTile,
  type SparseMaskTileResources,
  type SparseMaskViewportRect,
} from "../stage/shared/sparseMaskTileStore";

export type MaskMode = "brush" | "erase";
export type MaskEditorTool =
  | "slice_mask"
  | MaskMode
  | "lasso_add"
  | "lasso_subtract"
  | "fill_add"
  | "fill_subtract"
  | "component_keep"
  | "component_delete"
  | "component_copy"
  | "hole_fill";

export const MASK_BRUSH_MIN_PX = 1;
export const MASK_BRUSH_MAX_PX = 200;
export const MASK_BRUSH_DEFAULT_PX = 16;
const MASK_RESOURCE_BUDGET_MESSAGE = "Mask 资源不足，请缩小可见区域或保存后重试";

export interface MaskOperationPreview {
  id: number;
  name: string;
  sourceRevision: number;
  alpha: Uint8Array;
  report: MaskOperationReport;
}

export interface MaskInstanceOperationPreview {
  id: number;
  name: string;
  sourceRevision: number;
  plan: MaskInstanceOperationPlan;
}

export type MaskOperationStatus = "idle" | "computing" | "preview" | "error";
export type MaskEditorBackend = "dense" | "tiled";

export interface UseMaskEditorOptions {
  width: number;
  height: number;
  initialRadius?: number;
  workerPool?: RasterMaskWorkerPool;
  deviceMemory?: number | null;
  historyMaxBytes?: number;
  tileMaxBytes?: number;
  resourceCoordinator?: RasterResourceCoordinator;
}

export interface UseMaskEditorReturn {
  phase?: MaskEditorPhase;
  active: boolean;
  mode: MaskMode;
  tool: MaskEditorTool;
  brushShape: MaskBrushShape;
  connectivity: MaskConnectivity;
  radius: number;
  dirty: boolean;
  buffer: MaskBuffer | null;
  backend: MaskEditorBackend;
  tiledTiles: readonly SparseMaskRenderableTile[];
  tiledResources: SparseMaskTileResources | null;
  tiledReadOnly: boolean;
  commitInFlight: boolean;
  revision: number;
  canUndo: boolean;
  canRedo: boolean;
  historyResources: MaskHistoryResources;
  operationPreview: MaskOperationPreview | null;
  instanceOperationPreview: MaskInstanceOperationPreview | null;
  operationStatus: MaskOperationStatus;
  operationError: unknown;
  beginBlank: () => void;
  initFromPolygon: (points: ReadonlyArray<readonly [number, number]>) => void;
  initFromRle: (rle: CocoRle) => void;
  materializeFromRle: (rle: CocoRle) => void;
  paintAt: (x: number, y: number) => void;
  beginStroke: () => void;
  endStroke: () => void;
  undo: () => void;
  redo: () => void;
  setMode: (mode: MaskMode) => void;
  setTool: (tool: MaskEditorTool) => void;
  setBrushShape: (shape: MaskBrushShape) => void;
  setConnectivity: (connectivity: MaskConnectivity) => void;
  setRadius: (radius: number) => void;
  previewOperation: (name: string, result: MaskOperationResult, sourceRevision?: number) => boolean;
  runOperation: (
    name: string,
    operation: MaskOperationSpec,
    context?: { sessionId: string; generation: number },
  ) => Promise<boolean>;
  previewInstanceOperation: (
    name: string,
    plan: MaskInstanceOperationPlan,
    sourceRevision?: number,
  ) => boolean;
  runInstanceOperation: (
    name: string,
    operation: MaskInstanceOperationSpec,
    context?: { sessionId: string; generation: number },
  ) => Promise<boolean>;
  confirmOperation: () => boolean;
  cancelOperation: () => void;
  cancel: () => void;
  commitToPolygon: () => {
    points: [number, number][];
    multipleComponents: boolean;
    lossy: boolean;
    droppedComponents?: number;
    droppedHoles?: number;
    lossyReason?: string;
  } | null;
  commitToRle: () => CocoRle | null;
  commitToRleAsync: () => Promise<CocoRle | null>;
  setViewport: (rect: SparseMaskViewportRect | null) => void;
}

let sparseEditorSessionSequence = 0;

function clampRadius(radius: number): number {
  if (!Number.isFinite(radius)) return MASK_BRUSH_DEFAULT_PX;
  return Math.max(MASK_BRUSH_MIN_PX, Math.min(MASK_BRUSH_MAX_PX, Math.round(radius)));
}

function brushBounds(x: number, y: number, radius: number, width: number, height: number) {
  const effectiveRadius = Math.max(0.5, radius);
  const x0 = Math.max(0, Math.floor(x - effectiveRadius));
  const y0 = Math.max(0, Math.floor(y - effectiveRadius));
  const x1 = Math.min(width - 1, Math.ceil(x + effectiveRadius));
  const y1 = Math.min(height - 1, Math.ceil(y + effectiveRadius));
  return { x0, y0, x1: x1 + 1, y1: y1 + 1 };
}

function applyDenseHistoryBits(buffer: MaskBuffer, command: MaskHistoryCommand): void {
  for (const patch of command.patches) {
    buffer.applyXorBits(
      patch.tileX * MASK_HISTORY_TILE_SIZE,
      patch.tileY * MASK_HISTORY_TILE_SIZE,
      patch.width,
      patch.height,
      patch.xorBits,
    );
  }
}

export function useMaskEditor({
  width,
  height,
  initialRadius = MASK_BRUSH_DEFAULT_PX,
  workerPool,
  deviceMemory,
  historyMaxBytes,
  tileMaxBytes,
  resourceCoordinator,
}: UseMaskEditorOptions): UseMaskEditorReturn {
  const webGpuCandidateEnabled = import.meta.env.VITE_EXPERIMENTAL_RASTER_MASK_WEBGPU !== "false";
  const e2eTileMaxBytes =
    (import.meta.env.DEV || import.meta.env.MODE === "e2e") && typeof window !== "undefined"
      ? (window as typeof window & { __E2E_MASK_TILE_MAX_BYTES__?: number })
          .__E2E_MASK_TILE_MAX_BYTES__
      : undefined;
  const resolvedTileMaxBytes = tileMaxBytes ?? e2eTileMaxBytes;
  const resolvedHistoryMaxBytes =
    historyMaxBytes ??
    (deviceMemory === undefined
      ? navigatorMaskHistoryBudgetBytes()
      : maskHistoryBudgetBytes(deviceMemory));
  const bufferRef = useRef<MaskBuffer | null>(null);
  const tiledStoreRef = useRef<SparseMaskTileStore | null>(null);
  const tiledQueueRef = useRef<Promise<void>>(Promise.resolve());
  const tiledCommitPromiseRef = useRef<Promise<CocoRle | null> | null>(null);
  const viewportRef = useRef<SparseMaskViewportRect | null>(null);
  const viewportGenerationRef = useRef(0);
  const strokeCheckpointRef = useRef<{
    sourceRevision: number;
    checkpoint: MaskHistoryCheckpoint;
    wasDirty: boolean;
  } | null>(null);
  const denseBufferResourceRef = useRef<RasterResourceReservation | null>(null);
  const previewResourceRef = useRef<RasterResourceReservation | null>(null);
  const historyResourceReservationsRef = useRef(
    new WeakMap<MaskHistoryCommand, RasterResourceReservation>(),
  );
  const [historyRef] = useState(() => ({
    current: new MaskHistoryStore(resolvedHistoryMaxBytes),
  }));
  const operationPreviewRef = useRef<MaskOperationPreview | null>(null);
  const instanceOperationPreviewRef = useRef<MaskInstanceOperationPreview | null>(null);
  const operationIdRef = useRef(0);
  const operationAbortRef = useRef<AbortController | null>(null);
  const revisionRef = useRef(0);
  const [active, setActive] = useState(false);
  const [backend, setBackend] = useState<MaskEditorBackend>("dense");
  const [tiledTiles, setTiledTiles] = useState<readonly SparseMaskRenderableTile[]>([]);
  const [tiledResources, setTiledResources] = useState<SparseMaskTileResources | null>(null);
  const [tiledReadOnly, setTiledReadOnly] = useState(false);
  const [commitInFlight, setCommitInFlight] = useState(false);
  const [mode, setModeState] = useState<MaskMode>("brush");
  const [tool, setToolState] = useState<MaskEditorTool>("brush");
  const [brushShape, setBrushShape] = useState<MaskBrushShape>("circle");
  const [connectivity, setConnectivity] = useState<MaskConnectivity>(4);
  const [radius, setRadiusState] = useState(clampRadius(initialRadius));
  const [dirty, setDirty] = useState(false);
  const [revision, setRevision] = useState(0);
  const [historyRevision, setHistoryRevision] = useState(0);
  const [operationPreview, setOperationPreview] = useState<MaskOperationPreview | null>(null);
  const [instanceOperationPreview, setInstanceOperationPreview] =
    useState<MaskInstanceOperationPreview | null>(null);
  const [operationStatus, setOperationStatus] = useState<MaskOperationStatus>("idle");
  const [operationError, setOperationError] = useState<unknown>(undefined);
  void historyRevision;

  const shouldUseTiled =
    width > MAX_VIDEO_MASK_DIMENSION ||
    height > MAX_VIDEO_MASK_DIMENSION ||
    width * height > MAX_DENSE_MASK_PIXELS;

  const bump = useCallback(() => {
    revisionRef.current += 1;
    setRevision(revisionRef.current);
  }, []);

  const clearOperationPreview = useCallback(() => {
    previewResourceRef.current?.release();
    previewResourceRef.current = null;
    operationPreviewRef.current = null;
    instanceOperationPreviewRef.current = null;
    setOperationPreview(null);
    setInstanceOperationPreview(null);
    setOperationStatus("idle");
  }, []);

  const cancelActiveOperation = useCallback(() => {
    operationAbortRef.current?.abort();
    operationAbortRef.current = null;
    setOperationError(undefined);
    clearOperationPreview();
  }, [clearOperationPreview]);

  const resetHistory = useCallback(
    (store: SparseMaskTileStore | null = null) => {
      historyRef.current.clear();
      historyRef.current = new MaskHistoryStore(resolvedHistoryMaxBytes, undefined, {
        onRetain: (command) => {
          const resource = resourceCoordinator?.tryReserve({
            owner: "mask-history",
            category: "mask-history",
            priority: 0,
            bytes: command.chargedBytes,
            reconstructible: false,
            pinned: true,
          });
          if (resourceCoordinator && !resource) return false;
          if (resource && !resource.commit(command.chargedBytes)) {
            resource.release();
            return false;
          }
          try {
            store?.retainHistoryCommand(command);
          } catch (error) {
            resource?.release();
            throw error;
          }
          if (resource) historyResourceReservationsRef.current.set(command, resource);
          return true;
        },
        onRelease: (command) => {
          store?.releaseHistoryCommand(command);
          historyResourceReservationsRef.current.get(command)?.release();
          historyResourceReservationsRef.current.delete(command);
        },
      });
      strokeCheckpointRef.current = null;
      setHistoryRevision((value) => value + 1);
    },
    [historyRef, resolvedHistoryMaxBytes, resourceCoordinator],
  );

  const refreshTiledState = useCallback((store: SparseMaskTileStore) => {
    if (tiledStoreRef.current !== store) return;
    setTiledTiles(store.getRenderableTiles());
    const resources = store.snapshot();
    setTiledResources(resources);
    setTiledReadOnly(resources.admissionBlocked);
  }, []);

  const disposeTiledStore = useCallback(() => {
    const current = tiledStoreRef.current;
    if (!current) return;
    resetHistory();
    tiledStoreRef.current = null;
    current.dispose();
    tiledQueueRef.current = Promise.resolve();
    tiledCommitPromiseRef.current = null;
    viewportRef.current = null;
    viewportGenerationRef.current += 1;
    setTiledTiles([]);
    setTiledResources(null);
    setTiledReadOnly(false);
    setCommitInFlight(false);
  }, [resetHistory]);

  const enqueueTiledAction = useCallback(
    (store: SparseMaskTileStore, action: () => void | Promise<void>): Promise<boolean> => {
      let succeeded = false;
      const next = tiledQueueRef.current
        .then(async () => {
          if (tiledStoreRef.current !== store) return;
          await action();
          if (tiledStoreRef.current !== store) return;
          succeeded = true;
          refreshTiledState(store);
          bump();
        })
        .catch((error: unknown) => {
          if (tiledStoreRef.current !== store) return;
          if (error instanceof RasterMaskWorkerCancelledError) {
            setOperationStatus("idle");
            return;
          }
          setOperationError(error);
          setOperationStatus("error");
          refreshTiledState(store);
        });
      tiledQueueRef.current = next;
      return next.then(() => succeeded);
    },
    [bump, refreshTiledState],
  );

  const validateRleSize = useCallback(
    (rle: CocoRle) => {
      const [rleHeight, rleWidth] = rle.size;
      if (rleWidth !== width || rleHeight !== height) {
        throw new Error(
          `mask RLE size ${rleWidth}x${rleHeight} does not match editor ${width}x${height}`,
        );
      }
    },
    [height, width],
  );

  const installBuffer = useCallback(
    (buffer: MaskBuffer, isDirty: boolean) => {
      const existingResource = denseBufferResourceRef.current;
      if (existingResource) {
        if (!existingResource.update({ bytes: buffer.data.byteLength })) {
          throw new Error(MASK_RESOURCE_BUDGET_MESSAGE);
        }
      } else if (resourceCoordinator) {
        const resource = resourceCoordinator.tryReserve({
          owner: "mask-edit:dense",
          category: "mask-edit",
          priority: 0,
          bytes: buffer.data.byteLength,
          reconstructible: false,
          pinned: true,
        });
        if (!resource || !resource.commit(buffer.data.byteLength)) {
          resource?.release();
          throw new Error(MASK_RESOURCE_BUDGET_MESSAGE);
        }
        denseBufferResourceRef.current = resource;
      }
      disposeTiledStore();
      bufferRef.current = buffer;
      setBackend("dense");
      setActive(true);
      setDirty(isDirty);
      resetHistory();
      cancelActiveOperation();
      bump();
    },
    [bump, cancelActiveOperation, disposeTiledStore, resetHistory, resourceCoordinator],
  );

  const installTiledStore = useCallback(
    (rle: CocoRle, isDirty: boolean) => {
      if (!workerPool) {
        throw new LargeMaskFullScanRequiredError(
          "large Mask editing requires the Raster Mask Worker pool",
        );
      }
      const sequence = ++sparseEditorSessionSequence;
      const store = new SparseMaskTileStore({
        sessionId: `mask-editor-${sequence}`,
        sha256: sequence.toString(16).padStart(64, "0"),
        baseRle: rle,
        backend: workerPool,
        morphologyBackendPolicy: webGpuCandidateEnabled ? "webgpu-candidate" : "cpu",
        ...(deviceMemory === undefined
          ? webGpuCandidateEnabled
            ? {}
            : { gpuBufferBudgetBytes: 0 }
          : {
              cpuComputeBudgetBytes: sparseMaskCpuComputeBudgetBytes(deviceMemory),
              gpuBufferBudgetBytes: webGpuCandidateEnabled
                ? sparseMaskGpuBufferBudgetBytes(deviceMemory)
                : 0,
            }),
        ...(deviceMemory === undefined ? {} : { deviceMemory }),
        ...(resolvedTileMaxBytes === undefined ? {} : { maxCacheBytes: resolvedTileMaxBytes }),
        resourceCoordinator,
      });
      disposeTiledStore();
      bufferRef.current = null;
      denseBufferResourceRef.current?.release();
      denseBufferResourceRef.current = null;
      tiledStoreRef.current = store;
      resetHistory(store);
      tiledQueueRef.current = Promise.resolve();
      tiledCommitPromiseRef.current = null;
      setBackend("tiled");
      setActive(true);
      setDirty(isDirty);
      setTiledTiles([]);
      setTiledResources(store.snapshot());
      setTiledReadOnly(false);
      cancelActiveOperation();
      bump();
    },
    [
      bump,
      cancelActiveOperation,
      deviceMemory,
      disposeTiledStore,
      resetHistory,
      resourceCoordinator,
      resolvedTileMaxBytes,
      webGpuCandidateEnabled,
      workerPool,
    ],
  );

  const beginBlank = useCallback(() => {
    if (shouldUseTiled) {
      installTiledStore(
        {
          encoding: "coco_rle",
          size: [height, width],
          counts: [height * width],
        },
        false,
      );
      return;
    }
    installBuffer(new MaskBuffer({ width, height }), false);
  }, [height, installBuffer, installTiledStore, shouldUseTiled, width]);

  const initFromPolygon = useCallback(
    (points: ReadonlyArray<readonly [number, number]>) => {
      if (shouldUseTiled) {
        throw new LargeMaskFullScanRequiredError(
          "large image polygon refinement requires an explicit bounded ROI",
        );
      }
      const buffer = new MaskBuffer({ width, height });
      buffer.fromPolygon(points);
      installBuffer(buffer, false);
    },
    [height, installBuffer, shouldUseTiled, width],
  );

  const initFromRle = useCallback(
    (rle: CocoRle) => {
      validateRleSize(rle);
      if (shouldUseTiled) {
        installTiledStore(rle, false);
        return;
      }
      installBuffer(MaskBuffer.fromRle(rle), false);
    },
    [installBuffer, installTiledStore, shouldUseTiled, validateRleSize],
  );

  const materializeFromRle = useCallback(
    (rle: CocoRle) => {
      validateRleSize(rle);
      if (shouldUseTiled) {
        installTiledStore(rle, true);
        return;
      }
      installBuffer(MaskBuffer.fromRle(rle), true);
    },
    [installBuffer, installTiledStore, shouldUseTiled, validateRleSize],
  );

  const setRadius = useCallback((nextRadius: number) => {
    setRadiusState(clampRadius(nextRadius));
  }, []);

  const setMode = useCallback(
    (nextMode: MaskMode) => {
      setModeState(nextMode);
      setToolState(nextMode);
      cancelActiveOperation();
    },
    [cancelActiveOperation],
  );

  const setTool = useCallback(
    (nextTool: MaskEditorTool) => {
      setToolState(nextTool);
      if (nextTool === "brush" || nextTool === "erase") setModeState(nextTool);
      cancelActiveOperation();
    },
    [cancelActiveOperation],
  );

  const paintAt = useCallback(
    (x: number, y: number) => {
      const tiledStore = tiledStoreRef.current;
      if (tiledStore) {
        if (
          operationPreviewRef.current ||
          instanceOperationPreviewRef.current ||
          (tool !== "brush" && tool !== "erase")
        )
          return;
        const checkpoint = strokeCheckpointRef.current?.checkpoint;
        void enqueueTiledAction(tiledStore, async () => {
          const changed = await tiledStore.brush({
            cx: x,
            cy: y,
            radius,
            value: mode === "erase" ? 0 : 255,
            shape: brushShape,
            ...(checkpoint ? { checkpoint } : {}),
          });
          if (changed > 0) setDirty(true);
        });
        return;
      }
      const buffer = bufferRef.current;
      if (
        !buffer ||
        operationPreviewRef.current ||
        instanceOperationPreviewRef.current ||
        (tool !== "brush" && tool !== "erase")
      )
        return;
      strokeCheckpointRef.current?.checkpoint.captureDenseRect(
        buffer.data,
        brushBounds(x, y, radius, width, height),
      );
      if (mode === "erase") buffer.erase(x, y, radius, brushShape);
      else buffer.brush(x, y, radius, 255, brushShape);
      setDirty(true);
      bump();
    },
    [brushShape, bump, enqueueTiledAction, height, mode, radius, tool, width],
  );

  const beginStroke = useCallback(() => {
    if (
      (!bufferRef.current && !tiledStoreRef.current) ||
      strokeCheckpointRef.current ||
      operationPreviewRef.current ||
      instanceOperationPreviewRef.current ||
      (tool !== "brush" && tool !== "erase")
    )
      return;
    strokeCheckpointRef.current = {
      sourceRevision: revisionRef.current,
      checkpoint: new MaskHistoryCheckpoint(width, height),
      wasDirty: dirty,
    };
  }, [dirty, height, tool, width]);

  const endStroke = useCallback(() => {
    const stroke = strokeCheckpointRef.current;
    const tiledStore = tiledStoreRef.current;
    if (stroke && tiledStore) {
      strokeCheckpointRef.current = null;
      void enqueueTiledAction(tiledStore, () => {
        const command = tiledStore.finishHistoryCheckpoint(
          stroke.checkpoint,
          "stroke",
          stroke.sourceRevision,
        );
        if (!command) return;
        if (!historyRef.current.push(command)) {
          tiledStore.applyHistoryCommand(command);
          setDirty(stroke.wasDirty || tiledStore.snapshot().dirtyTiles > 0);
          throw new Error(`${MASK_RESOURCE_BUDGET_MESSAGE}；已撤销本次笔划`);
        }
        setHistoryRevision((value) => value + 1);
      });
      return;
    }
    const current = bufferRef.current;
    strokeCheckpointRef.current = null;
    if (!stroke || !current) return;
    const command = stroke.checkpoint.finishDense("stroke", stroke.sourceRevision, current.data);
    if (!command) return;
    if (!historyRef.current.push(command)) {
      applyDenseHistoryBits(current, command);
      setDirty(stroke.wasDirty);
      setOperationError(new Error(`${MASK_RESOURCE_BUDGET_MESSAGE}；已撤销本次笔划`));
      setOperationStatus("error");
      bump();
      return;
    }
    setHistoryRevision((value) => value + 1);
  }, [bump, enqueueTiledAction, historyRef]);

  const applyHistoryCommand = useCallback(
    (command: MaskHistoryCommand) => {
      const tiledStore = tiledStoreRef.current;
      if (tiledStore) {
        tiledStore.applyHistoryCommand(command);
        cancelActiveOperation();
        setDirty(true);
        return;
      }
      const current = bufferRef.current;
      if (!current) return;
      applyDenseHistoryBits(current, command);
      cancelActiveOperation();
      setDirty(true);
      bump();
    },
    [bump, cancelActiveOperation],
  );

  const undo = useCallback(() => {
    if (operationPreviewRef.current || instanceOperationPreviewRef.current) {
      clearOperationPreview();
      return;
    }
    const tiledStore = tiledStoreRef.current;
    if (tiledStore) {
      void enqueueTiledAction(tiledStore, () => {
        const command = historyRef.current.undo(applyHistoryCommand);
        if (!command) return;
        setHistoryRevision((value) => value + 1);
      });
      return;
    }
    if (!bufferRef.current) return;
    const command = historyRef.current.undo(applyHistoryCommand);
    if (!command) return;
    setHistoryRevision((value) => value + 1);
  }, [applyHistoryCommand, clearOperationPreview, enqueueTiledAction, historyRef]);

  const redo = useCallback(() => {
    const tiledStore = tiledStoreRef.current;
    if (tiledStore) {
      void enqueueTiledAction(tiledStore, () => {
        const command = historyRef.current.redo(applyHistoryCommand);
        if (!command) return;
        setHistoryRevision((value) => value + 1);
      });
      return;
    }
    if (!bufferRef.current) return;
    const command = historyRef.current.redo(applyHistoryCommand);
    if (!command) return;
    setHistoryRevision((value) => value + 1);
  }, [applyHistoryCommand, enqueueTiledAction, historyRef]);

  const previewOperation = useCallback(
    (name: string, result: MaskOperationResult, sourceRevision = revisionRef.current): boolean => {
      const current = bufferRef.current;
      if (!current || sourceRevision !== revisionRef.current) return false;
      if (result.alpha.length !== current.data.length) {
        throw new Error("mask operation preview dimensions do not match the editor buffer");
      }
      previewResourceRef.current?.release();
      previewResourceRef.current = null;
      if (resourceCoordinator) {
        const resource = resourceCoordinator.tryReserve({
          owner: "mask-compare",
          category: "mask-compare",
          priority: 1,
          bytes: result.alpha.byteLength,
          reconstructible: false,
          pinned: true,
        });
        if (!resource || !resource.commit(result.alpha.byteLength)) {
          resource?.release();
          setOperationError(new Error(MASK_RESOURCE_BUDGET_MESSAGE));
          setOperationStatus("error");
          return false;
        }
        previewResourceRef.current = resource;
      }
      operationIdRef.current += 1;
      const preview: MaskOperationPreview = {
        id: operationIdRef.current,
        name,
        sourceRevision,
        alpha: result.alpha,
        report: result.report,
      };
      operationPreviewRef.current = preview;
      setOperationPreview(preview);
      setOperationError(undefined);
      setOperationStatus("preview");
      return true;
    },
    [resourceCoordinator],
  );

  const runOperation = useCallback(
    async (
      name: string,
      operation: MaskOperationSpec,
      context: { sessionId: string; generation: number } = { sessionId: "local", generation: 0 },
    ): Promise<boolean> => {
      const tiledStore = tiledStoreRef.current;
      if (tiledStore) {
        cancelActiveOperation();
        const sourceRevision = revisionRef.current;
        const controller = new AbortController();
        operationAbortRef.current = controller;
        setOperationStatus("computing");
        setOperationError(undefined);
        let changedPixels = 0;
        const endForeground = resourceCoordinator?.beginForegroundOperation();
        let succeeded = false;
        try {
          succeeded = await enqueueTiledAction(tiledStore, async () => {
            let command: MaskHistoryCommand | null = null;
            if (operation.type === "polygon") {
              const checkpoint = tiledStore.beginHistoryCheckpoint();
              changedPixels = await tiledStore.lasso(operation.points, operation.value, {
                checkpoint,
                signal: controller.signal,
              });
              command = tiledStore.finishHistoryCheckpoint(checkpoint, name, sourceRevision);
            } else if (operation.type === "morphology" && viewportRef.current) {
              command = await tiledStore.morphologyRoi(viewportRef.current, operation, {
                name,
                sourceRevision,
                signal: controller.signal,
              });
              changedPixels = command?.changedPixels ?? 0;
            } else {
              throw new LargeMaskFullScanRequiredError();
            }
            if (!command) return;
            if (!historyRef.current.push(command)) {
              tiledStore.applyHistoryCommand(command);
              changedPixels = 0;
              throw new Error(`${MASK_RESOURCE_BUDGET_MESSAGE}；已撤销本次操作`);
            }
            setDirty(true);
            setHistoryRevision((value) => value + 1);
          });
        } finally {
          endForeground?.();
        }
        if (operationAbortRef.current === controller) operationAbortRef.current = null;
        if (succeeded) setOperationStatus("idle");
        return succeeded && changedPixels > 0;
      }
      const current = bufferRef.current;
      if (!current) return false;
      cancelActiveOperation();
      const sourceRevision = revisionRef.current;
      const rle = current.toRle();
      operationIdRef.current += 1;
      const operationId = operationIdRef.current;
      const controller = new AbortController();
      operationAbortRef.current = controller;
      setOperationStatus("computing");
      setOperationError(undefined);
      const endForeground = resourceCoordinator?.beginForegroundOperation();
      try {
        const shouldUseWorker =
          width * height > 1_000_000 ||
          ((operation.type === "morphology" || operation.type === "smooth") &&
            operation.radius > 4);
        const result = shouldUseWorker
          ? (
              await executeRasterMaskOperationAsync(
                rle,
                operation,
                { ...context, operationId },
                {
                  ...(workerPool ? { pool: workerPool } : {}),
                  priority: "editing",
                  signal: controller.signal,
                },
              )
            ).result
          : applyMaskOperation(current.data, width, height, operation);
        if (controller.signal.aborted || operationIdRef.current !== operationId) return false;
        operationAbortRef.current = null;
        const accepted = previewOperation(name, result, sourceRevision);
        if (!accepted) setOperationStatus("idle");
        return accepted;
      } catch (error) {
        if (operationAbortRef.current === controller) operationAbortRef.current = null;
        const isCurrent = operationIdRef.current === operationId;
        if (error instanceof RasterMaskWorkerCancelledError || controller.signal.aborted) {
          if (isCurrent) setOperationStatus("idle");
          return false;
        }
        if (!isCurrent) return false;
        setOperationError(error);
        setOperationStatus("error");
        return false;
      } finally {
        endForeground?.();
      }
    },
    [
      cancelActiveOperation,
      enqueueTiledAction,
      height,
      historyRef,
      previewOperation,
      resourceCoordinator,
      width,
      workerPool,
    ],
  );

  const previewInstanceOperation = useCallback(
    (
      name: string,
      plan: MaskInstanceOperationPlan,
      sourceRevision = revisionRef.current,
    ): boolean => {
      const current = bufferRef.current;
      if (!current || sourceRevision !== revisionRef.current) return false;
      const allAlphas = [plan.primary, plan.focusAlpha, ...plan.created];
      if (allAlphas.some((alpha) => alpha.length !== current.data.length)) {
        throw new Error("mask instance preview dimensions do not match the editor buffer");
      }
      const previewBytes = allAlphas.reduce((total, alpha) => total + alpha.byteLength, 0);
      previewResourceRef.current?.release();
      previewResourceRef.current = null;
      if (resourceCoordinator) {
        const resource = resourceCoordinator.tryReserve({
          owner: "mask-compare",
          category: "mask-compare",
          priority: 1,
          bytes: previewBytes,
          reconstructible: false,
          pinned: true,
        });
        if (!resource || !resource.commit(previewBytes)) {
          resource?.release();
          setOperationError(new Error(MASK_RESOURCE_BUDGET_MESSAGE));
          setOperationStatus("error");
          return false;
        }
        previewResourceRef.current = resource;
      }
      operationIdRef.current += 1;
      const preview: MaskInstanceOperationPreview = {
        id: operationIdRef.current,
        name,
        sourceRevision,
        plan,
      };
      operationPreviewRef.current = null;
      instanceOperationPreviewRef.current = preview;
      setOperationPreview(null);
      setInstanceOperationPreview(preview);
      setOperationError(undefined);
      setOperationStatus("preview");
      return true;
    },
    [resourceCoordinator],
  );

  const runInstanceOperation = useCallback(
    async (
      name: string,
      operation: MaskInstanceOperationSpec,
      context: { sessionId: string; generation: number } = { sessionId: "local", generation: 0 },
    ): Promise<boolean> => {
      if (tiledStoreRef.current) {
        cancelActiveOperation();
        setOperationError(new LargeMaskFullScanRequiredError());
        setOperationStatus("error");
        return false;
      }
      const current = bufferRef.current;
      if (!current) return false;
      cancelActiveOperation();
      const sourceRevision = revisionRef.current;
      const rle = current.toRle();
      operationIdRef.current += 1;
      const operationId = operationIdRef.current;
      const controller = new AbortController();
      operationAbortRef.current = controller;
      setOperationStatus("computing");
      setOperationError(undefined);
      const endForeground = resourceCoordinator?.beginForegroundOperation();
      try {
        const plan =
          width * height > 1_000_000
            ? (
                await executeRasterMaskInstanceOperationAsync(
                  rle,
                  operation,
                  { ...context, operationId },
                  {
                    ...(workerPool ? { pool: workerPool } : {}),
                    priority: "editing",
                    signal: controller.signal,
                  },
                )
              ).plan
            : applyMaskInstanceOperation(current.data, width, height, operation);
        if (controller.signal.aborted || operationIdRef.current !== operationId) return false;
        operationAbortRef.current = null;
        if (!plan) {
          setOperationStatus("idle");
          return false;
        }
        const accepted = previewInstanceOperation(name, plan, sourceRevision);
        if (!accepted) setOperationStatus("idle");
        return accepted;
      } catch (error) {
        if (operationAbortRef.current === controller) operationAbortRef.current = null;
        const isCurrent = operationIdRef.current === operationId;
        if (error instanceof RasterMaskWorkerCancelledError || controller.signal.aborted) {
          if (isCurrent) setOperationStatus("idle");
          return false;
        }
        if (!isCurrent) return false;
        setOperationError(error);
        setOperationStatus("error");
        return false;
      } finally {
        endForeground?.();
      }
    },
    [
      cancelActiveOperation,
      height,
      previewInstanceOperation,
      resourceCoordinator,
      width,
      workerPool,
    ],
  );

  const confirmOperation = useCallback((): boolean => {
    if (instanceOperationPreviewRef.current) return false;
    const preview = operationPreviewRef.current;
    const current = bufferRef.current;
    if (!preview || !current || preview.sourceRevision !== revisionRef.current) {
      clearOperationPreview();
      return false;
    }
    if (preview.report.changedPixels === 0) {
      clearOperationPreview();
      return false;
    }
    const command = createDenseMaskHistoryCommand(
      preview.name,
      preview.sourceRevision,
      current.data,
      preview.alpha,
      width,
      height,
      preview.report.bounds,
    );
    if (!command || command.changedPixels !== preview.report.changedPixels) {
      throw new Error("mask history patch does not match the confirmed operation");
    }
    if (!historyRef.current.push(command)) {
      clearOperationPreview();
      setOperationError(new Error(`${MASK_RESOURCE_BUDGET_MESSAGE}；本次操作未应用`));
      setOperationStatus("error");
      return false;
    }
    const change = current.replaceAlpha(preview.alpha);
    clearOperationPreview();
    if (change.changedPixels === 0) return false;
    setDirty(true);
    setHistoryRevision((value) => value + 1);
    bump();
    return true;
  }, [bump, clearOperationPreview, height, historyRef, width]);

  const cancelOperation = useCallback(() => {
    cancelActiveOperation();
  }, [cancelActiveOperation]);

  const cancel = useCallback(() => {
    bufferRef.current = null;
    denseBufferResourceRef.current?.release();
    denseBufferResourceRef.current = null;
    if (tiledStoreRef.current) disposeTiledStore();
    else resetHistory();
    setBackend("dense");
    setActive(false);
    setDirty(false);
    cancelActiveOperation();
    bump();
  }, [bump, cancelActiveOperation, disposeTiledStore, resetHistory]);

  const commitToPolygon = useCallback(() => {
    const buffer = bufferRef.current;
    if (!buffer) return null;
    const result = maskToPolygon(buffer);
    return result.points.length < 3 ? null : result;
  }, []);

  const commitToRle = useCallback((): CocoRle | null => {
    const endForeground = resourceCoordinator?.beginForegroundOperation();
    try {
      return bufferRef.current?.toRle() ?? null;
    } finally {
      endForeground?.();
    }
  }, [resourceCoordinator]);

  const commitToRleAsync = useCallback((): Promise<CocoRle | null> => {
    const dense = bufferRef.current;
    if (dense) {
      const endForeground = resourceCoordinator?.beginForegroundOperation();
      try {
        return Promise.resolve(dense.toRle());
      } finally {
        endForeground?.();
      }
    }
    const store = tiledStoreRef.current;
    if (!store) return Promise.resolve(null);
    if (tiledCommitPromiseRef.current) return tiledCommitPromiseRef.current;
    setCommitInFlight(true);
    const endForeground = resourceCoordinator?.beginForegroundOperation();
    const promise = (async () => {
      await tiledQueueRef.current;
      if (tiledStoreRef.current !== store) return null;
      return store.merge();
    })().finally(() => {
      endForeground?.();
      if (tiledCommitPromiseRef.current === promise) {
        tiledCommitPromiseRef.current = null;
        setCommitInFlight(false);
      }
    });
    tiledCommitPromiseRef.current = promise;
    return promise;
  }, [resourceCoordinator]);

  const setViewport = useCallback(
    (rect: SparseMaskViewportRect | null) => {
      viewportRef.current = rect;
      const store = tiledStoreRef.current;
      if (!store) return;
      const generation = ++viewportGenerationRef.current;
      try {
        store.setViewport(rect);
        refreshTiledState(store);
        void store
          .loadViewport()
          .then(() => {
            if (tiledStoreRef.current !== store || viewportGenerationRef.current !== generation)
              return;
            refreshTiledState(store);
            bump();
          })
          .catch((error: unknown) => {
            if (tiledStoreRef.current !== store || viewportGenerationRef.current !== generation)
              return;
            setOperationError(error);
            setOperationStatus("error");
            refreshTiledState(store);
          });
      } catch (error: unknown) {
        setOperationError(error);
        setOperationStatus("error");
      }
    },
    [bump, refreshTiledState],
  );

  useEffect(
    () => () => {
      historyRef.current.clear();
      denseBufferResourceRef.current?.release();
      denseBufferResourceRef.current = null;
      previewResourceRef.current?.release();
      previewResourceRef.current = null;
      tiledStoreRef.current?.dispose();
      tiledStoreRef.current = null;
    },
    [historyRef],
  );

  useEffect(() => {
    if (typeof window === "undefined") return;
    const onPageHide = (event: PageTransitionEvent) => {
      if (event.persisted) cancelActiveOperation();
    };
    const onPageShow = (event: PageTransitionEvent) => {
      if (!event.persisted) return;
      const store = tiledStoreRef.current;
      if (!store) return;
      void store
        .loadViewport()
        .then(() => {
          if (tiledStoreRef.current !== store) return;
          refreshTiledState(store);
          bump();
        })
        .catch((error: unknown) => {
          if (tiledStoreRef.current !== store) return;
          setOperationError(error);
          setOperationStatus("error");
        });
    };
    window.addEventListener("pagehide", onPageHide);
    window.addEventListener("pageshow", onPageShow);
    return () => {
      window.removeEventListener("pagehide", onPageHide);
      window.removeEventListener("pageshow", onPageShow);
    };
  }, [bump, cancelActiveOperation, refreshTiledState]);

  const historyResources = historyRef.current.snapshot();

  return {
    active,
    mode,
    tool,
    brushShape,
    connectivity,
    radius,
    dirty,
    buffer: bufferRef.current,
    backend,
    tiledTiles,
    tiledResources,
    tiledReadOnly,
    commitInFlight,
    revision,
    canUndo: historyRef.current.canUndo,
    canRedo: historyRef.current.canRedo,
    historyResources,
    operationPreview,
    instanceOperationPreview,
    operationStatus,
    operationError,
    beginBlank,
    initFromPolygon,
    initFromRle,
    materializeFromRle,
    paintAt,
    beginStroke,
    endStroke,
    undo,
    redo,
    setMode,
    setTool,
    setBrushShape,
    setConnectivity,
    setRadius,
    previewOperation,
    runOperation,
    previewInstanceOperation,
    runInstanceOperation,
    confirmOperation,
    cancelOperation,
    cancel,
    commitToPolygon,
    commitToRle,
    commitToRleAsync,
    setViewport,
  };
}

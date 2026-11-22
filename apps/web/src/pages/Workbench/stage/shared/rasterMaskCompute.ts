import type { CocoRle } from "./geometry/maskRle";
import { decodeCocoRle } from "./geometry/maskRle";
import {
  applyMaskOperation,
  type MaskOperationResult,
  type MaskOperationSpec,
} from "./geometry/maskOperations";
import {
  applyMaskInstanceOperation,
  type MaskInstanceOperationPlan,
  type MaskInstanceOperationSpec,
} from "./geometry/maskInstanceOperations";
import { analyzeRasterMaskAlpha, type RasterMaskAnalysis } from "./rasterMaskRender";
import type { RasterMaskOperationContext } from "./rasterMaskWorkerProtocol";
import {
  RasterMaskWorkerPool,
  type RasterMaskWorkerFactory,
  type RasterMaskWorkerPriority,
  RasterMaskWorkerCancelledError,
  RasterMaskWorkerError,
  RasterMaskWorkerQueueFullError,
  RasterMaskWorkerTimeoutError,
} from "./rasterMaskWorkerPool";

export type { RasterMaskOperationContext } from "./rasterMaskWorkerProtocol";
export {
  RasterMaskWorkerCancelledError,
  RasterMaskWorkerError,
  RasterMaskWorkerQueueFullError,
  RasterMaskWorkerTimeoutError,
};

interface RasterMaskComputeOptions {
  pool?: RasterMaskWorkerPool;
  createWorker?: RasterMaskWorkerFactory | null;
  priority?: RasterMaskWorkerPriority;
  signal?: AbortSignal;
  timeoutMs?: number;
}

let defaultPool: RasterMaskWorkerPool | null = null;

function analyzeSynchronously(rle: CocoRle): RasterMaskAnalysis {
  const [height, width] = rle.size;
  return analyzeRasterMaskAlpha(decodeCocoRle(rle), width, height);
}

function shouldUseSynchronousFallback(options: RasterMaskComputeOptions): boolean {
  return (
    options.createWorker === null ||
    (!options.pool &&
      options.createWorker === undefined &&
      typeof Worker === "undefined" &&
      import.meta.env.MODE === "test")
  );
}

function workerPoolFor(options: RasterMaskComputeOptions): {
  pool: RasterMaskWorkerPool;
  dispose: boolean;
} {
  if (options.pool) return { pool: options.pool, dispose: false };
  if (options.createWorker) {
    return {
      pool: new RasterMaskWorkerPool({ size: 1, createWorker: options.createWorker }),
      dispose: true,
    };
  }
  if (typeof Worker === "undefined") {
    throw new RasterMaskWorkerError("Raster Mask Worker is unavailable");
  }
  defaultPool ??= new RasterMaskWorkerPool();
  return { pool: defaultPool, dispose: false };
}

async function withWorkerPool<T>(
  options: RasterMaskComputeOptions,
  run: (pool: RasterMaskWorkerPool) => Promise<T>,
): Promise<T> {
  let selected: { pool: RasterMaskWorkerPool; dispose: boolean };
  try {
    selected = workerPoolFor(options);
  } catch (error) {
    throw error instanceof RasterMaskWorkerError ? error : new RasterMaskWorkerError(String(error));
  }
  try {
    return await run(selected.pool);
  } finally {
    if (selected.dispose) selected.pool.dispose();
  }
}

export function disposeDefaultRasterMaskWorkerPool(): void {
  defaultPool?.dispose();
  defaultPool = null;
}

export function analyzeRasterMaskRleAsync(
  rle: CocoRle,
  options: RasterMaskComputeOptions = {},
): Promise<RasterMaskAnalysis> {
  if (options.signal?.aborted) return Promise.reject(new RasterMaskWorkerCancelledError());
  if (shouldUseSynchronousFallback(options)) return Promise.resolve(analyzeSynchronously(rle));
  return withWorkerPool(options, (pool) =>
    pool.analyze(rle, {
      priority: options.priority,
      signal: options.signal,
      timeoutMs: options.timeoutMs,
    }),
  );
}

export function executeRasterMaskOperationAsync(
  rle: CocoRle,
  operation: MaskOperationSpec,
  context: RasterMaskOperationContext,
  options: RasterMaskComputeOptions = {},
): Promise<{ context: RasterMaskOperationContext; result: MaskOperationResult }> {
  if (options.signal?.aborted) return Promise.reject(new RasterMaskWorkerCancelledError());
  if (shouldUseSynchronousFallback(options)) {
    const [height, width] = rle.size;
    return Promise.resolve({
      context,
      result: applyMaskOperation(decodeCocoRle(rle), width, height, operation),
    });
  }
  return withWorkerPool(options, (pool) =>
    pool.executeOperation(rle, operation, context, {
      priority: options.priority,
      signal: options.signal,
      timeoutMs: options.timeoutMs,
    }),
  );
}

export function executeRasterMaskInstanceOperationAsync(
  rle: CocoRle,
  operation: MaskInstanceOperationSpec,
  context: RasterMaskOperationContext,
  options: RasterMaskComputeOptions = {},
): Promise<{ context: RasterMaskOperationContext; plan: MaskInstanceOperationPlan | null }> {
  if (options.signal?.aborted) return Promise.reject(new RasterMaskWorkerCancelledError());
  if (shouldUseSynchronousFallback(options)) {
    const [height, width] = rle.size;
    return Promise.resolve({
      context,
      plan: applyMaskInstanceOperation(decodeCocoRle(rle), width, height, operation),
    });
  }
  return withWorkerPool(options, (pool) =>
    pool.executeInstanceOperation(rle, operation, context, {
      priority: options.priority,
      signal: options.signal,
      timeoutMs: options.timeoutMs,
    }),
  );
}

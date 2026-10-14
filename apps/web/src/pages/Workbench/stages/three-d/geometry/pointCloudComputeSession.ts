import type { SensorCalibration } from "@/types";

import type { CameraSample } from "./colorize";
import type { GpuDepthRaster } from "./depthmap";
import {
  decodePointCloudFrame,
  decodeAuxiliaryPointCloudFrame,
  type DecodedPointCloudFrame,
  type DecodedAuxiliaryPointCloudFrame,
} from "./pointcloudFrame";
import type { LidarAxisConvention } from "./axisConvention";

type WorkerFactory = () => Worker;

export type PointCloudDecodeLane = "shared" | "active" | "prefetch";

type WorkerRequest =
  | {
      reqId: number;
      kind: "decode_pcd" | "decode_auxiliary_pcd";
      buffer: ArrayBuffer;
      convention: LidarAxisConvention;
      decimateThreshold: number;
    }
  | {
      reqId: number;
      kind: "colorize";
      positions: Float32Array;
      baseColors: Float32Array | null;
      samples: CameraSample[];
    }
  | {
      reqId: number;
      kind: "build_depth_rasters";
      positions: Float32Array;
      cameras: Array<{ calib: SensorCalibration; width: number; height: number }>;
    };

type WorkerRequestPayload = WorkerRequest extends infer Request
  ? Request extends WorkerRequest
    ? Omit<Request, "reqId">
    : never
  : never;

type WorkerSuccess =
  | { reqId: number; ok: true; kind: "decode_pcd"; frame: DecodedPointCloudFrame }
  | {
      reqId: number;
      ok: true;
      kind: "decode_auxiliary_pcd";
      frame: DecodedAuxiliaryPointCloudFrame;
    }
  | { reqId: number; ok: true; kind: "colorize"; colors: Float32Array }
  | { reqId: number; ok: true; kind: "build_depth_rasters"; rasters: GpuDepthRaster[] };
type WorkerResponse = WorkerSuccess | { reqId: number; ok: false; error: string };

interface PendingRequest {
  resolve: (value: WorkerSuccess) => void;
  reject: (reason: unknown) => void;
  timer: number;
  signal?: AbortSignal;
  abort?: () => void;
}

function createDefaultWorker(): Worker {
  return new Worker(new URL("./pointcloud.worker.ts", import.meta.url), { type: "module" });
}

function cloneSample(sample: CameraSample): CameraSample {
  return { ...sample, data: sample.data.slice() };
}

/** One worker per workbench page instead of one worker per frame/colorization. */
export class PointCloudComputeSession {
  private worker: Worker | null = null;
  private nextRequestId = 1;
  private readonly pending = new Map<number, PendingRequest>();

  constructor(private readonly createWorker: WorkerFactory = createDefaultWorker) {}

  getDiagnostics(): { workerActive: boolean; pendingRequests: number } {
    return { workerActive: this.worker !== null, pendingRequests: this.pending.size };
  }

  private ensureWorker(): Worker {
    if (this.worker) return this.worker;
    const worker = this.createWorker();
    worker.onmessage = (event: MessageEvent<WorkerResponse>) => {
      const response = event.data;
      const pending = this.pending.get(response.reqId);
      if (!pending) return;
      this.finish(response.reqId);
      if (response.ok) pending.resolve(response);
      else pending.reject(new Error(response.error));
    };
    worker.onerror = (event) => {
      this.failWorker(event.error ?? new Error(event.message));
    };
    this.worker = worker;
    return worker;
  }

  private finish(reqId: number): void {
    const pending = this.pending.get(reqId);
    if (!pending) return;
    globalThis.clearTimeout(pending.timer);
    if (pending.signal && pending.abort) {
      pending.signal.removeEventListener("abort", pending.abort);
    }
    this.pending.delete(reqId);
  }

  private failWorker(error: unknown): void {
    const pending = [...this.pending.entries()];
    for (const [reqId, request] of pending) {
      this.finish(reqId);
      request.reject(error);
    }
    this.worker?.terminate();
    this.worker = null;
  }

  private run(
    request: WorkerRequestPayload,
    transfer: Transferable[],
    options: {
      signal?: AbortSignal;
      timeoutMs?: number;
      terminateWorkerOnAbort?: boolean;
    } = {},
  ): Promise<WorkerSuccess> {
    if (options.signal?.aborted) {
      return Promise.reject(new DOMException("Point-cloud computation aborted", "AbortError"));
    }
    const worker = this.ensureWorker();
    const reqId = this.nextRequestId;
    this.nextRequestId += 1;
    return new Promise((resolve, reject) => {
      const abort = () => {
        const error = new DOMException("Point-cloud computation aborted", "AbortError");
        if (options.terminateWorkerOnAbort) {
          this.failWorker(error);
          return;
        }
        this.finish(reqId);
        reject(error);
      };
      const timer = globalThis.setTimeout(() => {
        this.failWorker(new Error("pointcloud worker timeout"));
      }, options.timeoutMs ?? 15_000) as unknown as number;
      this.pending.set(reqId, { resolve, reject, timer, signal: options.signal, abort });
      options.signal?.addEventListener("abort", abort, { once: true });
      try {
        worker.postMessage({ ...request, reqId } as WorkerRequest, transfer);
      } catch (error) {
        this.finish(reqId);
        reject(error);
      }
    });
  }

  async decodePcd(
    buffer: ArrayBuffer,
    convention: LidarAxisConvention,
    decimateThreshold: number,
    options: { signal?: AbortSignal; terminateWorkerOnAbort?: boolean } = {},
  ): Promise<DecodedPointCloudFrame> {
    const transferable = buffer.slice(0);
    const response = await this.run(
      { kind: "decode_pcd", buffer: transferable, convention, decimateThreshold },
      [transferable],
      options,
    );
    if (response.kind !== "decode_pcd") throw new Error("unexpected pointcloud worker response");
    return response.frame;
  }

  async colorize(
    positions: Float32Array,
    baseColors: Float32Array | null,
    samples: CameraSample[],
    options: {
      signal?: AbortSignal;
      timeoutMs?: number;
      terminateWorkerOnAbort?: boolean;
    } = {},
  ): Promise<Float32Array> {
    const clonedPositions = positions.slice();
    const clonedBaseColors = baseColors?.slice() ?? null;
    const clonedSamples = samples.map(cloneSample);
    const transfer: Transferable[] = [clonedPositions.buffer];
    if (clonedBaseColors) transfer.push(clonedBaseColors.buffer);
    for (const sample of clonedSamples) transfer.push(sample.data.buffer);
    const response = await this.run(
      {
        kind: "colorize",
        positions: clonedPositions,
        baseColors: clonedBaseColors,
        samples: clonedSamples,
      },
      transfer,
      options,
    );
    if (response.kind !== "colorize") throw new Error("unexpected pointcloud worker response");
    return response.colors;
  }

  async decodeAuxiliaryPcd(
    buffer: ArrayBuffer,
    convention: LidarAxisConvention,
    targetCount: number,
    signal?: AbortSignal,
  ): Promise<DecodedAuxiliaryPointCloudFrame> {
    const transferable = buffer.slice(0);
    const response = await this.run(
      {
        kind: "decode_auxiliary_pcd",
        buffer: transferable,
        convention,
        decimateThreshold: targetCount,
      },
      [transferable],
      { signal },
    );
    if (response.kind !== "decode_auxiliary_pcd")
      throw new Error("unexpected auxiliary worker response");
    return response.frame;
  }

  async buildDepthRasters(
    positions: Float32Array,
    cameras: Array<{ calib: SensorCalibration; width: number; height: number }>,
    options: { signal?: AbortSignal; terminateWorkerOnAbort?: boolean } = {},
  ): Promise<GpuDepthRaster[]> {
    const clonedPositions = positions.slice();
    const response = await this.run(
      { kind: "build_depth_rasters", positions: clonedPositions, cameras },
      [clonedPositions.buffer],
      options,
    );
    if (response.kind !== "build_depth_rasters") {
      throw new Error("unexpected pointcloud worker response");
    }
    return response.rasters;
  }

  dispose(): void {
    this.failWorker(new DOMException("Point-cloud compute session disposed", "AbortError"));
  }
}

let sharedSession: PointCloudComputeSession | null = null;
let activeDecodeSession: PointCloudComputeSession | null = null;
let prefetchDecodeSession: PointCloudComputeSession | null = null;
let colorizeSession: PointCloudComputeSession | null = null;
let activeDepthSession: PointCloudComputeSession | null = null;
let prefetchDepthSession: PointCloudComputeSession | null = null;
let sharedSessionOwners = 0;

export function getPointCloudComputeSession(): PointCloudComputeSession {
  sharedSession ??= new PointCloudComputeSession();
  return sharedSession;
}

export function getPointCloudColorizeSession(): PointCloudComputeSession {
  colorizeSession ??= new PointCloudComputeSession();
  return colorizeSession;
}

export function getPointCloudDepthSession(
  lane: "shared" | "active" | "prefetch",
): PointCloudComputeSession {
  if (lane === "active") {
    activeDepthSession ??= new PointCloudComputeSession();
    return activeDepthSession;
  }
  if (lane === "prefetch") {
    prefetchDepthSession ??= new PointCloudComputeSession();
    return prefetchDepthSession;
  }
  return getPointCloudComputeSession();
}

function getPointCloudDecodeSession(lane: PointCloudDecodeLane): PointCloudComputeSession {
  if (lane === "active") {
    activeDecodeSession ??= new PointCloudComputeSession();
    return activeDecodeSession;
  }
  if (lane === "prefetch") {
    prefetchDecodeSession ??= new PointCloudComputeSession();
    return prefetchDecodeSession;
  }
  return getPointCloudComputeSession();
}

/** Read-only worker counters used by the local renderer benchmark. */
export function getPointCloudComputeDiagnostics(): {
  workerActive: boolean;
  pendingRequests: number;
  owners: number;
} {
  const sessions = [
    sharedSession,
    activeDecodeSession,
    prefetchDecodeSession,
    colorizeSession,
    activeDepthSession,
    prefetchDepthSession,
  ].filter((session): session is PointCloudComputeSession => session !== null);
  const diagnostics = sessions.map((session) => session.getDiagnostics());
  return {
    workerActive: diagnostics.some((entry) => entry.workerActive),
    pendingRequests: diagnostics.reduce((total, entry) => total + entry.pendingRequests, 0),
    owners: sharedSessionOwners,
  };
}

if (import.meta.env.DEV && typeof window !== "undefined") {
  (
    window as Window & {
      __pointCloudComputeDiagnostics?: typeof getPointCloudComputeDiagnostics;
    }
  ).__pointCloudComputeDiagnostics = getPointCloudComputeDiagnostics;
}

export function retainPointCloudComputeSession(): () => void {
  sharedSessionOwners += 1;
  getPointCloudComputeSession();
  let released = false;
  return () => {
    if (released) return;
    released = true;
    sharedSessionOwners = Math.max(0, sharedSessionOwners - 1);
    if (sharedSessionOwners === 0) {
      sharedSession?.dispose();
      sharedSession = null;
      activeDecodeSession?.dispose();
      activeDecodeSession = null;
      prefetchDecodeSession?.dispose();
      prefetchDecodeSession = null;
      colorizeSession?.dispose();
      colorizeSession = null;
      activeDepthSession?.dispose();
      activeDepthSession = null;
      prefetchDepthSession?.dispose();
      prefetchDepthSession = null;
    }
  };
}

export async function decodePointCloudFrameAsync(
  buffer: ArrayBuffer,
  convention: LidarAxisConvention,
  decimateThreshold: number,
  options: { signal?: AbortSignal; lane?: PointCloudDecodeLane } = {},
): Promise<DecodedPointCloudFrame> {
  if (options.signal?.aborted) {
    throw new DOMException("Point-cloud computation aborted", "AbortError");
  }
  if (typeof Worker === "undefined") {
    return decodePointCloudFrame(buffer, convention, decimateThreshold);
  }
  try {
    const lane = options.lane ?? "shared";
    return await getPointCloudDecodeSession(lane).decodePcd(buffer, convention, decimateThreshold, {
      signal: options.signal,
      terminateWorkerOnAbort: lane !== "shared",
    });
  } catch (error) {
    if (options.signal?.aborted) throw error;
    console.warn("[pointcloud-worker] PCD decode fallback to main thread", error);
    return decodePointCloudFrame(buffer, convention, decimateThreshold);
  }
}

/** Low-priority caller starts this only after the measured frame is ready. */
export async function decodeAuxiliaryPointCloudFrameAsync(
  buffer: ArrayBuffer,
  convention: LidarAxisConvention,
  targetCount: number,
  signal?: AbortSignal,
): Promise<DecodedAuxiliaryPointCloudFrame> {
  if (signal?.aborted) throw new DOMException("Auxiliary decode aborted", "AbortError");
  if (typeof Worker === "undefined")
    return decodeAuxiliaryPointCloudFrame(buffer, convention, targetCount);
  return getPointCloudComputeSession().decodeAuxiliaryPcd(buffer, convention, targetCount, signal);
}

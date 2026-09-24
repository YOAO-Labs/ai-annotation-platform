import type { TaskPointCloudManifestResponse } from "@/api/generated";
import type { SensorCalibration } from "@/types";

import type { LidarAxisConvention } from "./geometry/axisConvention";
import type { GpuDepthRaster } from "./geometry/depthmap";
import {
  decodePointCloudFrameAsync,
  getPointCloudDepthSession,
} from "./geometry/pointCloudComputeSession";
import type { DecodedPointCloudFrame } from "./geometry/pointcloudFrame";
import { markPointCloudStage } from "./pointCloudTiming";

const POINT_CLOUD_CACHE_LIMIT = 3;
const DECODED_FRAME_CACHE_LIMIT = 3;
const CAMERA_ASSET_CACHE_LIMIT = 18;
const DEPTH_RASTER_CACHE_LIMIT = 8;
const DEPTH_RASTER_CACHE_BUDGET_BYTES = 8 * 1024 * 1024;
type PointCloudAssetLane = "shared" | "active" | "prefetch";

interface PointCloudBufferCacheEntry {
  promise: Promise<ArrayBuffer>;
  ready: boolean;
  signal?: AbortSignal;
}

const pointCloudBuffers = new Map<string, PointCloudBufferCacheEntry>();

interface DecodedFrameCacheEntry {
  promise: Promise<DecodedPointCloudFrame>;
  ready: boolean;
  frame: DecodedPointCloudFrame | null;
  lane: "shared" | "active" | "prefetch";
  signal?: AbortSignal;
}

const decodedFrames = new Map<string, DecodedFrameCacheEntry>();
interface CameraBlobEntry {
  promise: Promise<Blob>;
  ready: boolean;
  lane: PointCloudAssetLane;
  signal?: AbortSignal;
}

const cameraBlobs = new Map<string, CameraBlobEntry>();
let activeFrameDecodeController: AbortController | null = null;
let prefetchFrameDecodeController: AbortController | null = null;

interface DepthRasterCacheEntry {
  promise: Promise<GpuDepthRaster[]>;
  bytes: number;
  ready: boolean;
  lane: PointCloudAssetLane;
  signal?: AbortSignal;
}

const depthRasters = new Map<string, DepthRasterCacheEntry>();
let depthRasterCacheBytes = 0;

interface CameraBitmapEntry {
  promise: Promise<ImageBitmap>;
  bitmap: ImageBitmap | null;
  references: number;
  lane: PointCloudAssetLane;
  signal?: AbortSignal;
}

const cameraBitmaps = new Map<string, CameraBitmapEntry>();

export interface PointCloudAssetCacheDiagnostics {
  pointCloudBuffers: number;
  decodedFrames: number;
  cameraBlobs: number;
  cameraBitmaps: number;
  cameraBitmapReferences: number;
  depthRasters: number;
  depthRasterBytes: number;
  depthRasterInFlight: number;
}

/** Read-only cache counters used by the local renderer benchmark. */
export function getPointCloudAssetCacheDiagnostics(): PointCloudAssetCacheDiagnostics {
  return {
    pointCloudBuffers: pointCloudBuffers.size,
    decodedFrames: decodedFrames.size,
    cameraBlobs: cameraBlobs.size,
    cameraBitmaps: cameraBitmaps.size,
    cameraBitmapReferences: [...cameraBitmaps.values()].reduce(
      (total, entry) => total + entry.references,
      0,
    ),
    depthRasters: depthRasters.size,
    depthRasterBytes: depthRasterCacheBytes,
    depthRasterInFlight: [...depthRasters.values()].filter((entry) => entry.bytes === 0).length,
  };
}

if (import.meta.env.DEV && typeof window !== "undefined") {
  (
    window as Window & {
      __pointCloudAssetCacheDiagnostics?: () => PointCloudAssetCacheDiagnostics;
    }
  ).__pointCloudAssetCacheDiagnostics = getPointCloudAssetCacheDiagnostics;
}

function touch<T>(cache: Map<string, T>, key: string, value: T): void {
  cache.delete(key);
  cache.set(key, value);
}

function trimOldest<T>(cache: Map<string, T>, limit: number): void {
  while (cache.size > limit) {
    const oldest = cache.keys().next().value;
    if (oldest === undefined) return;
    cache.delete(oldest);
  }
}

function trimCameraBitmaps(): void {
  if (cameraBitmaps.size <= CAMERA_ASSET_CACHE_LIMIT) return;
  for (const [url, entry] of cameraBitmaps) {
    if (cameraBitmaps.size <= CAMERA_ASSET_CACHE_LIMIT) break;
    if (entry.references > 0 || !entry.bitmap) continue;
    cameraBitmaps.delete(url);
    entry.bitmap.close();
  }
}

function trimDepthRasters(): void {
  while (
    depthRasters.size > DEPTH_RASTER_CACHE_LIMIT ||
    depthRasterCacheBytes > DEPTH_RASTER_CACHE_BUDGET_BYTES
  ) {
    const oldest = depthRasters.entries().next().value;
    if (!oldest) return;
    const [key, entry] = oldest;
    depthRasters.delete(key);
    depthRasterCacheBytes = Math.max(0, depthRasterCacheBytes - entry.bytes);
  }
}

function depthRasterKey(
  pointCloudUrl: string,
  positions: Float32Array,
  cameras: readonly CameraDepthRasterInput[],
): string {
  return JSON.stringify([
    pointCloudUrl,
    positions.length,
    cameras.map((camera) => [
      camera.width,
      camera.height,
      camera.calibration.extrinsic,
      camera.calibration.intrinsic,
      camera.calibration.rect ?? null,
    ]),
  ]);
}

/** Shared raw-byte cache; an obsolete in-flight owner must not retain a network slot. */
export function loadPointCloudBuffer(url: string, signal?: AbortSignal): Promise<ArrayBuffer> {
  const cached = pointCloudBuffers.get(url);
  if (cached && (cached.ready || (!cached.signal?.aborted && cached.signal === signal))) {
    touch(pointCloudBuffers, url, cached);
    return cached.promise;
  }
  if (cached) pointCloudBuffers.delete(url);

  const entry: PointCloudBufferCacheEntry = {
    promise: Promise.resolve(new ArrayBuffer(0)),
    ready: false,
    signal,
  };
  entry.promise = fetch(url, { cache: "force-cache", signal })
    .then((response) => {
      if (!response.ok) throw new Error(`点云资源加载失败: ${response.status}`);
      return response.arrayBuffer();
    })
    .then((buffer) => {
      entry.ready = true;
      return buffer;
    })
    .catch((error) => {
      if (pointCloudBuffers.get(url) === entry) pointCloudBuffers.delete(url);
      throw error;
    });
  pointCloudBuffers.set(url, entry);
  trimOldest(pointCloudBuffers, POINT_CLOUD_CACHE_LIMIT);
  return entry.promise;
}

export async function prefetchPointCloudBuffer(url: string): Promise<void> {
  await loadPointCloudBuffer(url);
}

function decodedPointCloudFrameRequest(
  url: string,
  convention: LidarAxisConvention,
  decimateThreshold: number,
  options: {
    lane?: "shared" | "active" | "prefetch";
    signal?: AbortSignal;
  } = {},
): { promise: Promise<DecodedPointCloudFrame>; cacheHit: boolean } {
  const lane = options.lane ?? "shared";
  const key = `${url}\n${convention}\n${decimateThreshold}`;
  const cached = decodedFrames.get(key);
  const reusable =
    cached &&
    (cached.ready ||
      (!cached.signal?.aborted && cached.lane === lane && cached.signal === options.signal));
  if (reusable) {
    touch(decodedFrames, key, cached);
    return { promise: cached.promise, cacheHit: true };
  }

  const entry: DecodedFrameCacheEntry = {
    promise: Promise.resolve(null as unknown as DecodedPointCloudFrame),
    ready: false,
    frame: null,
    lane,
    signal: options.signal,
  };
  entry.promise = loadPointCloudBuffer(url, options.signal)
    .then((buffer) =>
      decodePointCloudFrameAsync(buffer, convention, decimateThreshold, {
        lane,
        signal: options.signal,
      }),
    )
    .then((frame) => {
      entry.ready = true;
      entry.frame = frame;
      return frame;
    })
    .catch((error) => {
      if (decodedFrames.get(key) === entry) decodedFrames.delete(key);
      throw error;
    });
  decodedFrames.set(key, entry);
  trimOldest(decodedFrames, DECODED_FRAME_CACHE_LIMIT);
  return { promise: entry.promise, cacheHit: false };
}

export function loadDecodedPointCloudFrame(
  url: string,
  convention: LidarAxisConvention,
  decimateThreshold: number,
  options: { signal?: AbortSignal } = {},
): Promise<DecodedPointCloudFrame> {
  return decodedPointCloudFrameRequest(url, convention, decimateThreshold, {
    lane: "shared",
    signal: options.signal,
  }).promise;
}

/** Read-only check for the assets needed by the selected renderer before a shorter click settle. */
export function isPointCloudFramePrefetchReady(
  manifest: TaskPointCloudManifestResponse,
  options: PointCloudFramePrefetchOptions = {},
): boolean {
  const url = manifest.point_cloud_url;
  const convention = manifest.axis_convention ?? "iso_8855";
  const threshold = options.decimateThreshold ?? 500_000;
  const decoded = decodedFrames.get(`${url}\n${convention}\n${threshold}`);
  if (!decoded?.ready || !decoded.frame) return false;
  if (!options.depthRasters) return true;

  const cameraAssets = manifest.cameras
    .filter((camera) => !!camera.image_url)
    .map((camera) => ({ camera, bitmap: cameraBitmaps.get(camera.image_url)?.bitmap }));
  if (cameraAssets.some(({ bitmap }) => !bitmap)) return false;
  const usableCameras = cameraAssets.flatMap(({ camera, bitmap }) =>
    camera.calibration && bitmap
      ? [{ calibration: camera.calibration, width: bitmap.width, height: bitmap.height }]
      : [],
  );
  if (usableCameras.length === 0 || usableCameras.length > 6) return true;
  const key = depthRasterKey(url, decoded.frame.positions, usableCameras);
  return depthRasters.get(key)?.ready === true;
}

/** Active-scene load with one unambiguous cache/timing event for the benchmark trace. */
export async function loadTimedDecodedPointCloudFrame(
  url: string,
  convention: LidarAxisConvention,
  decimateThreshold: number,
): Promise<DecodedPointCloudFrame> {
  prefetchFrameDecodeController?.abort();
  prefetchFrameDecodeController = null;
  activeFrameDecodeController?.abort();
  const controller = new AbortController();
  activeFrameDecodeController = controller;
  const startedAt = performance.now();
  const request = decodedPointCloudFrameRequest(url, convention, decimateThreshold, {
    lane: "active",
    signal: controller.signal,
  });
  try {
    const frame = await request.promise;
    markPointCloudStage("pcd-frame-ready", url, startedAt, request.cacheHit);
    return frame;
  } finally {
    if (activeFrameDecodeController === controller) activeFrameDecodeController = null;
  }
}

/** Stop obsolete active-frame CPU work when the owning scene effect is replaced or unmounted. */
export function cancelActivePointCloudFrameLoad(): void {
  activeFrameDecodeController?.abort();
  activeFrameDecodeController = null;
}

function loadCameraBlob(
  url: string,
  signal?: AbortSignal,
  lane: PointCloudAssetLane = "shared",
): Promise<Blob> {
  const cached = cameraBlobs.get(url);
  const reusable =
    cached &&
    (cached.ready || (!cached.signal?.aborted && cached.lane === lane && cached.signal === signal));
  if (reusable) {
    touch(cameraBlobs, url, cached);
    return cached.promise;
  }
  if (cached) cameraBlobs.delete(url);

  const entry: CameraBlobEntry = {
    promise: Promise.resolve(new Blob()),
    ready: false,
    lane,
    signal,
  };
  entry.promise = fetch(url, { cache: "force-cache", signal })
    .then((response) => {
      if (!response.ok) throw new Error(`相机资源加载失败: ${response.status}`);
      return response.blob();
    })
    .then((blob) => {
      entry.ready = true;
      return blob;
    })
    .catch((error) => {
      if (cameraBlobs.get(url) === entry) cameraBlobs.delete(url);
      throw error;
    });
  cameraBlobs.set(url, entry);
  trimOldest(cameraBlobs, CAMERA_ASSET_CACHE_LIMIT);
  return entry.promise;
}

function decodeImageBitmap(blob: Blob, signal?: AbortSignal): Promise<ImageBitmap> {
  const decode = createImageBitmap(blob);
  if (!signal) return decode;
  return new Promise((resolve, reject) => {
    let settled = false;
    const onAbort = () => {
      if (settled) return;
      settled = true;
      reject(new DOMException("Camera bitmap decode aborted", "AbortError"));
    };
    signal.addEventListener("abort", onAbort, { once: true });
    void decode.then(
      (bitmap) => {
        signal.removeEventListener("abort", onAbort);
        if (settled || signal.aborted) {
          bitmap.close();
          return;
        }
        settled = true;
        resolve(bitmap);
      },
      (error) => {
        signal.removeEventListener("abort", onAbort);
        if (settled) return;
        settled = true;
        reject(error);
      },
    );
    if (signal.aborted) onAbort();
  });
}

function loadCameraBitmap(
  url: string,
  signal?: AbortSignal,
  lane: PointCloudAssetLane = "shared",
): Promise<ImageBitmap> {
  const cached = cameraBitmaps.get(url);
  const reusable =
    cached &&
    (cached.bitmap !== null ||
      (!cached.signal?.aborted && cached.lane === lane && cached.signal === signal));
  if (reusable) {
    touch(cameraBitmaps, url, cached);
    return cached.promise;
  }
  if (cached) cameraBitmaps.delete(url);
  if (typeof createImageBitmap === "undefined") {
    return Promise.reject(new Error("ImageBitmap decode is unavailable"));
  }
  const entry: CameraBitmapEntry = {
    bitmap: null,
    references: 0,
    promise: Promise.resolve(null as unknown as ImageBitmap),
    lane,
    signal,
  };
  entry.promise = loadCameraBlob(url, signal, lane)
    .then((blob) => decodeImageBitmap(blob, signal))
    .then((bitmap) => {
      entry.bitmap = bitmap;
      trimCameraBitmaps();
      return bitmap;
    })
    .catch((error) => {
      if (cameraBitmaps.get(url) === entry) cameraBitmaps.delete(url);
      throw error;
    });
  cameraBitmaps.set(url, entry);
  trimCameraBitmaps();
  return entry.promise;
}

export interface AcquiredCameraBitmap {
  bitmap: ImageBitmap;
  cacheReady: boolean;
  release(): void;
}

/** Pins a decoded bitmap until the renderer releases its camera texture owner. */
export async function acquireCameraBitmap(
  url: string,
  signal?: AbortSignal,
  lane: PointCloudAssetLane = "active",
): Promise<AcquiredCameraBitmap> {
  const cached = cameraBitmaps.get(url);
  const cacheReady = cached?.bitmap != null;
  const promise = loadCameraBitmap(url, signal, lane);
  const entry = cameraBitmaps.get(url);
  if (!entry) throw new Error("Camera bitmap cache entry was not created");
  entry.references += 1;
  try {
    const bitmap = await promise;
    let released = false;
    return {
      bitmap,
      cacheReady,
      release() {
        if (released) return;
        released = true;
        entry.references = Math.max(0, entry.references - 1);
        trimCameraBitmaps();
      },
    };
  } catch (error) {
    entry.references = Math.max(0, entry.references - 1);
    throw error;
  }
}

export interface CameraDepthRasterInput {
  calibration: SensorCalibration;
  width: number;
  height: number;
}

export interface LoadedPointCloudDepthRasters {
  rasters: GpuDepthRaster[];
  cacheHit: boolean;
}

/** Share compact camera-depth rasters across prefetch and the active WebGPU frame. */
export function loadPointCloudDepthRasters(
  pointCloudUrl: string,
  positions: Float32Array,
  cameras: readonly CameraDepthRasterInput[],
  options: { signal?: AbortSignal; lane?: PointCloudAssetLane } = {},
): Promise<LoadedPointCloudDepthRasters> {
  const lane = options.lane ?? "shared";
  const key = depthRasterKey(pointCloudUrl, positions, cameras);
  const cached = depthRasters.get(key);
  const reusable =
    cached &&
    (cached.ready ||
      (!cached.signal?.aborted && cached.lane === lane && cached.signal === options.signal));
  if (reusable) {
    touch(depthRasters, key, cached);
    return cached.promise.then((rasters) => ({ rasters, cacheHit: true }));
  }
  if (cached) depthRasters.delete(key);

  const entry: DepthRasterCacheEntry = {
    promise: Promise.resolve([]),
    bytes: 0,
    ready: false,
    lane,
    signal: options.signal,
  };
  entry.promise = getPointCloudDepthSession(lane)
    .buildDepthRasters(
      positions,
      cameras.map((camera) => ({
        calib: camera.calibration,
        width: camera.width,
        height: camera.height,
      })),
      {
        signal: options.signal,
        terminateWorkerOnAbort: lane !== "shared",
      },
    )
    .then((rasters) => {
      entry.ready = true;
      if (depthRasters.get(key) === entry) {
        entry.bytes = rasters.reduce((total, raster) => total + raster.depth.byteLength, 0);
        depthRasterCacheBytes += entry.bytes;
        trimDepthRasters();
      }
      return rasters;
    })
    .catch((error) => {
      if (depthRasters.get(key) === entry) depthRasters.delete(key);
      throw error;
    });
  depthRasters.set(key, entry);
  trimDepthRasters();
  return entry.promise.then((rasters) => ({ rasters, cacheHit: false }));
}

export interface PointCloudFramePrefetchOptions {
  decimateThreshold?: number;
  depthRasters?: boolean;
}

/** Decode parsed PCD and camera bitmaps for an adjacent frame. */
async function prefetchPointCloudFrameAssetsWithSignal(
  manifest: TaskPointCloudManifestResponse,
  options: PointCloudFramePrefetchOptions,
  signal: AbortSignal,
): Promise<void> {
  const decimateThreshold = options.decimateThreshold ?? 500_000;
  const convention = manifest.axis_convention ?? "iso_8855";
  const framePromise = decodedPointCloudFrameRequest(
    manifest.point_cloud_url,
    convention,
    decimateThreshold,
    { lane: "prefetch", signal },
  ).promise.catch(() => null);
  const cameraPromises = manifest.cameras
    .filter((camera) => !!camera.image_url)
    .map(async (camera) => {
      try {
        if (typeof createImageBitmap === "undefined") {
          await loadCameraBlob(camera.image_url, signal, "prefetch");
          return null;
        }
        const bitmap = await loadCameraBitmap(camera.image_url, signal, "prefetch");
        return camera.calibration ? { camera, bitmap } : null;
      } catch {
        return null;
      }
    });
  const [frame, cameraAssets] = await Promise.all([framePromise, Promise.all(cameraPromises)]);
  if (!options.depthRasters || !frame) return;
  const usableCameras = cameraAssets.filter(
    (
      asset,
    ): asset is {
      camera: (typeof manifest.cameras)[number] & { calibration: SensorCalibration };
      bitmap: ImageBitmap;
    } => asset !== null && !!asset.camera.calibration,
  );
  if (usableCameras.length === 0 || usableCameras.length > 6) return;
  await loadPointCloudDepthRasters(
    manifest.point_cloud_url,
    frame.positions,
    usableCameras.map(({ camera, bitmap }) => ({
      calibration: camera.calibration,
      width: bitmap.width,
      height: bitmap.height,
    })),
    { signal, lane: "prefetch" },
  ).catch(() => {});
}

/** Keep only the newest speculative frame warm; active-frame decode uses a separate lane. */
export function prefetchPointCloudFrameAssets(
  manifest: TaskPointCloudManifestResponse,
  options: PointCloudFramePrefetchOptions = {},
): Promise<void> {
  prefetchFrameDecodeController?.abort();
  const controller = new AbortController();
  prefetchFrameDecodeController = controller;
  return prefetchPointCloudFrameAssetsWithSignal(manifest, options, controller.signal).finally(
    () => {
      if (prefetchFrameDecodeController === controller) prefetchFrameDecodeController = null;
    },
  );
}

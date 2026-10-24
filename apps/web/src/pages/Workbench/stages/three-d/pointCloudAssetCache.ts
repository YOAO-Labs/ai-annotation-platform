import type { TaskPointCloudManifestResponse } from "@/api/generated";
import {
  captureOriginalOwner,
  isOriginalDownloadPath,
  isOriginalOwnerCurrent,
  originalOwnerKey,
  promoteOriginalRead,
  readOriginalBytes,
  type OriginalOwner,
} from "@/api/originals";
import { useAuthStore } from "@/stores/authStore";
import { createOriginalImageAddress, typedOriginalImageBlob } from "@/utils/originalImageDisplay";
import type { SensorCalibration } from "@/types";
import type { LidarAxisConvention } from "./geometry/axisConvention";
import type { GpuDepthRaster } from "./geometry/depthmap";
import {
  decodePointCloudFrameAsync,
  getPointCloudDepthSession,
} from "./geometry/pointCloudComputeSession";
import type { DecodedPointCloudFrame } from "./geometry/pointcloudFrame";
import { markPointCloudStage } from "./pointCloudTiming";

type Lane = "shared" | "active" | "prefetch";
interface Entry<T> {
  promise: Promise<T>;
  controller: AbortController;
  ready: boolean;
  consumers: number;
  lane: Lane;
  value?: T;
  references: number;
  retired: boolean;
  bytes: number;
}
interface OwnerCache {
  key: string;
  owner: OriginalOwner | null;
  retired: boolean;
  buffers: Map<string, Entry<ArrayBuffer>>;
  frames: Map<string, Entry<DecodedPointCloudFrame>>;
  blobs: Map<string, Entry<Blob>>;
  bitmaps: Map<string, Entry<ImageBitmap>>;
  depths: Map<string, Entry<GpuDepthRaster[]>>;
  active: AbortController | null;
  prefetch: AbortController | null;
}
let current: OwnerCache | null = null;
const abortError = () => new DOMException("Point-cloud asset owner changed", "AbortError");
function check(state: OwnerCache, signal?: AbortSignal) {
  if (
    state.retired ||
    signal?.aborted ||
    state.key !== originalOwnerKey() ||
    (state.owner && !isOriginalOwnerCurrent(state.owner))
  )
    throw abortError();
}
function retire(state: OwnerCache) {
  state.retired = true;
  state.active?.abort();
  state.prefetch?.abort();
  for (const cache of [state.buffers, state.frames, state.blobs, state.bitmaps, state.depths]) {
    for (const entry of cache.values()) {
      entry.retired = true;
      entry.controller.abort();
    }
  }
  for (const entry of state.bitmaps.values()) {
    if (entry.value && entry.references === 0) {
      entry.value.close();
      entry.value = undefined;
    }
  }
  state.buffers.clear();
  state.frames.clear();
  state.blobs.clear();
  state.bitmaps.clear();
  state.depths.clear();
}
function stateForOwner(): OwnerCache {
  const key = originalOwnerKey();
  const owner = captureOriginalOwner();
  if (!current || current.key !== key || (!current.owner && owner)) {
    if (current) retire(current);
    current = {
      key,
      owner,
      retired: false,
      buffers: new Map(),
      frames: new Map(),
      blobs: new Map(),
      bitmaps: new Map(),
      depths: new Map(),
      active: null,
      prefetch: null,
    };
  }
  return current;
}
const unsubscribe = useAuthStore.subscribe(() => {
  if (current && current.key !== originalOwnerKey()) {
    retire(current);
    current = null;
  }
});
function touch<T>(cache: Map<string, T>, key: string, value: T) {
  cache.delete(key);
  cache.set(key, value);
}
function trim<T>(cache: Map<string, Entry<T>>, limit: number, dispose?: (entry: Entry<T>) => void) {
  for (const [key, entry] of cache) {
    if (cache.size <= limit) break;
    if (!entry.ready || entry.consumers || entry.references) continue;
    cache.delete(key);
    entry.retired = true;
    dispose?.(entry);
  }
}
function wait<T>(
  state: OwnerCache,
  entry: Entry<T>,
  signal?: AbortSignal,
  lane: Lane = "shared",
): Promise<T> {
  check(state, signal);
  if (lane !== "prefetch") {
    entry.lane = lane;
    promoteOriginalRead(entry.controller.signal);
  }
  entry.consumers++;
  return new Promise((resolve, reject) => {
    let done = false;
    const finish = (error: unknown, value?: T) => {
      if (done) return;
      done = true;
      signal?.removeEventListener("abort", onAbort);
      entry.controller.signal.removeEventListener("abort", onAbort);
      entry.consumers--;
      if (!entry.ready && entry.consumers === 0) entry.controller.abort();
      if (error) reject(error);
      else resolve(value!);
    };
    const onAbort = () => finish(abortError());
    signal?.addEventListener("abort", onAbort, { once: true });
    entry.controller.signal.addEventListener("abort", onAbort, { once: true });
    void entry.promise.then(
      (value) => {
        try {
          check(state, signal);
          finish(null, value);
        } catch (error) {
          finish(error);
        }
      },
      (error) => finish(error),
    );
    if (signal?.aborted) onAbort();
  });
}
function request<T>(
  state: OwnerCache,
  cache: Map<string, Entry<T>>,
  key: string,
  limit: number,
  build: (signal: AbortSignal) => Promise<T>,
  signal?: AbortSignal,
  lane: Lane = "shared",
  dispose?: (value: T) => void,
): { entry: Entry<T>; promise: Promise<T>; cacheHit: boolean } {
  check(state, signal);
  let entry = cache.get(key);
  const cacheHit = !!entry && !entry.controller.signal.aborted;
  if (!cacheHit) {
    entry = {
      controller: new AbortController(),
      lane,
      ready: false,
      consumers: 0,
      references: 0,
      retired: false,
      bytes: 0,
      promise: null!,
    };
    const candidate = entry;
    candidate.promise = Promise.resolve()
      .then(() => {
        check(state, candidate.controller.signal);
        return build(candidate.controller.signal);
      })
      .then((value) => {
        try {
          check(state, candidate.controller.signal);
        } catch (error) {
          dispose?.(value);
          throw error;
        }
        candidate.value = value;
        candidate.ready = true;
        trim(cache, limit, (e) => {
          if (e.value) dispose?.(e.value);
        });
        return value;
      })
      .catch((error) => {
        if (cache.get(key) === candidate) cache.delete(key);
        throw error;
      });
    cache.set(key, candidate);
  } else touch(cache, key, entry!);
  const promise = wait(state, entry!, signal, lane).finally(() => {
    trim(cache, limit, (e) => {
      if (e.value) dispose?.(e.value);
    });
  });
  return { entry: entry!, promise, cacheHit };
}

/** Non-original URLs are reserved for anonymous renderer fixtures; credentials are never attached. */
async function bytes(
  state: OwnerCache,
  url: string,
  signal: AbortSignal,
  lane: Lane,
): Promise<Blob> {
  check(state, signal);
  if (isOriginalDownloadPath(url)) {
    if (!state.owner) throw abortError();
    const blob = await readOriginalBytes(url, {
      owner: state.owner,
      signal,
      priority: lane === "prefetch" ? "prefetch" : "active",
    });
    check(state, signal);
    return blob;
  }
  const parsed = new URL(url, window.location.href);
  if (
    (parsed.protocol !== "https:" && parsed.protocol !== "http:") ||
    parsed.pathname.startsWith("/api/")
  )
    throw new Error("point_cloud_asset_uncontrolled_path");
  const response = await fetch(url, { cache: "force-cache", credentials: "omit", signal });
  check(state, signal);
  if (!response.ok) throw new Error(`资源加载失败: ${response.status}`);
  const blob = await response.blob();
  check(state, signal);
  return blob;
}
function bufferRequest(
  state: OwnerCache,
  url: string,
  signal?: AbortSignal,
  lane: Lane = "shared",
) {
  return request(
    state,
    state.buffers,
    url,
    3,
    async (privateSignal) => {
      const blob = await bytes(state, url, privateSignal, state.buffers.get(url)?.lane ?? lane);
      const buffer = await blob.arrayBuffer();
      check(state, privateSignal);
      return buffer;
    },
    signal,
    lane,
  );
}
export function loadPointCloudBuffer(url: string, signal?: AbortSignal): Promise<ArrayBuffer> {
  return bufferRequest(stateForOwner(), url, signal).promise;
}
export async function prefetchPointCloudBuffer(url: string): Promise<void> {
  await loadPointCloudBuffer(url);
}
function frameRequest(
  state: OwnerCache,
  url: string,
  convention: LidarAxisConvention,
  threshold: number,
  signal?: AbortSignal,
  lane: Lane = "shared",
) {
  if (lane !== "prefetch") {
    const buffer = state.buffers.get(url);
    if (buffer) promoteOriginalRead(buffer.controller.signal);
  }
  const key = `${url}\n${convention}\n${threshold}`;
  return request(
    state,
    state.frames,
    key,
    3,
    async (privateSignal) => {
      const buffer = await bufferRequest(
        state,
        url,
        privateSignal,
        state.frames.get(key)?.lane ?? lane,
      ).promise;
      check(state, privateSignal);
      const frame = await decodePointCloudFrameAsync(buffer, convention, threshold, {
        lane: state.frames.get(key)?.lane ?? lane,
        signal: privateSignal,
      });
      check(state, privateSignal);
      return frame;
    },
    signal,
    lane,
  );
}
export function loadDecodedPointCloudFrame(
  url: string,
  convention: LidarAxisConvention,
  threshold: number,
  options: { signal?: AbortSignal } = {},
): Promise<DecodedPointCloudFrame> {
  return frameRequest(stateForOwner(), url, convention, threshold, options.signal).promise;
}
export async function loadTimedDecodedPointCloudFrame(
  url: string,
  convention: LidarAxisConvention,
  threshold: number,
): Promise<DecodedPointCloudFrame> {
  const state = stateForOwner();
  state.active?.abort();
  const controller = new AbortController();
  state.active = controller;
  const startedAt = performance.now();
  const result = frameRequest(state, url, convention, threshold, controller.signal, "active");
  // Join first so cancelling the speculative consumer cannot cancel the shared work.
  state.prefetch?.abort();
  state.prefetch = null;
  try {
    const frame = await result.promise;
    check(state, controller.signal);
    markPointCloudStage("pcd-frame-ready", url, startedAt, result.cacheHit);
    return frame;
  } finally {
    if (state.active === controller) state.active = null;
  }
}
/** Drop speculative consumers when their workbench unmounts; shared active readers stay alive. */
export function cancelPointCloudFramePrefetch(): void {
  current?.prefetch?.abort();
  if (current) current.prefetch = null;
}
export function cancelActivePointCloudFrameLoad(): void {
  current?.active?.abort();
  if (current) current.active = null;
}
function blobRequest(state: OwnerCache, url: string, signal?: AbortSignal, lane: Lane = "shared") {
  return request(
    state,
    state.blobs,
    url,
    18,
    (privateSignal) => bytes(state, url, privateSignal, state.blobs.get(url)?.lane ?? lane),
    signal,
    lane,
  );
}
export async function acquireCameraImageAddress(
  url: string,
  signal?: AbortSignal,
  lane: Lane = "active",
) {
  const state = stateForOwner();
  const blob = await blobRequest(state, url, signal, lane).promise;
  check(state, signal);
  const address = await createOriginalImageAddress(blob, signal);
  try {
    check(state, signal);
    return address;
  } catch (error) {
    address.release();
    throw error;
  }
}
async function decodeBitmap(state: OwnerCache, blob: Blob, signal: AbortSignal) {
  const typed = await typedOriginalImageBlob(blob, signal);
  check(state, signal);
  if (typed.type !== "image/svg+xml") return checkedBitmap(state, typed, signal);

  // Chromium requires an already decoded Image for SVG; keep its document origin opaque.
  const address = await createOriginalImageAddress(typed, signal);
  let image: HTMLImageElement | null = null;
  const onAbort = () => {
    if (image) image.src = "";
  };
  try {
    check(state, signal);
    image = new Image();
    image.decoding = "async";
    signal.addEventListener("abort", onAbort, { once: true });
    check(state, signal);
    image.src = address.url;
    await image.decode();
    check(state, signal);
    return await checkedBitmap(state, image, signal);
  } finally {
    signal.removeEventListener("abort", onAbort);
    if (image) image.src = "";
    address.release();
  }
}
async function checkedBitmap(state: OwnerCache, source: ImageBitmapSource, signal: AbortSignal) {
  const bitmap = await createImageBitmap(source);
  try {
    check(state, signal);
    return bitmap;
  } catch (error) {
    bitmap.close();
    throw error;
  }
}
export interface AcquiredCameraBitmap {
  bitmap: ImageBitmap;
  cacheReady: boolean;
  release(): void;
}
export async function acquireCameraBitmap(
  url: string,
  signal?: AbortSignal,
  lane: Lane = "active",
): Promise<AcquiredCameraBitmap> {
  const state = stateForOwner();
  if (lane !== "prefetch") {
    const blob = state.blobs.get(url);
    if (blob) promoteOriginalRead(blob.controller.signal);
  }
  const result = request(
    state,
    state.bitmaps,
    url,
    18,
    async (privateSignal) => {
      const blob = await blobRequest(
        state,
        url,
        privateSignal,
        state.bitmaps.get(url)?.lane ?? lane,
      ).promise;
      check(state, privateSignal);
      return decodeBitmap(state, blob, privateSignal);
    },
    signal,
    lane,
    (bitmap) => bitmap.close(),
  );
  const entry = result.entry;
  const cacheReady = result.cacheHit && entry.ready;
  entry.references++;
  let released = false;
  const release = () => {
    if (released) return;
    released = true;
    entry.references--;
    if (entry.retired && entry.references === 0 && entry.value) {
      entry.value.close();
      entry.value = undefined;
    }
    trim(state.bitmaps, 18, (e) => e.value?.close());
  };
  try {
    const bitmap = await result.promise;
    check(state, signal);
    return { bitmap, cacheReady, release };
  } catch (error) {
    release();
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
export function loadPointCloudDepthRasters(
  pointCloudUrl: string,
  positions: Float32Array,
  cameras: readonly CameraDepthRasterInput[],
  options: { signal?: AbortSignal; lane?: Lane } = {},
): Promise<LoadedPointCloudDepthRasters> {
  const state = stateForOwner();
  const lane = options.lane ?? "shared";
  const key = JSON.stringify([
    pointCloudUrl,
    positions.length,
    cameras.map((c) => [
      c.width,
      c.height,
      c.calibration.extrinsic,
      c.calibration.intrinsic,
      c.calibration.rect ?? null,
    ]),
  ]);
  const result = request(
    state,
    state.depths,
    key,
    8,
    async (signal) => {
      const rasters = await getPointCloudDepthSession(lane).buildDepthRasters(
        positions,
        cameras.map((c) => ({ calib: c.calibration, width: c.width, height: c.height })),
        { signal, terminateWorkerOnAbort: lane !== "shared" },
      );
      check(state, signal);
      return rasters;
    },
    options.signal,
    lane,
  );
  return result.promise.then((rasters) => {
    check(state, options.signal);
    result.entry.bytes = rasters.reduce((n, r) => n + r.depth.byteLength, 0);
    let total = [...state.depths.values()].reduce((n, e) => n + e.bytes, 0);
    for (const [k, e] of state.depths) {
      if (total <= 8 * 1024 * 1024) break;
      if (!e.ready) continue;
      state.depths.delete(k);
      total -= e.bytes;
    }
    return { rasters, cacheHit: result.cacheHit };
  });
}
export interface PointCloudFramePrefetchOptions {
  decimateThreshold?: number;
  depthRasters?: boolean;
}
export async function prefetchPointCloudFrameAssets(
  manifest: TaskPointCloudManifestResponse,
  options: PointCloudFramePrefetchOptions = {},
): Promise<void> {
  const state = stateForOwner();
  state.prefetch?.abort();
  const controller = new AbortController();
  state.prefetch = controller;
  const signal = controller.signal;
  const handles: AcquiredCameraBitmap[] = [];
  try {
    const framePromise = manifest.point_cloud_url
      ? frameRequest(
          state,
          manifest.point_cloud_url,
          manifest.axis_convention ?? "iso_8855",
          options.decimateThreshold ?? 500_000,
          signal,
          "prefetch",
        ).promise.catch(() => null)
      : Promise.resolve(null);
    const cameraPromise = Promise.all(
      manifest.cameras.map(async (camera) => {
        if (!camera.image_url) return null;
        try {
          if (typeof createImageBitmap === "undefined") {
            await blobRequest(state, camera.image_url, signal, "prefetch").promise;
            return null;
          }
          const handle = await acquireCameraBitmap(camera.image_url, signal, "prefetch");
          handles.push(handle);
          return camera.calibration
            ? {
                calibration: camera.calibration,
                width: handle.bitmap.width,
                height: handle.bitmap.height,
              }
            : null;
        } catch {
          return null;
        }
      }),
    );
    const [frame, cameras] = await Promise.all([framePromise, cameraPromise]);
    check(state, signal);
    const usable = cameras.filter((camera): camera is CameraDepthRasterInput => camera !== null);
    if (
      options.depthRasters &&
      frame &&
      manifest.point_cloud_url &&
      usable.length > 0 &&
      usable.length <= 6
    )
      await loadPointCloudDepthRasters(manifest.point_cloud_url, frame.positions, usable, {
        signal,
        lane: "prefetch",
      });
  } catch (error) {
    if (!(error instanceof DOMException && error.name === "AbortError")) throw error;
  } finally {
    handles.forEach((handle) => handle.release());
    if (state.prefetch === controller) state.prefetch = null;
  }
}
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
export function getPointCloudAssetCacheDiagnostics(): PointCloudAssetCacheDiagnostics {
  const state = stateForOwner();
  return {
    pointCloudBuffers: state.buffers.size,
    decodedFrames: state.frames.size,
    cameraBlobs: state.blobs.size,
    cameraBitmaps: state.bitmaps.size,
    cameraBitmapReferences: [...state.bitmaps.values()].reduce((n, e) => n + e.references, 0),
    depthRasters: state.depths.size,
    depthRasterBytes: [...state.depths.values()].reduce((n, e) => n + e.bytes, 0),
    depthRasterInFlight: [...state.depths.values()].filter((e) => !e.ready).length,
  };
}
if (import.meta.env.DEV && typeof window !== "undefined")
  (
    window as Window & {
      __pointCloudAssetCacheDiagnostics?: typeof getPointCloudAssetCacheDiagnostics;
    }
  ).__pointCloudAssetCacheDiagnostics = getPointCloudAssetCacheDiagnostics;

if (import.meta.hot)
  import.meta.hot.dispose(() => {
    unsubscribe();
    if (current) retire(current);
    current = null;
  });

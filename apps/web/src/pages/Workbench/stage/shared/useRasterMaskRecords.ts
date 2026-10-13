import { useCallback, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { CocoRleMaskRef } from "@/types";
import type { CocoRle } from "./geometry/maskRle";
import { analyzeRasterMaskRleAsync, RasterMaskWorkerError } from "./rasterMaskCompute";
import type { RasterMaskWorkerPool, RasterMaskWorkerPriority } from "./rasterMaskWorkerPool";
import type {
  RasterResourceCoordinator,
  RasterResourcePriority,
  RasterResourceReservation,
} from "./rasterResourceCoordinator";
import {
  closeRasterMaskImage,
  createTintedRasterMaskImage,
  createTintedRasterMaskPreviewImage,
  rasterMaskPreviewDimensions,
  type RasterMaskAnalysis,
  type RasterMaskCroppedImage,
  type RasterMaskNormalizedBounds,
  type RasterMaskRenderRecord,
} from "./rasterMaskRender";

const MIB = 1024 * 1024;
const MAX_PREVIEW_PIXELS = 1024 * 1024;
const RLE_RETAINED_BASE_BYTES = 64;

export type RasterMaskDeviceTier = "low" | "standard" | "high";

export interface RasterMaskDeviceBudget {
  tier: RasterMaskDeviceTier;
  maxCacheBytes: number;
  maxConcurrent: number;
  workerPoolSize: number;
  tileCacheBytes: number;
  historyBytes: number;
}

export function rasterMaskDeviceBudget(deviceMemory?: number | null): RasterMaskDeviceBudget {
  if (
    deviceMemory != null &&
    Number.isFinite(deviceMemory) &&
    deviceMemory > 0 &&
    deviceMemory <= 2
  ) {
    return {
      tier: "low",
      maxCacheBytes: 64 * MIB,
      maxConcurrent: 1,
      workerPoolSize: 1,
      tileCacheBytes: 32 * MIB,
      historyBytes: 16 * MIB,
    };
  }
  if (deviceMemory != null && Number.isFinite(deviceMemory) && deviceMemory >= 8) {
    return {
      tier: "high",
      maxCacheBytes: 192 * MIB,
      maxConcurrent: 4,
      workerPoolSize: 2,
      tileCacheBytes: 128 * MIB,
      historyBytes: 64 * MIB,
    };
  }
  return {
    tier: "standard",
    maxCacheBytes: 128 * MIB,
    maxConcurrent: 2,
    workerPoolSize: 2,
    tileCacheBytes: 64 * MIB,
    historyBytes: 32 * MIB,
  };
}

export function estimateCocoRleRetainedBytes(rle: CocoRle): number {
  return RLE_RETAINED_BASE_BYTES + rle.counts.length * 8;
}

export interface RasterMaskRecordDescriptor<TSource extends string = string> {
  id: string;
  source: TSource;
  /** Immutable content identity; inline AI candidates intentionally have no object_key. */
  ref: Pick<CocoRleMaskRef, "size" | "sha256">;
  /** Annotation version, keyframe revision, or another immutable content revision. */
  revision: string | number;
  color: string;
  /** Must change whenever the rendered category color changes. */
  colorRevision: string | number;
  zOrder: number;
  selected: boolean;
  /** Higher-value work enters the queue first; selection still outranks current/prefetch. */
  loadPriority?: "editing" | "current" | "prefetch";
  /** Disable global request sharing when the loader owns a cancellable lifecycle. */
  deduplicateLoad?: boolean;
  load: () => Promise<CocoRle>;
}

export type RasterMaskLoadErrorReason =
  | "not_found"
  | "forbidden"
  | "corrupt"
  | "unavailable"
  | "invalid_content"
  | "render_failed"
  | "load_failed";

export type RasterMaskRecordStatus =
  | { state: "loading" }
  | {
      state: "ready";
      cacheKey: string;
      area: number;
      componentCount: number;
      holeCount: number;
      boundaryPixelCount: number;
      bounds: RasterMaskNormalizedBounds;
      preview: boolean;
    }
  | {
      state: "deferred";
      reason: "budget_exceeded";
      message: string;
      retryable: true;
      requiredBytes: number;
      budgetBytes: number;
      backendReason?: undefined;
      httpStatus?: undefined;
    }
  | {
      state: "error";
      reason: RasterMaskLoadErrorReason;
      backendReason?: string;
      message: string;
      retryable: boolean;
      httpStatus?: number;
    };

export interface UseRasterMaskRecordsOptions<TSource extends string = string> {
  scopeKey: string | null;
  descriptors: readonly RasterMaskRecordDescriptor<TSource>[];
  maxConcurrent?: number;
  /** Approximate retained crop alpha + bitmap RGBA + canonical RLE budget. */
  maxCacheBytes?: number;
  /** Optional secondary guard for callers that need a strict object-count cap. */
  maxCachedRecords?: number;
  /** Test/SSR override; omitted values read `navigator.deviceMemory`, then fall back to Standard. */
  deviceMemory?: number | null;
  /** Task-scoped shared pool; omitted tests retain the explicit synchronous fallback. */
  workerPool?: RasterMaskWorkerPool;
  /** Task-scoped aggregate resource admission shared with background, editing, and workers. */
  resourceCoordinator?: RasterResourceCoordinator;
  resourceOwner?: string;
}

export interface RasterMaskResourceCounters {
  tier: RasterMaskDeviceTier;
  maxCacheBytes: number;
  maxConcurrent: number;
  liveRecords: number;
  retainedAlphaBytes: number;
  retainedBitmapBytes: number;
  retainedRleBytes: number;
  retainedBytes: number;
  reservedBytes: number;
  queued: number;
  inFlight: number;
  deferred: number;
  bitmapsCreated: number;
  bitmapsClosed: number;
  liveBitmaps: number;
}

export interface UseRasterMaskRecordsResult<TSource extends string = string> {
  records: RasterMaskRenderRecord<TSource>[];
  statusById: ReadonlyMap<string, RasterMaskRecordStatus>;
  cacheBytes: number;
  resources: RasterMaskResourceCounters;
  retry: (id: string) => void;
}

interface CachedRasterMask {
  scopeKey: string | null;
  cacheKey: string;
  analysis: RasterMaskAnalysis;
  rendered: RasterMaskCroppedImage;
  rle: CocoRle;
  preview: boolean;
  alphaBytes: number;
  bitmapBytes: number;
  rleBytes: number;
  byteSize: number;
  resource?: RasterResourceReservation;
}

interface RasterMaskAdmission {
  bytes: number;
  resource?: RasterResourceReservation;
}

interface RasterMaskLoadJob<TSource extends string> {
  scopeGeneration: number;
  cacheKey: string;
  token: number;
  priority: number;
  sequence: number;
  attempt: number;
  descriptor: RasterMaskRecordDescriptor<TSource>;
}

type DeferredRasterMaskStatus = Extract<RasterMaskRecordStatus, { state: "deferred" }> & {
  selected: boolean;
  priority: number;
};

const rleLoadSingleFlights = new Map<
  string,
  { id: string; source: string; promise: Promise<CocoRle> }
>();

class InvalidRasterMaskContentError extends Error {}
class RasterMaskRenderError extends Error {}

export function rasterMaskRecordCacheKey(
  descriptor: Pick<RasterMaskRecordDescriptor, "id" | "revision" | "ref" | "colorRevision">,
): string {
  return JSON.stringify([
    descriptor.id,
    descriptor.revision,
    descriptor.ref.sha256,
    descriptor.colorRevision,
  ]);
}

function normalizePositiveLimit(value: number | undefined, fallback: number): number {
  if (value == null || !Number.isFinite(value)) return fallback;
  return Math.max(1, Math.floor(value));
}

function navigatorDeviceMemory(): number | undefined {
  if (typeof navigator === "undefined") return undefined;
  const value = (navigator as Navigator & { deviceMemory?: unknown }).deviceMemory;
  return typeof value === "number" ? value : undefined;
}

function descriptorPriority(descriptor: RasterMaskRecordDescriptor): number {
  if (descriptor.loadPriority === "editing") return 0;
  if (descriptor.selected) return 1;
  if (descriptor.loadPriority === "prefetch") return 3;
  return 2;
}

function descriptorWorkerPriority(
  descriptor: RasterMaskRecordDescriptor,
): RasterMaskWorkerPriority {
  if (descriptor.loadPriority === "editing") return "editing";
  if (descriptor.selected) return "selected";
  if (descriptor.loadPriority === "prefetch") return "prefetch";
  return "current";
}

function descriptorResourcePriority(
  descriptor: RasterMaskRecordDescriptor,
): RasterResourcePriority {
  if (descriptor.loadPriority === "editing" || descriptor.selected) return 1;
  if (descriptor.loadPriority === "prefetch") return 4;
  return 2;
}

function loadRleSingleFlight(descriptor: RasterMaskRecordDescriptor): Promise<CocoRle> {
  if (descriptor.deduplicateLoad === false) return descriptor.load();
  const key = descriptor.ref.sha256;
  const existing = rleLoadSingleFlights.get(key);
  if (existing) {
    if (existing.id === descriptor.id && existing.source === descriptor.source)
      return existing.promise;
    // Equal content can share a successful load, but authorization/existence errors
    // belong to the requested object. Confirm a different source using its own endpoint.
    return existing.promise.catch(() => descriptor.load());
  }
  let request: Promise<CocoRle>;
  try {
    request = descriptor.load();
  } catch (error) {
    request = Promise.reject(error);
  }
  rleLoadSingleFlights.set(key, { id: descriptor.id, source: descriptor.source, promise: request });
  void request.then(
    () => {
      if (rleLoadSingleFlights.get(key)?.promise === request) rleLoadSingleFlights.delete(key);
    },
    () => {
      if (rleLoadSingleFlights.get(key)?.promise === request) rleLoadSingleFlights.delete(key);
    },
  );
  return request;
}

function isImageBitmap(image: CanvasImageSource): boolean {
  return typeof ImageBitmap !== "undefined" && image instanceof ImageBitmap;
}

function httpStatusOf(error: unknown): number | undefined {
  if (!error || typeof error !== "object" || !("status" in error)) return undefined;
  const status = (error as { status?: unknown }).status;
  return typeof status === "number" && Number.isInteger(status) ? status : undefined;
}

function detailOf(error: unknown): Record<string, unknown> | undefined {
  if (!error || typeof error !== "object" || !("detailRaw" in error)) return undefined;
  const detail = (error as { detailRaw?: unknown }).detailRaw;
  return detail && typeof detail === "object" ? (detail as Record<string, unknown>) : undefined;
}

export function rasterMaskLoadError(
  error: unknown,
): Extract<RasterMaskRecordStatus, { state: "error" }> {
  if (error instanceof InvalidRasterMaskContentError) {
    return {
      state: "error",
      reason: "invalid_content",
      message: "Mask 内容无效或尺寸不一致",
      retryable: false,
    };
  }
  if (error instanceof RasterMaskRenderError) {
    return {
      state: "error",
      reason: "render_failed",
      message: "Mask 图像构建失败",
      retryable: true,
    };
  }
  const httpStatus = httpStatusOf(error);
  if (httpStatus === 401 || httpStatus === 403) {
    return {
      state: "error",
      reason: "forbidden",
      message: "无权读取 Mask 内容",
      retryable: false,
      httpStatus,
    };
  }
  if (httpStatus === 404) {
    return {
      state: "error",
      reason: "not_found",
      message: "Mask 内容不存在",
      retryable: false,
      httpStatus,
    };
  }
  if (httpStatus === 409) {
    const detail = detailOf(error);
    const backendReason = typeof detail?.reason === "string" ? detail.reason : undefined;
    return {
      state: "error",
      reason: "corrupt",
      ...(backendReason == null ? {} : { backendReason }),
      message:
        typeof detail?.message === "string" ? detail.message : "Mask 内容已损坏或与引用不一致",
      retryable: detail?.retryable === true,
      httpStatus,
    };
  }
  if (httpStatus === 429 || (httpStatus != null && httpStatus >= 500)) {
    return {
      state: "error",
      reason: "unavailable",
      message: "Mask 内容暂时不可用",
      retryable: true,
      httpStatus,
    };
  }
  return {
    state: "error",
    reason: "load_failed",
    message: "Mask 内容加载失败",
    retryable: true,
    ...(httpStatus == null ? {} : { httpStatus }),
  };
}

function toRenderRecord<TSource extends string>(
  descriptor: RasterMaskRecordDescriptor<TSource>,
  cached: CachedRasterMask,
): RasterMaskRenderRecord<TSource> {
  return {
    id: descriptor.id,
    source: descriptor.source,
    image: cached.rendered.image,
    sourceWidth: cached.analysis.sourceWidth,
    sourceHeight: cached.analysis.sourceHeight,
    crop: cached.analysis.crop,
    bounds: cached.analysis.bounds,
    area: cached.analysis.area,
    componentCount: cached.analysis.componentCount,
    holeCount: cached.analysis.holeCount,
    boundaryPixelCount: cached.analysis.boundaryPixelCount,
    zOrder: descriptor.zOrder,
    selected: descriptor.selected,
    cacheKey: cached.cacheKey,
    ...(cached.preview ? { rle: cached.rle } : {}),
    preview: cached.preview,
  };
}

export function useRasterMaskRecords<TSource extends string = string>(
  options: UseRasterMaskRecordsOptions<TSource>,
): UseRasterMaskRecordsResult<TSource> {
  const { scopeKey, descriptors, workerPool, resourceCoordinator } = options;
  const resourceOwner = options.resourceOwner ?? "mask-render";
  const deviceBudget = rasterMaskDeviceBudget(
    options.deviceMemory === undefined ? navigatorDeviceMemory() : options.deviceMemory,
  );
  const maxConcurrent = normalizePositiveLimit(options.maxConcurrent, deviceBudget.maxConcurrent);
  const maxCacheBytes = normalizePositiveLimit(options.maxCacheBytes, deviceBudget.maxCacheBytes);
  const maxCachedRecords =
    options.maxCachedRecords == null
      ? Number.MAX_SAFE_INTEGER
      : normalizePositiveLimit(options.maxCachedRecords, Number.MAX_SAFE_INTEGER);
  const [, forceRender] = useState(0);
  const mountedRef = useRef(false);
  const scopeRef = useRef(scopeKey);
  const scopeGenerationRef = useRef(0);
  const tokenRef = useRef(0);
  const queueSequenceRef = useRef(0);
  const cacheRef = useRef(new Map<string, CachedRasterMask>());
  const errorsRef = useRef(new Map<string, Extract<RasterMaskRecordStatus, { state: "error" }>>());
  const deferredRef = useRef(new Map<string, DeferredRasterMaskStatus>());
  const activeDescriptorsRef = useRef(new Map<string, RasterMaskRecordDescriptor<TSource>>());
  const descriptorsByIdRef = useRef(new Map<string, RasterMaskRecordDescriptor<TSource>>());
  const requestTokensRef = useRef(new Map<string, number>());
  const queueRef = useRef<RasterMaskLoadJob<TSource>[]>([]);
  const reservationsRef = useRef(new Map<string, RasterMaskAdmission>());
  const inFlightRef = useRef(0);
  const maxConcurrentRef = useRef(maxConcurrent);
  const maxCacheBytesRef = useRef(maxCacheBytes);
  const maxCachedRecordsRef = useRef(maxCachedRecords);
  const pumpRef = useRef<() => void>(() => undefined);
  const disposedImagesRef = useRef(new WeakSet<object>());
  const bitmapsCreatedRef = useRef(0);
  const bitmapsClosedRef = useRef(0);

  maxConcurrentRef.current = maxConcurrent;
  maxCacheBytesRef.current = maxCacheBytes;
  maxCachedRecordsRef.current = maxCachedRecords;

  const descriptorState = useMemo(() => {
    const byId = new Map<string, RasterMaskRecordDescriptor<TSource>>();
    const entries = descriptors.map((descriptor) => {
      if (byId.has(descriptor.id)) {
        throw new Error(`duplicate raster mask descriptor id: ${descriptor.id}`);
      }
      byId.set(descriptor.id, descriptor);
      return [rasterMaskRecordCacheKey(descriptor), descriptor] as const;
    });
    return { byId, entries };
  }, [descriptors]);
  descriptorsByIdRef.current = descriptorState.byId;
  const descriptorEntries = descriptorState.entries;

  const publish = useCallback(() => {
    if (mountedRef.current) forceRender((value) => value + 1);
  }, []);

  const disposeImage = useCallback((image: CanvasImageSource) => {
    const object = image as object;
    if (disposedImagesRef.current.has(object)) return;
    disposedImagesRef.current.add(object);
    if (isImageBitmap(image)) bitmapsClosedRef.current += 1;
    closeRasterMaskImage(image);
  }, []);

  const trackImage = useCallback((image: CanvasImageSource) => {
    if (isImageBitmap(image)) bitmapsCreatedRef.current += 1;
  }, []);

  const disposeCached = useCallback(
    (cached: CachedRasterMask) => {
      cached.resource?.release();
      disposeImage(cached.rendered.image);
    },
    [disposeImage],
  );

  const cacheBytesNow = useCallback(() => {
    let total = 0;
    for (const cached of cacheRef.current.values()) total += cached.byteSize;
    return total;
  }, []);

  const reservedBytesNow = useCallback(() => {
    let total = 0;
    for (const reservation of reservationsRef.current.values()) total += reservation.bytes;
    return total;
  }, []);

  const releaseReservation = useCallback((cacheKey: string) => {
    const reservation = reservationsRef.current.get(cacheKey);
    if (!reservation) return;
    reservationsRef.current.delete(cacheKey);
    reservation.resource?.release();
  }, []);

  const markDeferred = useCallback((cacheKey: string, requiredBytes: number) => {
    const descriptor = activeDescriptorsRef.current.get(cacheKey);
    if (!descriptor) return;
    deferredRef.current.set(cacheKey, {
      state: "deferred",
      reason: "budget_exceeded",
      message:
        requiredBytes > maxCacheBytesRef.current
          ? "Mask 即使使用受限预览仍超过当前设备缓存预算"
          : "Mask 已因当前缓存预算延后；选中对象或释放缓存后可重试",
      retryable: true,
      requiredBytes,
      budgetBytes: maxCacheBytesRef.current,
      selected: descriptor.selected,
      priority: descriptorPriority(descriptor),
    });
  }, []);

  const removeCached = useCallback(
    (cacheKey: string, deferActive: boolean) => {
      const cached = cacheRef.current.get(cacheKey);
      if (!cached) return;
      cacheRef.current.delete(cacheKey);
      disposeCached(cached);
      if (deferActive) markDeferred(cacheKey, cached.byteSize);
    },
    [disposeCached, markDeferred],
  );

  const pinnedCacheKeys = useCallback(() => {
    const pinned = new Set<string>();
    for (const [cacheKey, descriptor] of activeDescriptorsRef.current) {
      if (descriptor.selected || descriptor.loadPriority === "editing") pinned.add(cacheKey);
    }
    return pinned;
  }, []);

  const reserveAdmission = useCallback(
    (cacheKey: string, byteSize: number): boolean => {
      if (byteSize > maxCacheBytesRef.current) return false;
      const cache = cacheRef.current;
      const pinned = pinnedCacheKeys();
      const evictKeys: string[] = [];
      let projectedRecords = cache.size + reservationsRef.current.size + 1;
      let projectedBytes = cacheBytesNow() + reservedBytesNow() + byteSize;
      for (const [candidate, cached] of cache) {
        if (
          projectedRecords <= maxCachedRecordsRef.current &&
          projectedBytes <= maxCacheBytesRef.current
        )
          break;
        if (pinned.has(candidate)) continue;
        evictKeys.push(candidate);
        projectedRecords -= 1;
        projectedBytes -= cached.byteSize;
      }
      if (
        projectedRecords > maxCachedRecordsRef.current ||
        projectedBytes > maxCacheBytesRef.current
      )
        return false;
      for (const candidate of evictKeys) removeCached(candidate, true);
      const descriptor = activeDescriptorsRef.current.get(cacheKey);
      const resource =
        descriptor && resourceCoordinator
          ? resourceCoordinator.tryReserve({
              owner: resourceOwner,
              category: "mask-render",
              priority: descriptorResourcePriority(descriptor),
              bytes: byteSize,
              reconstructible: true,
              pinned: descriptor.selected || descriptor.loadPriority === "editing",
            })
          : undefined;
      if (resourceCoordinator && !resource) return false;
      reservationsRef.current.set(cacheKey, { bytes: byteSize, ...(resource ? { resource } : {}) });
      return true;
    },
    [
      cacheBytesNow,
      pinnedCacheKeys,
      removeCached,
      reservedBytesNow,
      resourceCoordinator,
      resourceOwner,
    ],
  );

  const enforceBudget = useCallback(() => {
    const cache = cacheRef.current;
    const pinned = pinnedCacheKeys();
    while (
      cache.size + reservationsRef.current.size > maxCachedRecordsRef.current ||
      cacheBytesNow() + reservedBytesNow() > maxCacheBytesRef.current
    ) {
      const evictKey =
        [...cache.keys()].find((candidate) => !pinned.has(candidate)) ??
        (cache.keys().next().value as string | undefined);
      if (!evictKey) break;
      removeCached(evictKey, true);
    }
  }, [cacheBytesNow, pinnedCacheKeys, removeCached, reservedBytesNow]);

  const isCurrentJob = useCallback(
    (job: RasterMaskLoadJob<TSource>) =>
      mountedRef.current &&
      job.scopeGeneration === scopeGenerationRef.current &&
      requestTokensRef.current.get(job.cacheKey) === job.token &&
      activeDescriptorsRef.current.has(job.cacheKey),
    [],
  );

  const startJob = useCallback(
    (job: RasterMaskLoadJob<TSource>) => {
      inFlightRef.current += 1;
      void (async () => {
        let rendered: RasterMaskCroppedImage | null = null;
        let reservationHeld = false;
        try {
          const rle = await loadRleSingleFlight(job.descriptor);
          if (
            rle.size[0] !== job.descriptor.ref.size[0] ||
            rle.size[1] !== job.descriptor.ref.size[1]
          ) {
            throw new InvalidRasterMaskContentError(
              "mask content size does not match its reference",
            );
          }
          let analysis: RasterMaskAnalysis;
          try {
            analysis = await analyzeRasterMaskRleAsync(rle, {
              ...(workerPool ? { pool: workerPool } : {}),
              priority: descriptorWorkerPriority(job.descriptor),
            });
            if (analysis.area === 0) {
              throw new InvalidRasterMaskContentError("mask content has no foreground pixels");
            }
          } catch (error) {
            if (error instanceof InvalidRasterMaskContentError) throw error;
            if (error instanceof RasterMaskWorkerError)
              throw new RasterMaskRenderError(error.message);
            throw new InvalidRasterMaskContentError(String(error));
          }
          const rleBytes = estimateCocoRleRetainedBytes(rle);
          const fullAlphaBytes = analysis.crop.alpha.byteLength;
          const fullBitmapBytes = analysis.crop.width * analysis.crop.height * 4;
          const fullBytes = rleBytes + fullAlphaBytes + fullBitmapBytes;
          let preview = false;
          let retainedBytes = fullBytes;
          if (reserveAdmission(job.cacheKey, fullBytes)) {
            reservationHeld = true;
          } else {
            preview = true;
            const pinned = pinnedCacheKeys();
            let protectedBytes = reservedBytesNow();
            for (const [cacheKey, cached] of cacheRef.current) {
              if (pinned.has(cacheKey)) protectedBytes += cached.byteSize;
            }
            const previewPixelBudget = Math.min(
              MAX_PREVIEW_PIXELS,
              Math.floor((maxCacheBytesRef.current - protectedBytes - rleBytes) / 4),
            );
            const previewDimensions = rasterMaskPreviewDimensions(
              analysis.crop.width,
              analysis.crop.height,
              previewPixelBudget,
            );
            retainedBytes = rleBytes + previewDimensions.width * previewDimensions.height * 4;
            if (
              previewDimensions.width === 0 ||
              previewDimensions.height === 0 ||
              !reserveAdmission(job.cacheKey, retainedBytes)
            ) {
              errorsRef.current.delete(job.cacheKey);
              markDeferred(job.cacheKey, Math.max(retainedBytes, rleBytes + 4));
              publish();
              return;
            }
            reservationHeld = true;
          }
          try {
            rendered = preview
              ? await createTintedRasterMaskPreviewImage(
                  analysis,
                  job.descriptor.color,
                  Math.min(MAX_PREVIEW_PIXELS, Math.floor((retainedBytes - rleBytes) / 4)),
                )
              : await createTintedRasterMaskImage(analysis, job.descriptor.color);
          } catch (error) {
            throw new RasterMaskRenderError(String(error));
          }
          if (!rendered) {
            throw new InvalidRasterMaskContentError("mask content has no renderable foreground");
          }
          trackImage(rendered.image);
          if (!isCurrentJob(job)) {
            disposeImage(rendered.image);
            rendered = null;
            return;
          }
          const previous = cacheRef.current.get(job.cacheKey);
          if (previous) disposeCached(previous);
          const retainedAnalysis = preview
            ? {
                ...analysis,
                crop: { ...analysis.crop, alpha: new Uint8Array() },
              }
            : analysis;
          const alphaBytes = retainedAnalysis.crop.alpha.byteLength;
          const bitmapBytes = rendered.width * rendered.height * 4;
          const cached: CachedRasterMask = {
            scopeKey: scopeRef.current,
            cacheKey: job.cacheKey,
            analysis: retainedAnalysis,
            rendered,
            rle,
            preview,
            alphaBytes,
            bitmapBytes,
            rleBytes,
            byteSize: alphaBytes + bitmapBytes + rleBytes,
          };
          const admission = reservationsRef.current.get(job.cacheKey);
          if (admission?.resource && !admission.resource.commit(cached.byteSize)) {
            releaseReservation(job.cacheKey);
            disposeImage(rendered.image);
            rendered = null;
            markDeferred(job.cacheKey, cached.byteSize);
            publish();
            return;
          }
          if (admission?.resource) cached.resource = admission.resource;
          rendered = null;
          reservationsRef.current.delete(job.cacheKey);
          reservationHeld = false;
          cacheRef.current.delete(job.cacheKey);
          cacheRef.current.set(job.cacheKey, cached);
          errorsRef.current.delete(job.cacheKey);
          deferredRef.current.delete(job.cacheKey);
          publish();
        } catch (error) {
          if (rendered) disposeImage(rendered.image);
          if (isCurrentJob(job)) {
            const status = rasterMaskLoadError(error);
            if (status.reason === "render_failed" && job.attempt < 1) {
              const descriptor = activeDescriptorsRef.current.get(job.cacheKey) ?? job.descriptor;
              const token = ++tokenRef.current;
              requestTokensRef.current.set(job.cacheKey, token);
              queueRef.current.push({
                ...job,
                token,
                attempt: job.attempt + 1,
                sequence: ++queueSequenceRef.current,
                descriptor,
              });
              queueRef.current.sort(
                (left, right) => left.priority - right.priority || left.sequence - right.sequence,
              );
            } else {
              errorsRef.current.set(job.cacheKey, status);
            }
            publish();
          }
        } finally {
          if (reservationHeld) releaseReservation(job.cacheKey);
          if (requestTokensRef.current.get(job.cacheKey) === job.token) {
            requestTokensRef.current.delete(job.cacheKey);
          }
          inFlightRef.current -= 1;
          publish();
          pumpRef.current();
        }
      })();
    },
    [
      disposeCached,
      disposeImage,
      isCurrentJob,
      markDeferred,
      pinnedCacheKeys,
      publish,
      reserveAdmission,
      reservedBytesNow,
      releaseReservation,
      trackImage,
      workerPool,
    ],
  );

  pumpRef.current = () => {
    while (
      mountedRef.current &&
      inFlightRef.current < maxConcurrentRef.current &&
      queueRef.current.length > 0
    ) {
      const job = queueRef.current.shift();
      if (!job || !isCurrentJob(job)) continue;
      startJob(job);
    }
  };

  const enqueue = useCallback((descriptor: RasterMaskRecordDescriptor<TSource>) => {
    const cacheKey = rasterMaskRecordCacheKey(descriptor);
    if (
      cacheRef.current.get(cacheKey)?.scopeKey === scopeRef.current ||
      requestTokensRef.current.has(cacheKey)
    ) {
      return;
    }
    const token = ++tokenRef.current;
    requestTokensRef.current.set(cacheKey, token);
    queueRef.current.push({
      scopeGeneration: scopeGenerationRef.current,
      cacheKey,
      token,
      priority: descriptorPriority(descriptor),
      sequence: ++queueSequenceRef.current,
      attempt: 0,
      descriptor,
    });
    queueRef.current.sort(
      (left, right) => left.priority - right.priority || left.sequence - right.sequence,
    );
  }, []);

  useLayoutEffect(() => {
    const activeDescriptors = activeDescriptorsRef;
    const requestTokens = requestTokensRef.current;
    const errors = errorsRef.current;
    const deferred = deferredRef.current;
    const cache = cacheRef.current;
    const reservations = reservationsRef.current;
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      scopeGenerationRef.current += 1;
      activeDescriptors.current.clear();
      requestTokens.clear();
      queueRef.current = [];
      for (const reservation of reservations.values()) reservation.resource?.release();
      reservations.clear();
      errors.clear();
      deferred.clear();
      for (const cached of cache.values()) disposeCached(cached);
      cache.clear();
    };
  }, [disposeCached]);

  useLayoutEffect(() => {
    const scopeChanged = scopeRef.current !== scopeKey;
    if (scopeChanged) {
      scopeRef.current = scopeKey;
      scopeGenerationRef.current += 1;
      requestTokensRef.current.clear();
      queueRef.current = [];
      for (const reservation of reservationsRef.current.values()) reservation.resource?.release();
      reservationsRef.current.clear();
      errorsRef.current.clear();
      deferredRef.current.clear();
      for (const cached of cacheRef.current.values()) disposeCached(cached);
      cacheRef.current.clear();
    }

    const activeDescriptors = new Map(descriptorEntries);
    activeDescriptorsRef.current = activeDescriptors;
    for (const cacheKey of [...requestTokensRef.current.keys()]) {
      if (!activeDescriptors.has(cacheKey)) requestTokensRef.current.delete(cacheKey);
    }
    queueRef.current = queueRef.current
      .filter((job) => activeDescriptors.has(job.cacheKey))
      .map((job) => {
        const descriptor = activeDescriptors.get(job.cacheKey) ?? job.descriptor;
        return { ...job, descriptor, priority: descriptorPriority(descriptor) };
      })
      .sort((left, right) => left.priority - right.priority || left.sequence - right.sequence);
    for (const cacheKey of [...errorsRef.current.keys()]) {
      if (!activeDescriptors.has(cacheKey)) errorsRef.current.delete(cacheKey);
    }
    for (const cacheKey of [...deferredRef.current.keys()]) {
      if (!activeDescriptors.has(cacheKey)) deferredRef.current.delete(cacheKey);
    }

    for (const [cacheKey] of descriptorEntries) {
      const cached = cacheRef.current.get(cacheKey);
      if (cached?.scopeKey === scopeKey) {
        const descriptor = activeDescriptors.get(cacheKey);
        if (descriptor) {
          cached.resource?.update({
            priority: descriptorResourcePriority(descriptor),
            pinned: descriptor.selected || descriptor.loadPriority === "editing",
          });
        }
        cacheRef.current.delete(cacheKey);
        cacheRef.current.set(cacheKey, cached);
      }
    }
    enforceBudget();
    const prioritizedEntries = [...descriptorEntries].sort(
      (left, right) => descriptorPriority(left[1]) - descriptorPriority(right[1]),
    );
    for (const [cacheKey, descriptor] of prioritizedEntries) {
      if (cacheRef.current.get(cacheKey)?.scopeKey === scopeKey) continue;
      const deferred = deferredRef.current.get(cacheKey);
      if (deferred) {
        const priority = descriptorPriority(descriptor);
        const shouldRetry =
          deferred.budgetBytes !== maxCacheBytesRef.current ||
          (descriptor.selected && !deferred.selected) ||
          priority < deferred.priority;
        if (!shouldRetry) continue;
        deferredRef.current.delete(cacheKey);
      }
      if (!errorsRef.current.has(cacheKey)) enqueue(descriptor);
    }
    pumpRef.current();
    if (scopeChanged) queueMicrotask(publish);
  }, [
    descriptorEntries,
    disposeCached,
    enforceBudget,
    enqueue,
    maxCacheBytes,
    maxCachedRecords,
    maxConcurrent,
    publish,
    scopeKey,
  ]);

  useLayoutEffect(() => {
    if (!resourceCoordinator) return;
    return resourceCoordinator.registerEvictor({
      owner: resourceOwner,
      evictableBytes: () => {
        const pinned = pinnedCacheKeys();
        let bytes = 0;
        for (const [cacheKey, cached] of cacheRef.current) {
          if (!pinned.has(cacheKey)) bytes += cached.byteSize;
        }
        return bytes;
      },
      evict: (targetBytes, reason) => {
        const pinned = pinnedCacheKeys();
        let freed = 0;
        for (const [cacheKey, cached] of [...cacheRef.current]) {
          if (reason !== "bfcache" && pinned.has(cacheKey)) continue;
          removeCached(cacheKey, true);
          freed += cached.byteSize;
          if (freed >= targetBytes) break;
        }
        if (freed > 0) publish();
        return freed;
      },
      resumePrefetch: (generation) => {
        if (generation !== resourceCoordinator.generation) return;
        const active = [...activeDescriptorsRef.current.values()].sort(
          (left, right) => descriptorPriority(left) - descriptorPriority(right),
        );
        for (const descriptor of active) {
          if (descriptor.loadPriority === "prefetch") continue;
          const cacheKey = rasterMaskRecordCacheKey(descriptor);
          if (cacheRef.current.has(cacheKey) || requestTokensRef.current.has(cacheKey)) continue;
          deferredRef.current.delete(cacheKey);
          if (!errorsRef.current.has(cacheKey)) enqueue(descriptor);
        }
        pumpRef.current();
        publish();
      },
    });
  }, [enqueue, pinnedCacheKeys, publish, removeCached, resourceCoordinator, resourceOwner]);

  const retry = useCallback(
    (id: string) => {
      const descriptor = descriptorsByIdRef.current.get(id);
      if (!descriptor) return;
      const cacheKey = rasterMaskRecordCacheKey(descriptor);
      const error = errorsRef.current.get(cacheKey);
      const deferred = deferredRef.current.get(cacheKey);
      if ((!error || !error.retryable) && !deferred) return;
      errorsRef.current.delete(cacheKey);
      deferredRef.current.delete(cacheKey);
      enqueue(descriptor);
      publish();
      pumpRef.current();
    },
    [enqueue, publish],
  );

  const records: RasterMaskRenderRecord<TSource>[] = [];
  const statusById = new Map<string, RasterMaskRecordStatus>();
  for (const [cacheKey, descriptor] of descriptorEntries) {
    const cached = cacheRef.current.get(cacheKey);
    if (cached?.scopeKey === scopeKey) {
      records.push(toRenderRecord(descriptor, cached));
      statusById.set(descriptor.id, {
        state: "ready",
        cacheKey,
        area: cached.analysis.area,
        componentCount: cached.analysis.componentCount,
        holeCount: cached.analysis.holeCount,
        boundaryPixelCount: cached.analysis.boundaryPixelCount,
        bounds: cached.analysis.bounds,
        preview: cached.preview,
      });
      continue;
    }
    const error = scopeRef.current === scopeKey ? errorsRef.current.get(cacheKey) : undefined;
    const deferred = scopeRef.current === scopeKey ? deferredRef.current.get(cacheKey) : undefined;
    if (deferred) {
      const { selected: _selected, priority: _priority, ...status } = deferred;
      statusById.set(descriptor.id, status);
    } else {
      statusById.set(descriptor.id, error ?? { state: "loading" });
    }
  }

  let cacheBytes = 0;
  let retainedAlphaBytes = 0;
  let retainedBitmapBytes = 0;
  let retainedRleBytes = 0;
  for (const cached of cacheRef.current.values()) {
    cacheBytes += cached.byteSize;
    retainedAlphaBytes += cached.alphaBytes;
    retainedBitmapBytes += cached.bitmapBytes;
    retainedRleBytes += cached.rleBytes;
  }
  const resources: RasterMaskResourceCounters = {
    tier: deviceBudget.tier,
    maxCacheBytes,
    maxConcurrent,
    liveRecords: cacheRef.current.size,
    retainedAlphaBytes,
    retainedBitmapBytes,
    retainedRleBytes,
    retainedBytes: cacheBytes,
    reservedBytes: reservedBytesNow(),
    queued: queueRef.current.length,
    inFlight: inFlightRef.current,
    deferred: deferredRef.current.size,
    bitmapsCreated: bitmapsCreatedRef.current,
    bitmapsClosed: bitmapsClosedRef.current,
    liveBitmaps: bitmapsCreatedRef.current - bitmapsClosedRef.current,
  };
  return { records, statusById, cacheBytes, resources, retry };
}

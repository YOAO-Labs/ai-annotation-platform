import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { tasksApi } from "@/api/tasks";
import { videoApi } from "@/api/videos";
import type { VideoChunkOut, VideoChunkSamplesResponse, VideoManifestV2Response } from "@/types";
import {
  useVideoChunkDecoder,
  detectWebCodecsSupport,
  resolveVideoChunkDecoderEnabled,
} from "./useVideoChunkDecoder";
import {
  assertVideoDerivativeOwner,
  assertVideoSamplesIdentity,
  isVideoGeneration,
  useVideoDerivativeOwnerKey,
  videoDerivativeKey,
  verifyVideoChunkBytes,
  retryPreparingVideoMetadata,
  retryTransientVideoDerivative,
  transientVideoDerivativeRetryDelay,
  videoMetadataRetryDelay,
} from "@/api/videoDerivatives";
import { ByteLru } from "./videoByteLru";
import { buildGopPlan, type PreciseFrameFallbackReason } from "./videoChunkDemux";
import type { CachedVideoBitmap } from "./useVideoBitmapCache";

export interface UseVideoPreciseFrameArgs {
  taskId: string | null | undefined;
  metadataGenerationId: string | null | undefined;
  frameIndex: number;
  enabled: boolean;
  decodeEnabled?: boolean;
  bitmapBudgetBytes: number;
  chunkBudgetBytes: number;
  prefetchFrames: number;
}

export type PreciseFrameSourceState =
  | "disabled"
  | "idle"
  | "chunk-pending"
  | "loading"
  | "decoding"
  | "ready"
  | "fallback";

export interface VideoPreciseFrameDiagnostics {
  supported: boolean;
  webcodecsEnabled: boolean;
  decoderActive: boolean;
  chunkId: number | null;
  datasetItemId: string | null;
  chunkSizeFrames: number | null;
  decodeRequests: number;
  decoderErrors: number;
  urlRefreshed: boolean;
}

export interface VideoPreciseFramePerformanceDiagnostics {
  manifestCacheHits: number;
  samplesCacheHits: number;
  chunkByteCacheHits: number;
  bitmapCacheHits: number;
  bytesFetched: number;
  bitmapBytes: number;
  bitmapBudgetBytes: number;
  activeDecoders: number;
  liveVideoFrames: number;
  chunkBytes: number;
  chunkBudgetBytes: number;
  sessionCreates: number;
  sessionResets: number;
  sessionDisposals: number;
  encodedChunksSubmitted: number;
  staleResults: number;
  prefetchRequests: number;
  prefetchHits: number;
  evictions: number;
  lastManifestMs: number | null;
  lastSamplesMs: number | null;
  lastChunkFetchMs: number | null;
  lastDemuxMs: number | null;
  lastQueueMs: number | null;
  lastCodecMs: number | null;
  lastDecodeMs: number | null;
  lastBitmapMs: number | null;
  lastDecodeMode: "bitmap-cache" | "supplemental-cache" | "session" | null;
  lastPaintMs: number | null;
  lastVisibleMs: number | null;
  paintedFrameIndex: number | null;
  gopStartDecodeIndex: number | null;
  targetTimestampUs: number | null;
  codec: string | null;
}

export interface UseVideoPreciseFrameResult {
  active: boolean;
  bitmap: CachedVideoBitmap | null;
  sourceState: PreciseFrameSourceState;
  fallbackReason: PreciseFrameFallbackReason | null;
  diagnostics: VideoPreciseFrameDiagnostics;
  performance: VideoPreciseFramePerformanceDiagnostics;
  markFramePainted: (frameIndex: number) => void;
}

const BYTES_GC_MS = 60_000;
const MANIFEST_STALE_MS = 30_000;
const MIN_POLL_MS = 1000;
const MAX_POLL_MS = 10_000;

interface ChunkBytesSource {
  targetKey: string;
  url: string;
  refresh: number;
  identityKey: string;
  identity: VideoChunkOut;
}

interface ChunkBytesCacheEntry {
  buffer: ArrayBuffer;
  expiresAt: number;
}

const QUERY_FALLBACK_REASONS: readonly PreciseFrameFallbackReason[] = [
  "api_unavailable",
  "chunk_failed",
  "samples_unavailable",
  "chunk_fetch_failed",
];

export function retryAfterSecondsToMs(seconds: number | null | undefined): number {
  if (!seconds || !Number.isFinite(seconds) || seconds <= 0) return MIN_POLL_MS;
  return Math.min(MAX_POLL_MS, Math.max(MIN_POLL_MS, Math.round(seconds * 1000)));
}

function errorStatus(err: unknown): number | undefined {
  if (err && typeof err === "object" && "status" in err) {
    const v = (err as { status?: unknown }).status;
    return typeof v === "number" ? v : undefined;
  }
  return undefined;
}

export function useVideoPreciseFrame({
  taskId,
  metadataGenerationId,
  frameIndex,
  enabled,
  decodeEnabled = enabled,
  bitmapBudgetBytes,
  chunkBudgetBytes,
  prefetchFrames,
}: UseVideoPreciseFrameArgs): UseVideoPreciseFrameResult {
  const ownerKey = useVideoDerivativeOwnerKey();
  const decoderEnabled = resolveVideoChunkDecoderEnabled();
  const active = detectWebCodecsSupport() && decoderEnabled;
  const pipelineEnabled =
    active && !!taskId && !!ownerKey && isVideoGeneration(metadataGenerationId) && enabled;
  const canDecode = pipelineEnabled && decodeEnabled;

  const latestRef = useRef({
    taskId,
    ownerKey,
    metadataGenerationId,
    frameIndex,
    enabled: canDecode,
  });
  latestRef.current = { taskId, ownerKey, metadataGenerationId, frameIndex, enabled: canDecode };
  const generationRef = useRef(0);
  const lastFrameIndexRef = useRef<number | null>(null);
  const directionRef = useRef(0);
  const prefetchedFramesRef = useRef<Set<number>>(new Set());
  const [planMeta, setPlanMeta] = useState<{
    taskId: string | null | undefined;
    frameIndex: number | null;
    gopStartDecodeIndex: number | null;
    targetTimestampUs: number | null;
    codec: string | null;
  }>({
    taskId: null,
    frameIndex: null,
    gopStartDecodeIndex: null,
    targetTimestampUs: null,
    codec: null,
  });
  const chunkBytesCacheRef = useRef(new ByteLru<string, ChunkBytesCacheEntry>(chunkBudgetBytes));
  const chunkExpiryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const expireChunkBytesRef = useRef<() => void>(() => undefined);
  const [prefetchRequests, setPrefetchRequests] = useState(0);
  const [prefetchHits, setPrefetchHits] = useState(0);
  const [bytesFetched, setBytesFetched] = useState(0);
  const [chunkByteCacheHits, setChunkByteCacheHits] = useState(0);
  const [chunkCacheVersion, setChunkCacheVersion] = useState(0);
  const [lastChunkFetchMs, setLastChunkFetchMs] = useState<number | null>(null);
  const [lastDemuxMs, setLastDemuxMs] = useState<number | null>(null);
  const [lastPaintMs, setLastPaintMs] = useState<number | null>(null);
  const [lastVisibleMs, setLastVisibleMs] = useState<number | null>(null);
  const [paintedFrameIndex, setPaintedFrameIndex] = useState<number | null>(null);
  const visibleTimingRef = useRef<{
    taskId: string | null | undefined;
    frameIndex: number;
    startedAt: number;
    readyAt: number | null;
  } | null>(null);

  const [decoding, setDecoding] = useState(false);
  const [fallbackReason, setFallbackReason] = useState<PreciseFrameFallbackReason | null>(null);
  const [urlRefreshed, setUrlRefreshed] = useState(false);
  const refreshedKeyRef = useRef<string | null>(null);
  const refreshingUrlKeyRef = useRef<string | null>(null);
  const [refreshingUrlKey, setRefreshingUrlKey] = useState<string | null>(null);
  const [bytesSource, setBytesSource] = useState<ChunkBytesSource | null>(null);
  const latestTargetKeyRef = useRef<string | null>(null);

  const expireChunkBytes = useCallback(() => {
    chunkExpiryTimerRef.current = null;
    const cache = chunkBytesCacheRef.current;
    const now = Date.now();
    let nextExpiry = Number.POSITIVE_INFINITY;
    let removed = false;
    for (const [key, entry] of cache.entries()) {
      if (entry.expiresAt <= now) {
        cache.delete(key);
        removed = true;
      } else {
        nextExpiry = Math.min(nextExpiry, entry.expiresAt);
      }
    }
    if (removed) setChunkCacheVersion((v) => v + 1);
    if (Number.isFinite(nextExpiry)) {
      chunkExpiryTimerRef.current = setTimeout(
        () => expireChunkBytesRef.current(),
        Math.max(0, nextExpiry - Date.now()),
      );
    }
  }, []);
  expireChunkBytesRef.current = expireChunkBytes;

  const scheduleChunkExpiry = useCallback(() => {
    if (chunkExpiryTimerRef.current !== null) return;
    let nextExpiry = Number.POSITIVE_INFINITY;
    for (const entry of chunkBytesCacheRef.current.values()) {
      nextExpiry = Math.min(nextExpiry, entry.expiresAt);
    }
    if (Number.isFinite(nextExpiry)) {
      chunkExpiryTimerRef.current = setTimeout(
        () => expireChunkBytesRef.current(),
        Math.max(0, nextExpiry - Date.now()),
      );
    }
  }, []);

  const manifestQuery = useQuery({
    queryKey: ["video-manifest-v2", ownerKey, taskId, metadataGenerationId],
    queryFn: async ({ signal }) => {
      assertVideoDerivativeOwner(ownerKey, signal);
      const value = await tasksApi.getVideoManifestV2(taskId as string, { signal });
      assertVideoDerivativeOwner(ownerKey, signal);
      return value;
    },
    enabled: pipelineEnabled,
    staleTime: MANIFEST_STALE_MS,
    refetchInterval: (query) => (query.state.error ? false : MANIFEST_STALE_MS),
    gcTime: 0,
    retry: retryPreparingVideoMetadata,
    retryDelay: videoMetadataRetryDelay,
  });

  const manifest: VideoManifestV2Response | undefined =
    manifestQuery.data?.metadata_generation_id === metadataGenerationId
      ? manifestQuery.data
      : undefined;
  const datasetItemId = manifest?.dataset_item_id ?? null;
  const chunkSizeFrames = manifest?.chunk_size_frames ?? null;
  const manifestValid =
    !!manifest &&
    !!manifest.dataset_item_id &&
    isVideoGeneration(manifest.metadata_generation_id) &&
    Number.isInteger(manifest.chunk_size_frames) &&
    manifest.chunk_size_frames > 0;

  const targetChunkId = useMemo(() => {
    if (!manifestValid || !manifest) return null;
    return Math.floor(Math.max(0, frameIndex) / manifest.chunk_size_frames);
  }, [manifestValid, manifest, frameIndex]);
  const targetKey =
    datasetItemId && targetChunkId !== null
      ? JSON.stringify([
          ownerKey,
          taskId,
          manifest?.metadata_generation_id,
          datasetItemId,
          targetChunkId,
        ])
      : null;
  latestTargetKeyRef.current = targetKey;

  useEffect(() => {
    setFallbackReason(null);
    setDecoding(false);
    setPaintedFrameIndex(null);
    setLastPaintMs(null);
    setLastVisibleMs(null);
  }, [taskId, metadataGenerationId, frameIndex]);

  useEffect(() => {
    refreshedKeyRef.current = null;
    refreshingUrlKeyRef.current = null;
    setRefreshingUrlKey(null);
    setUrlRefreshed(false);
    setBytesSource(null);
  }, [targetKey]);

  const chunkQuery = useQuery({
    queryKey: [
      "video-chunk",
      ownerKey,
      taskId,
      manifest?.metadata_generation_id,
      datasetItemId,
      targetChunkId,
    ],
    queryFn: async ({ signal }) => {
      assertVideoDerivativeOwner(ownerKey, signal);
      const value = await videoApi.getChunk(datasetItemId as string, targetChunkId as number, {
        signal,
      });
      assertVideoDerivativeOwner(ownerKey, signal);
      return value;
    },
    enabled: pipelineEnabled && manifestValid && targetChunkId !== null && !!datasetItemId,
    refetchInterval: (query) => {
      const data = query.state.data as VideoChunkOut | undefined;
      if (data && data.status === "pending") {
        return retryAfterSecondsToMs(data.retry_after);
      }
      return MANIFEST_STALE_MS;
    },
    staleTime: MANIFEST_STALE_MS,
    gcTime: 0,
    retry: retryTransientVideoDerivative,
    retryDelay: transientVideoDerivativeRetryDelay,
  });

  const chunk: VideoChunkOut | undefined = chunkQuery.data;
  const refetchChunk = chunkQuery.refetch;
  const chunkIdentity = chunk ? videoDerivativeKey(chunk) : null;
  const chunkReady =
    !manifestQuery.isError &&
    !chunkQuery.isError &&
    !!chunk &&
    chunk.status === "ready" &&
    !!chunk.url &&
    !!chunkIdentity &&
    chunk.metadata_generation_id === manifest?.metadata_generation_id;
  const chunkFailed = !!chunk && chunk.status === "failed";
  const chunkUrl = chunk?.url ?? null;

  const decoderTaskId = JSON.stringify([ownerKey, taskId, metadataGenerationId]);
  const decoder = useVideoChunkDecoder({
    taskId: decoderTaskId,
    resourceKey: chunkReady ? chunkIdentity : null,
    enabled: decoderEnabled,
    bitmapBudgetBytes,
  });
  const decoderRef = useRef(decoder);
  decoderRef.current = decoder;

  useEffect(() => {
    if (!pipelineEnabled || !chunkReady) return;
    const startedAt = typeof performance !== "undefined" ? performance.now() : 0;
    const shown = decoderRef.current.showFrame(frameIndex);
    if (!shown || shown.frameIndex !== frameIndex) return;
    const readyAt = typeof performance !== "undefined" ? performance.now() : startedAt;
    visibleTimingRef.current = { taskId, frameIndex, startedAt, readyAt };
    if (prefetchedFramesRef.current.has(frameIndex)) setPrefetchHits((n) => n + 1);
    setFallbackReason(null);
  }, [frameIndex, pipelineEnabled, taskId, chunkReady, chunkIdentity, decoderTaskId]);

  const refetchManifest = manifestQuery.refetch;
  useEffect(() => {
    if (
      chunk?.metadata_generation_id &&
      chunk.metadata_generation_id !== manifest?.metadata_generation_id
    ) {
      void refetchManifest();
    }
  }, [chunk?.metadata_generation_id, manifest?.metadata_generation_id, refetchManifest]);

  const samplesQuery = useQuery({
    queryKey: [
      "video-chunk-samples",
      ownerKey,
      taskId,
      datasetItemId,
      targetChunkId,
      chunkIdentity,
    ],
    queryFn: async ({ signal }) => {
      assertVideoDerivativeOwner(ownerKey, signal);
      const value = await videoApi.getChunkSamples(
        datasetItemId as string,
        targetChunkId as number,
        { signal },
        chunk?.output_id ?? undefined,
      );
      assertVideoDerivativeOwner(ownerKey, signal);
      assertVideoSamplesIdentity(chunk!, value);
      return value;
    },
    enabled: pipelineEnabled && chunkReady && !!datasetItemId,
    staleTime: MANIFEST_STALE_MS,
    gcTime: 0,
    retry: retryTransientVideoDerivative,
    retryDelay: transientVideoDerivativeRetryDelay,
  });
  const samples: VideoChunkSamplesResponse | undefined = samplesQuery.data;

  useEffect(() => {
    if (!targetKey || !chunkReady || !chunkUrl) return;
    setBytesSource((current) =>
      current?.targetKey === targetKey && current.identityKey === chunkIdentity
        ? current
        : { targetKey, url: chunkUrl, refresh: 0, identityKey: chunkIdentity!, identity: chunk! },
    );
  }, [chunkReady, chunkUrl, targetKey, chunkIdentity, chunk]);

  useEffect(() => {
    chunkBytesCacheRef.current.setBudget(chunkBudgetBytes);
    setChunkCacheVersion((v) => v + 1);
  }, [chunkBudgetBytes]);

  const bytesQuery = useQuery({
    queryKey: [
      "video-chunk-bytes",
      ownerKey,
      taskId,
      datasetItemId,
      targetChunkId,
      chunkIdentity,
      bytesSource?.refresh ?? 0,
    ],
    queryFn: async ({ signal }: { signal: AbortSignal }) => {
      assertVideoDerivativeOwner(ownerKey, signal);
      const source = bytesSource as ChunkBytesSource;
      const cacheKey = JSON.stringify([ownerKey, source.identityKey]);
      const cached = chunkBytesCacheRef.current.get(cacheKey);
      if (cached && cached.expiresAt > Date.now()) {
        setChunkByteCacheHits((n) => n + 1);
        setChunkCacheVersion((v) => v + 1);
        return cached.buffer;
      }
      if (cached) {
        chunkBytesCacheRef.current.delete(cacheKey);
        setChunkCacheVersion((v) => v + 1);
      }
      const startedAt = typeof performance !== "undefined" ? performance.now() : 0;
      const res = await fetch(bytesSource?.url as string, { signal });
      if (!res.ok) {
        const e = new Error(`chunk bytes HTTP ${res.status}`);
        (e as { status?: number }).status = res.status;
        throw e;
      }
      const buffer = await res.arrayBuffer();
      await verifyVideoChunkBytes(buffer, source.identity);
      assertVideoDerivativeOwner(ownerKey, signal);
      const fetchMs =
        typeof performance !== "undefined" ? Math.round(performance.now() - startedAt) : 0;
      setBytesFetched((n) => n + buffer.byteLength);
      setLastChunkFetchMs(fetchMs);
      chunkBytesCacheRef.current.set(cacheKey, {
        value: { buffer, expiresAt: Date.now() + BYTES_GC_MS },
        bytes: buffer.byteLength,
      });
      scheduleChunkExpiry();
      setChunkCacheVersion((v) => v + 1);
      return buffer;
    },
    enabled:
      pipelineEnabled &&
      chunkReady &&
      !!samples &&
      !!datasetItemId &&
      !!targetKey &&
      bytesSource?.targetKey === targetKey &&
      bytesSource.identityKey === chunkIdentity &&
      !!bytesSource.url,
    staleTime: Infinity,
    gcTime: 0,
    retry: false,
  });
  const bytes =
    chunkReady &&
    !samplesQuery.isError &&
    !bytesQuery.isError &&
    bytesSource?.identityKey === chunkIdentity &&
    videoDerivativeKey(samples ?? {}) === chunkIdentity
      ? bytesQuery.data
      : undefined;

  useEffect(() => {
    if (!bytesQuery.isError || !targetKey || !bytesSource) return;
    if (errorStatus(bytesQuery.error) !== 403) return;
    if (refreshedKeyRef.current === targetKey) return;
    refreshedKeyRef.current = targetKey;
    refreshingUrlKeyRef.current = targetKey;
    setRefreshingUrlKey(targetKey);
    setUrlRefreshed(true);
    void refetchChunk()
      .then((result) => {
        if (latestTargetKeyRef.current !== targetKey) return;
        assertVideoDerivativeOwner(ownerKey);
        const refreshed = result.data;
        const identityKey = refreshed ? videoDerivativeKey(refreshed) : null;
        if (!result.isSuccess || refreshed?.status !== "ready" || !refreshed.url || !identityKey)
          return;
        setBytesSource((current) => {
          if (!current || current.targetKey !== targetKey) return current;
          return {
            targetKey,
            url: refreshed.url as string,
            refresh: current.refresh + 1,
            identityKey,
            identity: refreshed,
          };
        });
      })
      .catch(() => undefined)
      .finally(() => {
        if (refreshingUrlKeyRef.current === targetKey) {
          refreshingUrlKeyRef.current = null;
        }
        setRefreshingUrlKey((current) => (current === targetKey ? null : current));
      });
  }, [bytesQuery.isError, bytesQuery.error, bytesSource, refetchChunk, targetKey, ownerKey]);

  useEffect(() => {
    if (samplesQuery.isError && errorStatus(samplesQuery.error) === 409) void refetchChunk();
  }, [samplesQuery.isError, samplesQuery.error, refetchChunk]);

  useEffect(() => {
    let next: PreciseFrameFallbackReason | null = null;
    if (!pipelineEnabled) {
      next = null;
    } else if (manifestQuery.isError || (manifestQuery.data && !manifestValid)) {
      next = "api_unavailable";
    } else if (chunkQuery.isError) {
      next = "api_unavailable";
    } else if (chunk?.status === "ready" && !chunkReady) {
      next = "api_unavailable";
    } else if (chunkFailed) {
      next = "chunk_failed";
    } else if (samplesQuery.isError) {
      next = errorStatus(samplesQuery.error) === 404 ? "samples_unavailable" : "api_unavailable";
    } else if (bytesQuery.isError) {
      const status = errorStatus(bytesQuery.error);
      const refreshing =
        status === 403 &&
        targetKey !== null &&
        (refreshingUrlKeyRef.current === targetKey || refreshingUrlKey === targetKey);
      if (!refreshing) next = "chunk_fetch_failed";
    }
    setFallbackReason((cur) => {
      if (next) return next;
      if (cur && QUERY_FALLBACK_REASONS.includes(cur)) return null;
      return cur;
    });
  }, [
    pipelineEnabled,
    manifestQuery.isError,
    manifestQuery.data,
    manifestValid,
    chunkQuery.isError,
    chunkFailed,
    chunk,
    chunkReady,
    samplesQuery.isError,
    samplesQuery.error,
    bytesQuery.isError,
    bytesQuery.error,
    targetKey,
    refreshingUrlKey,
  ]);

  useEffect(() => {
    if (!pipelineEnabled || !bytes || !samples || targetChunkId === null || !datasetItemId) {
      return;
    }
    const demuxStart = typeof performance !== "undefined" ? performance.now() : 0;
    const result = buildGopPlan(bytes, samples, frameIndex);
    const demuxMs = typeof performance !== "undefined" ? performance.now() - demuxStart : 0;
    if (!result.ok) {
      setPlanMeta({
        taskId: null,
        frameIndex: null,
        gopStartDecodeIndex: null,
        targetTimestampUs: null,
        codec: null,
      });
      setFallbackReason(result.reason);
      setDecoding(false);
      return;
    }
    setLastDemuxMs(demuxMs);
    const plan = result.plan;
    const targetSample = plan.samples.find((s) => s.frameIndex === frameIndex);
    setPlanMeta({
      taskId,
      frameIndex,
      gopStartDecodeIndex: plan.gopStartDecodeIndex,
      targetTimestampUs: targetSample?.timestampUs ?? null,
      codec: plan.config.codec ?? null,
    });
    if (decoderRef.current.activeBitmap?.frameIndex === frameIndex || !canDecode) {
      return;
    }
    visibleTimingRef.current = {
      taskId,
      frameIndex,
      startedAt: demuxStart,
      readyAt: null,
    };
    const gopIdentity = {
      taskId: decoderTaskId,
      datasetItemId,
      chunkId: plan.chunkId,
      gopStartDecodeIndex: plan.gopStartDecodeIndex,
      configFingerprint: JSON.stringify([plan.configFingerprint, chunkIdentity]),
    };
    generationRef.current += 1;
    const generation = generationRef.current;
    let cancelled = false;
    setDecoding(true);
    setFallbackReason(null);
    void (async () => {
      const decoded = await decoderRef.current.decodePlan({
        plan,
        identity: gopIdentity,
        targetFrameIndex: frameIndex,
        generation,
        retainUncached: true,
      });
      if (cancelled) return;
      setDecoding(false);
      const latest = latestRef.current;
      if (
        latest.taskId !== taskId ||
        latest.ownerKey !== ownerKey ||
        latest.metadataGenerationId !== metadataGenerationId ||
        latest.frameIndex !== frameIndex ||
        !latest.enabled
      ) {
        return;
      }
      if (!decoded.bitmap) {
        setFallbackReason(decoded.fallbackReason);
        return;
      }
      const shown = decoderRef.current.showFrame(frameIndex);
      if (!shown || shown.frameIndex !== frameIndex) {
        setFallbackReason("decode_failed");
        return;
      }
      if (prefetchedFramesRef.current.has(frameIndex)) {
        setPrefetchHits((n) => n + 1);
      }
      const timing = visibleTimingRef.current;
      if (timing && timing.taskId === taskId && timing.frameIndex === frameIndex) {
        timing.readyAt = typeof performance !== "undefined" ? performance.now() : timing.startedAt;
      }
      setFallbackReason(null);
    })();
    return () => {
      cancelled = true;
    };
  }, [
    bytes,
    samples,
    canDecode,
    frameIndex,
    targetChunkId,
    datasetItemId,
    taskId,
    pipelineEnabled,
    decoderTaskId,
    chunkIdentity,
    ownerKey,
    metadataGenerationId,
  ]);

  const bitmap = useMemo<CachedVideoBitmap | null>(() => {
    const ab = decoder.activeBitmap;
    if (
      pipelineEnabled &&
      chunkReady &&
      !samplesQuery.isError &&
      !bytesQuery.isError &&
      ab &&
      ab.frameIndex === frameIndex
    )
      return ab;
    return null;
  }, [
    decoder.activeBitmap,
    frameIndex,
    pipelineEnabled,
    chunkReady,
    samplesQuery.isError,
    bytesQuery.isError,
  ]);

  const markFramePainted = useCallback(
    (paintedFrame: number) => {
      const timing = visibleTimingRef.current;
      if (
        !timing ||
        timing.taskId !== taskId ||
        timing.frameIndex !== paintedFrame ||
        paintedFrame !== liveFrameIndexRef.current ||
        timing.readyAt === null
      ) {
        return;
      }
      const paintedAt = typeof performance !== "undefined" ? performance.now() : timing.readyAt;
      setPaintedFrameIndex((current) => (current === paintedFrame ? current : paintedFrame));
      setLastPaintMs(Math.max(0, paintedAt - timing.readyAt));
      setLastVisibleMs(Math.max(0, paintedAt - timing.startedAt));
    },
    [taskId],
  );

  useEffect(() => {
    if (lastFrameIndexRef.current !== null && lastFrameIndexRef.current !== frameIndex) {
      directionRef.current = Math.sign(frameIndex - lastFrameIndexRef.current) || 0;
    }
    lastFrameIndexRef.current = frameIndex;
  }, [frameIndex]);

  const liveFrameIndexRef = useRef(frameIndex);
  liveFrameIndexRef.current = frameIndex;

  useEffect(() => {
    if (pipelineEnabled) return;
    setPlanMeta({
      taskId: null,
      frameIndex: null,
      gopStartDecodeIndex: null,
      targetTimestampUs: null,
      codec: null,
    });
  }, [pipelineEnabled]);

  const lastTaskIdRef = useRef(decoderTaskId);
  useEffect(() => {
    if (lastTaskIdRef.current === decoderTaskId) return;
    lastTaskIdRef.current = decoderTaskId;
    prefetchedFramesRef.current = new Set();
    setPrefetchRequests(0);
    setPrefetchHits(0);
    setBytesFetched(0);
    setChunkByteCacheHits(0);
    setLastChunkFetchMs(null);
    setLastPaintMs(null);
    setLastVisibleMs(null);
    setPaintedFrameIndex(null);
    visibleTimingRef.current = null;
    chunkBytesCacheRef.current.clear();
    if (chunkExpiryTimerRef.current !== null) {
      clearTimeout(chunkExpiryTimerRef.current);
      chunkExpiryTimerRef.current = null;
    }
    setChunkCacheVersion((v) => v + 1);
    directionRef.current = 0;
    lastFrameIndexRef.current = frameIndex;
    setPlanMeta({
      taskId: null,
      frameIndex: null,
      gopStartDecodeIndex: null,
      targetTimestampUs: null,
      codec: null,
    });
  }, [decoderTaskId, frameIndex]);

  useEffect(() => {
    if (!pipelineEnabled || !bitmap || prefetchFrames <= 0) return;
    if (typeof document !== "undefined" && document.hidden) return;
    const dir = directionRef.current;
    if (dir === 0 || !bytes || !samples || targetChunkId === null || !datasetItemId || !manifest) {
      return;
    }
    const chunkSize = manifest.chunk_size_frames;
    const currentPlan = buildGopPlan(bytes, samples, frameIndex);
    if (!currentPlan.ok) return;
    const currentGopStart = currentPlan.plan.gopStartDecodeIndex;
    const targets: number[] = [];
    for (let i = 1; i <= prefetchFrames; i++) {
      const fi = frameIndex + dir * i;
      if (fi < 0) break;
      if (Math.floor(fi / chunkSize) !== targetChunkId) break;
      const fiPlan = buildGopPlan(bytes, samples, fi);
      if (!fiPlan.ok || fiPlan.plan.gopStartDecodeIndex !== currentGopStart) break;
      targets.push(fi);
    }
    if (targets.length === 0) return;
    let cancelled = false;
    const gen = generationRef.current;
    void (async () => {
      for (const fi of targets) {
        if (cancelled) return;
        const result = buildGopPlan(bytes, samples, fi);
        if (!result.ok) continue;
        setPrefetchRequests((n) => n + 1);
        const decoded = await decoderRef.current.decodePlan({
          plan: result.plan,
          identity: {
            taskId: decoderTaskId,
            datasetItemId,
            chunkId: result.plan.chunkId,
            gopStartDecodeIndex: result.plan.gopStartDecodeIndex,
            configFingerprint: JSON.stringify([result.plan.configFingerprint, chunkIdentity]),
          },
          targetFrameIndex: fi,
          generation: gen,
          retainUncached: false,
          allowReset: false,
        });
        if (decoded.bitmap) prefetchedFramesRef.current.add(fi);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [
    bitmap,
    frameIndex,
    prefetchFrames,
    pipelineEnabled,
    bytes,
    samples,
    targetChunkId,
    datasetItemId,
    taskId,
    manifest,
    decoderTaskId,
    chunkIdentity,
  ]);

  const sourceState = useMemo<PreciseFrameSourceState>(() => {
    if (!pipelineEnabled) return "disabled";
    if (fallbackReason) return "fallback";
    if (bitmap) return "ready";
    if (decoding) return "decoding";
    if (chunk?.status === "pending") return "chunk-pending";
    if (chunkReady) return "loading";
    return "idle";
  }, [pipelineEnabled, fallbackReason, bitmap, decoding, chunk, chunkReady]);

  const diagnostics = useMemo<VideoPreciseFrameDiagnostics>(
    () => ({
      supported: decoder.diagnostics.supported,
      webcodecsEnabled: decoder.diagnostics.enabled,
      decoderActive: active,
      chunkId: targetChunkId,
      datasetItemId,
      chunkSizeFrames,
      decodeRequests: decoder.diagnostics.decodes,
      decoderErrors: decoder.diagnostics.errors,
      urlRefreshed,
    }),
    [
      active,
      targetChunkId,
      datasetItemId,
      chunkSizeFrames,
      decoder.diagnostics.supported,
      decoder.diagnostics.enabled,
      decoder.diagnostics.decodes,
      decoder.diagnostics.errors,
      urlRefreshed,
    ],
  );

  const performanceDiagnostics = useMemo<VideoPreciseFramePerformanceDiagnostics>(() => {
    void chunkCacheVersion;
    const currentPlanMeta =
      pipelineEnabled && planMeta.taskId === taskId && planMeta.frameIndex === frameIndex
        ? planMeta
        : {
            gopStartDecodeIndex: null,
            targetTimestampUs: null,
            codec: null,
          };
    return {
      manifestCacheHits: 0,
      samplesCacheHits: 0,
      chunkByteCacheHits,
      bitmapCacheHits: decoder.diagnostics.hits,
      bytesFetched,
      bitmapBytes: decoder.diagnostics.bitmapBytes,
      bitmapBudgetBytes: decoder.diagnostics.bitmapBudgetBytes,
      activeDecoders: decoder.diagnostics.activeDecoders,
      liveVideoFrames: decoder.diagnostics.liveVideoFrames,
      chunkBytes: chunkBytesCacheRef.current.bytes,
      chunkBudgetBytes,
      sessionCreates: decoder.diagnostics.sessionCreates,
      sessionResets: decoder.diagnostics.sessionResets,
      sessionDisposals: decoder.diagnostics.sessionDisposals,
      encodedChunksSubmitted: decoder.diagnostics.encodedChunksSubmitted,
      staleResults: decoder.diagnostics.staleResults,
      prefetchRequests,
      prefetchHits,
      evictions: decoder.diagnostics.evictions + chunkBytesCacheRef.current.evictions,
      lastManifestMs: null,
      lastSamplesMs: null,
      lastChunkFetchMs,
      lastDemuxMs,
      lastQueueMs: decoder.diagnostics.lastQueueMs,
      lastCodecMs: decoder.diagnostics.lastCodecMs,
      lastDecodeMs: decoder.diagnostics.lastDecodeMs,
      lastBitmapMs: decoder.diagnostics.lastBitmapMs,
      lastDecodeMode: decoder.diagnostics.lastDecodeMode,
      lastPaintMs,
      lastVisibleMs,
      paintedFrameIndex,
      gopStartDecodeIndex: currentPlanMeta.gopStartDecodeIndex,
      targetTimestampUs: currentPlanMeta.targetTimestampUs,
      codec: currentPlanMeta.codec,
    };
  }, [
    decoder.diagnostics,
    bytesFetched,
    chunkByteCacheHits,
    chunkCacheVersion,
    chunkBudgetBytes,
    prefetchRequests,
    prefetchHits,
    lastDemuxMs,
    lastChunkFetchMs,
    lastPaintMs,
    lastVisibleMs,
    paintedFrameIndex,
    pipelineEnabled,
    planMeta,
    taskId,
    frameIndex,
  ]);

  useEffect(
    () => () => {
      if (chunkExpiryTimerRef.current !== null) {
        clearTimeout(chunkExpiryTimerRef.current);
        chunkExpiryTimerRef.current = null;
      }
      chunkBytesCacheRef.current.clear();
    },
    [],
  );

  return {
    active,
    bitmap,
    sourceState,
    fallbackReason,
    diagnostics,
    performance: performanceDiagnostics,
    markFramePainted,
  };
}

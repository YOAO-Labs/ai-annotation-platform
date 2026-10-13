import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { tasksApi } from "@/api/tasks";
import { videoApi } from "@/api/videos";
import type { VideoChunkOut, VideoChunkSamplesResponse, VideoManifestV2Response } from "@/types";
import { useVideoChunkDecoder } from "./useVideoChunkDecoder";
import { ByteLru } from "./videoByteLru";
import { buildGopPlan, type PreciseFrameFallbackReason } from "./videoChunkDemux";
import type { CachedVideoBitmap } from "./useVideoBitmapCache";

export interface UseVideoPreciseFrameArgs {
  taskId: string | null | undefined;
  frameIndex: number;
  /** 暂停态为 true；允许立即展示已经缓存的精确帧。 */
  enabled: boolean;
  /** 原生 seek 稳定后为 true；缓存未命中时才允许启动新的 WebCodecs decode。 */
  decodeEnabled?: boolean;
  /** 已解码 bitmap 的字节预算上限(性能档位 videoDecoderBitmapCacheBytes)。 */
  bitmapBudgetBytes: number;
  /** chunk 原始字节缓存的预算上限(性能档位 videoChunkByteCacheBytes)。 */
  chunkBudgetBytes: number;
  /** 暂停态同 GOP 方向感知预取帧数(性能档位 videoDecodePrefetchFrames;0 不预取)。 */
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
  /** WebCodecs 原语是否可用(与 flag 无关的能力探测)。 */
  supported: boolean;
  /** 实验开关是否开启(独立于 supported,供全局诊断区分能力缺失与用户关闭)。 */
  webcodecsEnabled: boolean;
  decoderActive: boolean;
  chunkId: number | null;
  datasetItemId: string | null;
  chunkSizeFrames: number | null;
  decodeRequests: number;
  decoderErrors: number;
  urlRefreshed: boolean;
}

/** 精确帧 pipeline 的分阶段性能与资源诊断(v0.23.14)。 */
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
  /** 当前精确帧 ready 到 Konva media layer 完成 draw 的时间。 */
  lastPaintMs: number | null;
  /** 当前精确帧从 buildGopPlan 开始到 Konva media layer 完成 draw 的总时间。 */
  lastVisibleMs: number | null;
  paintedFrameIndex: number | null;
  /** 最近一次成功 decode 所属 GOP 的起点 decode index(排障 GOP 边界 / reset)。 */
  gopStartDecodeIndex: number | null;
  /** 最近一次 decode 目标帧的 pts 微秒(确认 timestamp 匹配而非数组下标)。 */
  targetTimestampUs: number | null;
  /** 最近一次 decode 的 codec string(如 "avc1.42E01E");仅诊断,不含 description。 */
  codec: string | null;
}

export interface UseVideoPreciseFrameResult {
  /** precise pipeline 是否激活(flag on + 浏览器支持)。 */
  active: boolean;
  /** 当前 frameIndex 的精确 bitmap(仅当 frameIndex 匹配时);否则 null(调用方走 fallback)。 */
  bitmap: CachedVideoBitmap | null;
  sourceState: PreciseFrameSourceState;
  fallbackReason: PreciseFrameFallbackReason | null;
  diagnostics: VideoPreciseFrameDiagnostics;
  performance: VideoPreciseFramePerformanceDiagnostics;
  /** Konva media layer 完成当前精确帧 draw 后回执，用于可见帧门与阶段计时。 */
  markFramePainted: (frameIndex: number) => void;
}

const BYTES_GC_MS = 60_000;
const MANIFEST_STALE_MS = 30_000;
const MIN_POLL_MS = 1000;
const MAX_POLL_MS = 10_000;

interface ChunkBytesSource {
  targetKey: string;
  url: string;
  generation: number;
}

interface ChunkBytesCacheEntry {
  buffer: ArrayBuffer;
  expiresAt: number;
}

/** 由 query 阶段产生的 fallback reason(decode 阶段的 reason 由 decode effect 独立管理)。 */
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

/**
 * 精确帧 pipeline:manifest v2 → chunk 轮询 → samples → chunk bytes → demux plan → decode。
 *
 * 仅在暂停 + 稳定 seek 后(`enabled`)且 WebCodecs 可用时工作;连续播放与 seeking 中不发新
 * 请求。所有失败(manifest 缺失 / chunk pending-failed / samples 404 / bytes 网络错误 /
 * decode 失败)都安全 fallback,不阻断标注。latest-request-wins:旧 decode 可入缓存,但只有
 * 当前 `{taskId, frameIndex}` 能激活画面。
 */
export function useVideoPreciseFrame({
  taskId,
  frameIndex,
  enabled,
  decodeEnabled = enabled,
  bitmapBudgetBytes,
  chunkBudgetBytes,
  prefetchFrames,
}: UseVideoPreciseFrameArgs): UseVideoPreciseFrameResult {
  const decoder = useVideoChunkDecoder({ taskId, bitmapBudgetBytes });
  const active = decoder.active;
  // 1-2. decoder inactive(无 flag / 无 WebCodecs)→ 全链路 disabled。
  const pipelineEnabled = active && !!taskId && enabled;
  const canDecode = pipelineEnabled && decodeEnabled;

  const decoderRef = useRef(decoder);
  decoderRef.current = decoder;
  // latest-request-wins:effect 内异步 decode 完成后只激活仍匹配的 frame。
  const latestRef = useRef({ taskId, frameIndex, enabled: canDecode });
  latestRef.current = { taskId, frameIndex, enabled: canDecode };
  // 单调递增的请求 token;decode 完成后与 latestRef 一起保证旧请求不覆盖当前显示帧。
  const generationRef = useRef(0);
  // 方向感知预取:记录上次 frameIndex,差值符号决定预取方向;0 表示方向未知不预取。
  const lastFrameIndexRef = useRef<number | null>(null);
  const directionRef = useRef(0);
  // 预取过(已入缓存)的 frameIndex 集合,用于统计 prefetchHits。
  const prefetchedFramesRef = useRef<Set<number>>(new Set());
  // 当前 frame 成功 demux 的 plan 元数据；用 state 保证诊断对象在换帧/失败时同步失效。
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

  // 3. task manifest v2(仅 precise pipeline 激活时增量查询,不切主工作台 manifest)。
  const manifestQuery = useQuery({
    queryKey: ["video-manifest-v2", taskId],
    queryFn: ({ signal }) => tasksApi.getVideoManifestV2(taskId as string, { signal }),
    enabled: pipelineEnabled,
    staleTime: MANIFEST_STALE_MS,
    retry: false,
  });

  const manifest: VideoManifestV2Response | undefined = manifestQuery.data;
  const datasetItemId = manifest?.dataset_item_id ?? null;
  const chunkSizeFrames = manifest?.chunk_size_frames ?? null;
  const manifestValid =
    !!manifest &&
    !!manifest.dataset_item_id &&
    Number.isInteger(manifest.chunk_size_frames) &&
    manifest.chunk_size_frames > 0;

  // 4. 目标帧所在 chunk。
  const targetChunkId = useMemo(() => {
    if (!manifestValid || !manifest) return null;
    return Math.floor(Math.max(0, frameIndex) / manifest.chunk_size_frames);
  }, [manifestValid, manifest, frameIndex]);
  const targetKey =
    datasetItemId && targetChunkId !== null ? `${datasetItemId}:${targetChunkId}` : null;
  latestTargetKeyRef.current = targetKey;

  // 新帧先清旧 decode reason;跨 chunk / task 时还要重置 URL refresh 与 bytes source。
  useEffect(() => {
    setFallbackReason(null);
    setDecoding(false);
    setPaintedFrameIndex(null);
    setLastPaintMs(null);
    setLastVisibleMs(null);
  }, [taskId, frameIndex]);

  // 原生 <video> seek 尚未完成时不启动新 decode，但缓存帧不需要等待媒体元素。这样逐帧
  // 回看 / 小范围往返可以直接进入 Konva，缓存未命中仍保留“稳定 seek 后解码”的限流合同。
  useEffect(() => {
    if (!pipelineEnabled) return;
    const startedAt = typeof performance !== "undefined" ? performance.now() : 0;
    const shown = decoderRef.current.showFrame(frameIndex);
    if (!shown || shown.frameIndex !== frameIndex) return;
    const readyAt = typeof performance !== "undefined" ? performance.now() : startedAt;
    visibleTimingRef.current = {
      taskId,
      frameIndex,
      startedAt,
      readyAt,
    };
    if (prefetchedFramesRef.current.has(frameIndex)) {
      setPrefetchHits((n) => n + 1);
    }
    setFallbackReason(null);
  }, [frameIndex, pipelineEnabled, taskId]);
  useEffect(() => {
    refreshedKeyRef.current = null;
    refreshingUrlKeyRef.current = null;
    setRefreshingUrlKey(null);
    setUrlRefreshed(false);
    setBytesSource(null);
  }, [targetKey]);

  // 5. 单 chunk 状态:pending 按 retry_after 轮询;ready metadata 保留到显式 403 refresh。
  const chunkQuery = useQuery({
    queryKey: ["video-chunk", datasetItemId, targetChunkId],
    queryFn: ({ signal }) =>
      videoApi.getChunk(datasetItemId as string, targetChunkId as number, { signal }),
    enabled: pipelineEnabled && manifestValid && targetChunkId !== null && !!datasetItemId,
    refetchInterval: (query) => {
      const data = query.state.data as VideoChunkOut | undefined;
      if (data && data.status === "pending") {
        return retryAfterSecondsToMs(data.retry_after);
      }
      return false;
    },
    // ready metadata / signed URL 保持到显式 403 refresh;pending 仍由 refetchInterval 驱动。
    staleTime: Infinity,
    retry: false,
  });

  const chunk: VideoChunkOut | undefined = chunkQuery.data;
  const refetchChunk = chunkQuery.refetch;
  const chunkReady = !!chunk && chunk.status === "ready" && !!chunk.url;
  const chunkFailed = !!chunk && chunk.status === "failed";
  const chunkUrl = chunk?.url ?? null;

  // 6. packet sample manifest:按 dataset + chunk 长缓存(chunk 内容不变);404 不重试。
  const samplesQuery = useQuery({
    queryKey: ["video-chunk-samples", datasetItemId, targetChunkId],
    queryFn: ({ signal }) =>
      videoApi.getChunkSamples(datasetItemId as string, targetChunkId as number, { signal }),
    enabled: pipelineEnabled && chunkReady && !!datasetItemId,
    staleTime: Infinity,
    retry: false,
  });
  const samples: VideoChunkSamplesResponse | undefined = samplesQuery.data;

  useEffect(() => {
    if (!targetKey || !chunkReady || !chunkUrl) return;
    setBytesSource((current) =>
      current?.targetKey === targetKey ? current : { targetKey, url: chunkUrl, generation: 0 },
    );
  }, [chunkReady, chunkUrl, targetKey]);

  // 性能档位切换时立即把原始 chunk 缓存收缩到新预算；当前 query 正在使用的 buffer
  // 由 React Query 持有，不会因缓存淘汰而失效。
  useEffect(() => {
    chunkBytesCacheRef.current.setBudget(chunkBudgetBytes);
    setChunkCacheVersion((v) => v + 1);
  }, [chunkBudgetBytes]);

  // 7. chunk bytes:raw fetch(signed URL,无需 auth header)。generation 仅在显式 403 refresh
  // 后递增,既不把敏感 signed URL 放进 query key,也能保证新 URL 必定发起一次新请求。
  // React Query 只负责当前请求生命周期(gcTime=0);跨目标复用由显式 byte-LRU + 60s TTL
  // 控制，确保累计保留的 chunk bytes 真正受 chunkBudgetBytes 约束。
  const bytesQuery = useQuery({
    queryKey: ["video-chunk-bytes", datasetItemId, targetChunkId, bytesSource?.generation ?? 0],
    queryFn: async ({ signal }: { signal: AbortSignal }) => {
      const source = bytesSource as ChunkBytesSource;
      const cacheKey = `${source.targetKey}:${source.generation}`;
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
      !!bytesSource.url,
    // 当前 query 失去 observer 后立即释放引用；60s TTL 只在受预算约束的 byte-LRU 内实现。
    staleTime: Infinity,
    gcTime: 0,
    retry: false,
  });
  const bytes = bytesQuery.data;

  // signed URL 过期(403):先等待 chunk metadata 刷新完成,再递增 generation 触发新 URL bytes。
  // 同一 target visit 只刷新一次;第二次 403 进入 chunk_fetch_failed。
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
        const refreshed = result.data;
        if (!result.isSuccess || refreshed?.status !== "ready" || !refreshed.url) return;
        setBytesSource((current) => {
          if (!current || current.targetKey !== targetKey) return current;
          return {
            targetKey,
            url: refreshed.url as string,
            generation: current.generation + 1,
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
  }, [bytesQuery.isError, bytesQuery.error, bytesSource, refetchChunk, targetKey]);

  // manifest / chunk / samples / bytes 的失败 → fallback reason(chunk_pending 是过渡态,不计)。
  // 仅覆盖 query 阶段 reason;decode 阶段 reason(invalid_sample_range / description_unavailable /
  // codec_unsupported / decode_failed)由 decode effect 独立设置与清除。
  useEffect(() => {
    let next: PreciseFrameFallbackReason | null = null;
    if (!pipelineEnabled) {
      next = null;
    } else if (manifestQuery.isError || (manifest && !manifestValid)) {
      next = "api_unavailable";
    } else if (chunkQuery.isError) {
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
      // 无 query 错误时,清除遗留的 query 类 reason;保留 decode 类 reason。
      if (cur && QUERY_FALLBACK_REASONS.includes(cur)) return null;
      return cur;
    });
  }, [
    pipelineEnabled,
    manifestQuery.isError,
    manifest,
    manifestValid,
    chunkQuery.isError,
    chunkFailed,
    samplesQuery.isError,
    samplesQuery.error,
    bytesQuery.isError,
    bytesQuery.error,
    targetKey,
    refreshingUrlKey,
  ]);

  // 8-10. bytes + samples 就绪 → build GOP plan → decode → 仅当最新 ref 匹配才激活。
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
    // 缓存帧可以在原生 seek 尚未结束时立即显示，但仍需发布与画面同帧的 GOP / PTS
    // 诊断；因此先构建 metadata，再决定是否需要提交新的 decode。
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
      taskId: taskId as string,
      datasetItemId,
      chunkId: plan.chunkId,
      gopStartDecodeIndex: plan.gopStartDecodeIndex,
      configFingerprint: plan.configFingerprint,
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
      if (latest.taskId !== taskId || latest.frameIndex !== frameIndex || !latest.enabled) {
        return; // latest-request-wins:旧 decode 不激活
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
  ]);

  const bitmap = useMemo<CachedVideoBitmap | null>(() => {
    const ab = decoder.activeBitmap;
    if (ab && ab.frameIndex === frameIndex) return ab;
    return null;
  }, [decoder.activeBitmap, frameIndex]);

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
    // frameIndex 经 liveFrameIndexRef 读取（Issue #114 迟到回执防护），无需入依赖。
    [taskId],
  );

  // 方向感知:frameIndex 变化时用差值符号更新预取方向。
  useEffect(() => {
    if (lastFrameIndexRef.current !== null && lastFrameIndexRef.current !== frameIndex) {
      directionRef.current = Math.sign(frameIndex - lastFrameIndexRef.current) || 0;
    }
    lastFrameIndexRef.current = frameIndex;
  }, [frameIndex]);

  // Issue #114 · 帧号 live 镜像:markFramePainted 可能被 Konva draw 事件迟到触发,闭包里的
  // frameIndex 是回调创建时刻的值;校验必须读最新帧,否则过期绘制回执仍能发布旧 painted 帧。
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

  // task 切换清空预取统计与方向记忆(mount 时跳过,避免覆盖初始方向追踪)。
  const lastTaskIdRef = useRef<string | null | undefined>(taskId);
  useEffect(() => {
    if (lastTaskIdRef.current === taskId) return;
    lastTaskIdRef.current = taskId;
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
    lastFrameIndexRef.current = null;
    setPlanMeta({
      taskId: null,
      frameIndex: null,
      gopStartDecodeIndex: null,
      targetTimestampUs: null,
      codec: null,
    });
  }, [taskId]);

  // 受控预取:暂停态 + 同 chunk/GOP + 方向已知 + tab 可见时,沿最近方向预取少量帧。
  // 播放态 / seeking(enabled=false → pipelineEnabled=false)完全不预取,保持零额外请求。
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
      if (Math.floor(fi / chunkSize) !== targetChunkId) break; // 跨 chunk 不预取
      const fiPlan = buildGopPlan(bytes, samples, fi);
      if (!fiPlan.ok || fiPlan.plan.gopStartDecodeIndex !== currentGopStart) break; // 跨 GOP 不预取
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
            taskId: taskId as string,
            datasetItemId,
            chunkId: result.plan.chunkId,
            gopStartDecodeIndex: result.plan.gopStartDecodeIndex,
            configFingerprint: result.plan.configFingerprint,
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
      // manifest / samples 命中与耗时由 v0.23.15 并入全局诊断。
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

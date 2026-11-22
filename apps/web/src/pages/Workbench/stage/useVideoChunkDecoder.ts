import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  EncodedVideoDecodePlan,
  PreciseFrameFallbackReason,
  VideoGopPlan,
} from "./videoChunkDemux";
import { ByteLru } from "./videoByteLru";
import { VideoGopDecoderSession, type VideoGopSessionIdentity } from "./videoGopDecoderSession";
import { WORKBENCH_LOCAL_DEFAULTS } from "../state/workbenchLocalDefaults";

export const WEBCODECS_FLAG_STORAGE_KEY = "video.experimental.webcodecs";
export const WEBCODECS_FLAG_QUERY_KEY = "webcodecs";
export const WEBCODECS_DEFAULT_ENABLED = WORKBENCH_LOCAL_DEFAULTS["experiment.webcodecs"];

export interface DecodedVideoFrameBitmap {
  frameIndex: number;
  bitmap: ImageBitmap;
  width: number;
  height: number;
}

interface OwnedDecodedVideoFrameBitmap {
  key: string;
  entry: DecodedVideoFrameBitmap;
  bytes: number;
}

export type VideoChunkDecodeResult =
  | { bitmap: DecodedVideoFrameBitmap; fallbackReason: null }
  | { bitmap: null; fallbackReason: PreciseFrameFallbackReason };

export interface VideoChunkDecoderDiagnostics {
  supported: boolean;
  enabled: boolean;
  secureContext: boolean;
  cacheSize: number;
  activeFrameIndex: number | null;
  hits: number;
  misses: number;
  decodes: number;
  errors: number;
  configUnsupported: number;
  staleResults: number;
  lastFallbackReason: PreciseFrameFallbackReason | null;
  lastQueueMs: number | null;
  lastCodecMs: number | null;
  lastBitmapMs: number | null;
  lastDecodeMs: number | null;
  lastDecodeMode: "bitmap-cache" | "supplemental-cache" | "session" | null;
  bitmapBytes: number;
  bitmapBudgetBytes: number;
  activeDecoders: number;
  liveVideoFrames: number;
  evictions: number;
  encodedChunksSubmitted: number;
  sessionCreates: number;
  sessionResets: number;
  sessionDisposals: number;
}

interface UseVideoChunkDecoderArgs {
  taskId: string | null | undefined;
  resourceKey?: string | null;
  enabled?: boolean;
  bitmapBudgetBytes?: number;
}

const DEFAULT_BITMAP_BUDGET_BYTES = 256 * 1024 * 1024;

export function detectWebCodecsSupport(): boolean {
  return (
    typeof globalThis !== "undefined" &&
    typeof (globalThis as { VideoDecoder?: unknown }).VideoDecoder !== "undefined" &&
    typeof (globalThis as { EncodedVideoChunk?: unknown }).EncodedVideoChunk !== "undefined" &&
    typeof globalThis.createImageBitmap === "function"
  );
}

export function chunkDecoderCacheKey(
  taskId: string,
  frameIndex: number,
  resourceKey?: string | null,
): string {
  return resourceKey
    ? JSON.stringify([taskId, resourceKey, frameIndex])
    : `${taskId}:${frameIndex}`;
}

export function gopIdentityEquals(a: VideoGopSessionIdentity, b: VideoGopSessionIdentity): boolean {
  return (
    a.taskId === b.taskId &&
    a.datasetItemId === b.datasetItemId &&
    a.chunkId === b.chunkId &&
    a.gopStartDecodeIndex === b.gopStartDecodeIndex &&
    a.configFingerprint === b.configFingerprint
  );
}

export function isWebCodecsExperimentEnabled(
  search?: string | null,
  storage?: Pick<Storage, "getItem"> | null,
): boolean {
  const enabled = (value: string | null | undefined) => {
    if (value === null || value === undefined) return WEBCODECS_DEFAULT_ENABLED;
    if (value === "1" || value === "true") return true;
    if (value === "0" || value === "false") return false;
    return WEBCODECS_DEFAULT_ENABLED;
  };
  try {
    const params = new URLSearchParams(search ?? "");
    if (params.has(WEBCODECS_FLAG_QUERY_KEY)) {
      return enabled(params.get(WEBCODECS_FLAG_QUERY_KEY));
    }
  } catch {
    // 非法 search 串 → 忽略，继续看 localStorage。
  }
  try {
    return enabled(storage?.getItem(WEBCODECS_FLAG_STORAGE_KEY));
  } catch {
    return WEBCODECS_DEFAULT_ENABLED;
  }
}

export function resolveVideoChunkDecoderEnabled(): boolean {
  if (typeof window === "undefined") return false;
  return isWebCodecsExperimentEnabled(window.location.search, window.localStorage);
}

function closeBitmap(bitmap: ImageBitmap) {
  try {
    bitmap.close();
  } catch {
    // 部分测试替身 / 老引擎没有 close。
  }
}

function closeFrame(frame: VideoFrame) {
  try {
    frame.close();
  } catch {
    // 同上。
  }
}

function bitmapBytes(width: number, height: number): number {
  return Math.max(0, Math.round(width) * Math.round(height) * 4);
}

/**
 * 按 plan 解码目标帧:用 `VideoFrame.timestamp === plan.targetTimestampUs` 选目标输出
 * (B 帧下 output 顺序与 decode order 无关,只有 timestamp 可靠),其余 frame 立即 close。
 * flush 后 `createImageBitmap(wanted)` 返回;VideoFrame / VideoDecoder 在 finally 关闭,
 * bitmap 交给调用方(缓存)拥有,不在 finally 关闭。
 *
 * @param onFallback 可选诊断回调:config 不支持 → "codec_unsupported";其余失败 → "decode_failed"。
 */
export async function decodePlanToBitmap(
  plan: EncodedVideoDecodePlan,
  onFallback?: (reason: PreciseFrameFallbackReason) => void,
): Promise<DecodedVideoFrameBitmap | null> {
  if (!detectWebCodecsSupport()) {
    onFallback?.("codec_unsupported");
    return null;
  }
  if (plan.chunks.length === 0) {
    onFallback?.("decode_failed");
    return null;
  }

  let supportedConfig = plan.config;
  if (typeof VideoDecoder.isConfigSupported === "function") {
    try {
      let support = await VideoDecoder.isConfigSupported(plan.config);
      if (!support.supported && plan.config.hardwareAcceleration === "prefer-hardware") {
        const fallbackConfig = { ...plan.config };
        delete fallbackConfig.hardwareAcceleration;
        support = await VideoDecoder.isConfigSupported(fallbackConfig);
        if (support.supported) supportedConfig = support.config ?? fallbackConfig;
      }
      if (!support.supported) {
        onFallback?.("codec_unsupported");
        return null;
      }
      if (supportedConfig === plan.config) {
        supportedConfig = support.config ?? plan.config;
      }
    } catch {
      onFallback?.("codec_unsupported");
      return null;
    }
  }

  const wantedRef: { frame: VideoFrame | null } = { frame: null };
  let decodeError: DOMException | null = null;
  let decoder: VideoDecoder | null = null;

  try {
    try {
      decoder = new VideoDecoder({
        output: (frame) => {
          if (wantedRef.frame === null && frame.timestamp === plan.targetTimestampUs) {
            wantedRef.frame = frame;
          } else {
            closeFrame(frame);
          }
        },
        error: (err: DOMException) => {
          if (decodeError === null) decodeError = err;
        },
      });
    } catch {
      onFallback?.("decode_failed");
      return null;
    }

    try {
      decoder.configure(supportedConfig);
    } catch {
      onFallback?.("codec_unsupported");
      return null;
    }

    for (const chunk of plan.chunks) decoder.decode(chunk);
    await decoder.flush();

    const wanted = wantedRef.frame;
    if (decodeError !== null || wanted === null) {
      onFallback?.("decode_failed");
      return null;
    }

    const bitmap = await globalThis.createImageBitmap(wanted);
    return {
      frameIndex: plan.targetFrameIndex,
      bitmap,
      width: bitmap.width || wanted.displayWidth,
      height: bitmap.height || wanted.displayHeight,
    };
  } catch {
    onFallback?.("decode_failed");
    return null;
  } finally {
    if (wantedRef.frame) closeFrame(wantedRef.frame);
    if (decoder) {
      try {
        decoder.close();
      } catch {
        // decoder 可能已 close。
      }
    }
  }
}

export function useVideoChunkDecoder({
  taskId,
  resourceKey,
  enabled,
  bitmapBudgetBytes = DEFAULT_BITMAP_BUDGET_BYTES,
}: UseVideoChunkDecoderArgs) {
  const supported = detectWebCodecsSupport();
  const resolvedEnabled = enabled ?? resolveVideoChunkDecoderEnabled();
  const secureContext = typeof window !== "undefined" ? window.isSecureContext : false;
  const active = supported && resolvedEnabled;

  const cacheRef = useRef(new ByteLru<string, DecodedVideoFrameBitmap>(bitmapBudgetBytes));
  const bitmapBudgetBytesRef = useRef(bitmapBudgetBytes);
  bitmapBudgetBytesRef.current = bitmapBudgetBytes;
  const activeBitmapRef = useRef<OwnedDecodedVideoFrameBitmap | null>(null);
  const uncachedBitmapRef = useRef(new Map<string, OwnedDecodedVideoFrameBitmap>());
  const retiredBitmapRef = useRef<OwnedDecodedVideoFrameBitmap[]>([]);
  const sessionRef = useRef<{
    session: VideoGopDecoderSession | null;
    identity: VideoGopSessionIdentity | null;
  }>({ session: null, identity: null });
  const liveVideoFramesRef = useRef(0);
  const inFlightRef = useRef(new Map<string, Promise<VideoChunkDecodeResult>>());
  const taskIdRef = useRef(taskId);
  taskIdRef.current = taskId;
  const resourceKeyRef = useRef(resourceKey);
  resourceKeyRef.current = resourceKey;
  const mountedRef = useRef(true);
  const lifecycleGenerationRef = useRef(0);

  const [activeFrameIndex, setActiveFrameIndex] = useState<number | null>(null);
  const [version, setVersion] = useState(0);
  const [diagnostics, setDiagnostics] = useState<VideoChunkDecoderDiagnostics>({
    supported,
    enabled: resolvedEnabled,
    secureContext,
    cacheSize: 0,
    activeFrameIndex: null,
    hits: 0,
    misses: 0,
    decodes: 0,
    errors: 0,
    configUnsupported: 0,
    staleResults: 0,
    lastFallbackReason: null,
    lastQueueMs: null,
    lastCodecMs: null,
    lastBitmapMs: null,
    lastDecodeMs: null,
    lastDecodeMode: null,
    bitmapBytes: 0,
    bitmapBudgetBytes,
    activeDecoders: 0,
    liveVideoFrames: 0,
    evictions: 0,
    encodedChunksSubmitted: 0,
    sessionCreates: 0,
    sessionResets: 0,
    sessionDisposals: 0,
  });

  const bumpVersion = useCallback(() => setVersion((v) => v + 1), []);

  const ownedBitmapCount = useCallback(
    () =>
      cacheRef.current.size +
      (activeBitmapRef.current ? 1 : 0) +
      uncachedBitmapRef.current.size +
      retiredBitmapRef.current.length,
    [],
  );

  const nonCachedBitmapBytes = useCallback(() => {
    let bytes = activeBitmapRef.current?.bytes ?? 0;
    for (const owned of uncachedBitmapRef.current.values()) bytes += owned.bytes;
    for (const owned of retiredBitmapRef.current) bytes += owned.bytes;
    return bytes;
  }, []);

  const rebalanceBitmapCache = useCallback(() => {
    const availableBytes = Math.max(0, bitmapBudgetBytesRef.current - nonCachedBitmapBytes());
    cacheRef.current.setBudget(availableBytes);
  }, [nonCachedBitmapBytes]);

  const ownedBitmapBytes = useCallback(() => {
    return cacheRef.current.bytes + nonCachedBitmapBytes();
  }, [nonCachedBitmapBytes]);

  const remember = useCallback(
    (
      key: string,
      entry: DecodedVideoFrameBitmap,
      decodeMs: number,
      retainUncached: boolean,
      phases?: {
        queueMs: number | null;
        codecMs: number | null;
        bitmapMs: number;
        mode: "session";
      },
    ): boolean => {
      const cache = cacheRef.current;
      const bytes = bitmapBytes(entry.width, entry.height);
      rebalanceBitmapCache();
      const cached = cache.set(key, {
        value: entry,
        bytes,
        dispose: ({ bitmap }) => closeBitmap(bitmap),
      });
      if (!cached) {
        if (!retainUncached) {
          closeBitmap(entry.bitmap);
        } else {
          for (const owned of uncachedBitmapRef.current.values()) {
            closeBitmap(owned.entry.bitmap);
          }
          uncachedBitmapRef.current.clear();
          uncachedBitmapRef.current.set(key, { key, entry, bytes });
          rebalanceBitmapCache();
        }
      }
      setDiagnostics((cur) => ({
        ...cur,
        supported,
        enabled: resolvedEnabled,
        cacheSize: ownedBitmapCount(),
        decodes: cur.decodes + 1,
        lastQueueMs: phases ? phases.queueMs : cur.lastQueueMs,
        lastCodecMs: phases ? phases.codecMs : cur.lastCodecMs,
        lastBitmapMs: phases ? phases.bitmapMs : cur.lastBitmapMs,
        lastDecodeMs: phases ? decodeMs : cur.lastDecodeMs,
        lastDecodeMode: phases ? phases.mode : cur.lastDecodeMode,
      }));
      bumpVersion();
      return cached || retainUncached;
    },
    [bumpVersion, ownedBitmapCount, rebalanceBitmapCache, resolvedEnabled, supported],
  );

  useEffect(() => {
    rebalanceBitmapCache();
    setDiagnostics((cur) => ({
      ...cur,
      supported,
      enabled: resolvedEnabled,
      cacheSize: ownedBitmapCount(),
    }));
    bumpVersion();
  }, [
    bumpVersion,
    bitmapBudgetBytes,
    ownedBitmapCount,
    rebalanceBitmapCache,
    resolvedEnabled,
    supported,
  ]);

  useEffect(() => {
    const cache = cacheRef.current;
    const stats = sessionRef.current.session?.getStats();
    setDiagnostics((cur) => ({
      ...cur,
      cacheSize: ownedBitmapCount(),
      bitmapBytes: ownedBitmapBytes(),
      bitmapBudgetBytes,
      activeDecoders: stats?.activeDecoders ?? 0,
      liveVideoFrames: liveVideoFramesRef.current + (stats?.liveVideoFrames ?? 0),
      evictions: cache.evictions,
      encodedChunksSubmitted: stats?.submits ?? 0,
      sessionCreates: stats?.sessionCreates ?? 0,
      sessionResets: stats?.resets ?? 0,
      sessionDisposals: stats?.disposals ?? 0,
    }));
  }, [bitmapBudgetBytes, ownedBitmapBytes, ownedBitmapCount, version]);

  /**
   * 用当前 GOP session 解码目标帧并入缓存(**不自动激活**)。identity 匹配则复用同一
   * decoder(同 GOP 向前只提交增量 chunks),变化则销毁重建。flag 关闭 / 不支持 / 已缓存 /
   * 在途 → 直接返回。
   * @returns 命中缓存或解码成功的 entry;null 表示跳过 / 失败(调用方降级到 <video>)。
   */
  const decodePlan = useCallback(
    async (args: {
      plan: VideoGopPlan;
      identity: VideoGopSessionIdentity;
      targetFrameIndex: number;
      generation: number;
      retainUncached?: boolean;
      allowReset?: boolean;
    }): Promise<VideoChunkDecodeResult> => {
      if (!taskId || !active) {
        return { bitmap: null, fallbackReason: "flag_disabled" };
      }
      if (
        !mountedRef.current ||
        taskIdRef.current !== taskId ||
        resourceKeyRef.current !== resourceKey
      ) {
        return { bitmap: null, fallbackReason: "stale_request" };
      }
      const normalizedFrame = Math.max(0, Math.round(args.targetFrameIndex));
      const key = chunkDecoderCacheKey(taskId, normalizedFrame, resourceKey);
      const cached =
        activeBitmapRef.current?.key === key
          ? activeBitmapRef.current.entry
          : (cacheRef.current.peek(key) ?? uncachedBitmapRef.current.get(key)?.entry);
      if (cached) {
        if (args.retainUncached ?? true) {
          setDiagnostics((cur) => ({
            ...cur,
            lastQueueMs: 0,
            lastCodecMs: 0,
            lastBitmapMs: 0,
            lastDecodeMs: 0,
            lastDecodeMode: "bitmap-cache",
          }));
        }
        return { bitmap: cached, fallbackReason: null };
      }
      const existing = inFlightRef.current.get(key);
      if (existing) return existing;
      const lifecycleGeneration = lifecycleGenerationRef.current;
      const startedAt = typeof performance !== "undefined" ? performance.now() : 0;
      const promise = (async (): Promise<VideoChunkDecodeResult> => {
        const isStillCurrent = () =>
          mountedRef.current &&
          lifecycleGenerationRef.current === lifecycleGeneration &&
          taskIdRef.current === taskId &&
          resourceKeyRef.current === resourceKey;
        const rememberSupplementalFrame = async (
          frame: VideoFrame,
          frameIndex: number,
          isDecoderCurrent: () => boolean,
        ): Promise<boolean> => {
          const supplementalStartedAt = typeof performance !== "undefined" ? performance.now() : 0;
          liveVideoFramesRef.current += 1;
          if (mountedRef.current) {
            setDiagnostics((cur) => ({
              ...cur,
              liveVideoFrames: liveVideoFramesRef.current,
            }));
          }
          let bitmap: ImageBitmap;
          const displayWidth = frame.displayWidth;
          const displayHeight = frame.displayHeight;
          try {
            bitmap = await globalThis.createImageBitmap(frame);
          } catch {
            return false;
          } finally {
            closeFrame(frame);
            liveVideoFramesRef.current = Math.max(0, liveVideoFramesRef.current - 1);
            if (mountedRef.current) {
              setDiagnostics((cur) => ({
                ...cur,
                liveVideoFrames: liveVideoFramesRef.current,
              }));
            }
          }
          if (!isStillCurrent() || !isDecoderCurrent()) {
            closeBitmap(bitmap);
            return false;
          }
          const entry: DecodedVideoFrameBitmap = {
            frameIndex,
            bitmap,
            width: bitmap.width || displayWidth,
            height: bitmap.height || displayHeight,
          };
          const decodeMs =
            typeof performance !== "undefined"
              ? Math.round(performance.now() - supplementalStartedAt)
              : 0;
          return remember(
            chunkDecoderCacheKey(taskId, frameIndex, resourceKey),
            entry,
            decodeMs,
            false,
          );
        };
        const slot = sessionRef.current;
        const sessionState = slot.session?.getStats().state;
        const reuse =
          !!slot.session &&
          !!slot.identity &&
          gopIdentityEquals(slot.identity, args.identity) &&
          (sessionState === "idle" || sessionState === "ready");
        if (!reuse) {
          slot.session?.dispose();
          slot.session = new VideoGopDecoderSession({
            plan: args.plan,
            identity: args.identity,
            onSupplementalFrame: rememberSupplementalFrame,
          });
          slot.identity = args.identity;
        }
        const session = slot.session as VideoGopDecoderSession;
        const targetSample = args.plan.samples.find((s) => s.frameIndex === normalizedFrame);
        if (!targetSample) {
          return { bitmap: null, fallbackReason: "invalid_sample_range" };
        }
        let outcome = await session.decode({
          frameIndex: normalizedFrame,
          targetDecodeIndex: targetSample.decodeIndex,
          targetTimestampUs: targetSample.timestampUs,
          generation: args.generation,
          allowReset: args.allowReset,
        });
        const decodeMs =
          typeof performance !== "undefined" ? Math.round(performance.now() - startedAt) : 0;
        let sessionStats = session.getStats();
        if (!outcome.ok && outcome.reason === "supplemental_cached") {
          const supplemental =
            cacheRef.current.peek(key) ?? uncachedBitmapRef.current.get(key)?.entry ?? null;
          if (supplemental) {
            if (args.retainUncached ?? true) {
              setDiagnostics((cur) => ({
                ...cur,
                lastQueueMs: sessionStats.lastQueueMs,
                lastCodecMs: sessionStats.lastCodecMs,
                lastBitmapMs: 0,
                lastDecodeMs: decodeMs,
                lastDecodeMode: "supplemental-cache",
              }));
            }
            return { bitmap: supplemental, fallbackReason: null };
          }
          if (args.allowReset === false) {
            return { bitmap: null, fallbackReason: "stale_request" };
          }
          await session.reset();
          outcome = await session.decode({
            frameIndex: normalizedFrame,
            targetDecodeIndex: targetSample.decodeIndex,
            targetTimestampUs: targetSample.timestampUs,
            generation: args.generation,
            allowReset: args.allowReset,
          });
          sessionStats = session.getStats();
        }
        if (!outcome.ok) {
          if (outcome.reason === "reset_required") {
            return { bitmap: null, fallbackReason: "stale_request" };
          }
          if (isStillCurrent()) {
            const reason: PreciseFrameFallbackReason =
              outcome.reason === "stale_request" || outcome.reason === "disposed"
                ? "stale_request"
                : outcome.reason === "out_of_gop"
                  ? "invalid_sample_range"
                  : outcome.reason === "codec_unsupported"
                    ? "codec_unsupported"
                    : "decode_failed";
            setDiagnostics((cur) => ({
              ...cur,
              errors: cur.errors + 1,
              lastFallbackReason: reason,
              activeDecoders: session.getStats().activeDecoders,
              liveVideoFrames: liveVideoFramesRef.current,
            }));
            return { bitmap: null, fallbackReason: reason };
          }
          return { bitmap: null, fallbackReason: "stale_request" };
        }
        const frame = outcome.frame;
        liveVideoFramesRef.current += 1;
        if (mountedRef.current) {
          setDiagnostics((cur) => ({
            ...cur,
            activeDecoders: session.getStats().activeDecoders,
            liveVideoFrames: liveVideoFramesRef.current,
          }));
        }
        let bitmap: ImageBitmap;
        const displayWidth = frame.displayWidth;
        const displayHeight = frame.displayHeight;
        const bitmapStartedAt = typeof performance !== "undefined" ? performance.now() : 0;
        try {
          bitmap = await globalThis.createImageBitmap(frame);
        } catch {
          if (isStillCurrent()) {
            setDiagnostics((cur) => ({
              ...cur,
              errors: cur.errors + 1,
              lastFallbackReason: "decode_failed",
            }));
            return { bitmap: null, fallbackReason: "decode_failed" };
          }
          return { bitmap: null, fallbackReason: "stale_request" };
        } finally {
          closeFrame(frame);
          liveVideoFramesRef.current = Math.max(0, liveVideoFramesRef.current - 1);
          if (mountedRef.current) {
            const currentStats = sessionRef.current.session?.getStats();
            setDiagnostics((cur) => ({
              ...cur,
              activeDecoders: currentStats?.activeDecoders ?? 0,
              liveVideoFrames: liveVideoFramesRef.current,
            }));
          }
        }
        if (!isStillCurrent()) {
          closeBitmap(bitmap);
          if (mountedRef.current) {
            setDiagnostics((cur) => ({ ...cur, staleResults: cur.staleResults + 1 }));
          }
          return { bitmap: null, fallbackReason: "stale_request" };
        }
        const entry: DecodedVideoFrameBitmap = {
          frameIndex: normalizedFrame,
          bitmap,
          width: bitmap.width || displayWidth,
          height: bitmap.height || displayHeight,
        };
        const totalMs =
          typeof performance !== "undefined"
            ? Math.max(0, performance.now() - startedAt)
            : decodeMs;
        const bitmapMs =
          typeof performance !== "undefined" ? Math.max(0, performance.now() - bitmapStartedAt) : 0;
        if (
          !remember(key, entry, totalMs, args.retainUncached ?? true, {
            queueMs: sessionStats.lastQueueMs,
            codecMs: sessionStats.lastCodecMs,
            bitmapMs,
            mode: "session",
          })
        ) {
          return { bitmap: null, fallbackReason: "memory_budget_exceeded" };
        }
        return { bitmap: entry, fallbackReason: null };
      })();
      inFlightRef.current.set(key, promise);
      try {
        return await promise;
      } finally {
        if (inFlightRef.current.get(key) === promise) inFlightRef.current.delete(key);
      }
    },
    [active, remember, taskId, resourceKey],
  );

  const showFrame = useCallback(
    (frameIndex: number): DecodedVideoFrameBitmap | null => {
      if (!taskId || !active) return null;
      const normalizedFrame = Math.max(0, Math.round(frameIndex));
      const key = chunkDecoderCacheKey(taskId, normalizedFrame, resourceKey);
      const cache = cacheRef.current;
      let owned: OwnedDecodedVideoFrameBitmap | null = null;
      if (activeBitmapRef.current?.key === key) {
        owned = activeBitmapRef.current;
      } else {
        const cached = cache.take(key);
        if (cached) {
          owned = { key, entry: cached.value, bytes: cached.bytes };
        } else {
          owned = uncachedBitmapRef.current.get(key) ?? null;
          if (owned) uncachedBitmapRef.current.delete(key);
        }
      }
      if (owned) {
        const previous = activeBitmapRef.current;
        if (previous && previous !== owned) retiredBitmapRef.current.push(previous);
        activeBitmapRef.current = owned;
        rebalanceBitmapCache();
        setActiveFrameIndex(normalizedFrame);
        setDiagnostics((cur) => ({
          ...cur,
          cacheSize: ownedBitmapCount(),
          activeFrameIndex: normalizedFrame,
          hits: cur.hits + 1,
        }));
        bumpVersion();
        return owned.entry;
      }
      setDiagnostics((cur) => ({ ...cur, misses: cur.misses + 1 }));
      return null;
    },
    [active, bumpVersion, ownedBitmapCount, rebalanceBitmapCache, taskId, resourceKey],
  );

  useEffect(() => {
    if (retiredBitmapRef.current.length === 0) return;
    const retired = retiredBitmapRef.current.splice(0);
    const cache = cacheRef.current;
    rebalanceBitmapCache();
    for (const owned of retired) {
      if (cache.has(owned.key) || uncachedBitmapRef.current.has(owned.key)) {
        closeBitmap(owned.entry.bitmap);
        continue;
      }
      const cached = cache.set(owned.key, {
        value: owned.entry,
        bytes: owned.bytes,
        dispose: ({ bitmap }) => closeBitmap(bitmap),
      });
      if (!cached) closeBitmap(owned.entry.bitmap);
    }
    rebalanceBitmapCache();
    bumpVersion();
  }, [bumpVersion, rebalanceBitmapCache, version]);

  const clear = useCallback(() => {
    lifecycleGenerationRef.current += 1;
    cacheRef.current.clear();
    if (activeBitmapRef.current) closeBitmap(activeBitmapRef.current.entry.bitmap);
    activeBitmapRef.current = null;
    for (const owned of uncachedBitmapRef.current.values()) closeBitmap(owned.entry.bitmap);
    uncachedBitmapRef.current.clear();
    for (const owned of retiredBitmapRef.current) closeBitmap(owned.entry.bitmap);
    retiredBitmapRef.current = [];
    rebalanceBitmapCache();
    inFlightRef.current.clear();
    sessionRef.current.session?.dispose();
    sessionRef.current = { session: null, identity: null };
    setActiveFrameIndex(null);
    setDiagnostics((cur) => ({
      ...cur,
      cacheSize: 0,
      activeFrameIndex: null,
      activeDecoders: 0,
      liveVideoFrames: liveVideoFramesRef.current,
    }));
    bumpVersion();
  }, [bumpVersion, rebalanceBitmapCache]);

  useEffect(() => {
    mountedRef.current = true;
    const cache = cacheRef.current;
    const inFlight = inFlightRef.current;
    const sessions = sessionRef;
    const activeBitmap = activeBitmapRef;
    const uncachedBitmaps = uncachedBitmapRef;
    const retiredBitmaps = retiredBitmapRef;
    return () => {
      mountedRef.current = false;
      lifecycleGenerationRef.current += 1;
      cache.clear();
      if (activeBitmap.current) closeBitmap(activeBitmap.current.entry.bitmap);
      activeBitmap.current = null;
      for (const owned of uncachedBitmaps.current.values()) closeBitmap(owned.entry.bitmap);
      uncachedBitmaps.current.clear();
      for (const owned of retiredBitmaps.current) closeBitmap(owned.entry.bitmap);
      retiredBitmaps.current = [];
      inFlight.clear();
      const session = sessions.current.session;
      sessions.current = { session: null, identity: null };
      session?.dispose();
    };
  }, []);

  useEffect(() => {
    sessionRef.current.session?.dispose();
    sessionRef.current = { session: null, identity: null };
  }, [resourceKey]);

  useEffect(() => {
    clear();
  }, [clear, taskId]);

  const activeBitmap = useMemo(() => {
    void version;
    if (!taskId || activeFrameIndex === null) return null;
    const activeOwned = activeBitmapRef.current;
    return activeOwned?.key === chunkDecoderCacheKey(taskId, activeFrameIndex, resourceKey)
      ? activeOwned.entry
      : null;
  }, [activeFrameIndex, taskId, resourceKey, version]);

  return {
    supported,
    enabled: resolvedEnabled,
    active,
    activeBitmap,
    activeFrameIndex,
    decodePlan,
    showFrame,
    clear,
    diagnostics,
  };
}

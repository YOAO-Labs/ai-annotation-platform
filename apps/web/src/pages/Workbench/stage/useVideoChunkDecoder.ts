import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  EncodedVideoDecodePlan,
  PreciseFrameFallbackReason,
  VideoGopPlan,
} from "./videoChunkDemux";
import { ByteLru } from "./videoByteLru";
import { VideoGopDecoderSession, type VideoGopSessionIdentity } from "./videoGopDecoderSession";

/**
 * WebCodecs 精确帧解码核心(实验性,默认开启并按客户端能力安全回退)。
 *
 * 用浏览器原生 WebCodecs(`VideoDecoder`)解 chunk 字节得到精确帧,绕过原生 `<video>`
 * seek 不精确的问题。解出的 `VideoFrame`(按 timestamp 匹配目标)转 `ImageBitmap` 入缓存,
 * 风格刻意对齐 `useVideoBitmapCache`(LRU + 命中/未命中计数)。
 *
 * 本 hook 只实现「解码核心 + 缓存」:给定 `EncodedVideoDecodePlan`(由 `videoChunkDemux`
 * 从后端 sample manifest 构造),解码目标帧并入缓存。**它不做 mp4 demux、不拉 chunk
 * 字节、不轮询 chunk 状态**——那是 `useVideoPreciseFrame` 的职责。flag 关闭或 WebCodecs
 * 不可用时本路径完全不激活,调用方继续走原有 `<video>` / native bitmap 路径。
 *
 * 连续播放仍由隐藏 `<video>` 提供;精确帧只在暂停 / 逐帧 / 稳定 seek 后使用。
 *
 * 缓存按字节预算淘汰(v0.23.14):单张 bitmap 按 codedWidth*codedHeight*4 估算占用,
 * 预算由性能档位 `videoDecoderBitmapCacheBytes` 决定。decode 走有状态 GOP session
 * (`VideoGopDecoderSession`):同一 GOP 内向前逐帧只提交增量 chunks,identity 变化时
 * 重建 decoder;`decodePlanToBitmap` 保留为无状态回退参考实现。
 */

/** localStorage / URL query 开关键。缺省开启，显式 0 / false 关闭。 */
export const WEBCODECS_FLAG_STORAGE_KEY = "video.experimental.webcodecs";
export const WEBCODECS_FLAG_QUERY_KEY = "webcodecs";
export const WEBCODECS_DEFAULT_ENABLED = true;

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
  /** isSecureContext 只作诊断字段(某些本地环境实现差异),不单独阻断。 */
  secureContext: boolean;
  cacheSize: number;
  activeFrameIndex: number | null;
  hits: number;
  misses: number;
  decodes: number;
  errors: number;
  /** isConfigSupported 返回 false(或 configure 抛错)的次数。 */
  configUnsupported: number;
  /** task 变化后被丢弃的解码结果次数。 */
  staleResults: number;
  lastFallbackReason: PreciseFrameFallbackReason | null;
  /** 最近一次前台 decode 在 GOP session 串行队列中的等待时间。 */
  lastQueueMs: number | null;
  /** 最近一次前台 decode 的 codec/session 执行时间（不含 bitmap 转换）。 */
  lastCodecMs: number | null;
  /** 最近一次前台 VideoFrame → ImageBitmap 转换时间。 */
  lastBitmapMs: number | null;
  /** 最近一次前台 decodePlan 总耗时。 */
  lastDecodeMs: number | null;
  /** 最近一次前台请求实际走过的路径，避免缓存命中沿用上一轮阶段耗时。 */
  lastDecodeMode: "bitmap-cache" | "supplemental-cache" | "session" | null;
  /** bitmap 缓存与 GOP session 的实时资源诊断(从 ByteLru / session 读取)。 */
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
  /** Immutable server output identity; never a signed URL. */
  resourceKey?: string | null;
  /** feature flag 的解析结果由调用方传入(或缺省时本 hook 自行从环境探测)。 */
  enabled?: boolean;
  /** 已解码 bitmap 的字节预算上限(性能档位 videoDecoderBitmapCacheBytes)。 */
  bitmapBudgetBytes?: number;
}

const DEFAULT_BITMAP_BUDGET_BYTES = 256 * 1024 * 1024;

/** 能力探测:当前运行环境是否提供 WebCodecs 解码所需的全部原语。jsdom 下为 false。 */
export function detectWebCodecsSupport(): boolean {
  return (
    typeof globalThis !== "undefined" &&
    typeof (globalThis as { VideoDecoder?: unknown }).VideoDecoder !== "undefined" &&
    typeof (globalThis as { EncodedVideoChunk?: unknown }).EncodedVideoChunk !== "undefined" &&
    typeof globalThis.createImageBitmap === "function"
  );
}

/** 缓存键:与 useVideoBitmapCache 同构(`${taskId}:${frameIndex}`)。 */
export function chunkDecoderCacheKey(
  taskId: string,
  frameIndex: number,
  resourceKey?: string | null,
): string {
  return resourceKey
    ? JSON.stringify([taskId, resourceKey, frameIndex])
    : `${taskId}:${frameIndex}`;
}

/** session identity 逐字段相等性比较(任一字段变化都必须重建 decoder)。 */
export function gopIdentityEquals(a: VideoGopSessionIdentity, b: VideoGopSessionIdentity): boolean {
  return (
    a.taskId === b.taskId &&
    a.datasetItemId === b.datasetItemId &&
    a.chunkId === b.chunkId &&
    a.gopStartDecodeIndex === b.gopStartDecodeIndex &&
    a.configFingerprint === b.configFingerprint
  );
}

/**
 * 解析实验开关。优先级：URL query > localStorage > 默认开启。
 * `1` / `true` 开启，`0` / `false` 关闭。纯函数，便于单测。
 */
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
    // localStorage 在隐私模式 / SSR 下可能不可用。
    return WEBCODECS_DEFAULT_ENABLED;
  }
}

/** 从当前浏览器环境读取 flag（window.location.search + localStorage）。 */
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

/** 估算单张 bitmap 的字节占用(RGBA 4 bytes/pixel)。 */
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

  // WebCodecs 把能力探测定义在构造器上,不是 decoder 实例方法。旧实现若缺少该静态方法,
  // 仍允许走 configure try/catch;真实 Chromium 则使用返回的规范化 config。
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

  // 用对象 ref 持有目标帧:TS 对闭包赋值的 let 变量不做 control-flow 收窄,直接用 let 会让
  // flush 后的读取被判定为初始 null → never;改走对象属性保持 VideoFrame | null 类型。
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
    // flush 后即便 promise 正常,只要有 decode error 或未命中目标帧,都按失败处理(不重复无限重试)。
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
  // 活动画面拥有独立所有权，不留在 LRU 中；否则预取或预算收缩可能 close Konva 正在使用的
  // ImageBitmap。单张超预算的前台结果暂存在 uncached，直到 showFrame 接管。
  const activeBitmapRef = useRef<OwnedDecodedVideoFrameBitmap | null>(null);
  const uncachedBitmapRef = useRef(new Map<string, OwnedDecodedVideoFrameBitmap>());
  const retiredBitmapRef = useRef<OwnedDecodedVideoFrameBitmap[]>([]);
  // 当前 GOP decoder session:identity 匹配且未 closed 时跨帧复用,变化则销毁重建。
  const sessionRef = useRef<{
    session: VideoGopDecoderSession | null;
    identity: VideoGopSessionIdentity | null;
  }>({ session: null, identity: null });
  // session 交付目标 frame 后，hook 在 createImageBitmap 完成前仍拥有该 VideoFrame。
  // 单独记账这段生命周期，不能只看 session.pendingTarget（decode promise 返回时它已清空）。
  const liveVideoFramesRef = useRef(0);
  // single-flight:同 `taskId:frameIndex` 的并发请求共享同一 promise(Set 只能拒绝,无法去重 await)。
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

  /** decode 成功只入缓存(不自动激活 active 指针);active 由显式 showFrame 修改。 */
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
          // LRU 外只保留一个等待展示的超预算帧，避免快速请求绕过总预算无限累积。
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

  // 字节预算变化(性能档位切换)立即收缩已有缓存。
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

  // bitmap / session 资源诊断:每次 re-render(version 变化)从 ref 读实时值。
  // decodePlan / remember / showFrame / clear 都 bumpVersion,故 stats 会随之刷新。
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
      /** 前台目标允许单张超预算 bitmap 在 LRU 外等待 showFrame 接管；预取必须为 false。 */
      retainUncached?: boolean;
      /** 后台预取不得触发 decoder reset；需要回退重解时直接跳过。 */
      allowReset?: boolean;
    }): Promise<VideoChunkDecodeResult> => {
      if (!taskId || !active) {
        return { bitmap: null, fallbackReason: "flag_disabled" };
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
          if (!isStillCurrent()) {
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
        // session 复用:identity 匹配且处于可用状态才复用；failed/closed 必须销毁重建。
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
        // 在每个 await 之后重新评估:createImageBitmap 阻塞期间可能发生 unmount / task 切换。
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
        // task / generation 变化或组件已卸载:关闭旧结果,绝不写回已清空缓存。
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

  /**
   * 显式激活某帧。命中后把 bitmap 所有权移出 LRU；旧活动帧延迟到新画面 commit 后再
   * 回缓存或 close，避免 Konva 在同一 render 间隙引用已关闭对象。
   */
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

  // showFrame 已触发新 render 后，旧活动帧才可重新交给 LRU；若其本身超预算则在此关闭。
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

  // 卸载清理:先使在途 generation 失效;稍后完成的 bitmap 会在 promise 内立即 close。
  useEffect(() => {
    mountedRef.current = true;
    const cache = cacheRef.current;
    const inFlight = inFlightRef.current;
    const sessionSlot = sessionRef.current;
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
      sessionSlot.session?.dispose();
    };
  }, []);

  useEffect(() => {
    sessionRef.current.session?.dispose();
    sessionRef.current = { session: null, identity: null };
  }, [resourceKey]);

  // taskId 切换时清空缓存。
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

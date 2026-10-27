import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "@/api/client";
import { tasksApi } from "@/api/tasks";
import type { VideoFrameOut } from "@/types";
import {
  useVideoDerivativeOwnerKey,
  videoDerivativeKey,
  videoDerivativeOwnerKey,
} from "@/api/videoDerivatives";

export type VideoFramePreviewFormat = "webp" | "jpeg";

export type VideoFramePreview =
  | {
      frameIndex: number;
      status: "pending";
      url: null;
      width: number;
      format: VideoFramePreviewFormat;
      error: null;
    }
  | {
      frameIndex: number;
      status: "ready";
      url: string;
      width: number;
      format: VideoFramePreviewFormat;
      error: null;
    }
  | {
      frameIndex: number;
      status: "error";
      url: null;
      width: number;
      format: VideoFramePreviewFormat;
      error: string;
    };

export interface VideoFramePreviewDiagnostics {
  cacheSize: number;
  inFlight: number;
  cacheHits: number;
  cacheMisses: number;
  requests: number;
  prefetchRequests: number;
  prefetchFrames: number;
  errors: number;
  unsupported: boolean;
  lastFrameIndex: number | null;
  lastStatus: VideoFramePreview["status"] | "idle";
}

interface UseVideoFramePreviewArgs {
  taskId: string | null | undefined;
  maxFrame: number;
  metadataGenerationId?: string | null;
  enabled?: boolean;
  width?: number;
  format?: VideoFramePreviewFormat;
  maxCacheItems?: number;
  scrubPrefetchHalfWindow?: number;
  anchorPrefetchCount?: number;
}

const DEFAULT_MAX_CACHE_ITEMS = 120;
const READY_CACHE_MS = 30_000;
interface CachedPreview {
  requestKey: string;
  value: VideoFramePreview;
  identity: string;
  expiresAt: number;
}
const MAX_PENDING_RETRIES = 10;
const RETRY_DELAY_MS = 800;
const RETRY_INITIAL_DELAY_MS = 200;
const DEFAULT_SCRUB_PREFETCH_HALF_WINDOW = 3;
const SCRUB_PREFETCH_MIN_STEP = 2;
const DEFAULT_ANCHOR_PREFETCH_COUNT = 8;
const EMPTY_DIAGNOSTICS: VideoFramePreviewDiagnostics = {
  cacheSize: 0,
  inFlight: 0,
  cacheHits: 0,
  cacheMisses: 0,
  requests: 0,
  prefetchRequests: 0,
  prefetchFrames: 0,
  errors: 0,
  unsupported: false,
  lastFrameIndex: null,
  lastStatus: "idle",
};

function clampFrame(frameIndex: number, maxFrame: number) {
  if (!Number.isFinite(frameIndex)) return 0;
  return Math.max(0, Math.min(maxFrame, Math.round(frameIndex)));
}

function cacheKey(
  taskId: string,
  frameIndex: number,
  width: number,
  format: VideoFramePreviewFormat,
) {
  return `${taskId}:${frameIndex}:${width}:${format}`;
}

function previewFromFrame(
  frame: VideoFrameOut,
  fallbackWidth: number,
  fallbackFormat: VideoFramePreviewFormat,
  metadataGenerationId?: string | null,
): VideoFramePreview {
  if (
    frame.status === "ready" &&
    (!videoDerivativeKey(frame) ||
      (metadataGenerationId && metadataGenerationId !== frame.metadata_generation_id))
  ) {
    return {
      frameIndex: frame.frame_index,
      width: frame.width,
      format: frame.format,
      status: "error",
      url: null,
      error: "视频预览版本不一致，请刷新后重试",
    };
  }
  if (frame.status === "ready" && frame.url) {
    return {
      frameIndex: frame.frame_index,
      status: "ready",
      url: frame.url,
      width: frame.width,
      format: frame.format,
      error: null,
    };
  }
  if (frame.status === "failed") {
    return {
      frameIndex: frame.frame_index,
      status: "error",
      url: null,
      width: frame.width || fallbackWidth,
      format: frame.format || fallbackFormat,
      error: frame.error || "frame preview failed",
    };
  }
  return {
    frameIndex: frame.frame_index,
    status: "pending",
    url: null,
    width: frame.width || fallbackWidth,
    format: frame.format || fallbackFormat,
    error: null,
  };
}

export function useVideoFramePreview({
  taskId,
  maxFrame,
  metadataGenerationId,
  enabled = true,
  width = 320,
  format = "webp",
  maxCacheItems = DEFAULT_MAX_CACHE_ITEMS,
  scrubPrefetchHalfWindow = DEFAULT_SCRUB_PREFETCH_HALF_WINDOW,
  anchorPrefetchCount = DEFAULT_ANCHOR_PREFETCH_COUNT,
}: UseVideoFramePreviewArgs) {
  const ownerKey = useVideoDerivativeOwnerKey();
  const scopeKey = JSON.stringify([ownerKey, taskId, metadataGenerationId, width, format, enabled]);
  const liveScopeRef = useRef(scopeKey);
  liveScopeRef.current = scopeKey;
  const [previewState, setPreviewState] = useState<{
    scope: string;
    value: VideoFramePreview | null;
  } | null>(null);
  const preview =
    enabled && previewState?.scope === scopeKey && !!ownerKey ? previewState.value : null;
  const setPreview = useCallback(
    (
      value:
        | VideoFramePreview
        | null
        | ((previous: VideoFramePreview | null) => VideoFramePreview | null),
    ) => {
      if (liveScopeRef.current !== scopeKey || ownerKey !== videoDerivativeOwnerKey()) return;
      setPreviewState((previous) => ({
        scope: scopeKey,
        value:
          typeof value === "function"
            ? value(previous?.scope === scopeKey ? previous.value : null)
            : value,
      }));
    },
    [ownerKey, scopeKey],
  );
  const [diagnostics, setDiagnostics] = useState<VideoFramePreviewDiagnostics>(EMPTY_DIAGNOSTICS);
  const cacheRef = useRef(new Map<string, CachedPreview>());
  const cacheIndexRef = useRef(new Map<string, string>());
  const cacheRequestRef = useRef(new Map<string, number>());
  const cacheRequestSequenceRef = useRef(0);
  const inFlightRef = useRef(new Map<string, AbortController>());
  const unsupportedTaskRef = useRef<string | null>(null);
  const activeRequestKeyRef = useRef<string | null>(null);
  const requestSeqRef = useRef(0);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const rafHandleRef = useRef<number | null>(null);
  const scheduledFrameRef = useRef<number | null>(null);
  const lastPrefetchAnchorRef = useRef<number | null>(null);
  const anchorPrefetchedTaskRef = useRef<string | null>(null);
  const foregroundControllerRef = useRef<AbortController | null>(null);
  const prefetchControllerRef = useRef<AbortController | null>(null);
  const anchorPrefetchControllerRef = useRef<AbortController | null>(null);

  const clearRetry = useCallback(() => {
    if (retryTimerRef.current) {
      clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }
  }, []);

  const patchDiagnostics = useCallback(
    (patch: Partial<VideoFramePreviewDiagnostics>) => {
      setDiagnostics((cur) => ({
        ...cur,
        ...patch,
        cacheSize: patch.cacheSize ?? cacheRef.current.size,
        inFlight: patch.inFlight ?? inFlightRef.current.size,
        unsupported: patch.unsupported ?? (taskId ? unsupportedTaskRef.current === taskId : false),
      }));
    },
    [taskId],
  );

  const remember = useCallback(
    (key: string, value: VideoFramePreview, identity: string) => {
      if (
        value.status === "pending" ||
        liveScopeRef.current !== scopeKey ||
        ownerKey !== videoDerivativeOwnerKey()
      )
        return;
      const cache = cacheRef.current;
      const oldKey = cacheIndexRef.current.get(key);
      if (oldKey) cache.delete(oldKey);
      const fullKey = JSON.stringify([scopeKey, key, identity]);
      cacheIndexRef.current.set(key, fullKey);
      cache.set(fullKey, {
        requestKey: key,
        value,
        identity,
        expiresAt: Date.now() + (value.status === "ready" ? READY_CACHE_MS : 1_000),
      });
      while (cache.size > maxCacheItems) {
        const oldest = cache.keys().next().value;
        if (!oldest) break;
        const evicted = cache.get(oldest);
        if (evicted) cacheIndexRef.current.delete(evicted.requestKey);
        cache.delete(oldest);
      }
      patchDiagnostics({ cacheSize: cache.size });
    },
    [maxCacheItems, patchDiagnostics, ownerKey, scopeKey],
  );

  const cachedPreview = useCallback((key: string) => {
    const fullKey = cacheIndexRef.current.get(key);
    const cached = fullKey ? cacheRef.current.get(fullKey) : undefined;
    if (!cached || cached.expiresAt <= Date.now()) {
      if (fullKey) cacheRef.current.delete(fullKey);
      cacheIndexRef.current.delete(key);
      return undefined;
    }
    return cached.value;
  }, []);

  useEffect(() => {
    const cache = cacheRef.current;
    while (cache.size > maxCacheItems) {
      const oldest = cache.keys().next().value;
      if (!oldest) break;
      const evicted = cache.get(oldest);
      if (evicted) cacheIndexRef.current.delete(evicted.requestKey);
      cache.delete(oldest);
    }
    patchDiagnostics({ cacheSize: cache.size });
  }, [maxCacheItems, patchDiagnostics]);

  const abortForegroundRequest = useCallback(() => {
    const controller = foregroundControllerRef.current;
    controller?.abort();
    foregroundControllerRef.current = null;
    if (!controller) return;
    for (const [pendingKey, pendingController] of inFlightRef.current) {
      if (pendingController === controller) inFlightRef.current.delete(pendingKey);
    }
  }, []);

  const fetchFrame = useCallback(
    (frameIndex: number, requestId: number, retryAttempt: number) => {
      if (!taskId || !ownerKey || !enabled || unsupportedTaskRef.current === taskId) return;
      const key = cacheKey(scopeKey, frameIndex, width, format);
      abortForegroundRequest();
      const controller = new AbortController();
      foregroundControllerRef.current = controller;
      inFlightRef.current.set(key, controller);
      const cacheRequest = ++cacheRequestSequenceRef.current;
      cacheRequestRef.current.set(key, cacheRequest);
      const finishRequest = () => {
        if (cacheRequestRef.current.get(key) === cacheRequest) cacheRequestRef.current.delete(key);
      };
      controller.signal.addEventListener("abort", finishRequest, { once: true });
      const isCurrent = () =>
        !controller.signal.aborted &&
        liveScopeRef.current === scopeKey &&
        videoDerivativeOwnerKey() === ownerKey;
      const finishInFlight = () => {
        if (inFlightRef.current.get(key) === controller) {
          inFlightRef.current.delete(key);
        }
      };
      setDiagnostics((cur) => ({
        ...cur,
        requests: cur.requests + 1,
        cacheSize: cacheRef.current.size,
        inFlight: inFlightRef.current.size,
        unsupported: taskId ? unsupportedTaskRef.current === taskId : false,
      }));
      void tasksApi
        .getVideoFrame(taskId, frameIndex, { width, format }, { signal: controller.signal })
        .then((frame) => {
          finishInFlight();
          if (
            !isCurrent() ||
            requestSeqRef.current !== requestId ||
            cacheRequestRef.current.get(key) !== cacheRequest
          )
            return;
          const next = previewFromFrame(frame, width, format, metadataGenerationId);
          if (next.status !== "pending") remember(key, next, videoDerivativeKey(frame) ?? "failed");
          setPreview(next);
          setDiagnostics((cur) => ({
            ...cur,
            cacheSize: cacheRef.current.size,
            inFlight: inFlightRef.current.size,
            lastFrameIndex: next.frameIndex,
            lastStatus: next.status,
            errors: next.status === "error" ? cur.errors + 1 : cur.errors,
          }));
          if (next.status === "pending" && retryAttempt < MAX_PENDING_RETRIES) {
            // Exponential backoff: poll quickly after the first miss so frames
            // that finish extraction fast show up promptly, then back off to the
            // server hint / cap so a slow worker is not hammered.
            const expBase = Math.min(
              RETRY_INITIAL_DELAY_MS * Math.pow(2, retryAttempt),
              RETRY_DELAY_MS,
            );
            const hintMs = frame.retry_after ? frame.retry_after * 1000 : 0;
            const retryDelay = Math.min(Math.max(expBase, hintMs), RETRY_DELAY_MS);
            retryTimerRef.current = setTimeout(() => {
              fetchFrame(frameIndex, requestId, retryAttempt + 1);
            }, retryDelay);
          }
        })
        .catch((err: unknown) => {
          finishInFlight();
          if (!isCurrent()) return;
          if (err instanceof ApiError && (err.status === 400 || err.status === 404)) {
            unsupportedTaskRef.current = taskId;
            if (requestSeqRef.current === requestId) setPreview(null);
            patchDiagnostics({ inFlight: inFlightRef.current.size, unsupported: true });
            return;
          }
          if (requestSeqRef.current !== requestId) return;
          const next: VideoFramePreview = {
            frameIndex,
            status: "error",
            url: null,
            width,
            format,
            error: err instanceof Error ? err.message : "frame preview failed",
          };
          remember(key, next, "failed");
          setPreview(next);
          setDiagnostics((cur) => ({
            ...cur,
            cacheSize: cacheRef.current.size,
            inFlight: inFlightRef.current.size,
            lastFrameIndex: next.frameIndex,
            lastStatus: next.status,
            errors: cur.errors + 1,
          }));
        })
        .finally(() => {
          finishRequest();
          if (foregroundControllerRef.current === controller) {
            foregroundControllerRef.current = null;
          }
        });
    },
    [
      abortForegroundRequest,
      enabled,
      format,
      patchDiagnostics,
      remember,
      taskId,
      width,
      ownerKey,
      scopeKey,
      metadataGenerationId,
      setPreview,
    ],
  );

  const cancelScheduledFetch = useCallback(() => {
    if (rafHandleRef.current !== null) {
      cancelAnimationFrame(rafHandleRef.current);
      rafHandleRef.current = null;
    }
    scheduledFrameRef.current = null;
  }, []);

  const requestPrefetch = useCallback(
    (frames: number[], purpose: "window" | "anchor" = "window") => {
      if (!taskId || !ownerKey || frames.length === 0) return;
      const controllerRef =
        purpose === "anchor" ? anchorPrefetchControllerRef : prefetchControllerRef;
      controllerRef.current?.abort();
      const controller = new AbortController();
      controllerRef.current = controller;
      const sequence = ++cacheRequestSequenceRef.current;
      for (const frame of frames)
        cacheRequestRef.current.set(cacheKey(scopeKey, frame, width, format), sequence);
      const finishRequest = () => {
        for (const frame of frames) {
          const key = cacheKey(scopeKey, frame, width, format);
          if (cacheRequestRef.current.get(key) === sequence) cacheRequestRef.current.delete(key);
        }
      };
      controller.signal.addEventListener("abort", finishRequest, { once: true });
      const isCurrent = () =>
        !controller.signal.aborted &&
        liveScopeRef.current === scopeKey &&
        videoDerivativeOwnerKey() === ownerKey;
      setDiagnostics((cur) => ({
        ...cur,
        prefetchRequests: cur.prefetchRequests + 1,
        prefetchFrames: cur.prefetchFrames + frames.length,
      }));
      void Promise.resolve(
        tasksApi.prefetchVideoFrames(
          taskId,
          frames,
          { width, format },
          { signal: controller.signal },
        ),
      )
        .then((response) => {
          if (!isCurrent() || !response || !Array.isArray(response.frames)) return;
          for (const frame of response.frames) {
            const key = cacheKey(scopeKey, frame.frame_index, width, format);
            if (cacheRequestRef.current.get(key) !== sequence) continue;
            const next = previewFromFrame(frame, width, format, metadataGenerationId);
            if (next.status !== "pending")
              remember(key, next, videoDerivativeKey(frame) ?? "failed");
          }
        })
        .catch((err: unknown) => {
          if (!isCurrent()) return;
          if (err instanceof ApiError && (err.status === 400 || err.status === 404)) {
            unsupportedTaskRef.current = taskId;
            patchDiagnostics({ unsupported: true });
          }
        })
        .finally(() => {
          finishRequest();
          if (controllerRef.current === controller) {
            controllerRef.current = null;
          }
        });
    },
    [format, patchDiagnostics, remember, taskId, width, ownerKey, scopeKey, metadataGenerationId],
  );

  const prefetch = useCallback(
    (rawFrames: readonly number[], purpose: "window" | "anchor" = "window") => {
      if (!taskId || !enabled || unsupportedTaskRef.current === taskId) return;
      const frames = [...new Set(rawFrames.map((frame) => clampFrame(frame, maxFrame)))]
        .filter(
          (frame) =>
            !cachedPreview(cacheKey(scopeKey, frame, width, format)) &&
            !inFlightRef.current.has(cacheKey(scopeKey, frame, width, format)),
        )
        .slice(0, 50);
      requestPrefetch(frames, purpose);
    },
    [enabled, format, maxFrame, requestPrefetch, taskId, width, cachedPreview, scopeKey],
  );

  const scrubPrefetch = useCallback(
    (anchor: number) => {
      if (!taskId || !enabled || unsupportedTaskRef.current === taskId) return;
      const last = lastPrefetchAnchorRef.current;
      if (last !== null && Math.abs(anchor - last) < SCRUB_PREFETCH_MIN_STEP) return;
      lastPrefetchAnchorRef.current = anchor;
      const frames: number[] = [];
      for (let offset = -scrubPrefetchHalfWindow; offset <= scrubPrefetchHalfWindow; offset++) {
        if (offset === 0) continue;
        const f = anchor + offset;
        if (f < 0 || f > maxFrame) continue;
        const key = cacheKey(scopeKey, f, width, format);
        if (cachedPreview(key) || inFlightRef.current.has(key)) continue;
        frames.push(f);
      }
      if (frames.length === 0) return;
      requestPrefetch(frames);
    },
    [
      enabled,
      format,
      maxFrame,
      requestPrefetch,
      scrubPrefetchHalfWindow,
      taskId,
      width,
      cachedPreview,
      scopeKey,
    ],
  );

  const seedAnchorsIfNeeded = useCallback(() => {
    if (!taskId || maxFrame <= 0) return;
    if (anchorPrefetchedTaskRef.current === taskId) return;
    anchorPrefetchedTaskRef.current = taskId;
    const anchors: number[] = [];
    const count = Math.max(1, Math.round(anchorPrefetchCount));
    const span = Math.max(1, count - 1);
    for (let i = 0; i < count; i++) {
      anchors.push(Math.round((i / span) * maxFrame));
    }
    // First scrub on a fresh task: seed evenly-spaced anchor frames so distant
    // jumps later (e.g. clicking a bookmark) land on a warm cache instead of a
    // cold worker.
    prefetch(anchors, "anchor");
  }, [anchorPrefetchCount, maxFrame, prefetch, taskId]);

  const flushScheduledFetch = useCallback(() => {
    rafHandleRef.current = null;
    const target = scheduledFrameRef.current;
    scheduledFrameRef.current = null;
    if (target === null || !taskId || !enabled || unsupportedTaskRef.current === taskId) return;
    const frameIndex = clampFrame(target, maxFrame);
    const key = cacheKey(scopeKey, frameIndex, width, format);
    const cached = cachedPreview(key);
    if (cached) {
      // Window prefetch may have filled the cache between scheduling and flush.
      setPreview(cached);
      scrubPrefetch(frameIndex);
      seedAnchorsIfNeeded();
      return;
    }
    if (!inFlightRef.current.has(key)) {
      fetchFrame(frameIndex, requestSeqRef.current, 0);
    }
    scrubPrefetch(frameIndex);
    seedAnchorsIfNeeded();
  }, [
    enabled,
    fetchFrame,
    format,
    maxFrame,
    scrubPrefetch,
    seedAnchorsIfNeeded,
    taskId,
    width,
    cachedPreview,
    scopeKey,
    setPreview,
  ]);

  const previewFor = useCallback(
    (rawFrameIndex: number | null) => {
      if (rawFrameIndex === null || !taskId || !enabled || unsupportedTaskRef.current === taskId) {
        cancelScheduledFetch();
        clearRetry();
        abortForegroundRequest();
        activeRequestKeyRef.current = null;
        requestSeqRef.current += 1;
        setPreview(null);
        return;
      }
      const frameIndex = clampFrame(rawFrameIndex, maxFrame);
      const key = cacheKey(scopeKey, frameIndex, width, format);
      const sameActiveRequest = activeRequestKeyRef.current === key;
      if (!sameActiveRequest) {
        clearRetry();
        abortForegroundRequest();
        activeRequestKeyRef.current = key;
        requestSeqRef.current += 1;
      }
      const cached = cachedPreview(key);
      if (cached) {
        cancelScheduledFetch();
        setPreview(cached);
        setDiagnostics((cur) => ({
          ...cur,
          cacheHits: cur.cacheHits + 1,
          cacheSize: cacheRef.current.size,
          inFlight: inFlightRef.current.size,
          lastFrameIndex: cached.frameIndex,
          lastStatus: cached.status,
        }));
        return;
      }
      // Cache miss while scrubbing: keep the previous ready preview on screen so
      // the popover does not flash back to "Loading F X" between adjacent frames.
      // Only fall back to a pending placeholder if nothing has been shown yet.
      setPreview((prev) => {
        if (prev && prev.status === "ready") return prev;
        return { frameIndex, status: "pending", url: null, width, format, error: null };
      });
      setDiagnostics((cur) => ({
        ...cur,
        cacheMisses: cur.cacheMisses + 1,
        cacheSize: cacheRef.current.size,
        inFlight: inFlightRef.current.size,
        lastFrameIndex: frameIndex,
        lastStatus: "pending",
      }));
      // Coalesce rapid scrub events to one fetch per animation frame so fast
      // pointer moves don't fire one request per pixel.
      scheduledFrameRef.current = frameIndex;
      if (rafHandleRef.current === null) {
        rafHandleRef.current = requestAnimationFrame(flushScheduledFetch);
      }
    },
    [
      cancelScheduledFetch,
      clearRetry,
      abortForegroundRequest,
      enabled,
      flushScheduledFetch,
      format,
      maxFrame,
      taskId,
      width,
      cachedPreview,
      scopeKey,
      setPreview,
    ],
  );

  const clear = useCallback(() => {
    cancelScheduledFetch();
    clearRetry();
    abortForegroundRequest();
    prefetchControllerRef.current?.abort();
    prefetchControllerRef.current = null;
    anchorPrefetchControllerRef.current?.abort();
    anchorPrefetchControllerRef.current = null;
    requestSeqRef.current += 1;
    activeRequestKeyRef.current = null;
    lastPrefetchAnchorRef.current = null;
    anchorPrefetchedTaskRef.current = null;
    cacheRef.current.clear();
    cacheIndexRef.current.clear();
    cacheRequestRef.current.clear();
    inFlightRef.current.clear();
    setPreview(null);
    setDiagnostics({
      ...EMPTY_DIAGNOSTICS,
      unsupported: taskId ? unsupportedTaskRef.current === taskId : false,
    });
  }, [abortForegroundRequest, cancelScheduledFetch, clearRetry, taskId, setPreview]);

  useEffect(() => {
    unsupportedTaskRef.current = null;
    clear();
  }, [clear, taskId, scopeKey]);

  useEffect(
    () => () => {
      cancelScheduledFetch();
      clearRetry();
      abortForegroundRequest();
      prefetchControllerRef.current?.abort();
      prefetchControllerRef.current = null;
      anchorPrefetchControllerRef.current?.abort();
      anchorPrefetchControllerRef.current = null;
      inFlightRef.current.clear();
    },
    [abortForegroundRequest, cancelScheduledFetch, clearRetry],
  );

  return { preview, previewFor, prefetch, clear, diagnostics };
}

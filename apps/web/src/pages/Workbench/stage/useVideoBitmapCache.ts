import { useCallback, useEffect, useMemo, useRef, useState } from "react";

export interface CachedVideoBitmap {
  frameIndex: number;
  bitmap: ImageBitmap;
  width: number;
  height: number;
}

export interface VideoBitmapCacheDiagnostics {
  supported: boolean;
  cacheSize: number;
  activeFrameIndex: number | null;
  hits: number;
  misses: number;
  captures: number;
  errors: number;
}

interface UseVideoBitmapCacheArgs {
  taskId: string | null | undefined;
  sourceKey?: string;
  maxItems?: number;
}

export interface VideoBitmapCaptureProof {
  isCurrent: () => boolean;
}

interface OwnedVideoBitmap extends CachedVideoBitmap {
  ownerEpoch: number;
}

const DEFAULT_MAX_ITEMS = 48;

function closeBitmap(bitmap: ImageBitmap) {
  try {
    bitmap.close();
  } catch {
    // Some test doubles and older engines do not implement close.
  }
}

function bitmapKey(taskId: string, frameIndex: number) {
  return `${taskId}:${frameIndex}`;
}

function rangesFromFrames(frames: number[]) {
  const sorted = [...new Set(frames)].sort((a, b) => a - b);
  const ranges: { from: number; to: number }[] = [];
  for (const frame of sorted) {
    const last = ranges[ranges.length - 1];
    if (last && frame === last.to + 1) {
      last.to = frame;
    } else {
      ranges.push({ from: frame, to: frame });
    }
  }
  return ranges;
}

export function useVideoBitmapCache({
  taskId,
  sourceKey = "",
  maxItems = DEFAULT_MAX_ITEMS,
}: UseVideoBitmapCacheArgs) {
  const supported = typeof window !== "undefined" && typeof window.createImageBitmap === "function";
  const cacheRef = useRef(new Map<string, OwnedVideoBitmap>());
  const inFlightRef = useRef(new Map<string, symbol>());
  const ownerRef = useRef({ taskId, sourceKey, epoch: 0 });
  if (ownerRef.current.taskId !== taskId || ownerRef.current.sourceKey !== sourceKey) {
    ownerRef.current = { taskId, sourceKey, epoch: ownerRef.current.epoch + 1 };
  }
  const mountedRef = useRef(true);
  const [activeFrameIndex, setActiveFrameIndex] = useState<number | null>(null);
  const [version, setVersion] = useState(0);
  const [diagnostics, setDiagnostics] = useState<VideoBitmapCacheDiagnostics>({
    supported,
    cacheSize: 0,
    activeFrameIndex: null,
    hits: 0,
    misses: 0,
    captures: 0,
    errors: 0,
  });

  const bumpVersion = useCallback(() => setVersion((v) => v + 1), []);

  const remember = useCallback(
    (key: string, entry: OwnedVideoBitmap) => {
      const cache = cacheRef.current;
      const old = cache.get(key);
      if (old) closeBitmap(old.bitmap);
      cache.delete(key);
      cache.set(key, entry);
      while (cache.size > maxItems) {
        const oldestKey = cache.keys().next().value;
        if (!oldestKey) break;
        const oldest = cache.get(oldestKey);
        if (oldest) closeBitmap(oldest.bitmap);
        cache.delete(oldestKey);
      }
      setDiagnostics((cur) => ({
        ...cur,
        supported,
        cacheSize: cache.size,
        activeFrameIndex: entry.frameIndex,
        captures: cur.captures + 1,
      }));
      bumpVersion();
    },
    [bumpVersion, maxItems, supported],
  );

  useEffect(() => {
    const cache = cacheRef.current;
    while (cache.size > maxItems) {
      const oldestKey = cache.keys().next().value;
      if (!oldestKey) break;
      const oldest = cache.get(oldestKey);
      if (oldest) closeBitmap(oldest.bitmap);
      cache.delete(oldestKey);
    }
    setDiagnostics((cur) => ({ ...cur, supported, cacheSize: cache.size }));
    bumpVersion();
  }, [bumpVersion, maxItems, supported]);

  const capture = useCallback(
    async (video: HTMLVideoElement | null, frameIndex: number, proof: VideoBitmapCaptureProof) => {
      if (!taskId || !supported || !video || !mountedRef.current || !proof.isCurrent()) return null;
      if (!Number.isInteger(frameIndex) || frameIndex < 0) return null;
      if (video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) return null;
      if (!video.videoWidth || !video.videoHeight) return null;
      const epoch = ownerRef.current.epoch;
      const normalizedFrame = frameIndex;
      const key = bitmapKey(taskId, normalizedFrame);
      const cached = cacheRef.current.get(key);
      if (cached?.ownerEpoch === epoch) {
        setActiveFrameIndex(normalizedFrame);
        return cached;
      }
      if (inFlightRef.current.has(key)) return null;
      const request = Symbol(key);
      inFlightRef.current.set(key, request);
      const ownsCapture = () =>
        mountedRef.current &&
        ownerRef.current.epoch === epoch &&
        ownerRef.current.taskId === taskId &&
        ownerRef.current.sourceKey === sourceKey &&
        inFlightRef.current.get(key) === request &&
        proof.isCurrent();
      try {
        const bitmap = await window.createImageBitmap(video);
        if (!ownsCapture()) {
          closeBitmap(bitmap);
          return null;
        }
        const entry: OwnedVideoBitmap = {
          ownerEpoch: epoch,
          frameIndex: normalizedFrame,
          bitmap,
          width: bitmap.width || video.videoWidth,
          height: bitmap.height || video.videoHeight,
        };
        remember(key, entry);
        setActiveFrameIndex(normalizedFrame);
        return entry;
      } catch {
        if (!ownsCapture()) return null;
        setDiagnostics((cur) => ({
          ...cur,
          supported,
          errors: cur.errors + 1,
          cacheSize: cacheRef.current.size,
        }));
        return null;
      } finally {
        if (inFlightRef.current.get(key) === request) inFlightRef.current.delete(key);
      }
    },
    [remember, sourceKey, supported, taskId],
  );

  const showFrame = useCallback(
    (frameIndex: number) => {
      if (!taskId || !supported) return null;
      const normalizedFrame = Math.max(0, Math.round(frameIndex));
      const key = bitmapKey(taskId, normalizedFrame);
      const cached = cacheRef.current.get(key);
      if (cached?.ownerEpoch === ownerRef.current.epoch) {
        cacheRef.current.delete(key);
        cacheRef.current.set(key, cached);
        setActiveFrameIndex(normalizedFrame);
        setDiagnostics((cur) => ({
          ...cur,
          supported,
          cacheSize: cacheRef.current.size,
          activeFrameIndex: normalizedFrame,
          hits: cur.hits + 1,
        }));
        bumpVersion();
        return cached;
      }
      setDiagnostics((cur) => ({
        ...cur,
        supported,
        cacheSize: cacheRef.current.size,
        misses: cur.misses + 1,
      }));
      return null;
    },
    [bumpVersion, supported, taskId],
  );

  const clear = useCallback(() => {
    ownerRef.current.epoch += 1;
    for (const entry of cacheRef.current.values()) closeBitmap(entry.bitmap);
    cacheRef.current.clear();
    inFlightRef.current.clear();
    setActiveFrameIndex(null);
    setDiagnostics((cur) => ({
      ...cur,
      supported,
      cacheSize: 0,
      activeFrameIndex: null,
    }));
    bumpVersion();
  }, [bumpVersion, supported]);

  useEffect(() => {
    mountedRef.current = true;
    const cache = cacheRef.current;
    const inFlight = inFlightRef.current;
    return () => {
      mountedRef.current = false;
      ownerRef.current.epoch += 1;
      for (const entry of cache.values()) closeBitmap(entry.bitmap);
      cache.clear();
      inFlight.clear();
    };
  }, []);

  useEffect(() => {
    clear();
  }, [clear, sourceKey, taskId]);

  const ownerEpoch = ownerRef.current.epoch;
  const activeBitmap = useMemo(() => {
    void version;
    if (!taskId || activeFrameIndex === null) return null;
    const entry = cacheRef.current.get(bitmapKey(taskId, activeFrameIndex));
    return entry?.ownerEpoch === ownerEpoch ? entry : null;
  }, [activeFrameIndex, ownerEpoch, taskId, version]);

  const cachedRanges = useMemo(() => {
    void version;
    return rangesFromFrames(
      [...cacheRef.current.values()]
        .filter((entry) => entry.ownerEpoch === ownerEpoch)
        .map((entry) => entry.frameIndex),
    );
  }, [ownerEpoch, version]);

  return {
    activeBitmap,
    activeFrameIndex,
    cachedRanges,
    capture,
    showFrame,
    clear,
    diagnostics,
  };
}

import { useCallback, useEffect, useRef, useState } from "react";
import type { RefObject } from "react";
import { frameToSeekTime, timeToFrame, type FrameTimebase } from "./frameTimebase";

type FrameMetadata = { mediaTime: number };
type VideoFrameCallback = (now: DOMHighResTimeStamp, metadata: FrameMetadata) => void;
type VideoWithFrameCallback = HTMLVideoElement & {
  requestVideoFrameCallback?: (callback: VideoFrameCallback) => number;
  cancelVideoFrameCallback?: (handle: number) => void;
};

export interface FrameClockDiagnostics {
  seekCount: number;
  staleCallbacks: number;
  longTasks: number;
  lastSeekMs: number | null;
  lastFrameReadySource: "rvfc" | "seeked" | "timeupdate" | "raf" | "timeout" | null;
  recentSeeks: Array<{
    frameIndex: number;
    ms: number | null;
    source: "rvfc" | "seeked" | "timeupdate" | "raf" | "timeout";
    at: string;
  }>;
}

type FrameReadySource = NonNullable<FrameClockDiagnostics["lastFrameReadySource"]>;

export interface FrameSeekResult {
  status: "ready" | "cancelled" | "timeout" | "unavailable";
  frameIndex: number;
  source: FrameClockDiagnostics["lastFrameReadySource"];
}

export interface NativeVideoFrameEvidence {
  frameIndex: number;
  mediaTime: number;
  video: HTMLVideoElement;
  ownerEpoch: number;
}

interface UseFrameClockOptions {
  videoRef: RefObject<HTMLVideoElement | null>;
  sourceKey?: string;
  frameIndex: number;
  timebase: FrameTimebase;
  isPlaying: boolean;
  onFrameChange: (frameIndex: number) => void;
}

interface PendingSeek {
  id: number;
  frameIndex: number;
  ownerEpoch: number;
  startedAt: number;
  timer: ReturnType<typeof setTimeout> | null;
  resolve?: (result: FrameSeekResult) => void;
}

const SEEK_TIMEOUT_MS = 300;
const MAX_RECENT_SEEKS = 12;

export function useFrameClock({
  videoRef,
  sourceKey = "",
  frameIndex,
  timebase,
  isPlaying,
  onFrameChange,
}: UseFrameClockOptions) {
  const [isSeeking, setIsSeeking] = useState(false);
  const [nativeFrame, setNativeFrame] = useState<NativeVideoFrameEvidence | null>(null);
  const [diagnostics, setDiagnostics] = useState<FrameClockDiagnostics>({
    seekCount: 0,
    staleCallbacks: 0,
    longTasks: 0,
    lastSeekMs: null,
    lastFrameReadySource: null,
    recentSeeks: [],
  });
  const diagnosticsRef = useRef(diagnostics);
  const latestSeekIdRef = useRef(0);
  const pendingSeekRef = useRef<PendingSeek | null>(null);
  const seekFrameCallbackRef = useRef<{ video: VideoWithFrameCallback; handle: number } | null>(
    null,
  );
  const nativeFrameRef = useRef<NativeVideoFrameEvidence | null>(null);
  const mountedRef = useRef(false);
  const latestRef = useRef({ frameIndex, timebase, isPlaying, onFrameChange });
  latestRef.current = { frameIndex, timebase, isPlaying, onFrameChange };
  const identityRef = useRef({ sourceKey, video: videoRef.current, timebase, epoch: 0 });
  const previousIdentity = identityRef.current;
  if (
    previousIdentity.sourceKey !== sourceKey ||
    previousIdentity.video !== videoRef.current ||
    previousIdentity.timebase.fps !== timebase.fps ||
    previousIdentity.timebase.frameCount !== timebase.frameCount ||
    previousIdentity.timebase.ptsMs !== timebase.ptsMs
  ) {
    identityRef.current = {
      sourceKey,
      video: videoRef.current,
      timebase,
      epoch: previousIdentity.epoch + 1,
    };
  }
  const ownerEpoch = identityRef.current.epoch;

  const patchDiagnostics = useCallback((patch: Partial<FrameClockDiagnostics>) => {
    if (!mountedRef.current) return;
    const next = { ...diagnosticsRef.current, ...patch };
    diagnosticsRef.current = next;
    setDiagnostics(next);
  }, []);

  const clearSeekFrameCallback = useCallback(() => {
    const callback = seekFrameCallbackRef.current;
    seekFrameCallbackRef.current = null;
    if (callback) callback.video.cancelVideoFrameCallback?.(callback.handle);
  }, []);

  const settleSeek = useCallback(
    (pending: PendingSeek, result: FrameSeekResult, publish = true) => {
      if (pendingSeekRef.current !== pending) return;
      pendingSeekRef.current = null;
      if (pending.timer !== null) clearTimeout(pending.timer);
      clearSeekFrameCallback();
      pending.resolve?.(result);
      if (publish && mountedRef.current) setIsSeeking(false);
    },
    [clearSeekFrameCallback],
  );

  const cancelPendingSeek = useCallback(
    (publish = true) => {
      const pending = pendingSeekRef.current;
      if (pending)
        settleSeek(
          pending,
          { status: "cancelled", frameIndex: pending.frameIndex, source: null },
          publish,
        );
    },
    [settleSeek],
  );

  const getFrameEvidence = useCallback(
    (targetFrame: number): NativeVideoFrameEvidence | null => {
      const evidence = nativeFrameRef.current;
      const video = videoRef.current;
      const sourceTimebase = latestRef.current.timebase;
      const sourcePts = sourceTimebase.ptsMs?.[targetFrame];
      if (
        !mountedRef.current ||
        !evidence ||
        evidence.ownerEpoch !== identityRef.current.epoch ||
        evidence.video !== video ||
        evidence.frameIndex !== targetFrame ||
        !video ||
        video.seeking ||
        video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA ||
        !video.videoWidth ||
        !video.videoHeight ||
        sourceTimebase.source !== "ffprobe" ||
        sourcePts === undefined ||
        !Number.isFinite(sourcePts) ||
        Math.abs(evidence.mediaTime * 1000 - sourcePts) > 0.501 ||
        timeToFrame(video.currentTime, sourceTimebase) !== targetFrame
      )
        return null;
      return evidence;
    },
    [videoRef],
  );

  const recordSeek = useCallback(
    (pending: PendingSeek, source: FrameReadySource) => {
      const ms = source === "timeout" ? null : Math.round(performance.now() - pending.startedAt);
      patchDiagnostics({
        lastFrameReadySource: source,
        lastSeekMs: ms,
        recentSeeks: [
          { frameIndex: pending.frameIndex, ms, source, at: new Date().toISOString() },
          ...diagnosticsRef.current.recentSeeks,
        ].slice(0, MAX_RECENT_SEEKS),
      });
    },
    [patchDiagnostics],
  );

  const updateFrameFromTime = useCallback(
    (mediaTime: number, source: FrameReadySource, video: HTMLVideoElement, epoch: number) => {
      if (
        !mountedRef.current ||
        identityRef.current.epoch !== epoch ||
        videoRef.current !== video ||
        !Number.isFinite(mediaTime)
      )
        return;
      const latest = latestRef.current;
      const mediaFrame = timeToFrame(mediaTime, latest.timebase);
      if (source === "rvfc") {
        const previous = nativeFrameRef.current;
        if (
          !previous ||
          previous.frameIndex !== mediaFrame ||
          previous.mediaTime !== mediaTime ||
          previous.video !== video ||
          previous.ownerEpoch !== epoch
        ) {
          const evidence = { frameIndex: mediaFrame, mediaTime, video, ownerEpoch: epoch };
          nativeFrameRef.current = evidence;
          setNativeFrame(evidence);
        }
      }
      const pending = pendingSeekRef.current;
      if (pending) {
        if (pending.ownerEpoch !== epoch || pending.frameIndex !== mediaFrame) {
          patchDiagnostics({ staleCallbacks: diagnosticsRef.current.staleCallbacks + 1 });
          return;
        }
        latest.onFrameChange(mediaFrame);
        if (getFrameEvidence(mediaFrame)) {
          recordSeek(pending, "rvfc");
          settleSeek(pending, { status: "ready", frameIndex: mediaFrame, source: "rvfc" });
        } else if (
          source === "seeked" &&
          typeof (video as VideoWithFrameCallback).requestVideoFrameCallback !== "function"
        ) {
          settleSeek(pending, { status: "unavailable", frameIndex: pending.frameIndex, source });
        }
        return;
      }
      if (latest.isPlaying) latest.onFrameChange(mediaFrame);
      patchDiagnostics({ lastFrameReadySource: source });
    },
    [getFrameEvidence, patchDiagnostics, recordSeek, settleSeek, videoRef],
  );

  const startSeek = useCallback(
    (nextFrame: number, resolve?: (result: FrameSeekResult) => void) => {
      const latest = latestRef.current;
      const maxFrame = Math.max(0, latest.timebase.frameCount - 1);
      const target = Number.isFinite(nextFrame)
        ? Math.max(0, Math.min(maxFrame, Math.round(nextFrame)))
        : 0;
      cancelPendingSeek();
      const evidence = getFrameEvidence(target);
      const pending: PendingSeek = {
        id: ++latestSeekIdRef.current,
        frameIndex: target,
        ownerEpoch: identityRef.current.epoch,
        startedAt: performance.now(),
        timer: null,
        resolve,
      };
      pendingSeekRef.current = pending;
      patchDiagnostics({ seekCount: diagnosticsRef.current.seekCount + 1 });
      latest.onFrameChange(target);
      const video = videoRef.current;
      if (!mountedRef.current || !video) {
        settleSeek(pending, { status: "unavailable", frameIndex: target, source: null });
        return pending.id;
      }
      if (evidence) {
        recordSeek(pending, "rvfc");
        settleSeek(pending, { status: "ready", frameIndex: target, source: "rvfc" });
        return pending.id;
      }
      nativeFrameRef.current = null;
      setNativeFrame(null);
      setIsSeeking(true);
      pending.timer = setTimeout(() => {
        if (pendingSeekRef.current !== pending || pending.ownerEpoch !== identityRef.current.epoch)
          return;
        recordSeek(pending, "timeout");
        settleSeek(pending, { status: "timeout", frameIndex: target, source: "timeout" });
      }, SEEK_TIMEOUT_MS);
      const frameVideo = video as VideoWithFrameCallback;
      if (typeof frameVideo.requestVideoFrameCallback === "function") {
        const observe: VideoFrameCallback = (_now, metadata) => {
          if (
            pendingSeekRef.current !== pending ||
            pending.ownerEpoch !== identityRef.current.epoch
          )
            return;
          seekFrameCallbackRef.current = null;
          updateFrameFromTime(metadata.mediaTime, "rvfc", video, pending.ownerEpoch);
          if (pendingSeekRef.current === pending) {
            const handle = frameVideo.requestVideoFrameCallback?.(observe);
            if (handle !== undefined) seekFrameCallbackRef.current = { video: frameVideo, handle };
          }
        };
        seekFrameCallbackRef.current = {
          video: frameVideo,
          handle: frameVideo.requestVideoFrameCallback(observe),
        };
      }
      try {
        video.currentTime = frameToSeekTime(target, latest.timebase);
      } catch {
        settleSeek(pending, { status: "unavailable", frameIndex: target, source: null });
      }
      return pending.id;
    },
    [
      cancelPendingSeek,
      getFrameEvidence,
      patchDiagnostics,
      recordSeek,
      settleSeek,
      updateFrameFromTime,
      videoRef,
    ],
  );

  const seekTo = useCallback((nextFrame: number) => startSeek(nextFrame), [startSeek]);
  const seekToAsync = useCallback(
    (nextFrame: number) =>
      new Promise<FrameSeekResult>((resolve) => {
        startSeek(nextFrame, resolve);
      }),
    [startSeek],
  );

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      cancelPendingSeek(false);
      nativeFrameRef.current = null;
    };
  }, [cancelPendingSeek]);

  useEffect(() => {
    cancelPendingSeek();
    setIsSeeking(false);
    nativeFrameRef.current = null;
    setNativeFrame(null);
    return () => {
      cancelPendingSeek(false);
      nativeFrameRef.current = null;
    };
  }, [cancelPendingSeek, ownerEpoch]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    const epoch = ownerEpoch;
    if (identityRef.current.epoch === epoch) identityRef.current.video = video;
    let disposed = false;
    let frameHandle: number | null = null;
    const frameVideo = video as VideoWithFrameCallback;
    const onSeeked = () => updateFrameFromTime(video.currentTime, "seeked", video, epoch);
    const onTimeUpdate = () => updateFrameFromTime(video.currentTime, "timeupdate", video, epoch);
    video.addEventListener("seeked", onSeeked);
    video.addEventListener("timeupdate", onTimeUpdate);
    if (typeof frameVideo.requestVideoFrameCallback === "function") {
      const tick: VideoFrameCallback = (_now, metadata) => {
        if (disposed || identityRef.current.epoch !== epoch) return;
        updateFrameFromTime(metadata.mediaTime, "rvfc", video, epoch);
        frameHandle = frameVideo.requestVideoFrameCallback?.(tick) ?? null;
      };
      frameHandle = frameVideo.requestVideoFrameCallback(tick);
    }
    return () => {
      disposed = true;
      video.removeEventListener("seeked", onSeeked);
      video.removeEventListener("timeupdate", onTimeUpdate);
      if (frameHandle !== null) frameVideo.cancelVideoFrameCallback?.(frameHandle);
    };
  }, [isPlaying, ownerEpoch, updateFrameFromTime, videoRef]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video || isPlaying) return;
    const tolerance = Math.max(0.001, 0.5 / timebase.fps);
    const nextTime = frameToSeekTime(frameIndex, timebase);
    if (Math.abs(video.currentTime - nextTime) > tolerance) startSeek(frameIndex);
  }, [frameIndex, isPlaying, ownerEpoch, startSeek, timebase, videoRef]);

  useEffect(() => {
    const video = videoRef.current;
    if (
      !video ||
      !isPlaying ||
      typeof (video as VideoWithFrameCallback).requestVideoFrameCallback === "function"
    )
      return;
    const epoch = ownerEpoch;
    const schedule =
      typeof requestAnimationFrame === "function"
        ? requestAnimationFrame
        : (cb: FrameRequestCallback) => window.setTimeout(() => cb(performance.now()), 16);
    const cancel =
      typeof cancelAnimationFrame === "function" ? cancelAnimationFrame : window.clearTimeout;
    let disposed = false;
    let raf = 0;
    const tick = () => {
      if (disposed || identityRef.current.epoch !== epoch) return;
      updateFrameFromTime(video.currentTime, "raf", video, epoch);
      raf = schedule(tick);
    };
    raf = schedule(tick);
    return () => {
      disposed = true;
      cancel(raf);
    };
  }, [isPlaying, ownerEpoch, updateFrameFromTime, videoRef]);

  useEffect(() => {
    if (!import.meta.env.DEV || typeof PerformanceObserver === "undefined") return;
    const supportedEntryTypes =
      (PerformanceObserver as unknown as { supportedEntryTypes?: string[] }).supportedEntryTypes ??
      [];
    if (!supportedEntryTypes.includes("longtask")) return;
    const observer = new PerformanceObserver((list) => {
      patchDiagnostics({ longTasks: diagnosticsRef.current.longTasks + list.getEntries().length });
    });
    observer.observe({ entryTypes: ["longtask"] });
    return () => observer.disconnect();
  }, [patchDiagnostics]);

  return {
    currentFrame: frameIndex,
    isSeeking,
    seekTo,
    seekToAsync,
    getFrameEvidence,
    nativeFrame: nativeFrame?.ownerEpoch === ownerEpoch ? nativeFrame : null,
    diagnostics,
    diagnosticsRef,
  };
}

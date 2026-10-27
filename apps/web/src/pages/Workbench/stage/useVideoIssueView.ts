import { useCallback, useLayoutEffect, useRef, useState } from "react";
import type { Dispatch, RefObject, SetStateAction } from "react";
import type { Viewport } from "./shared/useViewportTransform";
import { fitToCanvas } from "./shared/viewport/fit";
import type { TimelineWindow } from "./timelineCoords";
import { captureVideoIssueViewport, restoreVideoIssueViewport } from "./videoIssueViewport";
import type {
  VideoIssueRestoreLease,
  VideoIssueView,
  VideoIssueViewCapture,
  VideoIssueViewRestoreResult,
  VideoTimelineWindowControls,
} from "./videoStageControls";

export const VIDEO_ISSUE_VIEW_READY_TIMEOUT_MS = 3000;

interface Options {
  sourceKey: string;
  taskId: string | null;
  frameIndex: number;
  selectedId: string | null;
  viewport: Viewport;
  setViewport: Dispatch<SetStateAction<Viewport>>;
  containerSize: { w: number; h: number };
  mediaSize: { w: number; h: number };
  hasRealMediaSize: boolean;
  sourceReady: boolean;
  autoFitOnResize: boolean;
  focusSelectionEnabled: boolean;
  focusObject: (id: string) => void;
  timelineRef: RefObject<VideoTimelineWindowControls | null>;
}

interface Waiter {
  epoch: number;
  finish: (ready: boolean) => void;
}

interface RestoreRequest {
  view: VideoIssueView;
  selectedId: string | null;
  promise: Promise<VideoIssueViewRestoreResult>;
  resolve: (result: VideoIssueViewRestoreResult) => void;
  applied: { viewport: Viewport; window: TimelineWindow; clamped: boolean } | null;
}

interface Lease {
  epoch: number;
  frameIndex: number;
  initialSelection: string | null;
  isRelevant: () => boolean;
  signal?: AbortSignal;
  abort: () => void;
  timer: ReturnType<typeof setTimeout> | null;
  closed: VideoIssueViewRestoreResult["status"] | null;
  request: RestoreRequest | null;
}

function sameViewport(left: Viewport, right: Viewport) {
  return left.scale === right.scale && left.tx === right.tx && left.ty === right.ty;
}

function sameWindow(left: TimelineWindow, right: TimelineWindow) {
  return left.from === right.from && left.to === right.to;
}

/** Coordinates one restore transaction; viewport and window remain owned by Stage and Overlay. */
export function useVideoIssueView(options: Options) {
  const latestRef = useRef(options);
  latestRef.current = options;
  const identityRef = useRef({ key: options.sourceKey, epoch: 0 });
  if (identityRef.current.key !== options.sourceKey) {
    identityRef.current = { key: options.sourceKey, epoch: identityRef.current.epoch + 1 };
  }
  const epoch = identityRef.current.epoch;
  const mountedRef = useRef(false);
  const [, setRevision] = useState(0);
  const wake = useCallback(() => setRevision((revision) => revision + 1), []);
  const waitersRef = useRef(new Set<Waiter>());
  const leaseRef = useRef<Lease | null>(null);
  const fitRef = useRef<{
    epoch: number;
    media: string;
    container: string;
    pending: Viewport | null;
    ready: boolean;
  } | null>(null);
  const selectionRef = useRef<{ epoch: number; id: string | null; enabled: boolean } | null>(null);

  const settleLease = useCallback(
    (lease: Lease, status: VideoIssueViewRestoreResult["status"], clamped = false) => {
      if (lease.closed) return;
      lease.closed = status;
      if (lease.timer !== null) clearTimeout(lease.timer);
      lease.signal?.removeEventListener("abort", lease.abort);
      if (leaseRef.current === lease) leaseRef.current = null;
      lease.request?.resolve({ status, clamped });
    },
    [],
  );

  const cancelIssueRestore = useCallback(() => {
    const lease = leaseRef.current;
    if (lease) settleLease(lease, "cancelled");
  }, [settleLease]);

  const isCurrent = useCallback((lease: Lease) => {
    if (
      !mountedRef.current ||
      lease.closed ||
      leaseRef.current !== lease ||
      lease.epoch !== identityRef.current.epoch ||
      lease.frameIndex !== latestRef.current.frameIndex ||
      lease.signal?.aborted
    )
      return false;
    try {
      return lease.isRelevant();
    } catch {
      return false;
    }
  }, []);

  const isReady = useCallback(() => {
    const latest = latestRef.current;
    const fit = fitRef.current;
    return (
      mountedRef.current &&
      !!latest.taskId &&
      latest.sourceReady &&
      latest.hasRealMediaSize &&
      latest.containerSize.w > 0 &&
      latest.containerSize.h > 0 &&
      fit?.epoch === identityRef.current.epoch &&
      fit.media === `${latest.mediaSize.w}:${latest.mediaSize.h}:${latest.hasRealMediaSize}` &&
      (!latest.autoFitOnResize ||
        fit.container === `${latest.containerSize.w}:${latest.containerSize.h}`) &&
      fit.ready &&
      !fit.pending &&
      !!latest.timelineRef.current?.capture()
    );
  }, []);

  const captureIssueView = useCallback((): VideoIssueViewCapture | null => {
    if (identityRef.current.epoch !== epoch || !isReady()) return null;
    const latest = latestRef.current;
    const viewport = captureVideoIssueViewport(
      latest.viewport,
      latest.containerSize,
      latest.mediaSize,
    );
    const window = latest.timelineRef.current?.capture();
    return viewport && window && latest.taskId
      ? { taskId: latest.taskId, frameIndex: latest.frameIndex, viewport, timeline_window: window }
      : null;
  }, [epoch, isReady]);

  const waitForIssueViewReady = useCallback(
    (signal: AbortSignal): Promise<boolean> => {
      if (!mountedRef.current || signal.aborted || identityRef.current.epoch !== epoch)
        return Promise.resolve(false);
      if (isReady()) return Promise.resolve(true);
      return new Promise((resolve) => {
        const waiter: Waiter = {
          epoch,
          finish: (ready) => {
            if (!waitersRef.current.delete(waiter)) return;
            clearTimeout(timer);
            signal.removeEventListener("abort", abort);
            resolve(ready);
          },
        };
        const abort = () => waiter.finish(false);
        const timer = setTimeout(abort, VIDEO_ISSUE_VIEW_READY_TIMEOUT_MS);
        waitersRef.current.add(waiter);
        signal.addEventListener("abort", abort, { once: true });
        wake();
      });
    },
    [epoch, isReady, wake],
  );

  const beginIssueRestore = useCallback(
    (isRelevant: () => boolean, signal?: AbortSignal): VideoIssueRestoreLease | null => {
      // A retained old handle must not retire a lease created by a newer source owner.
      if (
        !mountedRef.current ||
        !latestRef.current.taskId ||
        identityRef.current.epoch !== epoch ||
        signal?.aborted
      )
        return null;
      cancelIssueRestore();
      const lease: Lease = {
        epoch,
        frameIndex: latestRef.current.frameIndex,
        initialSelection: latestRef.current.selectedId,
        isRelevant,
        signal,
        abort: () => settleLease(lease, "cancelled"),
        timer: null,
        closed: null,
        request: null,
      };
      leaseRef.current = lease;
      if (!isCurrent(lease)) {
        settleLease(lease, "cancelled");
        return null;
      }
      lease.timer = setTimeout(
        () => settleLease(lease, "unavailable"),
        VIDEO_ISSUE_VIEW_READY_TIMEOUT_MS,
      );
      signal?.addEventListener("abort", lease.abort, { once: true });
      return {
        release: lease.abort,
        restore: (view, selectedId) => {
          if (!isCurrent(lease))
            return Promise.resolve({ status: lease.closed ?? "cancelled", clamped: false });
          if (lease.request) return lease.request.promise;
          let resolve!: RestoreRequest["resolve"];
          const promise = new Promise<VideoIssueViewRestoreResult>((done) => {
            resolve = done;
          });
          lease.request = {
            view: {
              viewport: view.viewport ? { ...view.viewport } : null,
              timeline_window: view.timeline_window ? { ...view.timeline_window } : null,
            },
            selectedId,
            promise,
            resolve,
            applied: null,
          };
          wake();
          return promise;
        },
      };
    },
    [cancelIssueRestore, epoch, isCurrent, settleLease, wake],
  );

  useLayoutEffect(() => {
    mountedRef.current = true;
    const waiters = waitersRef.current;
    return () => {
      mountedRef.current = false;
      cancelIssueRestore();
      for (const waiter of waiters) waiter.finish(false);
      fitRef.current = null;
      selectionRef.current = null;
    };
  }, [cancelIssueRestore]);

  useLayoutEffect(() => {
    const latest = latestRef.current;
    let lease = leaseRef.current;
    if (lease && !isCurrent(lease)) {
      settleLease(lease, "cancelled");
      lease = null;
    }
    for (const waiter of waitersRef.current) {
      if (waiter.epoch !== epoch) waiter.finish(false);
    }
    const fitted = fitToCanvas(
      latest.containerSize.w,
      latest.containerSize.h,
      latest.mediaSize.w,
      latest.mediaSize.h,
    );
    if (!latest.taskId || !fitted) return;
    const media = `${latest.mediaSize.w}:${latest.mediaSize.h}:${latest.hasRealMediaSize}`;
    const container = `${latest.containerSize.w}:${latest.containerSize.h}`;
    const previous = fitRef.current;
    if (
      previous?.epoch !== epoch ||
      previous.media !== media ||
      (latest.autoFitOnResize && previous.container !== container)
    ) {
      fitRef.current = { epoch, media, container, pending: fitted, ready: false };
      latest.setViewport(fitted);
      return;
    }
    if (previous.pending) {
      if (!sameViewport(previous.pending, latest.viewport)) return;
      previous.pending = null;
      previous.ready = latest.hasRealMediaSize;
      wake();
    }

    if (!previous.ready) return;

    const selection = selectionRef.current;
    const selectionChanged =
      selection?.epoch !== epoch ||
      selection.id !== latest.selectedId ||
      selection.enabled !== latest.focusSelectionEnabled;
    if (
      lease?.request &&
      latest.selectedId !== lease.initialSelection &&
      latest.selectedId !== lease.request.selectedId
    ) {
      settleLease(lease, "cancelled");
      lease = null;
    }
    if (selectionChanged) {
      selectionRef.current = {
        epoch,
        id: latest.selectedId,
        enabled: latest.focusSelectionEnabled,
      };
      if (!lease && latest.focusSelectionEnabled && latest.selectedId) {
        latest.focusObject(latest.selectedId);
        // Selection focus can update the viewport. Observe that commit before readiness.
        wake();
        return;
      }
    }
    if (!isReady()) return;
    for (const waiter of waitersRef.current) waiter.finish(true);
    const request = lease?.request;
    if (!lease || !request || latest.selectedId !== request.selectedId) return;
    if (request.applied) {
      const window = latest.timelineRef.current?.capture();
      if (
        window &&
        sameViewport(latest.viewport, request.applied.viewport) &&
        sameWindow(window, request.applied.window)
      )
        settleLease(lease, "restored", request.applied.clamped);
      return;
    }
    const viewportResult = request.view.viewport
      ? restoreVideoIssueViewport(request.view.viewport, latest.containerSize, latest.mediaSize)
      : { viewport: latest.viewport, clamped: false };
    const savedWindow = request.view.timeline_window;
    if (
      !viewportResult ||
      (savedWindow &&
        (!Number.isFinite(savedWindow.from) ||
          !Number.isFinite(savedWindow.to) ||
          savedWindow.to < savedWindow.from))
    ) {
      settleLease(lease, "unavailable");
      return;
    }
    const currentWindow = latest.timelineRef.current?.capture();
    const windowResult = savedWindow
      ? latest.timelineRef.current?.restore(savedWindow)
      : currentWindow
        ? { window: currentWindow, clamped: false }
        : null;
    if (!windowResult || !isCurrent(lease)) {
      settleLease(lease, "cancelled");
      return;
    }
    request.applied = {
      viewport: viewportResult.viewport,
      window: windowResult.window,
      clamped: viewportResult.clamped || windowResult.clamped,
    };
    if (request.view.viewport) latest.setViewport(viewportResult.viewport);
    wake();
  });

  return {
    captureIssueView,
    waitForIssueViewReady,
    beginIssueRestore,
    cancelIssueRestore,
    viewReady: isReady(),
  };
}

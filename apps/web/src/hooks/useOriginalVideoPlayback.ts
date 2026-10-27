import { useCallback, useLayoutEffect, useRef, useState } from "react";
import { ApiError } from "@/api/client";
import {
  captureOriginalOwner,
  describeOriginal,
  isOriginalOwnerCurrent,
  originalResourcePath,
  type OriginalIdentity,
  type OriginalOwner,
} from "@/api/originals";
import {
  prepareOriginalVideo,
  probeOriginalVideo,
  type OriginalVideoLease,
  type VideoOriginalResource,
} from "@/api/originalVideo";
import { useAuthStore } from "@/stores/authStore";

type Status = "idle" | "preparing" | "ready" | "refreshing" | "error";
interface Snapshot {
  mediaPath: string | undefined;
  identity: OriginalIdentity | null;
  contentKey: string | null;
  status: Status;
  error: string | null;
}
const idle: Snapshot = {
  mediaPath: undefined,
  identity: null,
  contentKey: null,
  status: "idle",
  error: null,
};
const liveSessions = new Set<VideoSession>();

function sourceMatches(video: HTMLVideoElement, path: string): boolean {
  const source = video.getAttribute("src");
  return source === path || source === new URL(path, window.location.href).href;
}
function detach(video: HTMLVideoElement | null, path: string | undefined): void {
  if (!video || !path || !sourceMatches(video, path)) return;
  video.pause();
  video.removeAttribute("src");
  video.load();
}
function message(error: unknown): string {
  if (error instanceof ApiError)
    return error.message || "原视频播放准备失败，请检查权限或连接后重试";
  if (error instanceof Error && error.name === "AbortError") return "原视频请求已取消，请重试";
  return "原视频播放准备失败，请检查连接后重试";
}

/** Owns a lease independently of the native decoder node and React's source rendering. */
class VideoSession {
  snapshot: Snapshot = idle;
  video: HTMLVideoElement | null = null;
  private lease: OriginalVideoLease | null = null;
  /** Leases handed to retirement whose in-page request has not settled yet. */
  private retiring = new Set<OriginalVideoLease>();
  /** The document is leaving; only keepalive retirement can still be delivered. */
  private closing = false;
  private disposed = false;
  private operation: Promise<void> | null = null;
  private probe: Promise<void> | null = null;
  private request: AbortController | null = null;
  private probeRequest: AbortController | null = null;
  private renewTimer: ReturnType<typeof setTimeout> | undefined;
  private expiryTimer: ReturnType<typeof setTimeout> | undefined;
  private renewNotBefore = 0;
  private resumeWanted = false;
  private cancelRevision = 0;
  private operationCancelRevision = 0;
  private recoveryAttempts = 0;
  private preparedToken: string | null = null;
  private unsubscribe: () => void;

  constructor(
    readonly key: string,
    private resource: VideoOriginalResource,
    private identityPath: string,
    private owner: OriginalOwner,
    private publish: (value: Snapshot) => void,
    private invalidated: () => void,
  ) {
    liveSessions.add(this);
    this.unsubscribe = useAuthStore.subscribe(this.checkOwner);
    window.addEventListener("storage", this.onStorage);
    window.addEventListener("focus", this.foreground);
    window.addEventListener("pagehide", this.onPageHide);
    document.addEventListener("visibilitychange", this.foreground);
  }
  private alive(): boolean {
    return !this.disposed && isOriginalOwnerCurrent(this.owner);
  }
  private checkOwner = (): void => {
    if (!this.alive()) {
      this.dispose();
      this.invalidated();
    }
  };
  private onStorage = (event: StorageEvent): void => {
    if (event.storageArea && event.storageArea !== localStorage) return;
    if (event.key === null || event.key === "token" || event.key === "auth-storage")
      this.checkOwner();
  };
  private update(fields: Partial<Snapshot>): void {
    if (!this.alive()) return;
    this.snapshot = { ...this.snapshot, ...fields };
    this.publish(this.snapshot);
  }
  private clearTimers(): void {
    clearTimeout(this.renewTimer);
    clearTimeout(this.expiryTimer);
    this.renewTimer = undefined;
    this.expiryTimer = undefined;
  }
  private releaseCurrent(rememberPlaying = true): void {
    const lease = this.lease;
    if (!lease) return;
    if (
      rememberPlaying &&
      this.cancelRevision === this.operationCancelRevision &&
      this.video &&
      sourceMatches(this.video, lease.mediaPath)
    )
      this.resumeWanted = !this.video.paused && !this.video.ended;
    detach(this.video, lease.mediaPath);
    this.lease = null;
    this.clearTimers();
    if (this.closing) {
      lease.retireOnUnload();
      return;
    }
    this.retiring.add(lease);
    void lease.release().finally(() => this.retiring.delete(lease));
  }
  /**
   * A document that is really leaving never unmounts React, so this is the only
   * owner that can hand the lease back before the server stage is exhausted.
   * Back/forward-cache entries keep their lease because the page can resume.
   */
  private onPageHide = (event: PageTransitionEvent): void => {
    if (event.persisted) return;
    this.closing = true;
    this.request?.abort();
    this.probeRequest?.abort();
    const leases = this.lease ? [...this.retiring, this.lease] : [...this.retiring];
    this.lease = null;
    this.retiring.clear();
    this.clearTimers();
    for (const lease of leases) lease.retireOnUnload();
  };
  private fail(error: unknown): void {
    this.releaseCurrent();
    this.update({ mediaPath: undefined, status: "error", error: message(error) });
  }
  bind(video: HTMLVideoElement | null): void {
    if (this.video !== video) detach(this.video, this.lease?.mediaPath);
    this.video = video;
    if (this.disposed) detach(video, this.snapshot.mediaPath);
  }
  private assertCurrent(signal: AbortSignal): void {
    signal.throwIfAborted();
    if (!this.alive()) throw new DOMException("登录会话已变更", "AbortError");
  }
  prepare = (): void => {
    void this.start();
  };
  private start(refreshIdentity = false): Promise<void> {
    if (!this.alive() || this.operation) return this.operation ?? Promise.resolve();
    const controller = new AbortController();
    this.request = controller;
    const revision = this.cancelRevision;
    this.operationCancelRevision = revision;
    this.update({ status: this.lease ? "refreshing" : "preparing", error: null });
    const operation = (async () => {
      await Promise.resolve();
      let candidate: OriginalVideoLease | null = null;
      try {
        this.assertCurrent(controller.signal);
        if (this.identityPath !== `/api/v1${originalResourcePath(this.resource)}-identity`)
          throw new ApiError(409, "原视频身份查询路径无效，请刷新任务后重试");
        let identity = this.snapshot.identity;
        if (!identity || refreshIdentity) {
          identity = await describeOriginal(this.resource, {
            owner: this.owner,
            signal: controller.signal,
          });
          this.assertCurrent(controller.signal);
          identity = Object.freeze({ ...identity });
          this.update({
            identity,
            contentKey: JSON.stringify([
              this.owner.userId,
              this.owner.sessionId,
              this.resource,
              identity.content_id,
              identity.sha256,
            ]),
          });
        }
        candidate = await prepareOriginalVideo(this.resource, {
          owner: this.owner,
          signal: controller.signal,
          identity,
        });
        this.assertCurrent(controller.signal);
        const previousExpiry = this.lease?.expiresAt;
        this.releaseCurrent(this.cancelRevision === revision);
        this.lease = candidate;
        candidate = null;
        this.preparedToken = useAuthStore.getState().token;
        this.update({ mediaPath: this.lease.mediaPath, status: "ready", error: null });
        this.schedule(this.lease, previousExpiry);
      } catch (error) {
        if (candidate) {
          if (this.closing) candidate.retireOnUnload();
          else void candidate.release();
        }
        if (!controller.signal.aborted && this.alive()) this.fail(error);
      } finally {
        if (this.request === controller) {
          this.request = null;
          this.operation = null;
        }
      }
    })();
    this.operation = operation;
    return operation;
  }
  private schedule(lease: OriginalVideoLease, previousExpiry?: number): void {
    this.clearTimers();
    const now = Date.now();
    const capped =
      lease.expiresAt - now <= 30_000 ||
      (previousExpiry !== undefined && lease.expiresAt <= previousExpiry + 1000);
    this.renewNotBefore = capped ? lease.expiresAt : lease.expiresAt - 30_000;
    this.renewTimer = setTimeout(
      () => {
        if (this.alive() && this.lease === lease) void this.start();
      },
      Math.max(1, this.renewNotBefore - now),
    );
    this.expiryTimer = setTimeout(
      () => {
        if (!this.alive() || this.lease !== lease) return;
        this.releaseCurrent();
        this.update({ mediaPath: undefined, status: "preparing" });
        void this.start();
      },
      Math.max(1, lease.expiresAt - now),
    );
  }
  private foreground = (): void => {
    if (document.visibilityState === "hidden" || !this.alive() || !this.lease) return;
    if (Date.now() < this.renewNotBefore) return;
    if (Date.now() >= this.lease.expiresAt) {
      if (!this.operation) this.operationCancelRevision = this.cancelRevision;
      this.releaseCurrent();
      this.update({ mediaPath: undefined, status: "preparing" });
    }
    void this.start();
  };
  recover = (): void => {
    if (!this.alive() || this.operation || this.probe || !this.lease) return;
    const lease = this.lease;
    const controller = new AbortController();
    this.probeRequest = controller;
    const tokenChanged = this.preparedToken !== useAuthStore.getState().token;
    const operation = (async () => {
      // Always yield before referring to the registered operation in finally.
      await Promise.resolve();
      try {
        this.assertCurrent(controller.signal);
        const result = tokenChanged
          ? "expired"
          : await probeOriginalVideo(lease, { signal: controller.signal });
        this.assertCurrent(controller.signal);
        if (this.lease !== lease) return;
        if (result === "valid")
          throw new ApiError(415, "原视频已通过内容校验，但浏览器无法解码或播放此视频");
        if (this.recoveryAttempts >= 2)
          throw new ApiError(410, "原视频租约连续失效，请检查服务状态后手动重试");
        this.recoveryAttempts++;
        this.operationCancelRevision = this.cancelRevision;
        this.releaseCurrent();
        this.update({ mediaPath: undefined, status: "preparing", error: null });
        await this.start();
      } catch (error) {
        if (!controller.signal.aborted && this.alive() && this.lease === lease) {
          if (this.preparedToken !== useAuthStore.getState().token && this.recoveryAttempts < 2) {
            this.recoveryAttempts++;
            this.operationCancelRevision = this.cancelRevision;
            this.releaseCurrent();
            this.update({ mediaPath: undefined, status: "preparing", error: null });
            await this.start();
          } else this.fail(error);
        }
      } finally {
        if (this.probeRequest === controller) {
          this.probeRequest = null;
          this.probe = null;
        }
      }
    })();
    this.probe = operation;
  };
  retry = (): void => {
    if (!this.alive() || this.operation || this.probe) return;
    this.recoveryAttempts = 0;
    void this.start(true);
  };
  cancelResume = (): void => {
    this.cancelRevision++;
    this.resumeWanted = false;
  };
  isSourceReady = (): boolean => {
    const lease = this.lease;
    const video = this.video;
    return !!(
      this.alive() &&
      lease &&
      lease.expiresAt > Date.now() &&
      video &&
      !video.error &&
      video.readyState >= 2 &&
      sourceMatches(video, lease.mediaPath) &&
      video.currentSrc === new URL(lease.mediaPath, window.location.href).href
    );
  };
  onPrimed = (): void => {
    if (!this.isSourceReady()) return;
    this.recoveryAttempts = 0;
    if (!this.resumeWanted || !this.video) return;
    this.resumeWanted = false;
    void this.video.play().catch(() => undefined);
  };
  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.request?.abort();
    this.probeRequest?.abort();
    this.clearTimers();
    this.releaseCurrent(false);
    this.unsubscribe();
    window.removeEventListener("storage", this.onStorage);
    window.removeEventListener("focus", this.foreground);
    window.removeEventListener("pagehide", this.onPageHide);
    document.removeEventListener("visibilitychange", this.foreground);
    liveSessions.delete(this);
  }
}

if (import.meta.hot)
  import.meta.hot.dispose(() => {
    for (const session of liveSessions) session.dispose();
  });

export function useOriginalVideoPlayback({
  resource,
  identityPath,
  video,
}: {
  resource: VideoOriginalResource | null;
  identityPath: string | null | undefined;
  video: HTMLVideoElement | null;
}) {
  const userId = useAuthStore((state) => state.user?.id);
  const sessionId = useAuthStore((state) => state.sessionId);
  const token = useAuthStore((state) => state.token);
  const [revision, setRevision] = useState(0);
  const owner = captureOriginalOwner();
  const resourceKey =
    resource?.kind === "task"
      ? JSON.stringify(["task", resource.taskId])
      : resource?.kind === "item"
        ? JSON.stringify(["item", resource.datasetId, resource.itemId])
        : "null";
  const enabled = !!resource && !!identityPath && !!owner && !!token;
  const key = JSON.stringify([userId, sessionId, resourceKey, identityPath, enabled, revision]);
  const session = useRef<VideoSession | null>(null);
  const [rendered, setRendered] = useState<{ key: string; value: Snapshot }>({ key, value: idle });

  useLayoutEffect(() => {
    if (!enabled) return;
    const captured = captureOriginalOwner();
    if (!captured || !resource || !identityPath) return;
    const ownedResource: VideoOriginalResource =
      resource.kind === "task"
        ? { kind: "task", taskId: resource.taskId }
        : { kind: "item", datasetId: resource.datasetId, itemId: resource.itemId };
    const owned = new VideoSession(
      key,
      ownedResource,
      identityPath,
      { ...captured },
      (value) => {
        if (session.current === owned) setRendered({ key, value });
      },
      () => setRevision((value) => value + 1),
    );
    session.current = owned;
    owned.bind(video);
    owned.prepare();
    return () => {
      if (session.current === owned) session.current = null;
      owned.dispose();
    };
    // Resource identity is canonicalized; a caller's new object or native node does not own the lease.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  const value = rendered.key === key && enabled ? rendered.value : idle;
  useLayoutEffect(() => {
    session.current?.bind(video);
  }, [video, value.mediaPath]);
  const invoke = useCallback(
    (action: "retry" | "recover" | "onPrimed" | "cancelResume") => {
      const current = session.current;
      if (current?.key === key) current[action]();
    },
    [key],
  );
  const retry = useCallback(() => invoke("retry"), [invoke]);
  const recover = useCallback(() => invoke("recover"), [invoke]);
  const onPrimed = useCallback(() => invoke("onPrimed"), [invoke]);
  const cancelResume = useCallback(() => invoke("cancelResume"), [invoke]);
  const isSourceReady = useCallback(
    () => session.current?.key === key && session.current.isSourceReady(),
    [key],
  );
  return { ...value, retry, recover, onPrimed, cancelResume, isSourceReady };
}

import type { VideoGopPlan } from "./videoChunkDemux";

export interface VideoGopSessionIdentity {
  taskId: string;
  datasetItemId: string;
  chunkId: number;
  gopStartDecodeIndex: number;
  configFingerprint: string;
}

export interface SessionDecodeRequest {
  frameIndex: number;
  targetDecodeIndex: number;
  targetTimestampUs: number;
  generation: number;
  allowReset?: boolean;
}

export type SessionFailureReason = "codec_unsupported" | "decoder_error";

export type SessionDecodeOutcome =
  | { ok: true; frame: VideoFrame }
  | {
      ok: false;
      reason:
        | "target_timestamp_missing"
        | "decoder_error"
        | "codec_unsupported"
        | "supplemental_cached"
        | "reset_required"
        | "disposed"
        | "stale_request"
        | "out_of_gop";
    };

export type VideoGopSessionState = "idle" | "ready" | "failed" | "closed";

export interface VideoGopSessionStats {
  state: VideoGopSessionState;
  activeDecoders: number;
  liveVideoFrames: number;
  lastQueueMs: number | null;
  lastCodecMs: number | null;
  cursor: number;
  submits: number;
  sessionCreates: number;
  resets: number;
  disposals: number;
  errors: number;
  duplicateOutputs: number;
}

interface PendingTarget {
  timestampUs: number;
  requestGeneration: number;
  sessionGeneration: number;
  settled: boolean;
  draining: boolean;
  frame: VideoFrame | null;
  promise: Promise<SessionDecodeOutcome>;
  resolve: (outcome: SessionDecodeOutcome) => void;
  timeout: ReturnType<typeof setTimeout> | null;
}

export interface VideoGopDecoderSessionOptions {
  plan: VideoGopPlan;
  identity: VideoGopSessionIdentity;
  idleTimeoutMs?: number;
  hiddenTimeoutMs?: number;
  outputTimeoutMs?: number;
  onIdleTimeout?: () => void;
  onHiddenTimeout?: () => void;
  onSupplementalFrame?: (
    frame: VideoFrame,
    frameIndex: number,
    isDecoderCurrent: () => boolean,
  ) => boolean | Promise<boolean>;
}

const DEFAULT_IDLE_TIMEOUT_MS = 15_000;
const DEFAULT_HIDDEN_TIMEOUT_MS = 30_000;
const DEFAULT_OUTPUT_TIMEOUT_MS = 5_000;

function closeFrame(frame: VideoFrame) {
  try {
    frame.close();
  } catch {
    // 部分测试替身 / 老引擎没有 close。
  }
}

function nowMs(): number {
  return typeof performance !== "undefined" ? performance.now() : Date.now();
}

export class VideoGopDecoderSession {
  readonly identity: VideoGopSessionIdentity;
  private readonly plan: VideoGopPlan;
  private readonly idleTimeoutMs: number;
  private readonly hiddenTimeoutMs: number;
  private readonly outputTimeoutMs: number;
  private readonly onIdleTimeout?: () => void;
  private readonly onHiddenTimeout?: () => void;
  private readonly onSupplementalFrame?: VideoGopDecoderSessionOptions["onSupplementalFrame"];

  private decoder: VideoDecoder | null = null;
  private configuredConfig: VideoDecoderConfig | null = null;
  private state: VideoGopSessionState = "idle";
  private cursor: number;
  private maxOutputTimestampUs = Number.NEGATIVE_INFINITY;
  private lastTargetTimestampUs = Number.NEGATIVE_INFINITY;
  private readonly supplementalTasks = new Map<number, Promise<boolean>>();
  private generation = 0;
  private latestRequestGeneration = Number.MIN_SAFE_INTEGER;
  private pendingTarget: PendingTarget | null = null;

  private chain: Promise<unknown> = Promise.resolve();

  private idleTimer: ReturnType<typeof setTimeout> | null = null;
  private hiddenTimer: ReturnType<typeof setTimeout> | null = null;

  private submits = 0;
  private sessionCreates = 0;
  private resets = 0;
  private disposals = 0;
  private errors = 0;
  private failureReason: SessionFailureReason | null = null;
  private duplicateOutputs = 0;
  private lastQueueMs: number | null = null;
  private lastCodecMs: number | null = null;

  constructor(options: VideoGopDecoderSessionOptions) {
    this.plan = options.plan;
    this.identity = options.identity;
    this.idleTimeoutMs = options.idleTimeoutMs ?? DEFAULT_IDLE_TIMEOUT_MS;
    this.hiddenTimeoutMs = options.hiddenTimeoutMs ?? DEFAULT_HIDDEN_TIMEOUT_MS;
    this.outputTimeoutMs = options.outputTimeoutMs ?? DEFAULT_OUTPUT_TIMEOUT_MS;
    this.onIdleTimeout = options.onIdleTimeout;
    this.onHiddenTimeout = options.onHiddenTimeout;
    this.onSupplementalFrame = options.onSupplementalFrame;
    this.cursor = this.plan.gopStartDecodeIndex - 1;
    this.setupVisibility();
    this.touchIdle();
  }

  getStats(): VideoGopSessionStats {
    return {
      state: this.state,
      activeDecoders: this.decoder ? 1 : 0,
      liveVideoFrames: this.pendingTarget?.frame ? 1 : 0,
      lastQueueMs: this.lastQueueMs,
      lastCodecMs: this.lastCodecMs,
      cursor: this.cursor,
      submits: this.submits,
      sessionCreates: this.sessionCreates,
      resets: this.resets,
      disposals: this.disposals,
      errors: this.errors,
      duplicateOutputs: this.duplicateOutputs,
    };
  }

  private getState(): VideoGopSessionState {
    return this.state;
  }

  decode(request: SessionDecodeRequest): Promise<SessionDecodeOutcome> {
    if (request.generation > this.latestRequestGeneration) {
      this.latestRequestGeneration = request.generation;
      const pending = this.pendingTarget;
      if (pending && pending.requestGeneration < request.generation) {
        this.settlePending(pending, { ok: false, reason: "stale_request" });
      }
    }
    const queuedAt = nowMs();
    return this.enqueue(async () => {
      const startedAt = nowMs();
      this.lastQueueMs = Math.max(0, startedAt - queuedAt);
      try {
        return await this.doDecode(request);
      } finally {
        this.lastCodecMs = Math.max(0, nowMs() - startedAt);
      }
    });
  }

  reset(): Promise<void> {
    return this.enqueue(() => this.doReset());
  }

  dispose(): void {
    if (this.state === "closed") return;
    this.state = "closed";
    this.disposals += 1;
    this.generation += 1;
    this.clearIdle();
    this.clearHidden();
    this.teardownVisibility();
    if (this.pendingTarget) {
      this.settlePending(this.pendingTarget, { ok: false, reason: "disposed" });
    }
    this.pendingTarget = null;
    this.supplementalTasks.clear();
    this.closeDecoder();
  }

  private async doDecode(req: SessionDecodeRequest): Promise<SessionDecodeOutcome> {
    if (this.state === "closed") return { ok: false, reason: "disposed" };
    if (req.generation !== this.latestRequestGeneration) {
      return { ok: false, reason: "stale_request" };
    }
    this.touchIdle();
    if (this.state === "failed")
      return { ok: false, reason: this.failureReason ?? "decoder_error" };

    await this.ensureReady();
    if (this.getState() !== "ready" || !this.decoder) {
      return { ok: false, reason: this.failureReason ?? "decoder_error" };
    }

    const { targetDecodeIndex, targetTimestampUs } = req;
    if (
      targetDecodeIndex < this.plan.gopStartDecodeIndex ||
      targetDecodeIndex >= this.plan.gopEndDecodeIndex
    ) {
      return { ok: false, reason: "out_of_gop" };
    }
    const targetSample = this.plan.samples[targetDecodeIndex - this.plan.gopStartDecodeIndex];
    if (
      !targetSample ||
      targetSample.frameIndex !== req.frameIndex ||
      targetSample.timestampUs !== targetTimestampUs
    ) {
      return { ok: false, reason: "out_of_gop" };
    }
    this.lastTargetTimestampUs = targetTimestampUs;
    const supplemental = this.supplementalTasks.get(targetTimestampUs);
    if (supplemental) {
      this.supplementalTasks.delete(targetTimestampUs);
      if (await supplemental) {
        return { ok: false, reason: "supplemental_cached" };
      }
      if (req.generation !== this.latestRequestGeneration) {
        return { ok: false, reason: "stale_request" };
      }
    }

    let submitThroughDecodeIndex = targetDecodeIndex;
    for (const sample of this.plan.samples) {
      if (
        sample.timestampUs <= targetTimestampUs &&
        sample.decodeIndex > submitThroughDecodeIndex
      ) {
        submitThroughDecodeIndex = sample.decodeIndex;
      }
    }
    const hasPresentationReordering = this.plan.samples.some(
      (sample, index, all) => index > 0 && sample.timestampUs < all[index - 1].timestampUs,
    );
    if (hasPresentationReordering) {
      const presentationRank = new Map(
        [...this.plan.samples]
          .sort((a, b) => a.timestampUs - b.timestampUs)
          .map((sample, rank) => [sample.decodeIndex, rank]),
      );
      const maxRankDisplacement = this.plan.samples.reduce((max, sample, decodeRank) => {
        const presentRank = presentationRank.get(sample.decodeIndex) ?? decodeRank;
        return Math.max(max, Math.abs(presentRank - decodeRank));
      }, 0);
      submitThroughDecodeIndex = Math.min(
        this.plan.gopEndDecodeIndex - 1,
        submitThroughDecodeIndex + maxRankDisplacement + 1,
      );
    }

    const resetRequired =
      submitThroughDecodeIndex <= this.cursor || targetTimestampUs <= this.maxOutputTimestampUs;
    if (resetRequired && req.allowReset === false) {
      return { ok: false, reason: "reset_required" };
    }
    if (resetRequired) {
      await this.hardReset();
      if (this.getState() !== "ready" || !this.decoder) {
        return { ok: false, reason: this.failureReason ?? "decoder_error" };
      }
    }
    if (req.generation !== this.latestRequestGeneration) {
      return { ok: false, reason: "stale_request" };
    }

    const fromRel = this.cursor + 1 - this.plan.gopStartDecodeIndex;
    const toRel = submitThroughDecodeIndex - this.plan.gopStartDecodeIndex;
    const pending = this.createPendingTarget(req);
    this.pendingTarget = pending;
    try {
      for (let rel = fromRel; rel <= toRel; rel++) {
        this.decoder.decode(this.plan.samples[rel].chunk);
        this.submits += 1;
        if (this.getState() === "failed") throw new Error("decoder failed");
      }
      this.cursor = submitThroughDecodeIndex;
      if (!pending.settled && submitThroughDecodeIndex === this.plan.gopEndDecodeIndex - 1) {
        pending.draining = true;
        void this.decoder.flush().then(
          () => this.finishDrain(pending),
          () => {
            if (!pending.settled) this.markFailed("decoder_error");
          },
        );
      }
    } catch {
      if (pending.settled && pending.frame === null) {
        if (this.pendingTarget === pending) this.pendingTarget = null;
        return pending.promise;
      }
      if (pending.frame) {
        closeFrame(pending.frame);
        pending.frame = null;
      }
      this.markFailed("decoder_error");
      this.settlePending(pending, { ok: false, reason: "decoder_error" });
      this.pendingTarget = null;
      return { ok: false, reason: "decoder_error" };
    }

    const outcome = await pending.promise;
    if (this.pendingTarget === pending) this.pendingTarget = null;
    return outcome;
  }

  private finishDrain(pending: PendingTarget): void {
    if (pending.settled) return;
    pending.draining = false;
    this.settlePending(
      pending,
      pending.frame
        ? { ok: true, frame: pending.frame }
        : { ok: false, reason: "target_timestamp_missing" },
    );
  }

  private closeDecoder(): void {
    const decoder = this.decoder;
    this.decoder = null;
    this.configuredConfig = null;
    try {
      decoder?.close();
    } catch {
      return;
    }
  }

  private async doReset(): Promise<void> {
    if (this.state === "closed") return;
    this.touchIdle();
    await this.hardReset();
  }

  private async ensureReady(): Promise<void> {
    if (this.state === "ready" && this.decoder) return;
    if (this.state === "closed" || this.state === "failed") return;

    const generation = this.generation;
    const isCurrent = () => this.getState() !== "closed" && this.generation === generation;
    let supportedConfig: VideoDecoderConfig = this.plan.config;
    if (
      typeof VideoDecoder !== "undefined" &&
      typeof VideoDecoder.isConfigSupported === "function"
    ) {
      try {
        let support = await VideoDecoder.isConfigSupported(this.plan.config);
        if (!isCurrent()) return;
        if (!support.supported && this.plan.config.hardwareAcceleration === "prefer-hardware") {
          const fallbackConfig = { ...this.plan.config };
          delete fallbackConfig.hardwareAcceleration;
          support = await VideoDecoder.isConfigSupported(fallbackConfig);
          if (!isCurrent()) return;
          if (support.supported) supportedConfig = support.config ?? fallbackConfig;
        }
        if (!support.supported) {
          this.markFailed("codec_unsupported");
          return;
        }
        if (supportedConfig === this.plan.config) {
          supportedConfig = support.config ?? this.plan.config;
        }
      } catch {
        if (isCurrent()) this.markFailed("codec_unsupported");
        return;
      }
    }

    if (!isCurrent()) return;
    try {
      const isDecoderCurrent = (): boolean => this.decoder === decoder;
      const decoder = new VideoDecoder({
        output: (frame) => {
          if (isDecoderCurrent()) this.onOutput(frame, isDecoderCurrent);
          else closeFrame(frame);
        },
        error: () => {
          if (isDecoderCurrent()) this.markFailed("decoder_error");
        },
      });
      this.decoder = decoder;
    } catch {
      this.markFailed("decoder_error");
      return;
    }
    try {
      this.decoder.configure(supportedConfig);
    } catch {
      this.markFailed("codec_unsupported");
      return;
    }
    this.configuredConfig = supportedConfig;
    this.state = "ready";
    this.cursor = this.plan.gopStartDecodeIndex - 1;
    this.maxOutputTimestampUs = Number.NEGATIVE_INFINITY;
    this.sessionCreates += 1;
  }

  private async hardReset(): Promise<void> {
    if (this.pendingTarget) {
      this.settlePending(this.pendingTarget, { ok: false, reason: "stale_request" });
      this.pendingTarget = null;
    }
    this.generation += 1;
    this.resets += 1;
    if (this.decoder && this.configuredConfig) {
      try {
        this.decoder.reset();
      } catch {
        // decoder 可能已 reset / close。
      }
      try {
        this.decoder.configure(this.configuredConfig);
      } catch {
        this.markFailed("decoder_error");
        return;
      }
    }
    this.cursor = this.plan.gopStartDecodeIndex - 1;
    this.maxOutputTimestampUs = Number.NEGATIVE_INFINITY;
  }

  private onOutput(frame: VideoFrame, isDecoderCurrent: () => boolean): void {
    this.maxOutputTimestampUs = Math.max(this.maxOutputTimestampUs, frame.timestamp);
    const target = this.pendingTarget;
    if (this.state !== "ready") {
      closeFrame(frame);
      return;
    }
    if (target && frame.timestamp === target.timestampUs) {
      if (target.settled || target.frame !== null) {
        this.duplicateOutputs += 1;
        closeFrame(frame);
        return;
      }
      if (
        target.sessionGeneration !== this.generation ||
        target.requestGeneration !== this.latestRequestGeneration
      ) {
        closeFrame(frame);
        return;
      }
      target.frame = frame;
      if (!target.draining) this.settlePending(target, { ok: true, frame });
      return;
    }
    const targetTimestampUs = target?.timestampUs ?? this.lastTargetTimestampUs;
    if (frame.timestamp <= targetTimestampUs || !this.onSupplementalFrame) {
      closeFrame(frame);
      return;
    }
    const sample = this.plan.samples.find((candidate) => candidate.timestampUs === frame.timestamp);
    if (!sample) {
      closeFrame(frame);
      return;
    }
    if (this.supplementalTasks.has(frame.timestamp)) {
      this.duplicateOutputs += 1;
      closeFrame(frame);
      return;
    }
    let task: Promise<boolean>;
    try {
      task = Promise.resolve(
        this.onSupplementalFrame(frame, sample.frameIndex, isDecoderCurrent),
      ).catch(() => false);
    } catch {
      closeFrame(frame);
      return;
    }
    if (isDecoderCurrent()) this.supplementalTasks.set(frame.timestamp, task);
  }

  private markFailed(reason: SessionFailureReason): void {
    if (this.state === "closed") return;
    this.state = "failed";
    this.failureReason = reason;
    this.generation += 1;
    this.errors += 1;
    if (this.pendingTarget && !this.pendingTarget.settled) {
      this.settlePending(this.pendingTarget, { ok: false, reason });
    }
  }

  private createPendingTarget(req: SessionDecodeRequest): PendingTarget {
    let resolve!: (outcome: SessionDecodeOutcome) => void;
    const promise = new Promise<SessionDecodeOutcome>((done) => {
      resolve = done;
    });
    const pending: PendingTarget = {
      timestampUs: req.targetTimestampUs,
      requestGeneration: req.generation,
      sessionGeneration: this.generation,
      settled: false,
      draining: false,
      frame: null,
      promise,
      resolve,
      timeout: null,
    };
    if (this.outputTimeoutMs > 0) {
      pending.timeout = setTimeout(() => {
        this.settlePending(pending, { ok: false, reason: "target_timestamp_missing" });
      }, this.outputTimeoutMs);
    }
    return pending;
  }

  private settlePending(pending: PendingTarget, outcome: SessionDecodeOutcome): void {
    if (pending.settled) return;
    pending.settled = true;
    if (pending.draining) {
      pending.draining = false;
      if (pending.frame) {
        closeFrame(pending.frame);
        pending.frame = null;
      }
      this.closeDecoder();
      this.supplementalTasks.clear();
      if (this.state === "ready") {
        this.state = "idle";
        this.generation += 1;
        this.cursor = this.plan.gopStartDecodeIndex - 1;
        this.maxOutputTimestampUs = Number.NEGATIVE_INFINITY;
      }
    }
    if (pending.timeout !== null) {
      clearTimeout(pending.timeout);
      pending.timeout = null;
    }
    pending.resolve(outcome);
  }

  private enqueue<T>(task: () => Promise<T> | T): Promise<T> {
    const run = this.chain.then(task, task);
    this.chain = run.then(
      () => undefined,
      () => undefined,
    );
    return run as Promise<T>;
  }

  private touchIdle(): void {
    this.clearIdle();
    if (this.state === "closed") return;
    if (this.idleTimeoutMs <= 0) return;
    this.idleTimer = setTimeout(() => {
      this.idleTimer = null;
      if (this.onIdleTimeout) {
        this.onIdleTimeout();
      } else {
        this.dispose();
      }
    }, this.idleTimeoutMs);
  }

  private clearIdle(): void {
    if (this.idleTimer !== null) {
      clearTimeout(this.idleTimer);
      this.idleTimer = null;
    }
  }

  private setupVisibility(): void {
    if (typeof document === "undefined") return;
    document.addEventListener("visibilitychange", this.onVisibilityChange);
    if (document.hidden) this.startHiddenTimer();
  }

  private teardownVisibility(): void {
    if (typeof document === "undefined") return;
    document.removeEventListener("visibilitychange", this.onVisibilityChange);
  }

  private onVisibilityChange = (): void => {
    if (typeof document === "undefined") return;
    if (document.hidden) this.startHiddenTimer();
    else this.clearHidden();
  };

  private startHiddenTimer(): void {
    this.clearHidden();
    if (this.hiddenTimeoutMs <= 0) return;
    this.hiddenTimer = setTimeout(() => {
      this.hiddenTimer = null;
      if (this.onHiddenTimeout) {
        this.onHiddenTimeout();
      } else {
        this.dispose();
      }
    }, this.hiddenTimeoutMs);
  }

  private clearHidden(): void {
    if (this.hiddenTimer !== null) {
      clearTimeout(this.hiddenTimer);
      this.hiddenTimer = null;
    }
  }
}

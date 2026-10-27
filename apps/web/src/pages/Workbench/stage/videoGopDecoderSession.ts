// 有状态 GOP decoder session(v0.23.14)。
//
// v0.23.13 的 decodePlanToBitmap 每次都新建 decoder、从 keyframe 重解整段 GOP —— 同 GOP
// 逐帧前进时会平方级重复提交(30 帧 GOP 走到末尾需 1+2+…+30=465 个 chunk)。本 session
// 把这条路径优化成有界、可取消、可诊断的持久会话:同一 GOP 内向前逐帧只提交尚未解码的
// access unit;后退 / 跨 GOP / identity 变化时确定性 reset。
//
// 边界:本模块只管理「一个 VideoDecoder + decode plan + cursor + timestamp waiter + 串行
// 命令队列 + generation + 资源释放」。它不读 React store、不发 HTTP 请求、不控制 Konva、
// 也不持有 bitmap cache —— 那些是 hook 层的职责。目标 output 的 VideoFrame 交给调用方；
// B-frame lookahead 的未来 output 可移交 hook 转 bitmap，其余未命中 output 立即 close。

import type { VideoGopPlan } from "./videoChunkDemux";

/** 决定是否复用当前 session 的身份;任一字段变化都必须销毁旧 session、创建新 session。 */
export interface VideoGopSessionIdentity {
  taskId: string;
  datasetItemId: string;
  chunkId: number;
  gopStartDecodeIndex: number;
  configFingerprint: string;
}

/** 单次目标解码请求。generation 由调用方(hook)的 latest-request-wins 保证。 */
export interface SessionDecodeRequest {
  frameIndex: number;
  targetDecodeIndex: number;
  targetTimestampUs: number;
  generation: number;
  /** 后台预取设为 false：若目标需要回退/重置，直接跳过，不能阻塞前台绘制。 */
  allowReset?: boolean;
}

/** session 进入 failed 的细分原因(映射到 precise-frame fallback reason)。 */
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
  /** 最近一次 decode 在串行命令队列中的等待时间。 */
  lastQueueMs: number | null;
  /** 最近一次 decode 从开始执行命令到目标 outcome 返回的 codec/session 时间。 */
  lastCodecMs: number | null;
  /** 已提交到的绝对 decode index(gopStartDecodeIndex - 1 表示尚未提交)。 */
  cursor: number;
  /** 累计提交的 EncodedVideoChunk 数(诊断增量 decode 是否生效的核心指标)。 */
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
  frame: VideoFrame | null;
  promise: Promise<SessionDecodeOutcome>;
  resolve: (outcome: SessionDecodeOutcome) => void;
  timeout: ReturnType<typeof setTimeout> | null;
}

export interface VideoGopDecoderSessionOptions {
  plan: VideoGopPlan;
  identity: VideoGopSessionIdentity;
  /** 闲置超时(ms):无 decode / reset 活动超过该值则调用 onIdleTimeout。默认 15s。 */
  idleTimeoutMs?: number;
  /** document 持续 hidden 超时(ms):超过则调用 onHiddenTimeout。默认 30s。 */
  hiddenTimeoutMs?: number;
  /** 等待目标 output 的超时(ms)。默认 5s；防损坏码流让串行队列永久悬挂。 */
  outputTimeoutMs?: number;
  /** 闲置超时回调;缺省 dispose。 */
  onIdleTimeout?: () => void;
  /** hidden 超时回调;缺省 dispose。 */
  onHiddenTimeout?: () => void;
  /**
   * B-frame lookahead 提前输出未来展示帧时，把所有权交给 bitmap cache。
   * 回调必须关闭 VideoFrame；返回 true 表示对应 bitmap 已可从调用方缓存读取。
   */
  onSupplementalFrame?: (frame: VideoFrame, frameIndex: number) => boolean | Promise<boolean>;
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

/**
 * 单 decoder、串行命令队列、timestamp waiter 的 GOP 解码会话。
 *
 * 状态机:
 *   idle ──configure──→ ready(cursor = gopStart - 1)
 *     ready ├──decode forward──→ ready(cursor = target)
 *           ├──reset──────────→ ready(cursor = gopStart - 1)
 *           ├──decoder error──→ failed
 *           └──dispose────────→ closed
 *   failed ├──replace identity(recreate)──→ ready
 *          └──dispose──────────────────────→ closed
 */
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
  /** 已提交到的绝对 decode index;gopStartDecodeIndex - 1 表示尚未提交任何 chunk。 */
  private cursor: number;
  /** decoder 已交付过的最高 PTS；未命中目标的 output 会关闭，但不能假装未来还能重发。 */
  private maxOutputTimestampUs = Number.NEGATIVE_INFINITY;
  private lastTargetTimestampUs = Number.NEGATIVE_INFINITY;
  private readonly supplementalTasks = new Map<number, Promise<boolean>>();
  private generation = 0;
  private latestRequestGeneration = Number.MIN_SAFE_INTEGER;
  private pendingTarget: PendingTarget | null = null;

  /** 串行命令队列:所有 public command 进入同一 promise chain,避免 decode / reset 交错。 */
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

  /**
   * 读取当前状态(方法调用不被 TS 跨 await 收窄)。doDecode 在 await ensureReady /
   * hardReset 之后必须用本方法读 state —— error callback 可能在 await 期间把 state 置为 failed。
   */
  private getState(): VideoGopSessionState {
    return this.state;
  }

  /**
   * 解码目标帧。同一 GOP 向前逐帧只提交 (cursor, target] 的增量 chunks;后退 / 原地
   * (cache miss) 触发 hard reset 从 key 重解。返回的 VideoFrame 由调用方拥有(转 bitmap
   * 后需自行 close)。
   */
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

  /** 显式 reset:关闭并重建 decoder,cursor 回到 GOP 起点(用于 identity 不变的强制刷新)。 */
  reset(): Promise<void> {
    return this.enqueue(() => this.doReset());
  }

  /** 幂等释放:关闭 decoder、清理 timer、标记 closed。closed 后新请求返回 disposed。 */
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
    if (this.decoder) {
      try {
        this.decoder.close();
      } catch {
        // decoder 可能已 close。
      }
      this.decoder = null;
    }
  }

  // ── 命令实现(均在串行队列内执行)──────────────────────────────────────────

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

    // VideoDecoder 按 presentation order 输出。B 帧下，目标之前展示的帧可能位于目标 packet
    // 之后的 decode order；必须至少提交所有 timestamp <= target 的 access unit，才能等待
    // 目标 output。不能用 flush() 强行出帧：规范要求 flush 后下一次 decode 必须重新从 key 开始。
    let submitThroughDecodeIndex = targetDecodeIndex;
    for (const sample of this.plan.samples) {
      if (
        sample.timestampUs <= targetTimestampUs &&
        sample.decodeIndex > submitThroughDecodeIndex
      ) {
        submitThroughDecodeIndex = sample.decodeIndex;
      }
    }
    // AVC B-frame 流即使启用 optimizeForLatency，decoder 仍可能需要看到目标之后的若干
    // access unit，才会确认 reorder queue 中的目标帧可以交付。以 GOP 内 decode rank 与
    // presentation rank 的最大位移 + 1 作为有界 drain lookahead；I/P-only 流保持原来的
    // 严格增量提交，不能为解决 B 帧又退回整 GOP 预解。
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

    // 后退 / 原地，或目标所需的 presentation lookahead 已被提交但 output 未缓存时，
    // decoder 不会重发已解码帧，必须 hard reset 从 key sample 重解。
    // forward(target > cursor)则只提交增量。
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
      // 只有已经提交到 GOP 尾且目标仍未 output 时才 drain。flush 会要求下一次输入重新从
      // key chunk 开始；此时 session 的任何后续目标都会因 cursor 在 GOP 尾而先 hardReset，
      // 因而不会重现“逐帧 flush 后继续喂 delta”的 DataError。
      if (!pending.settled && submitThroughDecodeIndex === this.plan.gopEndDecodeIndex - 1) {
        await this.decoder.flush();
        if (this.getState() === "failed") throw new Error("decoder failed");
        if (!pending.settled) {
          this.settlePending(pending, {
            ok: false,
            reason: "target_timestamp_missing",
          });
        }
      }
    } catch {
      // dispose / 新 generation / error callback 可能已在 await flush 期间结算了无 frame
      // waiter；保留该更精确的终态，不能覆盖成 decoder_error。
      if (pending.settled && pending.frame === null) {
        if (this.pendingTarget === pending) this.pendingTarget = null;
        return pending.promise;
      }
      if (pending.frame) closeFrame(pending.frame);
      this.markFailed("decoder_error");
      this.settlePending(pending, { ok: false, reason: "decoder_error" });
      this.pendingTarget = null;
      return { ok: false, reason: "decoder_error" };
    }

    const outcome = await pending.promise;
    if (this.pendingTarget === pending) this.pendingTarget = null;
    return outcome;
  }

  private async doReset(): Promise<void> {
    if (this.state === "closed") return;
    this.touchIdle();
    await this.hardReset();
  }

  /** idle → ready:isConfigSupported 通过后 new VideoDecoder + configure。 */
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
      this.decoder = new VideoDecoder({
        output: (frame) => this.onOutput(frame),
        error: () => this.markFailed("decoder_error"),
      });
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

  /**
   * hard reset:generation 递增使旧 waiter 失效;decoder.reset() 把内部状态置回 unconfigured,
   * 随后用同一 config 重新 configure,cursor 回到 GOP 起点。
   */
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

  /**
   * output 选择:只接受 timestamp 等于当前目标且 generation 仍有效的第一张 frame。
   * B-frame lookahead 提前交付的未来帧交给 hook 异步转 bitmap；其它 output 立即 close。
   */
  private onOutput(frame: VideoFrame): void {
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
      this.settlePending(target, { ok: true, frame });
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
      task = Promise.resolve(this.onSupplementalFrame(frame, sample.frameIndex)).catch(() => false);
    } catch {
      closeFrame(frame);
      return;
    }
    this.supplementalTasks.set(frame.timestamp, task);
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
    if (pending.timeout !== null) {
      clearTimeout(pending.timeout);
      pending.timeout = null;
    }
    pending.resolve(outcome);
  }

  // ── 串行命令队列 ───────────────────────────────────────────────────────────

  private enqueue<T>(task: () => Promise<T> | T): Promise<T> {
    // 无论前一个命令成功或失败都继续执行后续命令(failed/closed 由各命令自行判断)。
    const run = this.chain.then(task, task);
    this.chain = run.then(
      () => undefined,
      () => undefined,
    );
    return run as Promise<T>;
  }

  // ── 闲置 / hidden 计时 ─────────────────────────────────────────────────────

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

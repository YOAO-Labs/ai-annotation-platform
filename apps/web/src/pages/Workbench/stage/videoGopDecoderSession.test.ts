// VideoGopDecoderSession 状态机:增量 decode、reset、timestamp waiter、generation、
// 串行命令队列、idle/hidden 计时。用可控 fake VideoDecoder 覆盖正确性与资源释放。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { buildGopPlan, type VideoGopPlan } from "./videoChunkDemux";
import type { VideoChunkSampleEntry, VideoChunkSamplesResponse } from "@/types";
import {
  VideoGopDecoderSession,
  type SessionDecodeRequest,
  type VideoGopSessionIdentity,
} from "./videoGopDecoderSession";

// ── fake 基础设施 ──────────────────────────────────────────────────────────────

interface RecordedChunkInit {
  type: "key" | "delta";
  timestamp: number;
  duration?: number;
  data: Uint8Array;
}

class FakeEncodedVideoChunk {
  readonly type: RecordedChunkInit["type"];
  readonly timestamp: number;
  readonly duration: number | undefined;
  readonly byteLength: number;
  constructor(init: RecordedChunkInit) {
    this.type = init.type;
    this.timestamp = init.timestamp;
    this.duration = init.duration;
    this.byteLength = init.data.byteLength;
  }
  copyTo(): void {}
  clone(): FakeEncodedVideoChunk {
    return new FakeEncodedVideoChunk({
      type: this.type,
      timestamp: this.timestamp,
      duration: this.duration,
      data: new Uint8Array(this.byteLength),
    });
  }
}

class FakeSessionDecoder {
  readonly output: (f: VideoFrame) => void;
  private readonly errorCb: (e: DOMException) => void;
  configure = vi.fn();
  decode = vi.fn();
  flush = vi.fn();
  reset = vi.fn();
  close = vi.fn();
  constructor(init: { output: (f: VideoFrame) => void; error: (e: DOMException) => void }) {
    this.output = init.output;
    this.errorCb = init.error;
  }
  emit(frame: VideoFrame) {
    this.output(frame);
  }
  emitError(err: DOMException) {
    this.errorCb(err);
  }
}

interface SessionDecoderCtrl {
  supported: boolean;
  supportConfig: VideoDecoderConfig | undefined;
  supportThrow: boolean;
  configureThrow: boolean;
  decodeImpl: ((d: FakeSessionDecoder, chunk: FakeEncodedVideoChunk) => void) | null;
  decodeThrow: boolean;
  decodeError: DOMException | null;
  flushImpl: ((d: FakeSessionDecoder) => void | Promise<void>) | null;
}

let ctrl: SessionDecoderCtrl;
let decoders: FakeSessionDecoder[];

function installSessionDecoder(): void {
  ctrl = {
    supported: true,
    supportConfig: undefined,
    supportThrow: false,
    configureThrow: false,
    decodeImpl: null,
    decodeThrow: false,
    decodeError: null,
    flushImpl: null,
  };
  decoders = [];
  vi.stubGlobal("EncodedVideoChunk", FakeEncodedVideoChunk);
  class FakeVideoDecoder extends FakeSessionDecoder {
    static isConfigSupported = vi.fn(async () => {
      if (ctrl.supportThrow) throw new Error("support failed");
      return { supported: ctrl.supported, config: ctrl.supportConfig };
    });
    constructor(init: { output: (f: VideoFrame) => void; error: (e: DOMException) => void }) {
      super(init);
      decoders.push(this);
      let keyChunkRequired = true;
      this.configure.mockImplementation(() => {
        if (ctrl.configureThrow) throw new Error("configure failed");
        keyChunkRequired = true;
      });
      this.decode.mockImplementation((chunk: FakeEncodedVideoChunk) => {
        if (ctrl.decodeThrow) throw new Error("decode failed");
        if (keyChunkRequired && chunk.type !== "key") {
          throw new DOMException("key chunk required", "DataError");
        }
        keyChunkRequired = false;
        if (ctrl.decodeError) init.error(ctrl.decodeError);
        ctrl.decodeImpl?.(this, chunk);
      });
      this.flush.mockImplementation(async () => {
        keyChunkRequired = true;
        await ctrl.flushImpl?.(this);
      });
      this.reset.mockImplementation(() => {
        keyChunkRequired = true;
      });
    }
  }
  vi.stubGlobal("VideoDecoder", FakeVideoDecoder);
}

function fakeFrame(timestamp: number) {
  return {
    timestamp,
    displayWidth: 320,
    displayHeight: 240,
    close: vi.fn(),
  } as unknown as VideoFrame;
}

const lastDecoder = () => decoders[decoders.length - 1];

/** 默认 decode:每个提交的 chunk 立即产生同 timestamp output。 */
function defaultDecodeEmitChunk(): void {
  ctrl.decodeImpl = (d, chunk) => {
    d.emit(fakeFrame(chunk.timestamp));
  };
}

// ── fixtures ──────────────────────────────────────────────────────────────────

function makeBytes(n: number): ArrayBuffer {
  const buf = new ArrayBuffer(n);
  const view = new Uint8Array(buf);
  for (let i = 0; i < n; i++) view[i] = (i * 7) & 0xff;
  return buf;
}

function makeSamples(
  entries: Array<{ frame: number; pts: number; dur?: number; key?: boolean }>,
  opts: { width?: number; height?: number } = {},
): VideoChunkSamplesResponse {
  const samples: VideoChunkSampleEntry[] = [];
  let cursor = 0;
  for (const e of entries) {
    const size = 10;
    const offset = cursor;
    cursor = offset + size;
    samples.push({
      frame_index: e.frame,
      pts_ms: e.pts,
      duration_ms: e.dur ?? 33,
      is_keyframe: e.key ?? false,
      size_bytes: size,
      offset_in_chunk: offset,
    });
  }
  return {
    dataset_item_id: "ds-1",
    chunk_id: 0,
    codec_string: "avc1.4d001e",
    description: btoa("config-bytes"),
    width: opts.width ?? 320,
    height: opts.height ?? 240,
    samples,
  };
}

/** 单 GOP(I/P,decode order == presentation order):frame 0 为 key,其余 delta。 */
function gopPlan(frameCount: number): VideoGopPlan {
  const entries = Array.from({ length: frameCount }, (_, f) => ({
    frame: f,
    pts: f * 33,
    key: f === 0,
  }));
  const samples = makeSamples(entries);
  const bytes = makeBytes(frameCount * 10 + 16);
  const result = buildGopPlan(bytes, samples, 0);
  if (!result.ok) throw new Error("gopPlan build failed");
  return result.plan;
}

function identity(plan: VideoGopPlan, taskId = "t1"): VideoGopSessionIdentity {
  return {
    taskId,
    datasetItemId: "ds-1",
    chunkId: plan.chunkId,
    gopStartDecodeIndex: plan.gopStartDecodeIndex,
    configFingerprint: plan.configFingerprint,
  };
}

function req(plan: VideoGopPlan, decodeIndex: number, generation = 0): SessionDecodeRequest {
  const sample = plan.samples[decodeIndex - plan.gopStartDecodeIndex];
  return {
    frameIndex: sample.frameIndex,
    targetDecodeIndex: decodeIndex,
    targetTimestampUs: sample.timestampUs,
    generation,
  };
}

function newSession(
  plan: VideoGopPlan,
  opts: Partial<ConstructorParameters<typeof VideoGopDecoderSession>[0]> = {},
) {
  return new VideoGopDecoderSession({ plan, identity: identity(plan), ...opts });
}

// ── 测试 ──────────────────────────────────────────────────────────────────────

describe("VideoGopDecoderSession", () => {
  beforeEach(() => {
    installSessionDecoder();
    defaultDecodeEmitChunk();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("configure 使用 isConfigSupported 返回的规范化 config", async () => {
    const plan = gopPlan(10);
    const normalized: VideoDecoderConfig = { ...plan.config, codedWidth: 1280 };
    ctrl.supportConfig = normalized;
    const session = newSession(plan);
    await session.decode(req(plan, 0));
    expect(lastDecoder().configure).toHaveBeenCalledWith(normalized);
    expect(session.getStats().sessionCreates).toBe(1);
  });

  it("prefer-hardware 不可用时回退到默认配置，而不是误报 codec unsupported", async () => {
    const plan = gopPlan(10);
    const fallbackConfig = { ...plan.config };
    delete fallbackConfig.hardwareAcceleration;
    vi.mocked(VideoDecoder.isConfigSupported)
      .mockResolvedValueOnce({ supported: false, config: plan.config })
      .mockResolvedValueOnce({ supported: true, config: fallbackConfig });
    const session = newSession(plan);

    const outcome = await session.decode(req(plan, 0));

    expect(outcome.ok).toBe(true);
    expect(VideoDecoder.isConfigSupported).toHaveBeenNthCalledWith(1, plan.config);
    expect(VideoDecoder.isConfigSupported).toHaveBeenNthCalledWith(2, fallbackConfig);
    expect(lastDecoder().configure).toHaveBeenCalledWith(fallbackConfig);
  });

  it.each(["initial", "hardware-fallback", "rejected"] as const)(
    "dispose during %s config support await cannot reopen the session",
    async (phase) => {
      const plan = gopPlan(10);
      let resolveSupport!: (value: VideoDecoderSupport) => void;
      let rejectSupport!: (reason: Error) => void;
      const deferred = new Promise<VideoDecoderSupport>((resolve, reject) => {
        resolveSupport = resolve;
        rejectSupport = reject;
      });
      const support = vi.mocked(VideoDecoder.isConfigSupported);
      if (phase === "hardware-fallback") support.mockResolvedValueOnce({ supported: false });
      support.mockReturnValueOnce(deferred);
      const session = newSession(plan);
      const pending = session.decode(req(plan, 0));
      await Promise.resolve();
      await Promise.resolve();
      expect(support).toHaveBeenCalledTimes(phase === "hardware-fallback" ? 2 : 1);
      session.dispose();
      if (phase === "rejected") rejectSupport(new Error("late failure"));
      else resolveSupport({ supported: true });
      expect((await pending).ok).toBe(false);
      expect(session.getStats().state).toBe("closed");
      expect(session.getStats().activeDecoders).toBe(0);
      expect(decoders).toHaveLength(0);
    },
  );

  it("首目标从 GOP key sample 提交到目标(提交 0..target 共 target+1 个 chunk)", async () => {
    const plan = gopPlan(30);
    const session = newSession(plan);
    await session.decode(req(plan, 5));
    expect(lastDecoder().decode).toHaveBeenCalledTimes(6);
    expect(session.getStats().submits).toBe(6);
    expect(session.getStats().cursor).toBe(5);
  });

  it("同 GOP 向前逐帧只提交增量 chunks(不退回平方级重解)", async () => {
    const plan = gopPlan(30);
    const session = newSession(plan);
    await session.decode(req(plan, 5));
    await session.decode(req(plan, 10));
    // 第二次只提交 samples[6..10] = 5 个,而非重新提交 0..10。
    expect(lastDecoder().decode).toHaveBeenCalledTimes(6 + 5);
    expect(session.getStats().submits).toBe(11);
    expect(session.getStats().resets).toBe(0);
    // flush 会把 VideoDecoder 置为下一输入必须是 key chunk；持久 session 主路径禁止调用。
    expect(lastDecoder().flush).not.toHaveBeenCalled();
  });

  it("同一目标 cache miss(原地)触发 reset 重解", async () => {
    const plan = gopPlan(30);
    const session = newSession(plan);
    await session.decode(req(plan, 5));
    vi.mocked(lastDecoder().decode).mockClear();
    await session.decode(req(plan, 5)); // target === cursor → hardReset + 重解 0..5
    expect(lastDecoder().reset).toHaveBeenCalledTimes(1);
    expect(lastDecoder().decode).toHaveBeenCalledTimes(6);
    expect(session.getStats().resets).toBe(1);
  });

  it("后退(target < cursor)触发 reset + reconfigure,从 key 重解", async () => {
    const plan = gopPlan(30);
    const session = newSession(plan);
    await session.decode(req(plan, 10));
    vi.mocked(lastDecoder().decode).mockClear();
    await session.decode(req(plan, 5)); // 后退
    expect(lastDecoder().reset).toHaveBeenCalledTimes(1);
    expect(lastDecoder().configure).toHaveBeenCalledTimes(2); // 初始 + reconfigure
    expect(lastDecoder().decode).toHaveBeenCalledTimes(6); // 0..5
    expect(session.getStats().resets).toBe(1);
  });

  it("后台预取需要后退时直接跳过，不 reset 前台 session", async () => {
    const plan = gopPlan(30);
    const session = newSession(plan);
    await session.decode(req(plan, 10));
    vi.mocked(lastDecoder().decode).mockClear();

    const outcome = await session.decode({
      ...req(plan, 5, 1),
      allowReset: false,
    });

    expect(outcome).toEqual({ ok: false, reason: "reset_required" });
    expect(lastDecoder().reset).not.toHaveBeenCalled();
    expect(lastDecoder().decode).not.toHaveBeenCalled();
    expect(session.getStats().resets).toBe(0);
  });

  it("dispose 关闭 decoder;新建 session 与旧 session 互不干扰(identity 隔离)", async () => {
    const plan = gopPlan(10);
    const session = newSession(plan);
    await session.decode(req(plan, 3));
    const firstDecoder = lastDecoder();
    session.dispose();
    expect(firstDecoder.close).toHaveBeenCalledTimes(1);
    expect(session.getStats().state).toBe("closed");
    // closed 后新请求返回 disposed。
    const outcome = await session.decode(req(plan, 4));
    expect(outcome).toEqual({ ok: false, reason: "disposed" });
  });

  it("B 帧目标会提交 presentation lookahead,且仍按 timestamp 命中", async () => {
    // decode order: frame0(key,pts0) → frame2(pts66) → frame1(pts33)。
    const samples = makeSamples([
      { frame: 0, pts: 0, key: true },
      { frame: 2, pts: 66, key: false },
      { frame: 1, pts: 33, key: false },
    ]);
    const bytes = makeBytes(64);
    const built = buildGopPlan(bytes, samples, 2);
    if (!built.ok) throw new Error("build failed");
    const plan = built.plan;
    ctrl.decodeImpl = (d, chunk) => {
      if (chunk.timestamp !== 33000) return;
      // 第三个 access unit(B,pts33)提交后，decoder 才能按 presentation order 输出到
      // 目标 P 帧(pts66)。若只提交到目标自身 decodeIndex=1，waiter 会超时。
      d.emit(fakeFrame(0));
      d.emit(fakeFrame(33000));
      d.emit(fakeFrame(66000));
    };
    const session = newSession(plan);
    const outcome = await session.decode(req(plan, 1)); // decodeIndex 1 = frame2(pts66)
    expect(outcome.ok).toBe(true);
    if (outcome.ok) expect(outcome.frame.timestamp).toBe(66000);
    expect(lastDecoder().decode).toHaveBeenCalledTimes(3);
  });

  it("B-frame GOP 为 reorder queue 额外提交一个有界 lookahead", async () => {
    // 真实 x264 main 常见 decode order：0, 1, 3, 2。目标 frame1 的 access unit 已在
    // decodeIndex1，但 Chromium AVC decoder 看到后续 frame3 才交付 frame1。
    const samples = makeSamples([
      { frame: 0, pts: 0, key: true },
      { frame: 1, pts: 33 },
      { frame: 3, pts: 100 },
      { frame: 2, pts: 67 },
    ]);
    const built = buildGopPlan(makeBytes(64), samples, 1);
    if (!built.ok) throw new Error("build failed");
    ctrl.decodeImpl = (d, chunk) => {
      if (chunk.timestamp === 67000) d.emit(fakeFrame(33000));
    };
    const session = newSession(built.plan);
    const outcome = await session.decode(req(built.plan, 1));
    expect(outcome.ok).toBe(true);
    expect(lastDecoder().decode).toHaveBeenCalledTimes(4);
    expect(session.getStats().cursor).toBe(3);
  });

  it("lookahead 已输出并关闭未来目标时 reset 后重发该帧", async () => {
    const samples = makeSamples([
      { frame: 0, pts: 0, key: true },
      { frame: 1, pts: 33 },
      { frame: 3, pts: 100 },
      { frame: 2, pts: 67 },
    ]);
    const built = buildGopPlan(makeBytes(64), samples, 0);
    if (!built.ok) throw new Error("build failed");
    // 模拟低延迟 decoder：每个输入立即 output。frame0 请求的 B-frame lookahead 会让
    // frame1 先被关闭；随后请求 frame1 必须 reset，不能指望 decoder 重发旧 output。
    defaultDecodeEmitChunk();
    const session = newSession(built.plan);
    expect((await session.decode(req(built.plan, 0))).ok).toBe(true);
    expect((await session.decode(req(built.plan, 1, 1))).ok).toBe(true);
    expect(lastDecoder().reset).toHaveBeenCalledTimes(1);
    expect(session.getStats().resets).toBe(1);
  });

  it("lookahead 未来帧移交 bitmap cache 后不 reset 重解", async () => {
    const samples = makeSamples([
      { frame: 0, pts: 0, key: true },
      { frame: 1, pts: 33 },
      { frame: 3, pts: 100 },
      { frame: 2, pts: 67 },
    ]);
    const built = buildGopPlan(makeBytes(64), samples, 0);
    if (!built.ok) throw new Error("build failed");
    defaultDecodeEmitChunk();
    const cached = new Set<number>();
    const session = newSession(built.plan, {
      onSupplementalFrame: async (frame, frameIndex) => {
        cached.add(frameIndex);
        frame.close();
        return true;
      },
    });

    expect((await session.decode(req(built.plan, 0))).ok).toBe(true);
    const next = await session.decode(req(built.plan, 1, 1));

    expect(next).toEqual({ ok: false, reason: "supplemental_cached" });
    expect(cached).toContain(1);
    expect(lastDecoder().reset).not.toHaveBeenCalled();
    expect(session.getStats().resets).toBe(0);
  });

  it("非目标 output 立即 close", async () => {
    const plan = gopPlan(10);
    const nonTarget = fakeFrame(0); // decodeIndex 0 的 pts=0,目标是 decodeIndex 3(pts 99→99000)
    ctrl.decodeImpl = (d, chunk) => {
      if (chunk.timestamp === 0) d.emit(nonTarget);
      if (chunk.timestamp === 99000) d.emit(fakeFrame(chunk.timestamp));
    };
    const session = newSession(plan);
    await session.decode(req(plan, 3));
    expect(vi.mocked(nonTarget.close)).toHaveBeenCalledTimes(1);
  });

  it("重复 timestamp 只保留第一份,其余 close 并计诊断", async () => {
    const plan = gopPlan(10);
    const first = fakeFrame(99000);
    const dup = fakeFrame(99000);
    ctrl.decodeImpl = (d, chunk) => {
      if (chunk.timestamp !== 99000) return;
      d.emit(first);
      d.emit(dup);
    };
    const session = newSession(plan);
    await session.decode(req(plan, 3));
    expect(vi.mocked(first.close)).not.toHaveBeenCalled();
    expect(vi.mocked(dup.close)).toHaveBeenCalledTimes(1);
    expect(session.getStats().duplicateOutputs).toBe(1);
  });

  it("decoder error 拒绝当前 waiter 并进入 failed;后续请求也失败", async () => {
    const plan = gopPlan(10);
    ctrl.decodeError = new DOMException("decode error");
    const session = newSession(plan);
    const outcome = await session.decode(req(plan, 3));
    expect(outcome).toEqual({ ok: false, reason: "decoder_error" });
    expect(session.getStats().state).toBe("failed");
    const again = await session.decode(req(plan, 4));
    expect(again).toEqual({ ok: false, reason: "decoder_error" });
  });

  it("目标 output 已到但后续 lookahead decode 抛错时关闭已持有 frame", async () => {
    const samples = makeSamples([
      { frame: 0, pts: 0, key: true },
      { frame: 2, pts: 66 },
      { frame: 1, pts: 33 },
    ]);
    const built = buildGopPlan(makeBytes(64), samples, 2);
    if (!built.ok) throw new Error("build failed");
    const target = fakeFrame(66000);
    ctrl.decodeImpl = (d, chunk) => {
      if (chunk.timestamp === 66000) d.emit(target);
      if (chunk.timestamp === 33000) throw new Error("corrupt lookahead");
    };
    const session = newSession(built.plan);
    await expect(session.decode(req(built.plan, 1))).resolves.toEqual({
      ok: false,
      reason: "decoder_error",
    });
    expect(vi.mocked(target.close)).toHaveBeenCalledTimes(1);
  });

  it("等待超时仍无目标 output → target_timestamp_missing", async () => {
    vi.useFakeTimers();
    const plan = gopPlan(10);
    ctrl.decodeImpl = () => undefined;
    const session = newSession(plan, { outputTimeoutMs: 10 });
    const pending = session.decode(req(plan, 3));
    await vi.advanceTimersByTimeAsync(11);
    const outcome = await pending;
    expect(outcome).toEqual({ ok: false, reason: "target_timestamp_missing" });
  });

  it("仅 GOP 尾目标缺少即时 output 时 flush drain；后续请求先 reset 再从 key 开始", async () => {
    const plan = gopPlan(10);
    ctrl.decodeImpl = () => undefined;
    ctrl.flushImpl = (d) => d.emit(fakeFrame(297000));
    const session = newSession(plan);
    const tail = await session.decode(req(plan, 9));
    expect(tail.ok).toBe(true);
    expect(lastDecoder().flush).toHaveBeenCalledTimes(1);

    defaultDecodeEmitChunk();
    const rewind = await session.decode(req(plan, 5));
    expect(rewind.ok).toBe(true);
    expect(lastDecoder().reset).toHaveBeenCalledTimes(1);
  });

  it("dispose 结算 pending waiter；迟到 output 被释放且 dispose 幂等", async () => {
    const plan = gopPlan(10);
    ctrl.decodeImpl = () => undefined;
    const session = newSession(plan, { outputTimeoutMs: 1000 });
    const pending = session.decode(req(plan, 3));
    await new Promise((resolve) => setTimeout(resolve, 0));
    session.dispose();
    session.dispose(); // 幂等
    await expect(pending).resolves.toEqual({ ok: false, reason: "disposed" });
    const late = fakeFrame(99000);
    lastDecoder().emit(late);
    expect(vi.mocked(late.close)).toHaveBeenCalledTimes(1);
    expect(lastDecoder().close).toHaveBeenCalledTimes(1);
    expect(session.getStats().disposals).toBe(1);
    expect(session.getStats().state).toBe("closed");
  });

  it("并发命令严格串行(第二个 decode 在第一个完成后才提交)", async () => {
    const plan = gopPlan(30);
    const session = newSession(plan);
    const p1 = session.decode(req(plan, 5));
    const p2 = session.decode(req(plan, 8));
    await Promise.all([p1, p2]);
    // 第二个 decode 在第一个之后只提交增量 samples[6..8] = 3 个。
    expect(lastDecoder().decode).toHaveBeenCalledTimes(6 + 3);
    expect(session.getStats().submits).toBe(6 + 3);
  });

  it("更高 request generation 立即取消旧 waiter，不让 stale 命令阻塞最新目标", async () => {
    const plan = gopPlan(30);
    ctrl.decodeImpl = () => undefined;
    const session = newSession(plan, { outputTimeoutMs: 1000 });
    const stale = session.decode(req(plan, 5, 1));
    await new Promise((resolve) => setTimeout(resolve, 0));

    ctrl.decodeImpl = (d, chunk) => {
      if (chunk.timestamp === 264000) d.emit(fakeFrame(chunk.timestamp));
    };
    const latest = session.decode(req(plan, 8, 2));

    await expect(stale).resolves.toEqual({ ok: false, reason: "stale_request" });
    const latestOutcome = await latest;
    expect(latestOutcome.ok).toBe(true);
    if (latestOutcome.ok) expect(latestOutcome.frame.timestamp).toBe(264000);
  });

  it("hard reset 递增 generation,旧 waiter 失效后重新 decode 得到新 frame", async () => {
    const plan = gopPlan(30);
    const session = newSession(plan);
    const first = await session.decode(req(plan, 5));
    expect(first.ok).toBe(true);
    // 原地再解(reset)→ 新的 output frame,不是残留。
    const second = await session.decode(req(plan, 5));
    expect(second.ok).toBe(true);
    if (first.ok && second.ok) {
      expect(second.frame).not.toBe(first.frame); // 不同 VideoFrame 实例
      expect(vi.mocked(first.frame.close)).not.toHaveBeenCalled(); // 旧 frame 归调用方
    }
    expect(session.getStats().resets).toBe(1);
  });

  it("idle 超时关闭 session(fake timers)", async () => {
    vi.useFakeTimers();
    const plan = gopPlan(10);
    const session = newSession(plan, { idleTimeoutMs: 1000 });
    await session.decode(req(plan, 3));
    expect(session.getStats().state).toBe("ready");
    await vi.advanceTimersByTimeAsync(1001);
    expect(session.getStats().state).toBe("closed");
    expect(session.getStats().disposals).toBe(1);
  });

  it("document 持续 hidden 超时关闭 session;恢复可见则不触发", async () => {
    vi.useFakeTimers();
    Object.defineProperty(document, "hidden", { value: true, configurable: true });
    const plan = gopPlan(10);
    const session = newSession(plan, { hiddenTimeoutMs: 1000 });
    await vi.advanceTimersByTimeAsync(999);
    expect(session.getStats().state).not.toBe("closed");
    // 恢复可见 → 清除 hidden 计时。
    Object.defineProperty(document, "hidden", { value: false, configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(2000);
    expect(session.getStats().state).not.toBe("closed");
    session.dispose();
    Object.defineProperty(document, "hidden", { value: false, configurable: true });
  });
});

// ── benchmark:30-frame GOP 的提交计数(§12)──────────────────────────────────

describe("VideoGopDecoderSession · 30-frame GOP benchmark", () => {
  beforeEach(() => {
    installSessionDecoder();
    defaultDecodeEmitChunk();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("场景 A:0→29 顺序逐帧,encoded chunk submit 总数 ≤ 30(不退回 465)", async () => {
    const plan = gopPlan(30);
    const session = newSession(plan);
    for (let i = 0; i < 30; i++) {
      const outcome = await session.decode(req(plan, i));
      expect(outcome.ok).toBe(true);
    }
    expect(session.getStats().submits).toBeLessThanOrEqual(30);
    expect(session.getStats().resets).toBe(0);
    session.dispose();
    expect(decoders.filter((d) => vi.mocked(d.close).mock.calls.length > 0)).toHaveLength(1);
  });

  it("场景 B:0→10→20→5→6,只在后退(5)时 reset 一次", async () => {
    const plan = gopPlan(30);
    const session = newSession(plan);
    for (const idx of [0, 10, 20, 5, 6]) {
      const outcome = await session.decode(req(plan, idx));
      expect(outcome.ok).toBe(true);
    }
    expect(session.getStats().resets).toBe(1);
    expect(session.getStats().submits).toBeLessThanOrEqual(30);
    session.dispose();
  });

  it("场景 C:快速 3→12→7→29,每次 decode 都成功返回目标 frame", async () => {
    const plan = gopPlan(30);
    const session = newSession(plan);
    for (const idx of [3, 12, 7, 29]) {
      const outcome = await session.decode(req(plan, idx));
      expect(outcome.ok).toBe(true);
      if (outcome.ok) {
        const sample = plan.samples[idx - plan.gopStartDecodeIndex];
        expect(outcome.frame.timestamp).toBe(sample.timestampUs);
      }
    }
    session.dispose();
  });
});

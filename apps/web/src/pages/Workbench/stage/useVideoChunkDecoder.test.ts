import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { buildGopPlan, type EncodedVideoDecodePlan, type VideoGopPlan } from "./videoChunkDemux";
import type { VideoChunkSampleEntry, VideoChunkSamplesResponse } from "@/types";
import type { VideoGopSessionIdentity } from "./videoGopDecoderSession";
import {
  WEBCODECS_FLAG_QUERY_KEY,
  chunkDecoderCacheKey,
  decodePlanToBitmap,
  detectWebCodecsSupport,
  isWebCodecsExperimentEnabled,
  useVideoChunkDecoder,
  type VideoChunkDecodeResult,
} from "./useVideoChunkDecoder";

describe("chunkDecoderCacheKey", () => {
  it("与 useVideoBitmapCache 同构 (`${taskId}:${frameIndex}`)", () => {
    expect(chunkDecoderCacheKey("task-1", 0)).toBe("task-1:0");
    expect(chunkDecoderCacheKey("abc", 42)).toBe("abc:42");
  });
});

describe("isWebCodecsExperimentEnabled · flag 解析", () => {
  const storageWith = (value: string | null) => ({ getItem: () => value });

  it("缺省开启 (无 query 无 storage)", () => {
    expect(isWebCodecsExperimentEnabled("", storageWith(null))).toBe(true);
    expect(isWebCodecsExperimentEnabled(null, null)).toBe(true);
  });

  it("URL query ?webcodecs=1 / =true 开启", () => {
    expect(isWebCodecsExperimentEnabled(`?${WEBCODECS_FLAG_QUERY_KEY}=1`, null)).toBe(true);
    expect(isWebCodecsExperimentEnabled(`?${WEBCODECS_FLAG_QUERY_KEY}=true`, null)).toBe(true);
  });

  it("URL query ?webcodecs=0 显式关闭 (覆盖 storage)", () => {
    expect(isWebCodecsExperimentEnabled(`?${WEBCODECS_FLAG_QUERY_KEY}=0`, storageWith("1"))).toBe(
      false,
    );
    expect(
      isWebCodecsExperimentEnabled(`?${WEBCODECS_FLAG_QUERY_KEY}=false`, storageWith("1")),
    ).toBe(false);
  });

  it("localStorage 显式值覆盖默认值 (query 缺省时)", () => {
    expect(isWebCodecsExperimentEnabled("", storageWith("1"))).toBe(true);
    expect(isWebCodecsExperimentEnabled("", storageWith("true"))).toBe(true);
    expect(isWebCodecsExperimentEnabled("", storageWith("0"))).toBe(false);
    expect(isWebCodecsExperimentEnabled("", storageWith("false"))).toBe(false);
    expect(isWebCodecsExperimentEnabled("", storageWith("invalid"))).toBe(true);
  });

  it("storage.getItem 抛错时仍使用默认开启并由能力探测负责回退", () => {
    const throwing = {
      getItem: () => {
        throw new Error("blocked");
      },
    };
    expect(isWebCodecsExperimentEnabled("", throwing)).toBe(true);
  });
});

describe("detectWebCodecsSupport · 能力探测", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("jsdom 无 VideoDecoder → false", () => {
    expect(detectWebCodecsSupport()).toBe(false);
  });

  it("存在 VideoDecoder + EncodedVideoChunk + createImageBitmap → true", () => {
    vi.stubGlobal("VideoDecoder", class {});
    vi.stubGlobal("EncodedVideoChunk", class {});
    vi.stubGlobal("createImageBitmap", () => Promise.resolve({} as ImageBitmap));
    expect(detectWebCodecsSupport()).toBe(true);
  });
});

// ── mock 基础设施 ──────────────────────────────────────────────────────────────

function fakeFrame(timestamp: number) {
  const close = vi.fn();
  const frame = {
    timestamp,
    displayWidth: 320,
    displayHeight: 240,
    close,
  } as unknown as VideoFrame;
  return { frame, close };
}

function fakeBitmap() {
  return { width: 320, height: 240, close: vi.fn() } as unknown as ImageBitmap;
}

function makePlan(targetTimestampUs = 33000, frameIndex = 1): EncodedVideoDecodePlan {
  return {
    config: { codec: "avc1.4d001e", codedWidth: 320, codedHeight: 240 },
    chunks: [{} as EncodedVideoChunk],
    targetFrameIndex: frameIndex,
    targetTimestampUs,
    chunkId: 0,
    gopStartDecodeIndex: 0,
    targetDecodeIndex: 0,
  };
}

function makeBytes(n: number): ArrayBuffer {
  const buf = new ArrayBuffer(n);
  const view = new Uint8Array(buf);
  for (let i = 0; i < n; i++) view[i] = (i * 7) & 0xff;
  return buf;
}

function makeSamples(
  entries: Array<{ frame: number; pts: number; key?: boolean }>,
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
      duration_ms: 33,
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
    width: 320,
    height: 240,
    samples,
  };
}

/** 单 sample GOP:frame `frameIndex` 为 key,pts=33ms(→ 33000us,匹配 FakeDecoder 默认 emit)。 */
function gopPlan(frameIndex: number): VideoGopPlan {
  const samples = makeSamples([{ frame: frameIndex, pts: 33, key: true }]);
  const result = buildGopPlan(makeBytes(64), samples, frameIndex);
  if (!result.ok) throw new Error("gopPlan build failed");
  return result.plan;
}

function gopIdentity(plan: VideoGopPlan, taskId = "task-1"): VideoGopSessionIdentity {
  return {
    taskId,
    datasetItemId: "ds-1",
    chunkId: plan.chunkId,
    gopStartDecodeIndex: plan.gopStartDecodeIndex,
    configFingerprint: plan.configFingerprint,
  };
}

/** 构造 decodePlan 新签名所需的参数对象。 */
function decodeArgs(frameIndex: number, taskId = "task-1", generation = 0) {
  const plan = gopPlan(frameIndex);
  return {
    plan,
    identity: gopIdentity(plan, taskId),
    targetFrameIndex: frameIndex,
    generation,
  };
}

/** 可控 VideoDecoder 替身:flush 默认 resolve,行为经 controller 配置。 */
class FakeDecoder {
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
}

interface DecoderCtrl {
  supported: boolean | null;
  supportThrow: boolean;
  configureThrow: boolean;
  decodeImpl: ((d: FakeDecoder, chunk: EncodedVideoChunk) => void) | null;
  decodeThrow: boolean;
  decodeError: DOMException | null;
  flushImpl: ((d: FakeDecoder) => void | Promise<void>) | null;
  flushThrow: boolean;
  flushError: DOMException | null;
}

/** 安装 VideoDecoder/EncodedVideoChunk 全局替身,返回行为控制器与已建 decoder 列表。 */
function installDecoderMock(): { ctrl: DecoderCtrl; decoders: FakeDecoder[] } {
  const ctrl: DecoderCtrl = {
    supported: null,
    supportThrow: false,
    configureThrow: false,
    decodeImpl: null,
    decodeThrow: false,
    decodeError: null,
    flushImpl: null,
    flushThrow: false,
    flushError: null,
  };
  const decoders: FakeDecoder[] = [];
  class FakeVideoDecoder extends FakeDecoder {
    static isConfigSupported = vi.fn(async () => {
      if (ctrl.supportThrow) throw new Error("support failed");
      return { supported: ctrl.supported ?? true, config: undefined };
    });
    constructor(init: { output: (f: VideoFrame) => void; error: (e: DOMException) => void }) {
      super(init);
      decoders.push(this);
      this.configure.mockImplementation(() => {
        if (ctrl.configureThrow) throw new Error("configure failed");
      });
      this.decode.mockImplementation((chunk: EncodedVideoChunk) => {
        if (ctrl.decodeThrow) throw new Error("decode failed");
        if (ctrl.decodeError) init.error(ctrl.decodeError);
        ctrl.decodeImpl?.(this, chunk);
      });
      this.flush.mockImplementation(async () => {
        if (ctrl.flushThrow) throw new Error("flush failed");
        if (ctrl.flushError) init.error(ctrl.flushError);
        if (ctrl.flushImpl) await ctrl.flushImpl(this);
      });
    }
  }
  vi.stubGlobal("VideoDecoder", FakeVideoDecoder);
  vi.stubGlobal(
    "EncodedVideoChunk",
    class {
      readonly timestamp: number;
      constructor(init: EncodedVideoChunkInit) {
        this.timestamp = init.timestamp;
      }
    },
  );
  return { ctrl, decoders };
}

// ── decodePlanToBitmap:按 timestamp 命中 + 资源清理 ───────────────────────────

describe("decodePlanToBitmap · 按 timestamp 选目标 + 资源清理", () => {
  let ctrl: DecoderCtrl;
  let decoders: FakeDecoder[];
  const last = () => decoders[decoders.length - 1];
  beforeEach(() => {
    vi.stubGlobal("createImageBitmap", () => Promise.resolve(fakeBitmap()));
    ({ ctrl, decoders } = installDecoderMock());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("output 乱序仍按 timestamp 命中目标;非目标 frame 立即 close", async () => {
    const a = fakeFrame(66000); // 非 target
    const b = fakeFrame(33000); // target
    ctrl.flushImpl = async (d) => {
      d.emit(a.frame); // 先到的非目标
      d.emit(b.frame); // 后到的目标
    };
    const result = await decodePlanToBitmap(makePlan(33000));
    expect(result).not.toBeNull();
    expect(result?.frameIndex).toBe(1);
    expect(a.close).toHaveBeenCalledTimes(1); // 非目标 emit 时立即 close
    expect(b.close).toHaveBeenCalledTimes(1); // 目标在 finally close
    expect(last().close).toHaveBeenCalledTimes(1);
  });

  it("没有命中 target timestamp → null", async () => {
    const a = fakeFrame(0);
    ctrl.flushImpl = async (d) => {
      d.emit(a.frame);
    };
    expect(await decodePlanToBitmap(makePlan(33000))).toBeNull();
    expect(a.close).toHaveBeenCalledTimes(1);
  });

  it("isConfigSupported=false 不 configure/decode,记 codec_unsupported", async () => {
    ctrl.supported = false;
    const reason = vi.fn();
    expect(await decodePlanToBitmap(makePlan(), reason)).toBeNull();
    expect(decoders).toHaveLength(0);
    expect(reason).toHaveBeenCalledWith("codec_unsupported");
  });

  it("isConfigSupported 抛错 → null + codec_unsupported", async () => {
    ctrl.supportThrow = true;
    const reason = vi.fn();
    expect(await decodePlanToBitmap(makePlan(), reason)).toBeNull();
    expect(decoders).toHaveLength(0);
    expect(reason).toHaveBeenCalledWith("codec_unsupported");
  });

  it("configure 抛错 → null + codec_unsupported", async () => {
    ctrl.configureThrow = true;
    const reason = vi.fn();
    expect(await decodePlanToBitmap(makePlan(), reason)).toBeNull();
    expect(reason).toHaveBeenCalledWith("codec_unsupported");
  });

  it("flush reject → null + decode_failed", async () => {
    ctrl.flushThrow = true;
    const reason = vi.fn();
    expect(await decodePlanToBitmap(makePlan(), reason)).toBeNull();
    expect(reason).toHaveBeenCalledWith("decode_failed");
  });

  it("error callback 触发 → null + decode_failed", async () => {
    ctrl.flushError = new DOMException("decode error");
    const reason = vi.fn();
    expect(await decodePlanToBitmap(makePlan(), reason)).toBeNull();
    expect(reason).toHaveBeenCalledWith("decode_failed");
  });

  it("createImageBitmap 抛错 → null;wanted frame 仍被 close", async () => {
    vi.stubGlobal("createImageBitmap", () => Promise.reject(new Error("oom")));
    const b = fakeFrame(33000);
    ctrl.flushImpl = async (d) => {
      d.emit(b.frame);
    };
    expect(await decodePlanToBitmap(makePlan())).toBeNull();
    expect(b.close).toHaveBeenCalledTimes(1); // finally 仍 close wanted
  });

  it("成功交付的 bitmap 不被 decoder finally close(由缓存接管)", async () => {
    const bmp = fakeBitmap();
    const bmpClose = vi.mocked(bmp.close);
    vi.stubGlobal("createImageBitmap", () => Promise.resolve(bmp));
    ctrl.flushImpl = async (d) => {
      d.emit(fakeFrame(33000).frame);
    };
    const result = await decodePlanToBitmap(makePlan());
    expect(result).not.toBeNull();
    expect(bmpClose).not.toHaveBeenCalled();
  });

  it("不支持 WebCodecs 时返回 null", async () => {
    vi.unstubAllGlobals();
    expect(await decodePlanToBitmap(makePlan())).toBeNull();
  });
});

// ── useVideoChunkDecoder hook ─────────────────────────────────────────────────

describe("useVideoChunkDecoder · flag 关闭 (默认) 行为", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });
  it("WebCodecs 不可用 / flag 关闭时 active=false,decodePlan no-op", async () => {
    // buildGopPlan 构造 chunk 需要 EncodedVideoChunk;不 stub VideoDecoder/createImageBitmap,
    // 保持 detectWebCodecsSupport=false(active=false)。
    vi.stubGlobal("EncodedVideoChunk", class {});
    const { result } = renderHook(() => useVideoChunkDecoder({ taskId: "task-1", enabled: false }));
    expect(result.current.active).toBe(false);
    expect(result.current.enabled).toBe(false);
    const decoded: { current: VideoChunkDecodeResult | null } = { current: null };
    await act(async () => {
      decoded.current = await result.current.decodePlan(decodeArgs(1));
    });
    expect(decoded.current?.bitmap).toBeNull();
    expect(decoded.current?.fallbackReason).toBe("flag_disabled");
    expect(result.current.showFrame(0)).toBeNull();
  });
});

describe("useVideoChunkDecoder · 缓存、single-flight 与诊断 (mock WebCodecs)", () => {
  let ctrl: DecoderCtrl;
  let decoders: FakeDecoder[];
  const bitmapClose = vi.fn();

  beforeEach(() => {
    bitmapClose.mockClear();
    vi.stubGlobal("createImageBitmap", () =>
      Promise.resolve({ width: 4, height: 4, close: bitmapClose } as unknown as ImageBitmap),
    );
    ({ ctrl, decoders } = installDecoderMock());
    // 持久 session 的 output 由 decode 产生；它不得依赖 flush。
    ctrl.decodeImpl = (d, chunk) => {
      d.emit(fakeFrame(chunk.timestamp).frame);
    };
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("decodePlan 入缓存(不自动激活);showFrame 命中记 hit 并激活", async () => {
    const { result } = renderHook(() => useVideoChunkDecoder({ taskId: "task-1", enabled: true }));
    expect(result.current.active).toBe(true);

    await act(async () => {
      await result.current.decodePlan(decodeArgs(7));
    });
    expect(result.current.diagnostics.decodes).toBe(1);
    expect(result.current.diagnostics.cacheSize).toBe(1);
    // decode 成功只入缓存,不自动激活 active 指针。
    expect(result.current.activeFrameIndex).toBeNull();

    let hit: unknown = null;
    act(() => {
      hit = result.current.showFrame(7);
    });
    expect(hit).not.toBeNull();
    expect(result.current.diagnostics.hits).toBe(1);
    expect(result.current.activeFrameIndex).toBe(7);

    act(() => {
      result.current.showFrame(99);
    });
    expect(result.current.diagnostics.misses).toBe(1);
  });

  it("相同 frame 并发只调用一次底层 decode(single-flight)", async () => {
    const { result } = renderHook(() => useVideoChunkDecoder({ taskId: "t1", enabled: true }));
    let pair: [VideoChunkDecodeResult, VideoChunkDecodeResult] = [
      { bitmap: null, fallbackReason: "decode_failed" },
      { bitmap: null, fallbackReason: "decode_failed" },
    ];
    await act(async () => {
      pair = await Promise.all([
        result.current.decodePlan(decodeArgs(5, "t1")),
        result.current.decodePlan(decodeArgs(5, "t1")),
      ]);
    });
    expect(decoders).toHaveLength(1); // 并发共享同一 in-flight promise
    expect(pair[0].bitmap).not.toBeNull();
    expect(pair[1].bitmap).not.toBeNull();
  });

  it("clear() 释放缓存中全部 ImageBitmap", async () => {
    const { result } = renderHook(() => useVideoChunkDecoder({ taskId: "task-1", enabled: true }));
    await act(async () => {
      await result.current.decodePlan(decodeArgs(1));
    });
    bitmapClose.mockClear();
    act(() => {
      result.current.clear();
    });
    expect(bitmapClose).toHaveBeenCalledTimes(1);
    expect(result.current.diagnostics.cacheSize).toBe(0);
  });

  it("卸载时清理缓存里的 ImageBitmap", async () => {
    const { result, unmount } = renderHook(() =>
      useVideoChunkDecoder({ taskId: "task-1", enabled: true }),
    );
    let second: VideoChunkDecodeResult = { bitmap: null, fallbackReason: "decode_failed" };
    await act(async () => {
      second = await result.current.decodePlan(decodeArgs(2));
    });
    expect(second).toMatchObject({ fallbackReason: null });
    expect(second.bitmap).not.toBeNull();
    bitmapClose.mockClear();
    unmount();
    expect(bitmapClose).toHaveBeenCalledTimes(1);
  });

  it.each(["resource", "task", "clear"] as const)(
    "%s 切换后卸载释放当前会话，所有实际 decoder 均关闭",
    async (transition) => {
      const initial = { taskId: "task-1", resourceKey: "output-1" };
      const { result, rerender, unmount } = renderHook(
        (props) => useVideoChunkDecoder({ ...props, enabled: true }),
        { initialProps: initial },
      );
      await act(async () => {
        await result.current.decodePlan(decodeArgs(1));
      });
      expect(decoders).toHaveLength(1);
      const next = {
        taskId: transition === "task" ? "task-2" : initial.taskId,
        resourceKey: transition === "resource" ? "output-2" : initial.resourceKey,
      };
      if (transition === "clear") act(() => result.current.clear());
      else rerender(next);
      expect(decoders[0].close).toHaveBeenCalledTimes(1);
      await act(async () => {
        await result.current.decodePlan(decodeArgs(2, next.taskId));
      });
      expect(decoders).toHaveLength(2);
      expect(result.current.diagnostics.activeDecoders).toBe(1);
      unmount();
      expect(decoders.every((decoder) => decoder.close.mock.calls.length === 1)).toBe(true);
      expect(decoders.filter((decoder) => decoder.close.mock.calls.length === 0)).toHaveLength(0);
    },
  );

  it("retired decode callbacks cannot create sessions after a resource switch or unmount", async () => {
    const { result, rerender, unmount } = renderHook(
      ({ resourceKey }) => useVideoChunkDecoder({ taskId: "task-1", resourceKey, enabled: true }),
      { initialProps: { resourceKey: "output-1" } },
    );
    const retiredDecode = result.current.decodePlan;
    rerender({ resourceKey: "output-2" });
    await act(async () => {
      expect(await retiredDecode(decodeArgs(1))).toMatchObject({
        bitmap: null,
        fallbackReason: "stale_request",
      });
    });
    expect(decoders).toHaveLength(0);
    const currentDecode = result.current.decodePlan;
    unmount();
    expect(await currentDecode(decodeArgs(2))).toMatchObject({
      bitmap: null,
      fallbackReason: "stale_request",
    });
    expect(decoders).toHaveLength(0);
  });

  it.each(["clear", "unmount"] as const)(
    "%s 期间迟到的配置检查不会创建遗留 decoder",
    async (transition) => {
      let resolveSupport!: (value: VideoDecoderSupport) => void;
      vi.mocked(VideoDecoder.isConfigSupported).mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveSupport = resolve;
          }),
      );
      const { result, unmount } = renderHook(() =>
        useVideoChunkDecoder({ taskId: "task-1", enabled: true }),
      );
      let pending!: Promise<VideoChunkDecodeResult>;
      act(() => {
        pending = result.current.decodePlan(decodeArgs(1));
      });
      await waitFor(() => expect(resolveSupport).toBeTypeOf("function"));
      if (transition === "clear") {
        act(() => result.current.clear());
        await act(async () => {
          await result.current.decodePlan(decodeArgs(2));
        });
        expect(decoders).toHaveLength(1);
      }
      unmount();
      await act(async () => {
        resolveSupport({ supported: true });
        expect((await pending).bitmap).toBeNull();
      });
      expect(decoders).toHaveLength(transition === "clear" ? 1 : 0);
      expect(decoders.every((decoder) => decoder.close.mock.calls.length === 1)).toBe(true);
    },
  );

  it("单张 bitmap 超预算时不入 LRU，但前台目标仍可显示并由 active 所有权释放", async () => {
    const { result } = renderHook(() =>
      useVideoChunkDecoder({ taskId: "task-1", enabled: true, bitmapBudgetBytes: 1 }),
    );
    let decoded: VideoChunkDecodeResult = { bitmap: null, fallbackReason: "decode_failed" };
    await act(async () => {
      decoded = await result.current.decodePlan(decodeArgs(1));
    });
    expect(decoded.bitmap).not.toBeNull();
    expect(result.current.diagnostics.bitmapBytes).toBe(64);

    act(() => {
      expect(result.current.showFrame(1)).toBe(decoded.bitmap);
    });
    expect(result.current.activeBitmap).toBe(decoded.bitmap);
    expect(bitmapClose).not.toHaveBeenCalled();

    act(() => result.current.clear());
    expect(bitmapClose).toHaveBeenCalledTimes(1);
  });

  it("活动 bitmap 移出 LRU，后续缓存写入与淘汰不会提前关闭 Konva 正在显示的帧", async () => {
    const { result } = renderHook(() =>
      useVideoChunkDecoder({ taskId: "task-1", enabled: true, bitmapBudgetBytes: 64 }),
    );
    await act(async () => {
      await result.current.decodePlan(decodeArgs(1));
    });
    act(() => {
      result.current.showFrame(1);
    });
    bitmapClose.mockClear();

    await act(async () => {
      await result.current.decodePlan(decodeArgs(2));
    });
    expect(result.current.activeBitmap?.frameIndex).toBe(1);
    expect(bitmapClose).not.toHaveBeenCalled();
  });

  it("活动 bitmap 与 LRU 共同受总字节预算约束", async () => {
    const isolatedArgs = (frameIndex: number) => {
      const args = decodeArgs(frameIndex);
      return {
        ...args,
        identity: {
          ...args.identity,
          configFingerprint: `${args.identity.configFingerprint}:${frameIndex}`,
        },
      };
    };
    const { result } = renderHook(() =>
      useVideoChunkDecoder({ taskId: "task-1", enabled: true, bitmapBudgetBytes: 128 }),
    );
    await act(async () => {
      await result.current.decodePlan(isolatedArgs(1));
    });
    act(() => {
      result.current.showFrame(1);
    });

    let second: VideoChunkDecodeResult = { bitmap: null, fallbackReason: "decode_failed" };
    await act(async () => {
      second = await result.current.decodePlan(isolatedArgs(2));
    });
    expect(second.bitmap).not.toBeNull();
    expect(second.fallbackReason).toBeNull();
    expect(result.current.diagnostics).toMatchObject({ bitmapBytes: 128, cacheSize: 2 });
    await act(async () => {
      await result.current.decodePlan(isolatedArgs(3));
    });

    expect(result.current.activeBitmap?.frameIndex).toBe(1);
    expect(result.current.diagnostics).toMatchObject({
      bitmapBytes: 128,
      bitmapBudgetBytes: 128,
      cacheSize: 2,
    });
    expect(result.current.diagnostics.bitmapBytes).toBeLessThanOrEqual(
      result.current.diagnostics.bitmapBudgetBytes,
    );
    expect(bitmapClose).toHaveBeenCalledTimes(1);
  });

  it("failed session 不复用；下一次请求会新建 decoder 并恢复", async () => {
    const { result } = renderHook(() => useVideoChunkDecoder({ taskId: "t1", enabled: true }));
    ctrl.decodeError = new DOMException("decode error");
    let first: VideoChunkDecodeResult = { bitmap: null, fallbackReason: "decode_failed" };
    await act(async () => {
      first = await result.current.decodePlan(decodeArgs(1, "t1", 1));
    });
    expect(first.bitmap).toBeNull();
    expect(decoders).toHaveLength(1);

    ctrl.decodeError = null;
    let second: VideoChunkDecodeResult = { bitmap: null, fallbackReason: "decode_failed" };
    await act(async () => {
      second = await result.current.decodePlan(decodeArgs(1, "t1", 2));
    });
    expect(second.bitmap).not.toBeNull();
    expect(decoders).toHaveLength(2);
    expect(decoders[0].close).toHaveBeenCalledTimes(1);
  });

  it("task 切换后旧解码结果关闭 bitmap 且不入新缓存(staleResults)", async () => {
    // 在 wanted 帧确定之后、createImageBitmap resolve 之前阻塞,以便切换 task。
    let resolveBitmap: ((b: ImageBitmap) => void) | null = null;
    vi.stubGlobal(
      "createImageBitmap",
      () =>
        new Promise<ImageBitmap>((resolve) => {
          resolveBitmap = resolve;
        }),
    );
    const { result, rerender } = renderHook(
      ({ tid }: { tid: string }) => useVideoChunkDecoder({ taskId: tid, enabled: true }),
      { initialProps: { tid: "t1" } },
    );
    let decodePromise = Promise.resolve({
      bitmap: null,
      fallbackReason: "decode_failed",
    } as VideoChunkDecodeResult);
    await act(async () => {
      decodePromise = result.current.decodePlan(decodeArgs(3, "t1"));
      // 推进 microtask 到 createImageBitmap 阻塞点。
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(result.current.diagnostics.activeDecoders).toBe(1);
    expect(result.current.diagnostics.liveVideoFrames).toBe(1);
    // decode 进行中(wanted 已命中,等待 createImageBitmap),task 切到 t2
    // → 旧 taskId(t1) 闭包不再匹配 taskIdRef.current(t2)。
    act(() => {
      rerender({ tid: "t2" });
    });
    let resolved: unknown = "sentinel";
    await act(async () => {
      resolveBitmap?.({
        width: 4,
        height: 4,
        close: bitmapClose,
      } as unknown as ImageBitmap);
      resolved = await decodePromise;
    });
    expect((resolved as VideoChunkDecodeResult).bitmap).toBeNull();
    expect((resolved as VideoChunkDecodeResult).fallbackReason).toBe("stale_request");
    expect(bitmapClose).toHaveBeenCalledTimes(1); // 旧 bitmap 被关闭,不入新缓存
    expect(result.current.diagnostics.staleResults).toBe(1);
    expect(result.current.diagnostics.activeDecoders).toBe(0);
    expect(result.current.diagnostics.liveVideoFrames).toBe(0);
  });

  it("在途 decode 完成前卸载 → 新 bitmap 立即 close,不写回已卸载缓存", async () => {
    let resolveBitmap: ((b: ImageBitmap) => void) | null = null;
    vi.stubGlobal(
      "createImageBitmap",
      () =>
        new Promise<ImageBitmap>((resolve) => {
          resolveBitmap = resolve;
        }),
    );
    const { result, unmount } = renderHook(() =>
      useVideoChunkDecoder({ taskId: "t1", enabled: true }),
    );
    let decodePromise = Promise.resolve({
      bitmap: null,
      fallbackReason: "decode_failed",
    } as VideoChunkDecodeResult);
    await act(async () => {
      decodePromise = result.current.decodePlan(decodeArgs(9, "t1"));
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    unmount();

    const lateBitmap = {
      width: 4,
      height: 4,
      close: bitmapClose,
    } as unknown as ImageBitmap;
    const decoded: { current: VideoChunkDecodeResult | null } = { current: null };
    await act(async () => {
      resolveBitmap?.(lateBitmap);
      decoded.current = await decodePromise;
    });

    expect(decoded.current?.bitmap).toBeNull();
    expect(decoded.current?.fallbackReason).toBe("stale_request");
    expect(bitmapClose).toHaveBeenCalledTimes(1);
  });
});

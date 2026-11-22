import { afterEach, describe, expect, it, vi } from "vitest";
import { buildEncodedVideoDecodePlan, type EncodedVideoDecodePlan } from "./videoChunkDemux";
import type { VideoChunkSampleEntry, VideoChunkSamplesResponse } from "@/types";

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
  readonly recorded: RecordedChunkInit;
  constructor(init: RecordedChunkInit) {
    this.type = init.type;
    this.timestamp = init.timestamp;
    this.duration = init.duration;
    this.byteLength = init.data.byteLength;
    this.recorded = init;
  }
  copyTo(): void {}
  clone(): FakeEncodedVideoChunk {
    return new FakeEncodedVideoChunk(this.recorded);
  }
}

function recordedInit(plan: EncodedVideoDecodePlan): RecordedChunkInit[] {
  return plan.chunks.map((c) => (c as unknown as { recorded: RecordedChunkInit }).recorded);
}

function makeBytes(n: number): ArrayBuffer {
  const buf = new ArrayBuffer(n);
  const view = new Uint8Array(buf);
  for (let i = 0; i < n; i++) view[i] = (i * 7) & 0xff;
  return buf;
}

function makeSamples(
  entries: Array<{
    frame: number;
    pts: number;
    dur?: number;
    key?: boolean;
    offset?: number;
    size?: number;
  }>,
  opts: { description?: string | null; width?: number; height?: number } = {},
): VideoChunkSamplesResponse {
  const samples: VideoChunkSampleEntry[] = [];
  let cursor = 0;
  for (const e of entries) {
    const offset = e.offset ?? cursor;
    const size = e.size ?? 10;
    if (e.offset === undefined) cursor = offset + size;
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
    description: opts.description === undefined ? btoa("config-bytes") : opts.description,
    width: opts.width ?? 1920,
    height: opts.height ?? 1080,
    samples,
  };
}

describe("buildEncodedVideoDecodePlan", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("普通 I/P GOP:从最近 key 回溯到 target;timestamp/duration 为微秒", () => {
    vi.stubGlobal("EncodedVideoChunk", FakeEncodedVideoChunk);
    const samples = makeSamples([
      { frame: 0, pts: 0, key: true },
      { frame: 1, pts: 33, key: false },
      { frame: 2, pts: 66, key: false },
    ]);
    const result = buildEncodedVideoDecodePlan(makeBytes(64), samples, 2);
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.plan.chunks).toHaveLength(3);
    expect(result.plan.targetDecodeIndex).toBe(2);
    expect(result.plan.gopStartDecodeIndex).toBe(0);
    expect(result.plan.targetTimestampUs).toBe(66000);
    const init = recordedInit(result.plan);
    expect(init.map((c) => c.timestamp)).toEqual([0, 33000, 66000]);
    expect(init.map((c) => c.type)).toEqual(["key", "delta", "delta"]);
    expect(init[0].duration).toBe(33000);
  });

  it("target 即关键帧:只构造一个 key chunk", () => {
    vi.stubGlobal("EncodedVideoChunk", FakeEncodedVideoChunk);
    const samples = makeSamples([
      { frame: 0, pts: 0, key: true },
      { frame: 1, pts: 33, key: false },
      { frame: 5, pts: 165, key: true },
      { frame: 6, pts: 198, key: false },
    ]);
    const result = buildEncodedVideoDecodePlan(makeBytes(64), samples, 5);
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.plan.chunks).toHaveLength(1);
    expect(result.plan.gopStartDecodeIndex).toBe(result.plan.targetDecodeIndex);
    expect(recordedInit(result.plan)[0].type).toBe("key");
  });

  it("B 帧 decode order [10,12,11]:target 11/12 各自 timestamp,不按展示顺序重排输入", () => {
    vi.stubGlobal("EncodedVideoChunk", FakeEncodedVideoChunk);
    const samples = makeSamples([
      { frame: 10, pts: 0, key: true },
      { frame: 12, pts: 66, key: false },
      { frame: 11, pts: 33, key: false },
    ]);

    const r11 = buildEncodedVideoDecodePlan(makeBytes(64), samples, 11);
    expect(r11.ok).toBe(true);
    if (!r11.ok) return;
    expect(r11.plan.chunks).toHaveLength(3);
    expect(r11.plan.targetTimestampUs).toBe(33000);
    expect(recordedInit(r11.plan).map((c) => c.timestamp)).toEqual([0, 66000, 33000]);

    const r12 = buildEncodedVideoDecodePlan(makeBytes(64), samples, 12);
    expect(r12.ok).toBe(true);
    if (!r12.ok) return;
    expect(r12.plan.chunks).toHaveLength(2);
    expect(r12.plan.targetTimestampUs).toBe(66000);
    expect(recordedInit(r12.plan).map((c) => c.timestamp)).toEqual([0, 66000]);
  });

  it("两个 GOP:target 只回溯到同 GOP 最近 key,不跨 GOP", () => {
    vi.stubGlobal("EncodedVideoChunk", FakeEncodedVideoChunk);
    const samples = makeSamples([
      { frame: 0, pts: 0, key: true },
      { frame: 1, pts: 33, key: false },
      { frame: 2, pts: 66, key: true },
      { frame: 3, pts: 99, key: false },
    ]);
    const result = buildEncodedVideoDecodePlan(makeBytes(64), samples, 3);
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.plan.gopStartDecodeIndex).toBe(2);
    expect(result.plan.chunks).toHaveLength(2);
  });

  it("target 不存在 → invalid_sample_range", () => {
    vi.stubGlobal("EncodedVideoChunk", FakeEncodedVideoChunk);
    const samples = makeSamples([{ frame: 0, pts: 0, key: true }]);
    const result = buildEncodedVideoDecodePlan(makeBytes(64), samples, 99);
    expect(result).toEqual({ ok: false, reason: "invalid_sample_range" });
  });

  it("GOP 起点前无关键帧 → invalid_sample_range,不伪造 key", () => {
    vi.stubGlobal("EncodedVideoChunk", FakeEncodedVideoChunk);
    const samples = makeSamples([
      { frame: 0, pts: 0, key: false },
      { frame: 1, pts: 33, key: false },
    ]);
    const result = buildEncodedVideoDecodePlan(makeBytes(64), samples, 1);
    expect(result).toEqual({ ok: false, reason: "invalid_sample_range" });
  });

  it("offset 负数 → invalid_sample_range", () => {
    vi.stubGlobal("EncodedVideoChunk", FakeEncodedVideoChunk);
    const samples = makeSamples([
      { frame: 0, pts: 0, key: true, offset: -1, size: 4 },
      { frame: 1, pts: 33, key: false, offset: 4, size: 4 },
    ]);
    const result = buildEncodedVideoDecodePlan(makeBytes(64), samples, 1);
    expect(result).toEqual({ ok: false, reason: "invalid_sample_range" });
  });

  it("offset 小数 → invalid_sample_range", () => {
    vi.stubGlobal("EncodedVideoChunk", FakeEncodedVideoChunk);
    const samples = makeSamples([{ frame: 0, pts: 0, key: true, offset: 1.5, size: 4 }]);
    const result = buildEncodedVideoDecodePlan(makeBytes(64), samples, 0);
    expect(result).toEqual({ ok: false, reason: "invalid_sample_range" });
  });

  it("offset + size 越界 → invalid_sample_range", () => {
    vi.stubGlobal("EncodedVideoChunk", FakeEncodedVideoChunk);
    const samples = makeSamples([{ frame: 0, pts: 0, key: true, offset: 60, size: 10 }]);
    const result = buildEncodedVideoDecodePlan(makeBytes(64), samples, 0);
    expect(result).toEqual({ ok: false, reason: "invalid_sample_range" });
  });

  it("size 0 → invalid_sample_range", () => {
    vi.stubGlobal("EncodedVideoChunk", FakeEncodedVideoChunk);
    const samples = makeSamples([{ frame: 0, pts: 0, key: true, offset: 0, size: 0 }]);
    const result = buildEncodedVideoDecodePlan(makeBytes(64), samples, 0);
    expect(result).toEqual({ ok: false, reason: "invalid_sample_range" });
  });

  it("description base64 → config.description 字节一致;data 为 chunkBytes view 不复制", () => {
    vi.stubGlobal("EncodedVideoChunk", FakeEncodedVideoChunk);
    const samples = makeSamples([{ frame: 0, pts: 0, key: true }], {
      description: btoa("hello-config"),
    });
    const chunkBytes = makeBytes(64);
    const result = buildEncodedVideoDecodePlan(chunkBytes, samples, 0);
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    const desc = new Uint8Array(result.plan.config.description as Uint8Array);
    expect(Array.from(desc)).toEqual(Array.from("hello-config").map((c) => c.charCodeAt(0)));
    expect(result.plan.config.optimizeForLatency).toBe(true);
    expect(result.plan.config.hardwareAcceleration).toBe("prefer-hardware");
    expect(recordedInit(result.plan)[0].data.buffer).toBe(chunkBytes);
  });

  it("description 非法 base64 → description_unavailable", () => {
    vi.stubGlobal("EncodedVideoChunk", FakeEncodedVideoChunk);
    const samples = makeSamples([{ frame: 0, pts: 0, key: true }], {
      description: "@@not-base64@@",
    });
    const result = buildEncodedVideoDecodePlan(makeBytes(64), samples, 0);
    expect(result).toEqual({ ok: false, reason: "description_unavailable" });
  });

  it("description 缺失(null)→ description_unavailable(不猜 Annex-B)", () => {
    vi.stubGlobal("EncodedVideoChunk", FakeEncodedVideoChunk);
    const samples = makeSamples([{ frame: 0, pts: 0, key: true }], {
      description: null,
    });
    const result = buildEncodedVideoDecodePlan(makeBytes(64), samples, 0);
    expect(result).toEqual({ ok: false, reason: "description_unavailable" });
  });

  it("duration_ms=0 → chunk init 不带 duration", () => {
    vi.stubGlobal("EncodedVideoChunk", FakeEncodedVideoChunk);
    const samples = makeSamples([{ frame: 0, pts: 0, key: true, dur: 0 }]);
    const result = buildEncodedVideoDecodePlan(makeBytes(64), samples, 0);
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(recordedInit(result.plan)[0].duration).toBeUndefined();
  });

  it("coded width/height 非正数 → invalid_sample_range", () => {
    vi.stubGlobal("EncodedVideoChunk", FakeEncodedVideoChunk);
    const samplesW = makeSamples([{ frame: 0, pts: 0, key: true }], { width: 0 });
    expect(buildEncodedVideoDecodePlan(makeBytes(64), samplesW, 0)).toEqual({
      ok: false,
      reason: "invalid_sample_range",
    });
    const samplesH = makeSamples([{ frame: 0, pts: 0, key: true }], { height: -1 });
    expect(buildEncodedVideoDecodePlan(makeBytes(64), samplesH, 0)).toEqual({
      ok: false,
      reason: "invalid_sample_range",
    });
  });
});

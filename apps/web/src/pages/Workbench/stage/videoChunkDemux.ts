import type { VideoChunkSamplesResponse } from "@/types";

export type PreciseFrameFallbackReason =
  | "flag_disabled"
  | "api_unavailable"
  | "dataset_item_missing"
  | "chunk_pending"
  | "chunk_failed"
  | "samples_unavailable"
  | "description_unavailable"
  | "codec_unsupported"
  | "invalid_sample_range"
  | "chunk_fetch_failed"
  | "decode_failed"
  | "stale_request"
  | "memory_budget_exceeded";

export interface EncodedVideoDecodePlan {
  config: VideoDecoderConfig;
  chunks: EncodedVideoChunk[];
  targetFrameIndex: number;
  targetTimestampUs: number;
  chunkId: number;
  gopStartDecodeIndex: number;
  targetDecodeIndex: number;
}

export type BuildEncodedVideoDecodePlanResult =
  | { ok: true; plan: EncodedVideoDecodePlan }
  | { ok: false; reason: PreciseFrameFallbackReason };

function decodeBase64ToBytes(b64: string): Uint8Array | null {
  try {
    const binary = atob(b64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return bytes;
  } catch {
    return null;
  }
}

export function buildEncodedVideoDecodePlan(
  chunkBytes: ArrayBuffer,
  samples: VideoChunkSamplesResponse,
  targetFrameIndex: number,
): BuildEncodedVideoDecodePlanResult {
  if (
    !samples.codec_string ||
    !Number.isFinite(samples.width) ||
    samples.width <= 0 ||
    !Number.isFinite(samples.height) ||
    samples.height <= 0 ||
    !Array.isArray(samples.samples) ||
    samples.samples.length === 0
  ) {
    return { ok: false, reason: "invalid_sample_range" };
  }

  const targetDecodeIndex = samples.samples.findIndex((s) => s.frame_index === targetFrameIndex);
  if (targetDecodeIndex < 0) {
    return { ok: false, reason: "invalid_sample_range" };
  }

  let gopStartDecodeIndex = -1;
  for (let i = targetDecodeIndex; i >= 0; i--) {
    if (samples.samples[i].is_keyframe) {
      gopStartDecodeIndex = i;
      break;
    }
  }
  if (gopStartDecodeIndex < 0) {
    return { ok: false, reason: "invalid_sample_range" };
  }

  const byteLen = chunkBytes.byteLength;
  for (let i = gopStartDecodeIndex; i <= targetDecodeIndex; i++) {
    const s = samples.samples[i];
    if (
      !Number.isSafeInteger(s.offset_in_chunk) ||
      !Number.isSafeInteger(s.size_bytes) ||
      s.offset_in_chunk < 0 ||
      s.size_bytes <= 0 ||
      s.offset_in_chunk + s.size_bytes > byteLen ||
      !Number.isFinite(s.pts_ms) ||
      !Number.isFinite(s.duration_ms)
    ) {
      return { ok: false, reason: "invalid_sample_range" };
    }
  }

  const descRaw = samples.description;
  if (!descRaw) {
    return { ok: false, reason: "description_unavailable" };
  }
  const description = decodeBase64ToBytes(descRaw);
  if (!description || description.length === 0) {
    return { ok: false, reason: "description_unavailable" };
  }

  const chunks: EncodedVideoChunk[] = [];
  for (let i = gopStartDecodeIndex; i <= targetDecodeIndex; i++) {
    const s = samples.samples[i];
    const view = new Uint8Array(chunkBytes, s.offset_in_chunk, s.size_bytes);
    const init: EncodedVideoChunkInit = {
      type: s.is_keyframe ? "key" : "delta",
      timestamp: Math.round(s.pts_ms * 1000),
      data: view,
    };
    if (s.duration_ms > 0) init.duration = Math.round(s.duration_ms * 1000);
    chunks.push(new EncodedVideoChunk(init));
  }

  const targetSample = samples.samples[targetDecodeIndex];
  const targetTimestampUs = Math.round(targetSample.pts_ms * 1000);

  const config: VideoDecoderConfig = {
    codec: samples.codec_string,
    codedWidth: samples.width,
    codedHeight: samples.height,
    description,
    hardwareAcceleration: "prefer-hardware",
    optimizeForLatency: true,
  };

  return {
    ok: true,
    plan: {
      config,
      chunks,
      targetFrameIndex,
      targetTimestampUs,
      chunkId: samples.chunk_id,
      gopStartDecodeIndex,
      targetDecodeIndex,
    },
  };
}

export function videoDecoderConfigFingerprint(config: VideoDecoderConfig): string {
  let descBytes: number[] = [];
  if (config.description) {
    descBytes = Array.from(config.description as unknown as Uint8Array);
  }
  return `${config.codec ?? ""}|${config.codedWidth ?? 0}x${config.codedHeight ?? 0}|${config.hardwareAcceleration ?? "no-preference"}|${descBytes.join(",")}`;
}

export interface GopSample {
  decodeIndex: number;
  frameIndex: number;
  timestampUs: number;
  chunk: EncodedVideoChunk;
}

export interface VideoGopPlan {
  config: VideoDecoderConfig;
  configFingerprint: string;
  chunkId: number;
  gopStartDecodeIndex: number;
  gopEndDecodeIndex: number;
  samples: GopSample[];
}

export type BuildGopPlanResult =
  | { ok: true; plan: VideoGopPlan }
  | { ok: false; reason: PreciseFrameFallbackReason };

export function buildGopPlan(
  chunkBytes: ArrayBuffer,
  samples: VideoChunkSamplesResponse,
  targetFrameIndex: number,
): BuildGopPlanResult {
  if (
    !samples.codec_string ||
    !Number.isFinite(samples.width) ||
    samples.width <= 0 ||
    !Number.isFinite(samples.height) ||
    samples.height <= 0 ||
    !Array.isArray(samples.samples) ||
    samples.samples.length === 0
  ) {
    return { ok: false, reason: "invalid_sample_range" };
  }

  const targetDecodeIndex = samples.samples.findIndex((s) => s.frame_index === targetFrameIndex);
  if (targetDecodeIndex < 0) {
    return { ok: false, reason: "invalid_sample_range" };
  }

  let gopStartDecodeIndex = -1;
  for (let i = targetDecodeIndex; i >= 0; i--) {
    if (samples.samples[i].is_keyframe) {
      gopStartDecodeIndex = i;
      break;
    }
  }
  if (gopStartDecodeIndex < 0) {
    return { ok: false, reason: "invalid_sample_range" };
  }

  let gopEndDecodeIndex = samples.samples.length;
  for (let i = gopStartDecodeIndex + 1; i < samples.samples.length; i++) {
    if (samples.samples[i].is_keyframe) {
      gopEndDecodeIndex = i;
      break;
    }
  }

  const byteLen = chunkBytes.byteLength;
  for (let i = gopStartDecodeIndex; i < gopEndDecodeIndex; i++) {
    const s = samples.samples[i];
    if (
      !Number.isSafeInteger(s.offset_in_chunk) ||
      !Number.isSafeInteger(s.size_bytes) ||
      s.offset_in_chunk < 0 ||
      s.size_bytes <= 0 ||
      s.offset_in_chunk + s.size_bytes > byteLen ||
      !Number.isFinite(s.pts_ms) ||
      !Number.isFinite(s.duration_ms)
    ) {
      return { ok: false, reason: "invalid_sample_range" };
    }
  }

  const descRaw = samples.description;
  if (!descRaw) {
    return { ok: false, reason: "description_unavailable" };
  }
  const description = decodeBase64ToBytes(descRaw);
  if (!description || description.length === 0) {
    return { ok: false, reason: "description_unavailable" };
  }

  const gopSamples: GopSample[] = [];
  for (let i = gopStartDecodeIndex; i < gopEndDecodeIndex; i++) {
    const s = samples.samples[i];
    const view = new Uint8Array(chunkBytes, s.offset_in_chunk, s.size_bytes);
    const init: EncodedVideoChunkInit = {
      type: s.is_keyframe ? "key" : "delta",
      timestamp: Math.round(s.pts_ms * 1000),
      data: view,
    };
    if (s.duration_ms > 0) init.duration = Math.round(s.duration_ms * 1000);
    gopSamples.push({
      decodeIndex: i,
      frameIndex: s.frame_index,
      timestampUs: Math.round(s.pts_ms * 1000),
      chunk: new EncodedVideoChunk(init),
    });
  }

  const config: VideoDecoderConfig = {
    codec: samples.codec_string,
    codedWidth: samples.width,
    codedHeight: samples.height,
    description,
    hardwareAcceleration: "prefer-hardware",
    optimizeForLatency: true,
  };

  return {
    ok: true,
    plan: {
      config,
      configFingerprint: videoDecoderConfigFingerprint(config),
      chunkId: samples.chunk_id,
      gopStartDecodeIndex,
      gopEndDecodeIndex,
      samples: gopSamples,
    },
  };
}

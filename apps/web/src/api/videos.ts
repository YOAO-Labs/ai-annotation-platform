import { apiClient } from "./client";
import type { VideoChunkOut, VideoChunkSamplesResponse } from "@/types";

/**
 * 视频 chunk / sample manifest 相关 API(WebCodecs 精确帧链路)。
 *
 * 单 chunk + samples:精确帧 pipeline 按目标帧定位所在 chunk,拉字节 + sample manifest 后
 * 构造 EncodedVideoDecodePlan 解码。不再用无范围 `GET /chunks` 拉整段视频 chunk 列表
 * (会为长视频创建 / 调度全部 chunk)。
 */
export const videoApi = {
  /** 单 chunk 状态 + signed URL;pending 时后端返回 202 + Retry-After。 */
  getChunk: (datasetItemId: string, chunkId: number, init?: RequestInit) =>
    apiClient.silentGet<VideoChunkOut>(`/videos/${datasetItemId}/chunks/${chunkId}`, init),
  /** chunk 的 packet sample manifest(decode order + pts/duration/offset/size + codec description)。 */
  getChunkSamples: (
    datasetItemId: string,
    chunkId: number,
    init?: RequestInit,
    expectedOutputId?: string,
  ) =>
    apiClient.silentGet<VideoChunkSamplesResponse>(
      `/videos/${datasetItemId}/chunks/${chunkId}/samples${expectedOutputId ? `?expected_output_id=${encodeURIComponent(expectedOutputId)}` : ""}`,
      init,
    ),
};

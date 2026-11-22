import { apiClient } from "./client";
import type { VideoChunkOut, VideoChunkSamplesResponse } from "@/types";

export const videoApi = {
  getChunk: (datasetItemId: string, chunkId: number, init?: RequestInit) =>
    apiClient.silentGet<VideoChunkOut>(`/videos/${datasetItemId}/chunks/${chunkId}`, init),
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

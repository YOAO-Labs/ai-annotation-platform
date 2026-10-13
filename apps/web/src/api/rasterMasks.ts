import type { CocoRleMaskRef } from "@/types";
import {
  prepareCocoRleGzipUpload,
  type CocoRle,
} from "@/pages/Workbench/stage/shared/geometry/maskRle";
import { apiClient } from "./client";

export const rasterMasksApi = {
  /** 获取图片 Raster Mask 或视频单帧 Mask 的静态内容。 */
  annotationRasterMaskContent: (annotationId: string, init?: RequestInit) =>
    // Revalidate even a still-fresh response cached before the server policy changed.
    // `init` carries cancellation (AbortSignal) for read-only preview surfaces.
    apiClient.get<CocoRle>(`/annotations/${annotationId}/mask-content`, {
      cache: "no-cache",
      ...init,
    }),
  /** 获取视频掩码关键帧内容 (video_track_mask)。 */
  annotationVideoMaskContent: (annotationId: string, frameIndex: number) =>
    apiClient.get<CocoRle>(`/annotations/${annotationId}/mask-content/${frameIndex}`, {
      cache: "no-cache",
    }),
  predictionVideoMaskContent: (
    taskId: string,
    predictionId: string,
    shapeIndex: number,
    frameIndex: number,
  ) =>
    apiClient.get<CocoRle>(
      `/tasks/${taskId}/predictions/${predictionId}/mask-content/${shapeIndex}/${frameIndex}`,
    ),
  uploadTaskContent: async (taskId: string, rle: CocoRle) => {
    const gzip = await prepareCocoRleGzipUpload(rle);
    if (!gzip) {
      return apiClient.post<CocoRleMaskRef>(`/tasks/${taskId}/mask-content`, rle);
    }
    return apiClient.post<CocoRleMaskRef>(`/tasks/${taskId}/mask-content`, undefined, {
      body: gzip.body,
      headers: { "Content-Encoding": "gzip" },
    });
  },
};

import type { CocoRleMaskRef } from "@/types";
import {
  prepareCocoRleGzipUpload,
  type CocoRle,
} from "@/pages/Workbench/stage/shared/geometry/maskRle";
import { apiClient } from "./client";

export const rasterMasksApi = {
  annotationRasterMaskContent: (annotationId: string, init?: RequestInit) =>
    apiClient.get<CocoRle>(`/annotations/${annotationId}/mask-content`, {
      cache: "no-cache",
      ...init,
    }),
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
  predictionRasterMaskContent: (
    taskId: string,
    predictionId: string,
    shapeIndex: number,
    init?: RequestInit,
  ) =>
    apiClient.get<CocoRle>(
      `/tasks/${taskId}/predictions/${predictionId}/mask-content/${shapeIndex}`,
      { cache: "no-cache", ...init },
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

import type { AnnotationResponse, CocoRleMaskRef } from "@/types";
import {
  prepareCocoRleGzipUpload,
  type CocoRle,
} from "@/pages/Workbench/stage/shared/geometry/maskRle";
import { authSessionToken, useAuthStore } from "@/stores/authStore";
import { apiClient, ApiError } from "./client";

const videoMaskReads = new Map<string, { token: string; promise: Promise<CocoRle> }>();

function annotationVideoMaskContent(
  annotation: Pick<AnnotationResponse, "task_id" | "id" | "version">,
  frameIndex: number,
  reference: CocoRleMaskRef,
): Promise<CocoRle> {
  const { user, sessionId } = useAuthStore.getState();
  const session = user?.id && sessionId ? { userId: user.id, sessionId } : null;
  const token = session && authSessionToken(session);
  if (!session || !token) return Promise.reject(new ApiError(401, "登录会话已变更"));

  const key = JSON.stringify([
    session.userId,
    session.sessionId,
    annotation.task_id,
    annotation.id,
    annotation.version,
    frameIndex,
    reference.object_key,
    reference.sha256,
  ]);
  const pending = videoMaskReads.get(key);
  if (pending?.token === token) return pending.promise;

  const assertCurrent = () => {
    if (authSessionToken(session) !== token) throw new DOMException("登录会话已变更", "AbortError");
  };
  const promise = apiClient
    .getForSession<CocoRle>(
      `/annotations/${annotation.id}/mask-content/${frameIndex}`,
      { cache: "no-cache" },
      session,
      `"${reference.sha256}"`,
    )
    .then(
      (rle) => {
        assertCurrent();
        return rle;
      },
      (error: unknown) => {
        assertCurrent();
        throw error;
      },
    )
    .finally(() => {
      if (videoMaskReads.get(key)?.promise === promise) videoMaskReads.delete(key);
    });
  videoMaskReads.set(key, { token, promise });
  return promise;
}

export const rasterMasksApi = {
  annotationRasterMaskContent: (annotationId: string, init?: RequestInit) =>
    apiClient.get<CocoRle>(`/annotations/${annotationId}/mask-content`, {
      cache: "no-cache",
      ...init,
    }),
  annotationVideoMaskContent,
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

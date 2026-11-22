import { apiClient } from "./client";
import type {
  Mention,
  Attachment,
  CommentAnchor,
  CanvasDrawing,
  AnnotationCommentOut,
  AnnotationCommentCreate,
} from "./generated/types.gen";

export type CommentMention = Mention;
export type CommentAttachment = Attachment;
export type AnnotationCommentAnchor = CommentAnchor;
export type CommentCanvasDrawing = CanvasDrawing;
export type AnnotationCommentResponse = AnnotationCommentOut;
export type CreateCommentPayload = AnnotationCommentCreate;

export interface AnnotationCommentListPage {
  items: AnnotationCommentResponse[];
  next_cursor: string | null;
}

export const commentsApi = {
  listByAnnotation: (annotationId: string) =>
    apiClient.get<AnnotationCommentResponse[]>(`/annotations/${annotationId}/comments`),

  listByAnnotationKeyset: (
    annotationId: string,
    params?: { limit?: number; cursor?: string },
    signal?: AbortSignal,
  ) => {
    const search = new URLSearchParams();
    if (params?.limit) search.set("limit", String(params.limit));
    if (params?.cursor) search.set("cursor", params.cursor);
    const qs = search.toString();
    return apiClient.get<AnnotationCommentListPage>(
      `/annotations/${annotationId}/comments/page${qs ? `?${qs}` : ""}`,
      signal ? { signal } : undefined,
    );
  },

  listByTaskKeyset: (taskId: string, params?: { limit?: number; cursor?: string }) => {
    const search = new URLSearchParams();
    if (params?.limit) search.set("limit", String(params.limit));
    if (params?.cursor) search.set("cursor", params.cursor);
    const qs = search.toString();
    return apiClient.get<AnnotationCommentListPage>(
      `/tasks/${taskId}/comments/page${qs ? `?${qs}` : ""}`,
    );
  },

  create: (annotationId: string, payload: CreateCommentPayload) =>
    apiClient.post<AnnotationCommentResponse>(`/annotations/${annotationId}/comments`, payload),

  patch: (id: string, payload: { body?: string; is_resolved?: boolean }) =>
    apiClient.patch<AnnotationCommentResponse>(`/comments/${id}`, payload),

  remove: (id: string) => apiClient.delete<void>(`/comments/${id}`),

  attachmentUpload: (annotationId: string, file: File, signal?: AbortSignal) =>
    apiClient.silentPost<CommentAttachment>(
      `/annotations/${annotationId}/comment-attachments/upload?file_name=${encodeURIComponent(file.name)}`,
      undefined,
      { body: file, headers: { "Content-Type": file.type || "application/octet-stream" }, signal },
    ),

  attachmentDownload: (annotationId: string, storageKey: string, signal?: AbortSignal) =>
    apiClient.silentGetBlob(
      `/annotations/${annotationId}/comment-attachments/download?key=${encodeURIComponent(storageKey)}`,
      { signal },
    ),
};

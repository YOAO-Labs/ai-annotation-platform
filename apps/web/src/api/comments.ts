import { apiClient } from "./client";
import type {
  Mention,
  Attachment,
  CommentAnchor,
  CanvasDrawing,
  AnnotationCommentOut,
  AnnotationCommentCreate,
} from "./generated/types.gen";

// ── 类型再导出（向后兼容旧 import 名） ─────────────────────────────
//
// v0.6.4 起后端把 mentions / attachments / canvas_drawing 字段从 dict 改成
// 结构化的 Mention / Attachment / CanvasDrawing Pydantic 模型，codegen 直接
// 出强类型。下面把生成的类型按旧名再导出。

export type CommentMention = Mention;
export type CommentAttachment = Attachment;
export type AnnotationCommentAnchor = CommentAnchor;
export type CommentCanvasDrawing = CanvasDrawing;
export type AnnotationCommentResponse = AnnotationCommentOut;
export type CreateCommentPayload = AnnotationCommentCreate;

// v0.8.8 · keyset 分页响应（base64-urlsafe cursor）
export interface AnnotationCommentListPage {
  items: AnnotationCommentResponse[];
  next_cursor: string | null;
}

export const commentsApi = {
  listByAnnotation: (annotationId: string) =>
    apiClient.get<AnnotationCommentResponse[]>(`/annotations/${annotationId}/comments`),

  // v0.8.8 · keyset 分页：单标注 100+ 评论时按需「加载更早」，避免初始化卡顿。
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

  // I4 · 任务级评论 — DiscussionPanel 在未选中标注时降级展示, 聚合 task 下所有 annotation 的评论.
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

  /** The authenticated response is the saved per-file receipt, including empty files. */
  attachmentUpload: (annotationId: string, file: File, signal?: AbortSignal) =>
    apiClient.silentPost<CommentAttachment>(
      `/annotations/${annotationId}/comment-attachments/upload?file_name=${encodeURIComponent(file.name)}`,
      undefined,
      { body: file, headers: { "Content-Type": file.type || "application/octet-stream" }, signal },
    ),

  /** The API authorizes and verifies the complete file before returning its bytes. */
  attachmentDownload: (annotationId: string, storageKey: string, signal?: AbortSignal) =>
    apiClient.silentGetBlob(
      `/annotations/${annotationId}/comment-attachments/download?key=${encodeURIComponent(storageKey)}`,
      { signal },
    ),
};

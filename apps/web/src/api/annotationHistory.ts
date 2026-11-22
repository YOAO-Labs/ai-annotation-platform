import { apiClient } from "./client";
import type { UserBrief } from "@/types";

export interface HistoryEntry {
  kind: "audit" | "comment";
  timestamp: string;
  actor: UserBrief | null;
  action: string | null;
  detail: Record<string, unknown> | null;
  comment_id: string | null;
  body: string | null;
}

export interface AnnotationHistoryResponse {
  annotation_id: string | null;
  task_id: string;
  entries: HistoryEntry[];
}

export const annotationHistoryApi = {
  get: (annotationId: string) =>
    apiClient.get<AnnotationHistoryResponse>(`/annotations/${annotationId}/history`),
  getByTask: (taskId: string) =>
    apiClient.get<AnnotationHistoryResponse>(`/tasks/${taskId}/audit-history`),
};

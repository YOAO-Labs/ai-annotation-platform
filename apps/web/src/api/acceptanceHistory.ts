import { apiClient } from "./client";

export interface AcceptanceHistoryRestoreRequest {
  target: "before" | "after";
  expected_versions: Record<string, number>;
  idempotency_key: string;
  video_segment_id: string | null;
}

export interface AcceptanceHistoryRestoreResponse {
  acceptance_key: string;
  target: "before" | "after";
  result_versions: Record<string, number>;
  active_annotation_ids: string[];
  restore_expires_at: string;
  idempotent_replay: boolean;
}

export const acceptanceHistoryApi = {
  restore: (taskId: string, acceptanceKey: string, payload: AcceptanceHistoryRestoreRequest) =>
    apiClient.silentPost<AcceptanceHistoryRestoreResponse>(
      `/tasks/${taskId}/acceptance-history/${encodeURIComponent(acceptanceKey)}/restore`,
      payload,
    ),
};

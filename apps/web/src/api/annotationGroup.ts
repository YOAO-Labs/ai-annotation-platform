import { apiClient } from "./client";

export interface AnnotationBulkPatch {
  class_name?: string;
  attributes?: Record<string, unknown>;
  z_order?: number;
  is_locked?: boolean;
  is_hidden?: boolean;
}

export interface BulkUpdateRequest {
  ids: string[];
  patch: AnnotationBulkPatch;
}

export interface BulkUpdateResponse {
  updated_ids: string[];
  updated_count: number;
}

export const annotationGroupApi = {
  bulkUpdate: (payload: BulkUpdateRequest) =>
    apiClient.post<BulkUpdateResponse>("/annotations/bulk-update", payload),
};

/**
 * 批量编辑 API client。
 *
 * 标注编组(group / ungroup)端点已下线,仅保留 bulk-update
 * (选中多框一次改 class/属性/状态位)。ids 需属于同一 task(router 层强约束)。
 */
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

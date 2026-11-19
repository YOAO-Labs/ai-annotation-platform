/**
 * 项目 alias 频率聚合 API.
 *
 * 后端 GET /admin/projects/:id/alias-frequency 返回各 detected label 出现次数,
 * 让 AIPreAnnotate chips 按真实预标频率排序, 高频常用类别浮上来.
 */

import { apiClient } from "./client";

export interface AliasFrequencyResponse {
  project_id: string;
  total_predictions: number;
  /** detected label → count, 已按 count desc 截断到 200 条. */
  frequency: Record<string, number>;
  last_computed_at: string;
}

export const aliasFrequencyApi = {
  byProject: (projectId: string) =>
    apiClient.get<AliasFrequencyResponse>(`/admin/projects/${projectId}/alias-frequency`),
};

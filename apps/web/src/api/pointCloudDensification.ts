import { apiClient } from "./client";
import type { AsyncJobStatus } from "./asyncJobs";

export interface PreparationOptions {
  deployment_enabled: boolean;
  producers: Array<{ id: string; name: string; profile: string }>;
  scenes: Array<{ id: string; name: string }>;
}

export interface PreparationRequest {
  scope: "task_ids" | "scene_id";
  task_ids?: string[];
  scene_id?: string;
  retry_job_id?: string;
}

export const pointCloudDensificationApi = {
  options: (projectId: string, signal?: AbortSignal) =>
    apiClient.get<PreparationOptions>(
      `/projects/${projectId}/point-cloud/augmentation/preparation`,
      { signal },
    ),
  create: (projectId: string, request: PreparationRequest) =>
    apiClient.post<{
      job_id: string;
      status: AsyncJobStatus;
      total_tasks: number;
      reused: boolean;
    }>(`/projects/${projectId}/point-cloud/augmentation/jobs`, request),
};

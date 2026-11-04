import { apiClient } from "./client";
import type {
  ProjectPipelineApplyRequest,
  ProjectPipelineCreate,
  ProjectPipelineOut,
  ProjectPipelineUpdate,
} from "./generated/types.gen";
import type { PipelineStagePayload } from "@/hooks/usePreannotation";

export type ProjectPipelineScope = ProjectPipelineOut["scope"];

export type ProjectPipeline = Omit<ProjectPipelineOut, "stages"> & {
  stages: PipelineStagePayload[];
};

export interface ProjectPipelineListParams {
  scope?: ProjectPipelineScope;
  project_id?: string;
  organization_id?: string;
}

export type ProjectPipelineCreatePayload = Omit<ProjectPipelineCreate, "stages"> & {
  stages: PipelineStagePayload[];
};

export type ProjectPipelineUpdatePayload = Omit<ProjectPipelineUpdate, "stages"> & {
  stages?: PipelineStagePayload[];
};

function normalizePipeline(pipeline: ProjectPipelineOut): ProjectPipeline {
  return {
    ...pipeline,
    stages: pipeline.stages as unknown as PipelineStagePayload[],
  };
}

function buildQuery(params?: ProjectPipelineListParams): string {
  const q = new URLSearchParams();
  if (params?.scope) q.set("scope", params.scope);
  if (params?.project_id) q.set("project_id", params.project_id);
  if (params?.organization_id) q.set("organization_id", params.organization_id);
  const qs = q.toString();
  return qs ? `?${qs}` : "";
}

export const projectPipelinesApi = {
  list: async (params?: ProjectPipelineListParams) => {
    const rows = await apiClient.get<ProjectPipelineOut[]>(
      `/project-pipelines${buildQuery(params)}`,
    );
    return rows.map(normalizePipeline);
  },

  create: async (payload: ProjectPipelineCreatePayload) =>
    normalizePipeline(
      await apiClient.post<ProjectPipelineOut>("/project-pipelines", {
        ...payload,
        stages: payload.stages as unknown as ProjectPipelineCreate["stages"],
      }),
    ),

  update: async (id: string, payload: ProjectPipelineUpdatePayload) =>
    normalizePipeline(
      await apiClient.put<ProjectPipelineOut>(`/project-pipelines/${id}`, {
        ...payload,
        stages: payload.stages as unknown as ProjectPipelineUpdate["stages"],
      }),
    ),

  remove: (id: string) => apiClient.delete<void>(`/project-pipelines/${id}`),

  apply: async (projectId: string, payload: ProjectPipelineApplyRequest) =>
    normalizePipeline(
      await apiClient.post<ProjectPipelineOut>(`/projects/${projectId}/pipelines/apply`, payload),
    ),
};

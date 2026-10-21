import { apiClient } from "./client";
import type {
  CocoRleContent,
  ModelDeploymentOut,
  ModelReleaseCreate,
  ModelReleaseOut,
  TrainedModelCreate,
  TrainedModelOut,
} from "./generated/types.gen";

export type {
  ModelDeploymentOut,
  ModelReleaseCreate,
  ModelReleaseOut,
  TrainedModelCreate,
  TrainedModelOut,
};
export type ModelTask = "detect" | "segment";

export type TrainedModelListFilters = {
  task?: ModelTask;
  q?: string;
  framework?: string;
  created_from?: string;
  created_to?: string;
};

const base = (projectId: string) => `/projects/${projectId}`;
export const trainedModelsApi = {
  list: (
    projectId: string,
    offset = 0,
    options?: RequestInit,
    archive: "exclude" | "include" | "only" = "exclude",
    filters?: TrainedModelListFilters,
    limit = 20,
  ) => {
    const query = new URLSearchParams({ limit: String(limit), offset: String(offset), archive });
    for (const key of ["q", "framework", "task", "created_from", "created_to"] as const) {
      if (filters?.[key]) query.set(key, filters[key]);
    }
    return apiClient.get<{ items: TrainedModelOut[]; total: number }>(
      `${base(projectId)}/trained-models?${query}`,
      options,
    );
  },
  get: (projectId: string, versionId: string, options?: RequestInit) =>
    apiClient.get<{ model: TrainedModelOut; deployments: ModelDeploymentOut[] }>(
      `${base(projectId)}/trained-models/${versionId}`,
      options,
    ),
  register: (projectId: string, body: TrainedModelCreate) =>
    apiClient.post<TrainedModelOut>(`${base(projectId)}/trained-models`, body),
  deploy: (projectId: string, versionId: string, registryId: string) =>
    apiClient.post<ModelDeploymentOut>(`${base(projectId)}/trained-models/${versionId}/deploy`, {
      registry_id: registryId,
    }),
  download: (projectId: string, versionId: string) =>
    apiClient.get<{ url: string; sha256: string; size_bytes: number }>(
      `${base(projectId)}/trained-models/${versionId}/download`,
    ),
  trialMaskContent: (
    projectId: string,
    versionId: string,
    deploymentId: string,
    shapeIndex: number,
    options?: RequestInit,
  ) =>
    apiClient.get<CocoRleContent>(
      `${base(projectId)}/trained-models/${versionId}/deployments/${deploymentId}/mask-content/${shapeIndex}`,
      options,
    ),
  releases: (projectId: string, offset = 0, task: ModelTask = "detect", limit = 20) =>
    apiClient.get<{
      items: ModelReleaseOut[];
      total: number;
      generation: number;
      current_release_id: string | null;
      current: ModelReleaseOut | null;
    }>(`${base(projectId)}/model-releases?limit=${limit}&offset=${offset}&task=${task}`),
  publish: (projectId: string, body: ModelReleaseCreate) =>
    apiClient.post<ModelReleaseOut>(`${base(projectId)}/model-releases`, body),
};

export function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

export function modelMetrics(model: TrainedModelOut): Record<string, number> {
  const summary = record(model.evaluation.summary);
  const values = Object.keys(summary).length ? summary : model.evaluation;
  return Object.fromEntries(
    Object.entries(values).filter(
      (pair): pair is [string, number] => typeof pair[1] === "number" && Number.isFinite(pair[1]),
    ),
  );
}

export function comparisonIdentity(model: TrainedModelOut): string | null {
  if (model.framework === "libreyolo") {
    const digest = model.evaluation.comparison_compatibility_digest;
    return model.evaluation.comparison_identity_status === "verified" &&
      typeof digest === "string" &&
      digest.length === 64
      ? JSON.stringify([
          model.task ?? "detect",
          digest,
          model.framework,
          model.framework_version,
          model.binding_digest,
        ])
      : null;
  }
  const config = record(model.evaluation.evaluation);
  const validation = model.provenance.validation_digest;
  if (
    !Object.keys(config).length ||
    !validation ||
    model.evaluation.validation_digest !== validation
  )
    return null;
  const observed = Array.isArray(model.evaluation.per_class)
    ? model.evaluation.per_class.map((item) => record(item).index).sort()
    : [];
  return JSON.stringify([
    model.task ?? "detect",
    validation,
    model.framework,
    model.framework_version,
    model.binding_digest,
    model.classes,
    Object.entries(config).sort(([a], [b]) => a.localeCompare(b)),
    observed,
  ]);
}

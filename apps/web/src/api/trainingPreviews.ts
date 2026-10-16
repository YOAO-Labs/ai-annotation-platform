import { apiClient } from "./client";
import type {
  PreviewCapabilities,
  TrainingPreviewCreate,
  TrainingPreviewOut,
} from "./generated/types.gen";

export type {
  PreviewBox,
  PreviewOutput,
  PreviewSource,
  TrainingPreviewCreate,
  TrainingPreviewOut,
  TrainingPreviewReport,
} from "./generated/types.gen";

const base = (projectId: string) => `/projects/${projectId}/training-previews`;
export const trainingPreviewsApi = {
  capabilities: (projectId: string, options?: RequestInit) =>
    apiClient.silentGet<PreviewCapabilities>(`${base(projectId)}/capabilities`, options),
  create: (projectId: string, body: TrainingPreviewCreate, options?: RequestInit) =>
    apiClient.post<TrainingPreviewOut>(base(projectId), body, options),
  get: (projectId: string, id: string, options?: RequestInit) =>
    apiClient.get<TrainingPreviewOut>(`${base(projectId)}/${id}`, options),
  cancel: (projectId: string, id: string, options?: RequestInit) =>
    apiClient.post<TrainingPreviewOut>(`${base(projectId)}/${id}/cancel`, undefined, options),
  image: (projectId: string, id: string, index: number, options?: RequestInit) =>
    apiClient.getBlob(`${base(projectId)}/${id}/images/${index}`, options),
};

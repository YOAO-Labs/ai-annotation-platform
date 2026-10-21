import { apiClient } from "./client";
import type {
  TrainingHubCatalog,
  TrainingProjectPage,
  TrainingQueuePage,
} from "./generated/types.gen";

export type {
  TrainingHubCatalog,
  TrainingProjectSummary,
  TrainingQueuePage,
} from "./generated/types.gen";
export type QueueState = "active" | "finished" | "failed" | "all";

export const trainingHubApi = {
  catalog: () => apiClient.get<TrainingHubCatalog>("/training/catalog"),
  projects: (search = "", offset = 0, limit = 20) =>
    apiClient.get<TrainingProjectPage>(
      `/training/projects?limit=${limit}&offset=${offset}&search=${encodeURIComponent(search)}`,
    ),
  runs: (state: QueueState, offset = 0, framework = "", limit = 20) =>
    apiClient.get<TrainingQueuePage>(
      `/training/runs?state=${state}&limit=${limit}&offset=${offset}${framework ? `&framework=${encodeURIComponent(framework)}` : ""}`,
    ),
};

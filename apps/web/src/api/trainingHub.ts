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
  projects: (search = "", offset = 0) =>
    apiClient.get<TrainingProjectPage>(
      `/training/projects?limit=20&offset=${offset}&search=${encodeURIComponent(search)}`,
    ),
  runs: (state: QueueState, offset = 0, framework = "") =>
    apiClient.get<TrainingQueuePage>(
      `/training/runs?state=${state}&limit=20&offset=${offset}${framework ? `&framework=${encodeURIComponent(framework)}` : ""}`,
    ),
};

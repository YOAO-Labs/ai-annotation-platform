import { apiClient } from "./client";
import type { TrajectoryResponse } from "@/api/generated/types.gen";

export const scenesApi = {
  getTrajectory: (sceneId: string, init?: RequestInit) =>
    apiClient.get<TrajectoryResponse>(`/scenes/${sceneId}/trajectory`, init),
};

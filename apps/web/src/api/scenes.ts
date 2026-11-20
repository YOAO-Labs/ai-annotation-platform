/**
 * Scenes API client。
 *
 * 目前只消费 trajectory(scene 的有序逐帧 ego pose);scene CRUD 由
 * 数据集管理页按需另加。
 */
import { apiClient } from "./client";
import type { TrajectoryResponse } from "@/api/generated/types.gen";

export const scenesApi = {
  // 无位姿 scene → 200 + poses=[](非 nuScenes 来源/历史数据,调用方按无轨迹降级)。
  getTrajectory: (sceneId: string, init?: RequestInit) =>
    apiClient.get<TrajectoryResponse>(`/scenes/${sceneId}/trajectory`, init),
};

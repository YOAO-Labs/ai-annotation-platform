/**
 * 跨帧目标延续: scene 内前后 k 个邻居 task。
 *
 * 封装 GET /tasks/{id}/neighbors?k=K，不感知几何类型。
 * 3D / 2D / 视频段 workbench 共用同一份。task 无 scene(历史未 backfill / 单帧)时
 * 端点返回 scene_id=null 和空 prev/next，
 * 调用方据此判定"无跨帧能力"。
 */
import { useQuery } from "@tanstack/react-query";
import { tasksApi } from "@/api/tasks";

export function useFrameNeighbors(taskId: string | null | undefined, k = 1) {
  const query = useQuery({
    queryKey: ["frame-neighbors", taskId, k],
    queryFn: ({ signal }) => tasksApi.getNeighbors(taskId!, k, { signal }),
    enabled: !!taskId,
    staleTime: 5 * 60 * 1000,
  });

  return {
    data: query.data ?? null,
    isLoading: query.isLoading,
    error: query.error,
  };
}

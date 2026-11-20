/**
 * 邻帧框叠加: 拉 prev/next task 的邻帧 annotations。
 * 改走批量端点 GET /tasks/{id}/neighbor-annotations(一条请求),
 *   替代旧版「对 2k 个邻帧 task 各发一条 getAnnotations + client 端按 track 过滤」。
 * ADR-0045 · 跨帧链按 track_id(原 group_id)服务端过滤。
 *
 * - scope=selected(trackId 非 null)→ 服务端只回该 track;trackId 为 null 时调用方传
 *   enabled=false 短路(不发请求)。
 * - scope=all(trackId=null 但 enabled=true)→ 服务端回区间全部框。
 */
import { useQuery } from "@tanstack/react-query";
import { tasksApi } from "@/api/tasks";
import type { AnnotationResponse } from "@/types";

export interface NeighborAnnotationsResult {
  /** taskId → 该邻帧 task 的 annotations(已按 scope 在服务端过滤)。 */
  byTask: Record<string, AnnotationResponse[]>;
  isLoading: boolean;
}

export function useNeighborAnnotations(
  taskId: string | null,
  k: number,
  trackId: string | null,
  enabled: boolean,
): NeighborAnnotationsResult {
  const active = enabled && taskId != null && k > 0;
  const query = useQuery({
    queryKey: ["neighbor-annotations", taskId, k, trackId],
    queryFn: ({ signal }) =>
      tasksApi.getNeighborAnnotations(taskId as string, k, trackId, { signal }),
    enabled: active,
    staleTime: 60 * 1000,
  });

  const byTask: Record<string, AnnotationResponse[]> = {};
  for (const f of query.data?.frames ?? []) {
    byTask[f.task_id] = (f.annotations ?? []) as AnnotationResponse[];
  }

  return { byTask, isLoading: active && query.isLoading };
}

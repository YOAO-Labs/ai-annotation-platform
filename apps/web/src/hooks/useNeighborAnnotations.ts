import { useQuery } from "@tanstack/react-query";
import { tasksApi } from "@/api/tasks";
import type { AnnotationResponse } from "@/types";

export interface NeighborAnnotationsResult {
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

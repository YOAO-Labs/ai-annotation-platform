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

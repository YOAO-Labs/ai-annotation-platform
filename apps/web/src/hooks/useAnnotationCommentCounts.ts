import { useQuery } from "@tanstack/react-query";
import { discussionApi } from "@/api/discussion";
import { discussionKeys } from "@/pages/Workbench/state/discussionTypes";
import { useAuthStore } from "@/stores/authStore";

export function annotationCommentCountsQueryKey(
  taskId: string,
  projectId: string | null | undefined,
  userId: string | null | undefined,
) {
  return [
    ...discussionKeys.task(taskId),
    "annotation-counts",
    projectId ?? null,
    userId ?? null,
  ] as const;
}

export function useAnnotationCommentCounts(
  taskId: string | null | undefined,
  projectId: string | null | undefined,
  enabled = true,
) {
  const userId = useAuthStore((state) => state.user?.id ?? null);
  return useQuery({
    queryKey: annotationCommentCountsQueryKey(taskId ?? "", projectId, userId),
    queryFn: ({ signal }) => discussionApi.getAnnotationCommentCounts(taskId!, signal),
    enabled: enabled && !!taskId,
    staleTime: 30 * 1000,
  });
}

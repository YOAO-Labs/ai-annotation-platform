import { useInfiniteQuery, type InfiniteData } from "@tanstack/react-query";
import {
  discussionApi,
  discussionItemKey,
  type TaskDiscussionItem,
  type TaskDiscussionPage,
} from "@/api/discussion";
import { discussionKeys, type DiscussionReadScope } from "@/pages/Workbench/state/discussionTypes";
import { useAuthStore } from "@/stores/authStore";

export const TASK_DISCUSSION_PAGE_LIMIT = 50;

export function useTaskDiscussion(
  taskId: string | null | undefined,
  scope: DiscussionReadScope = "all",
  annotationId: string | null | undefined = null,
  enabled = true,
  projectId: string | null | undefined = null,
) {
  const effectiveAnnotationId = scope === "annotation" ? annotationId : null;
  const userId = useAuthStore((state) => state.user?.id ?? null);
  return useInfiniteQuery({
    queryKey: taskDiscussionQueryKey(taskId ?? "", scope, effectiveAnnotationId, projectId, userId),
    queryFn: ({ pageParam, signal }) =>
      discussionApi.listTaskDiscussion(
        taskId!,
        {
          scope,
          annotation_id: effectiveAnnotationId,
          limit: TASK_DISCUSSION_PAGE_LIMIT,
          cursor: pageParam ?? undefined,
        },
        signal,
      ),
    enabled: enabled && !!taskId,
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    staleTime: 30 * 1000,
  });
}

export function taskDiscussionQueryKey(
  taskId: string,
  scope: DiscussionReadScope = "all",
  annotationId: string | null | undefined = null,
  projectId: string | null | undefined = null,
  userId: string | null | undefined = null,
) {
  return [
    ...discussionKeys.feed(
      taskId,
      scope,
      scope === "annotation" ? annotationId : null,
      TASK_DISCUSSION_PAGE_LIMIT,
    ),
    projectId ?? null,
    userId ?? null,
  ] as const;
}

export function flattenTaskDiscussion(
  data: InfiniteData<TaskDiscussionPage> | undefined,
): TaskDiscussionItem[] {
  const seen = new Set<string>();
  const items: TaskDiscussionItem[] = [];
  for (const page of data?.pages ?? []) {
    for (const item of page.items) {
      const key = discussionItemKey(item);
      if (seen.has(key)) continue;
      seen.add(key);
      items.push(item);
    }
  }
  return items;
}

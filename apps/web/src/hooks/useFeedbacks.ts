import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  feedbacksApi,
  type AnnotationFeedback,
  type CreateFeedbackPayload,
  type ListFeedbacksParams,
  type PatchFeedbackPayload,
} from "@/api/feedbacks";
import { discussionKeys } from "@/pages/Workbench/state/discussionTypes";
import { useAuthStore } from "@/stores/authStore";

export function feedbacksQueryKey(params: ListFeedbacksParams, ownerId?: string | null) {
  return [
    ...discussionKeys.feedbacks(params.project_id),
    params.task_id ?? null,
    params.annotation_id ?? null,
    params.kind ?? null,
    params.anchor_type ?? null,
    params.status ?? null,
    params.root_only ?? null,
    params.include_counts ?? null,
    params.limit ?? null,
    params.cursor ?? null,
    ownerId ?? null,
  ] as const;
}

function invalidateFeedbackFamily(
  queryClient: ReturnType<typeof useQueryClient>,
  params: ListFeedbacksParams,
) {
  queryClient.invalidateQueries({ queryKey: discussionKeys.feedbacks(params.project_id) });
  if (params.task_id) {
    queryClient.invalidateQueries({ queryKey: discussionKeys.task(params.task_id) });
  }
  queryClient.invalidateQueries({ queryKey: ["feedback-thread"] });
}

function feedbackScope(data: unknown, fallback: ListFeedbacksParams): ListFeedbacksParams | null {
  if (!data || typeof data !== "object") return null;
  const source = data as {
    project_id?: unknown;
    task_id?: unknown;
    annotation_id?: unknown;
  };
  if (typeof source.project_id !== "string" || source.project_id.length === 0) return null;
  return {
    ...fallback,
    project_id: source.project_id,
    task_id: typeof source.task_id === "string" ? source.task_id : undefined,
    annotation_id: typeof source.annotation_id === "string" ? source.annotation_id : undefined,
  };
}

function invalidatePublicDiscussionFamily(queryClient: ReturnType<typeof useQueryClient>) {
  queryClient.invalidateQueries({ queryKey: ["feedbacks"] });
  queryClient.invalidateQueries({ queryKey: ["task-discussion"] });
  queryClient.invalidateQueries({ queryKey: ["feedback-thread"] });
}

export function useFeedbacks(params: ListFeedbacksParams, enabled = true) {
  const ownerId = useAuthStore((state) => state.user?.id ?? null);
  return useQuery({
    queryKey: feedbacksQueryKey(params, ownerId),
    queryFn: ({ signal }) => feedbacksApi.list(params, signal),
    enabled: enabled && !!params.project_id,
    staleTime: 30 * 1000,
  });
}

export function useInfiniteFeedbacks(params: ListFeedbacksParams, enabled = true) {
  const ownerId = useAuthStore((state) => state.user?.id ?? null);
  const queryKey = feedbacksQueryKey({ ...params, cursor: undefined }, ownerId);
  return useInfiniteQuery({
    queryKey: [...queryKey, "infinite"],
    queryFn: ({ pageParam, signal }) =>
      feedbacksApi.list({ ...params, cursor: pageParam ?? undefined }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled: enabled && !!params.project_id,
    staleTime: 30 * 1000,
  });
}

export function useCreateFeedback(invalidateParams: ListFeedbacksParams) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: CreateFeedbackPayload) => feedbacksApi.create(payload),
    onSuccess: (data) =>
      invalidateFeedbackFamily(qc, feedbackScope(data, invalidateParams) ?? invalidateParams),
  });
}

export function usePatchFeedback(invalidateParams: ListFeedbacksParams) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: PatchFeedbackPayload }) =>
      feedbacksApi.patch(id, payload),
    onSuccess: (data) =>
      invalidateFeedbackFamily(qc, feedbackScope(data, invalidateParams) ?? invalidateParams),
  });
}

export function useDeleteFeedback(_invalidateParams: ListFeedbacksParams) {
  const qc = useQueryClient();
  type DeleteInput = string | { id: string; scope?: ListFeedbacksParams };
  return useMutation({
    mutationFn: (input: DeleteInput) =>
      feedbacksApi.remove(typeof input === "string" ? input : input.id),
    onSuccess: (_data, input) => {
      const scope = typeof input === "string" ? undefined : input.scope;
      if (scope) invalidateFeedbackFamily(qc, scope);
      else invalidatePublicDiscussionFamily(qc);
    },
  });
}

export function useReplyFeedback(invalidateParams: ListFeedbacksParams) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      body,
      attachments,
    }: {
      id: string;
      body: string;
      attachments?: Array<Record<string, unknown>>;
    }) => feedbacksApi.reply(id, { body, attachments }),
    onSuccess: (data, variables) => {
      invalidateFeedbackFamily(qc, feedbackScope(data, invalidateParams) ?? invalidateParams);
      qc.invalidateQueries({ queryKey: discussionKeys.thread(variables.id) });
    },
  });
}

export type { AnnotationFeedback };

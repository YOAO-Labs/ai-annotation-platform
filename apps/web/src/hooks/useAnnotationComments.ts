import {
  type InfiniteData,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useToastStore } from "@/components/ui/Toast";
import {
  commentsApi,
  type AnnotationCommentListPage,
  type AnnotationCommentResponse,
  type CreateCommentPayload,
} from "@/api/comments";
import { discussionKeys } from "@/pages/Workbench/state/discussionTypes";

function invalidateDiscussionFeed(qc: ReturnType<typeof useQueryClient>, taskId?: string | null) {
  qc.invalidateQueries({
    queryKey: taskId ? discussionKeys.task(taskId) : ["task-discussion"],
  });
}

export function useAnnotationComments(annotationId: string | null | undefined) {
  return useQuery({
    queryKey: ["annotation-comments", annotationId],
    queryFn: () => commentsApi.listByAnnotation(annotationId!),
    enabled: !!annotationId,
  });
}

const COMMENTS_PAGE_LIMIT = 50;

export interface ScopedCreateCommentInput {
  annotationId: string;
  taskId?: string | null;
  payload: string | CreateCommentPayload;
}

type CreateCommentInput = string | CreateCommentPayload | ScopedCreateCommentInput;

function isScopedCreateCommentInput(value: CreateCommentInput): value is ScopedCreateCommentInput {
  return (
    typeof value === "object" && value !== null && "annotationId" in value && "payload" in value
  );
}

export function useAnnotationCommentsInfinite(annotationId: string | null | undefined) {
  return useInfiniteQuery({
    queryKey: ["annotation-comments-page", annotationId],
    queryFn: ({ pageParam }) =>
      commentsApi.listByAnnotationKeyset(annotationId!, {
        limit: COMMENTS_PAGE_LIMIT,
        cursor: pageParam ?? undefined,
      }),
    enabled: !!annotationId,
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
  });
}

export function useTaskCommentsInfinite(taskId: string | null | undefined, enabled = true) {
  return useInfiniteQuery({
    queryKey: ["task-comments-page", taskId],
    queryFn: ({ pageParam }) =>
      commentsApi.listByTaskKeyset(taskId!, {
        limit: COMMENTS_PAGE_LIMIT,
        cursor: pageParam ?? undefined,
      }),
    enabled: enabled && !!taskId,
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
  });
}

export function useCreateComment(annotationId: string | null | undefined, taskId?: string | null) {
  const qc = useQueryClient();
  const pushToast = useToastStore((s) => s.push);
  return useMutation({
    mutationFn: (input: CreateCommentInput) => {
      const scoped = isScopedCreateCommentInput(input) ? input : undefined;
      const targetAnnotationId = scoped?.annotationId ?? annotationId;
      if (!targetAnnotationId) throw new Error("No annotation selected");
      const payload = scoped ? scoped.payload : (input as string | CreateCommentPayload);
      const body = typeof payload === "string" ? { body: payload } : payload;
      return commentsApi.create(targetAnnotationId, body);
    },
    onError: (e) => pushToast({ msg: "评论发送失败", sub: String(e), kind: "error" }),
    onSuccess: (data, input) => {
      const scoped = isScopedCreateCommentInput(input) ? input : undefined;
      const targetAnnotationId = scoped?.annotationId ?? data.annotation_id ?? annotationId;
      const targetTaskId = scoped?.taskId ?? taskId;
      qc.invalidateQueries({ queryKey: ["annotation-comments", targetAnnotationId] });
      qc.invalidateQueries({ queryKey: ["annotation-comments-page", targetAnnotationId] });
      qc.invalidateQueries({ queryKey: ["task-comments-page"] });
      invalidateDiscussionFeed(qc, targetTaskId);
      qc.invalidateQueries({ queryKey: ["notifications"] });
    },
  });
}

export interface ScopedPatchCommentInput {
  id: string;
  payload: { body?: string; is_resolved?: boolean };
  annotationId?: string | null;
  taskId?: string | null;
}

export function usePatchComment(annotationId: string | null | undefined, taskId?: string | null) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, payload }: ScopedPatchCommentInput) => commentsApi.patch(id, payload),
    onSuccess: (_data, input) => {
      const targetAnnotationId =
        input.annotationId === undefined ? annotationId : input.annotationId;
      const targetTaskId = input.taskId === undefined ? taskId : input.taskId;
      qc.invalidateQueries({ queryKey: ["annotation-comments", targetAnnotationId] });
      qc.invalidateQueries({ queryKey: ["annotation-comments-page", targetAnnotationId] });
      qc.invalidateQueries({ queryKey: ["task-comments-page"] });
      invalidateDiscussionFeed(qc, targetTaskId);
    },
  });
}

const removeFromPages =
  (id: string) => (old: InfiniteData<AnnotationCommentListPage> | undefined) =>
    old
      ? {
          ...old,
          pages: old.pages.map((p) => ({ ...p, items: p.items.filter((c) => c.id !== id) })),
        }
      : old;

export interface ScopedDeleteCommentInput {
  id: string;
  annotationId?: string | null;
  taskId?: string | null;
}

type DeleteCommentInput = string | ScopedDeleteCommentInput;

function normalizeDeleteCommentInput(
  input: DeleteCommentInput,
  fallbackAnnotationId: string | null | undefined,
  fallbackTaskId: string | null | undefined,
): { id: string; annotationId: string | null | undefined; taskId: string | null | undefined } {
  if (typeof input === "string") {
    return { id: input, annotationId: fallbackAnnotationId, taskId: fallbackTaskId };
  }
  return {
    id: input.id,
    annotationId: input.annotationId === undefined ? fallbackAnnotationId : input.annotationId,
    taskId: input.taskId === undefined ? fallbackTaskId : input.taskId,
  };
}

export function useDeleteComment(annotationId: string | null | undefined, taskId?: string | null) {
  const qc = useQueryClient();
  const taskPagePrefix = ["task-comments-page"];
  return useMutation({
    mutationFn: (input: DeleteCommentInput) => {
      const { id } = normalizeDeleteCommentInput(input, annotationId, taskId);
      return commentsApi.remove(id);
    },
    onMutate: async (input: DeleteCommentInput) => {
      const scoped = normalizeDeleteCommentInput(input, annotationId, taskId);
      const pageKey = ["annotation-comments-page", scoped.annotationId];
      const listKey = ["annotation-comments", scoped.annotationId];
      const taskPageKey = scoped.taskId ? ["task-comments-page", scoped.taskId] : taskPagePrefix;
      await Promise.all([
        qc.cancelQueries({ queryKey: pageKey }),
        qc.cancelQueries({ queryKey: listKey }),
        qc.cancelQueries({ queryKey: taskPageKey }),
      ]);
      const prevPages = qc.getQueryData<InfiniteData<AnnotationCommentListPage>>(pageKey);
      const prevList = qc.getQueryData<AnnotationCommentResponse[]>(listKey);
      const prevTaskPages = qc.getQueriesData<InfiniteData<AnnotationCommentListPage>>({
        queryKey: taskPageKey,
      });
      qc.setQueryData<InfiniteData<AnnotationCommentListPage>>(pageKey, removeFromPages(scoped.id));
      qc.setQueryData<AnnotationCommentResponse[]>(listKey, (old) =>
        old ? old.filter((c) => c.id !== scoped.id) : old,
      );
      qc.setQueriesData<InfiniteData<AnnotationCommentListPage>>(
        { queryKey: taskPageKey },
        removeFromPages(scoped.id),
      );
      return { ...scoped, pageKey, listKey, taskPageKey, prevPages, prevList, prevTaskPages };
    },
    onError: (_e, _input, ctx) => {
      if (ctx?.prevPages !== undefined) qc.setQueryData(ctx.pageKey, ctx.prevPages);
      if (ctx?.prevList !== undefined) qc.setQueryData(ctx.listKey, ctx.prevList);
      ctx?.prevTaskPages?.forEach(([key, data]) => qc.setQueryData(key, data));
    },
    onSettled: (_data, _error, input, ctx) => {
      const scoped = ctx ?? normalizeDeleteCommentInput(input, annotationId, taskId);
      const pageKey = ctx?.pageKey ?? ["annotation-comments-page", scoped.annotationId];
      const listKey = ctx?.listKey ?? ["annotation-comments", scoped.annotationId];
      const taskPageKey =
        ctx?.taskPageKey ??
        (scoped.taskId ? ["task-comments-page", scoped.taskId] : taskPagePrefix);
      qc.invalidateQueries({ queryKey: listKey });
      qc.invalidateQueries({ queryKey: pageKey });
      qc.invalidateQueries({ queryKey: taskPageKey });
      invalidateDiscussionFeed(qc, scoped.taskId);
    },
  });
}

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
  // A task id lets React Query invalidate only the affected discussion family.
  // Legacy callers do not have one, so invalidate the C0 family prefix rather
  // than trying to merge a second browser-side feed.
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

// keyset 分页 + 「加载更早评论」按钮。CommentsPanel 切换到这条 hook
// 后单标注 100+ 评论不再初始化卡顿；老 hook（`useAnnotationComments`）保留作
// 简单场景兜底（list_attachments 等）。
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

/**
 * I4 · 任务级评论 — DiscussionPanel 未选中标注时降级展示.
 */
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
      // 任务级聚合视图（未选中标注）下 annotationId 为 null，上面的 key 命中不到
      // ["task-comments-page", taskId]；按前缀失效任务级缓存，保证汇总列表即时刷新。
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
  // 任务级聚合视图（未选中标注）缓存按 taskId 分键，删除时不知道 taskId，
  // 故按前缀对所有 ["task-comments-page", *] 缓存乐观剔除。
  const taskPagePrefix = ["task-comments-page"];
  return useMutation({
    mutationFn: (input: DeleteCommentInput) => {
      const { id } = normalizeDeleteCommentInput(input, annotationId, taskId);
      return commentsApi.remove(id);
    },
    // 乐观移除：直接从缓存剔除被删项。后端是软删 (is_active=false) + 列表过滤，
    // 仅靠 invalidate+refetch 在快速切换标注时会偶现"删除后重现"（stale 缓存竞态）。
    // 直接改缓存条目可彻底避免；失败则在 onError 回滚（不掩盖失败的删除）。
    onMutate: async (input: DeleteCommentInput) => {
      const scoped = normalizeDeleteCommentInput(input, annotationId, taskId);
      const pageKey = ["annotation-comments-page", scoped.annotationId];
      const listKey = ["annotation-comments", scoped.annotationId];
      // A scoped mutation knows its task, so never snapshot or roll back every
      // task aggregate. The broad prefix remains for legacy callers that have
      // no task id at all.
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

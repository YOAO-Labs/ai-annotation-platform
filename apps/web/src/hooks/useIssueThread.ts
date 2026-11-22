import {
  type InfiniteData,
  useInfiniteQuery,
  type UseInfiniteQueryResult,
} from "@tanstack/react-query";
import { ApiError } from "@/api/client";
import {
  feedbacksApi,
  type AnnotationFeedback,
  type AnnotationFeedbackThreadPage,
} from "@/api/feedbacks";
import { discussionKeys } from "@/pages/Workbench/state/discussionTypes";
import { useDiscussionDraftStore } from "@/pages/Workbench/state/DiscussionDraftProvider";
import { useAuthStore } from "@/stores/authStore";

export type IssueThreadState =
  | "idle"
  | "loading"
  | "ready"
  | "permission-denied"
  | "unavailable"
  | "error";

export interface IssueThreadParentContext {
  id: string;
  body: string;
  authorName: string | null;
  available: boolean;
}

export type IssueThreadReply = AnnotationFeedback & {
  parentContext: IssueThreadParentContext | null;
};

export interface UseIssueThreadParams {
  rootId: string | null | undefined;
  projectId: string | null | undefined;
  taskId: string | null | undefined;
  allowProjectScope?: boolean;
  rootSnapshot?: AnnotationFeedback | null;
  enabled?: boolean;
  limit?: number;
}

export interface UseIssueThreadResult {
  root: AnnotationFeedback | null;
  replies: IssueThreadReply[];
  state: IssueThreadState;
  isLoading: boolean;
  isFetching: boolean;
  isFetchingNextPage: boolean;
  hasNextPage: boolean;
  error: unknown;
  paginationError: unknown;
  loadEarlierReplies: () => Promise<unknown>;
  retryEarlierReplies: () => Promise<unknown>;
  retry: () => Promise<unknown>;
  query: UseInfiniteQueryResult<InfiniteData<AnnotationFeedbackThreadPage, string | null>, Error>;
}

export function isValidIssueRoot(
  value: AnnotationFeedback | null | undefined,
  params: Pick<UseIssueThreadParams, "projectId" | "taskId" | "allowProjectScope"> & {
    rootId?: string | null;
  },
): value is AnnotationFeedback {
  if (!value || value.kind !== "issue" || value.thread_parent_id !== null || !value.is_active)
    return false;
  if (params.rootId && value.id !== params.rootId) return false;
  if (!params.projectId || value.project_id !== params.projectId) return false;
  if (params.allowProjectScope) return true;
  return Boolean(params.taskId && value.task_id === params.taskId);
}

export function flattenIssueThread(
  data: InfiniteData<AnnotationFeedbackThreadPage, string | null> | undefined,
  root: AnnotationFeedback | null,
): IssueThreadReply[] {
  if (!root) return [];
  const seen = new Set<string>();
  const all: AnnotationFeedback[] = [];
  for (const page of data?.pages ?? []) {
    for (const item of page.items) {
      if (
        item.id === root.id ||
        item.thread_parent_id === null ||
        !item.is_active ||
        item.project_id !== root.project_id ||
        item.task_id !== root.task_id ||
        seen.has(item.id)
      )
        continue;
      seen.add(item.id);
      all.push(item);
    }
  }
  all.reverse();
  const byId = new Map<string, AnnotationFeedback>([[root.id, root]]);
  for (const item of all) byId.set(item.id, item);
  return all.map((item) => {
    const parentId = item.thread_parent_id;
    const parent = parentId ? byId.get(parentId) : undefined;
    return {
      ...item,
      parentContext:
        parentId && parentId !== root.id
          ? {
              id: parentId,
              body: parent?.body ?? "上级回复尚未加载或已不可见",
              authorName: parent?.author_name ?? null,
              available: Boolean(parent),
            }
          : null,
    };
  });
}

export function issueThreadQueryKey(
  rootId: string,
  userId: string | null,
  authSession: string | null,
  projectId: string | null,
  taskId: string | null,
) {
  return [...discussionKeys.thread(rootId), userId, authSession, projectId, taskId] as const;
}

function errorState(error: unknown): IssueThreadState {
  if (error instanceof ApiError && error.status === 403) return "permission-denied";
  if (error instanceof ApiError && (error.status === 404 || error.status === 410))
    return "unavailable";
  return "error";
}

export function useIssueThread({
  rootId,
  projectId,
  taskId,
  allowProjectScope = false,
  rootSnapshot: _rootSnapshot = null,
  enabled = true,
  limit = 50,
}: UseIssueThreadParams): UseIssueThreadResult {
  const userId = useAuthStore((state) => state.user?.id ?? null);
  const authToken = useAuthStore((state) => state.token ?? null);
  const draftStore = useDiscussionDraftStore();
  const authSession = draftStore?.getOwner().sessionId ?? null;
  const queryKey = rootId
    ? issueThreadQueryKey(rootId, userId, authSession, projectId ?? null, taskId ?? null)
    : (["feedback-thread", null, userId, authSession, projectId ?? null, taskId ?? null] as const);
  const query = useInfiniteQuery<
    AnnotationFeedbackThreadPage,
    Error,
    InfiniteData<AnnotationFeedbackThreadPage, string | null>,
    typeof queryKey,
    string | null
  >({
    queryKey,
    queryFn: ({ pageParam, signal }) =>
      feedbacksApi.thread(rootId!, { limit, cursor: pageParam ?? undefined }, signal),
    enabled: enabled && Boolean(rootId && projectId && userId && authToken && authSession),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    staleTime: 30_000,
    retry: false,
  });
  const paginationError = query.isFetchNextPageError ? query.error : null;

  const fetchedRoot = query.data?.pages.find((page) => page.root)?.root ?? null;
  const fetchedRootValid = isValidIssueRoot(fetchedRoot, {
    projectId,
    taskId,
    allowProjectScope,
    rootId,
  });
  const requestState = query.isError
    ? errorState(query.error)
    : fetchedRoot && !fetchedRootValid
      ? "unavailable"
      : query.isPending
        ? "loading"
        : fetchedRootValid
          ? "ready"
          : "unavailable";
  const root =
    requestState === "permission-denied" || requestState === "unavailable"
      ? null
      : fetchedRootValid
        ? fetchedRoot
        : null;
  const replies = flattenIssueThread(query.data, fetchedRootValid ? fetchedRoot : root);
  const loading =
    Boolean(rootId && projectId && userId && authToken && authSession) && query.isPending;

  return {
    root,
    replies,
    state: rootId && projectId && userId && authToken && authSession ? requestState : "idle",
    isLoading: loading,
    isFetching: query.isFetching,
    isFetchingNextPage: query.isFetchingNextPage,
    hasNextPage: Boolean(query.hasNextPage),
    error: query.error,
    paginationError,
    loadEarlierReplies: () => query.fetchNextPage(),
    retryEarlierReplies: () => query.fetchNextPage(),
    retry: () => query.refetch(),
    query,
  };
}

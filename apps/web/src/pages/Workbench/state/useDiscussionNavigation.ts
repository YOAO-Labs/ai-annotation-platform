import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "@/api/client";
import { commentsApi } from "@/api/comments";
import { tasksApi } from "@/api/tasks";
import { resolveActiveDiscussionAnnotation } from "@/api/discussionTargets";
import type { AnnotationFeedback } from "@/api/feedbacks";
import type { AnnotationResponse } from "@/types";
import { isValidIssueRoot, useIssueThread } from "@/hooks/useIssueThread";
import { flattenTaskDiscussion, useTaskDiscussion } from "@/hooks/useTaskDiscussion";
import { isCurrentAuthOwner, useAuthStore } from "@/stores/authStore";
import type {
  WorkbenchDiscussionRequest,
  WorkbenchDiscussionTarget,
} from "@/utils/workbenchNavigation";
import { useDiscussionDraftStore } from "./DiscussionDraftProvider";

export type DiscussionNavigationState =
  | { status: "idle" }
  | { status: "loading"; requestId: string; message: string; checked: number }
  | {
      status: "ready";
      requestId: string;
      target: WorkbenchDiscussionTarget;
      root?: AnnotationFeedback;
      annotation?: AnnotationResponse;
      canvasAvailable?: boolean;
    }
  | { status: "error" | "cancelled"; requestId: string; message: string }
  | { status: "complete"; requestId: string };

export interface DiscussionNavigation {
  state: DiscussionNavigationState;
  cancel: () => void;
  retry: () => void;
  dismiss: () => void;
  consume: (requestId: string) => void;
}

interface Options {
  navigationKey: string;
  request: WorkbenchDiscussionRequest;
  projectId: string | null | undefined;
  taskId: string | null | undefined;
  reveal: () => void;
  selectAnnotation: (
    annotation: AnnotationResponse,
    isCurrent: () => boolean,
  ) => Promise<boolean | "unloaded">;
}

export interface DiscussionReplyFocus {
  requestId: string;
  rootId: string;
  replyId: string;
}

export type DiscussionCommentFocus =
  | {
      requestId: string;
      annotationId: string;
      commentId: string;
      annotationLabel: string;
      canvasAvailable: boolean;
      source?: "annotation_comment";
    }
  | {
      requestId: string;
      commentId: string;
      source: "feedback";
    };

class UnavailableTarget extends Error {}

function failureMessage(error: unknown): string {
  if (error instanceof UnavailableTarget) return error.message;
  if (error instanceof ApiError && [403, 404, 410].includes(error.status))
    return "讨论目标已删除或当前无权访问";
  return "讨论加载失败，请重试";
}

export function useDiscussionNavigation(options: Options): DiscussionNavigation {
  const { navigationKey, request, projectId, taskId } = options;
  const requestKey = JSON.stringify(request);
  const userId = useAuthStore((state) => state.user?.id ?? null);
  const hasAuth = useAuthStore((state) => Boolean(state.token));
  const draftStore = useDiscussionDraftStore();
  const sessionId = draftStore?.getOwner().sessionId ?? null;
  const [attempt, setAttempt] = useState(0);
  const owner = JSON.stringify([navigationKey, userId, sessionId, projectId, taskId]);
  const latest = useRef({ ...options, owner });
  latest.current = { ...options, owner };
  const [ownedState, setOwnedState] = useState<{
    owner: string;
    state: DiscussionNavigationState;
  }>({ owner, state: { status: "idle" } });
  const active = useRef<{ owner: string; controller: AbortController } | null>(null);
  const consumed = useRef<{ key: string; attempt: number; started: boolean }>({
    key: navigationKey,
    attempt,
    started: false,
  });
  const target = request.status === "valid" ? request.target : null;
  const thread = useIssueThread({
    rootId: target?.kind === "issue" ? target.issueId : null,
    projectId,
    taskId,
    enabled: false,
  });
  const comments = useTaskDiscussion(
    taskId,
    target?.kind === "task_comment" ? "task" : "annotation",
    target?.kind === "comment" ? target.annotationId : null,
    false,
    projectId,
  );
  const readers = useRef({ thread: thread.query, comments });
  readers.current = { thread: thread.query, comments };

  useEffect(() => {
    if (consumed.current.key !== navigationKey || consumed.current.attempt !== attempt) {
      consumed.current = { key: navigationKey, attempt, started: false };
    }
    if (request.status === "none") {
      setOwnedState({ owner, state: { status: "idle" } });
      return;
    }
    if (consumed.current.started || !userId || !sessionId || !hasAuth || !projectId) return;
    if (request.status === "valid" && (!projectId || taskId !== request.taskId)) return;
    const requestId = JSON.stringify([owner, attempt]);
    if (request.status === "invalid") {
      consumed.current.started = true;
      latest.current.reveal();
      setOwnedState({ owner, state: { status: "error", requestId, message: request.message } });
      return;
    }

    const running = { owner, controller: new AbortController() };
    active.current = running;
    const isCurrent = () =>
      active.current === running &&
      !running.controller.signal.aborted &&
      latest.current.owner === owner &&
      isCurrentAuthOwner(userId);
    const update = (state: DiscussionNavigationState) => {
      if (isCurrent()) setOwnedState({ owner, state });
    };
    const progress = (message: string, checked = 0) =>
      update({ status: "loading", requestId, message, checked });
    const signal = running.controller.signal;
    const threadReader = readers.current.thread;
    const commentReader = readers.current.comments;
    latest.current.reveal();
    progress("正在核对讨论目标");

    void Promise.resolve()
      .then(async () => {
        if (!isCurrent()) return;
        consumed.current.started = true;
        const task = await tasksApi.get(request.taskId, { signal });
        if (!isCurrent()) return;
        if (task.id !== request.taskId || task.project_id !== projectId)
          throw new UnavailableTarget("讨论目标不属于当前任务");

        if (request.target.kind === "issue") {
          const { issueId, replyId } = request.target;
          progress("正在刷新问题对话");
          const joinedFetch = readers.current.thread.isFetching;
          let result = await threadReader.refetch({ cancelRefetch: false });
          if (!isCurrent()) return;
          if (joinedFetch) result = await threadReader.refetch({ cancelRefetch: false });
          const cursors = new Set<string>();
          while (isCurrent()) {
            if (result.isError) throw result.error;
            const pages = result.data?.pages ?? [];
            const root = pages[0]?.root;
            if (!isValidIssueRoot(root, { rootId: issueId, projectId, taskId }))
              throw new UnavailableTarget("问题已删除或不属于当前任务");
            if (
              pages.some(
                (page) => !isValidIssueRoot(page.root, { rootId: issueId, projectId, taskId }),
              )
            )
              throw new UnavailableTarget("问题对话归属已变化，请重试");
            const replies = pages.flatMap((page) => page.items);
            const found = replies.find(
              (reply) =>
                reply.id === replyId &&
                reply.id !== issueId &&
                reply.thread_parent_id !== null &&
                reply.is_active &&
                reply.task_id === taskId &&
                reply.project_id === projectId,
            );
            if (!replyId || found) {
              update({ status: "ready", requestId, target: request.target, root });
              return;
            }
            progress("正在查找通知中的回复", replies.length);
            const cursor = pages[pages.length - 1]?.next_cursor;
            if (!cursor) throw new UnavailableTarget("回复已删除或不属于这个问题");
            if (cursors.has(cursor)) throw new Error("Repeated thread cursor");
            result = await threadReader.fetchNextPage({ cancelRefetch: false });
            if (result.data?.pageParams.includes(cursor)) cursors.add(cursor);
          }
          return;
        }

        if (request.target.kind === "task_comment") {
          const { commentId } = request.target;
          progress("正在查找任务留言");
          const feedCursors = new Set<string>();
          const joinedFeedFetch = readers.current.comments.isFetching;
          let result = await commentReader.refetch({ cancelRefetch: false });
          if (!isCurrent()) return;
          if (joinedFeedFetch) result = await commentReader.refetch({ cancelRefetch: false });
          while (isCurrent()) {
            if (result.isError) throw result.error;
            const items = flattenTaskDiscussion(result.data);
            const matching = items.find(
              (item) => item.source === "feedback" && item.data.id === commentId,
            );
            if (matching) {
              if (
                matching.source !== "feedback" ||
                matching.data.kind !== "comment" ||
                matching.data.anchor_type !== "task" ||
                matching.data.thread_parent_id !== null ||
                !matching.data.is_active ||
                matching.data.project_id !== projectId ||
                matching.data.task_id !== task.id
              ) {
                throw new UnavailableTarget("任务留言已删除或不属于当前任务");
              }
              update({ status: "ready", requestId, target: request.target });
              return;
            }
            const pages = result.data?.pages ?? [];
            const nextCursor = pages[pages.length - 1]?.next_cursor;
            if (!nextCursor) throw new UnavailableTarget("任务留言已删除或不可访问");
            if (feedCursors.has(nextCursor)) throw new Error("Repeated discussion cursor");
            progress("正在查找任务留言", items.length);
            const nextResult = await commentReader.fetchNextPage({ cancelRefetch: false });
            if (!isCurrent()) return;
            if (nextResult.data?.pageParams.includes(nextCursor)) feedCursors.add(nextCursor);
            result = nextResult;
          }
          return;
        }

        const { annotationId, commentId } = request.target;
        const annotation = await resolveActiveDiscussionAnnotation(task, annotationId, signal);
        if (!isCurrent()) return;
        if (!annotation) throw new UnavailableTarget("评论所属标注已删除或不可访问");
        let cursor: string | undefined;
        let checked = 0;
        const legacyCursors = new Set<string>();
        while (isCurrent()) {
          progress("正在查找原标注评论", checked);
          const page = await commentsApi.listByAnnotationKeyset(
            annotationId,
            { limit: 50, cursor },
            signal,
          );
          if (!isCurrent()) return;
          if (
            page.items.some((item) => item.id === commentId && item.annotation_id === annotationId)
          )
            break;
          checked += page.items.length;
          if (!page.next_cursor) throw new UnavailableTarget("原标注评论已删除或不可访问");
          if (legacyCursors.has(page.next_cursor)) throw new Error("Repeated comment cursor");
          legacyCursors.add(page.next_cursor);
          cursor = page.next_cursor;
        }
        if (!isCurrent()) return;
        progress("正在打开标注评论");
        const joinedFeedFetch = readers.current.comments.isFetching;
        let result = await commentReader.refetch({ cancelRefetch: false });
        if (!isCurrent()) return;
        if (joinedFeedFetch) result = await commentReader.refetch({ cancelRefetch: false });
        const feedCursors = new Set<string>();
        while (isCurrent()) {
          if (result.isError) throw result.error;
          const items = flattenTaskDiscussion(result.data);
          if (
            items.some(
              (item) =>
                item.source === "annotation_comment" &&
                item.data.id === commentId &&
                item.data.annotation_id === annotationId,
            )
          ) {
            const selected = await latest.current.selectAnnotation(annotation, isCurrent);
            if (selected === false) {
              update({ status: "cancelled", requestId, message: "已取消讨论定位，原草稿保持不变" });
              return;
            }
            update({
              status: "ready",
              requestId,
              target: request.target,
              annotation,
              canvasAvailable: selected === true,
            });
            return;
          }
          progress("正在打开标注评论", items.length);
          const feedPages = result.data?.pages ?? [];
          const nextCursor = feedPages[feedPages.length - 1]?.next_cursor;
          if (!nextCursor) throw new UnavailableTarget("原标注评论已删除或不可访问");
          if (feedCursors.has(nextCursor)) throw new Error("Repeated discussion cursor");
          result = await commentReader.fetchNextPage({ cancelRefetch: false });
          if (result.data?.pageParams.includes(nextCursor)) feedCursors.add(nextCursor);
        }
      })
      .catch((error: unknown) => {
        update({ status: "error", requestId, message: failureMessage(error) });
      })
      .finally(() => {
        if (active.current === running) active.current = null;
      });
    return () => {
      running.controller.abort();
      if (active.current === running) active.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [navigationKey, requestKey, projectId, taskId, userId, sessionId, hasAuth, owner, attempt]);

  const cancel = useCallback(() => {
    active.current?.controller.abort();
    active.current = null;
    setOwnedState((previous) =>
      previous.owner === latest.current.owner && previous.state.status === "loading"
        ? {
            ...previous,
            state: {
              status: "cancelled",
              requestId: previous.state.requestId,
              message: "已取消查找讨论",
            },
          }
        : previous,
    );
  }, []);
  const retry = useCallback(() => setAttempt((value) => value + 1), []);
  const dismiss = useCallback(() => {
    cancel();
    setOwnedState({ owner: latest.current.owner, state: { status: "idle" } });
  }, [cancel]);
  const consume = useCallback((requestId: string) => {
    setOwnedState((previous) =>
      previous.owner === latest.current.owner &&
      previous.state.status === "ready" &&
      previous.state.requestId === requestId
        ? { ...previous, state: { status: "complete", requestId } }
        : previous,
    );
  }, []);

  return {
    state: ownedState.owner === owner ? ownedState.state : { status: "idle" },
    cancel,
    retry,
    dismiss,
    consume,
  };
}

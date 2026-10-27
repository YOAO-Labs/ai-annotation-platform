import { CopyableError } from "@/components/ui/CopyableError";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  hasPixelAnchor,
  type AnnotationFeedback,
  type FeedbackSeverity,
  type FeedbackStatus,
  type ListFeedbacksParams,
} from "@/api/feedbacks";
import { useDeleteFeedback, usePatchFeedback, useReplyFeedback } from "@/hooks/useFeedbacks";
import { useIssueThread } from "@/hooks/useIssueThread";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { useAuthStore } from "@/stores/authStore";
import { CommentInput } from "./CommentInput";
import type { DiscussionPayload } from "../state/discussionTypes";
import { useDiscussionDraftStore } from "../state/DiscussionDraftProvider";
import { useIssueSequence } from "../state/useIssueSequence";
import type { DiscussionReplyFocus } from "../state/useDiscussionNavigation";
import { issueObjectLabel } from "../stage/issuePinVisuals";

export interface DiscussionIssueDetailProps {
  replyFocus?: DiscussionReplyFocus | null;
  onReplyFocusHandled?: (requestId: string) => void;
  rootId: string;
  projectId: string;
  taskId: string;
  allowProjectScope?: boolean;
  /** Current list scope. Project scope is only passed by the video Workbench. */
  listScope?: "task" | "project";
  rootSnapshot?: AnnotationFeedback | null;
  onBack: () => void;
  onOpenIssue: (issue: AnnotationFeedback) => void;
  onLocate?: (issue: AnnotationFeedback) => void;
  /** Optional bridge for switching the surrounding list to project scope. */
  onRequestProjectScope?: () => void;
}

const STATUS_TEXT: Record<FeedbackStatus, string> = {
  open: "未解决",
  resolved: "已解决",
  wont_fix: "搁置",
};

const SEVERITY_TEXT: Record<FeedbackSeverity, string> = {
  info: "提示",
  warn: "警告",
  blocker: "阻断",
};

const STATUS_CHIP: Record<FeedbackStatus, string> = {
  open: "border-status-caution/60 bg-status-caution-soft text-status-caution",
  resolved: "border-status-positive/60 bg-status-positive-soft text-status-positive",
  wont_fix: "border-border bg-muted text-muted-foreground",
};

function cn(...xs: Array<string | false | null | undefined>): string {
  return xs.filter(Boolean).join(" ");
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

function issueActionAllowed(
  issue: AnnotationFeedback | null,
  action: keyof NonNullable<AnnotationFeedback["actions"]>,
) {
  return Boolean(issue?.actions?.[action]);
}

function threadListParams(projectId: string, taskId: string): ListFeedbacksParams {
  return {
    project_id: projectId,
    task_id: taskId,
    kind: "issue",
    root_only: true,
    include_counts: true,
    limit: 50,
  };
}

type PendingAction = { kind: "status"; status: FeedbackStatus } | { kind: "delete" };
type ConfirmAction = { kind: "delete" } | null;
type SequenceDirection = "previous" | "next";

export function DiscussionIssueDetail({
  replyFocus,
  onReplyFocusHandled,
  rootId,
  projectId,
  taskId,
  allowProjectScope = false,
  listScope = "task",
  rootSnapshot = null,
  onBack,
  onOpenIssue,
  onLocate,
  onRequestProjectScope,
}: DiscussionIssueDetailProps) {
  const [confirmAction, setConfirmAction] = useState<ConfirmAction>(null);
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [pendingAction, setPendingAction] = useState<PendingAction | null>(null);
  const [deleted, setDeleted] = useState(false);
  const userId = useAuthStore((state) => state.user?.id ?? null);
  const draftStore = useDiscussionDraftStore();
  const sessionId = draftStore?.getOwner().sessionId ?? null;
  const ownerKey = JSON.stringify([userId, sessionId, projectId, taskId, rootId]);
  const ownerKeyRef = useRef(ownerKey);
  const actionRef = useRef<{ owner: string; action: PendingAction } | null>(null);
  ownerKeyRef.current = ownerKey;

  const thread = useIssueThread({
    rootId,
    projectId,
    taskId,
    allowProjectScope,
    rootSnapshot,
  });
  const root = deleted ? null : thread.root;
  const rootTaskId = root?.task_id ?? rootSnapshot?.task_id ?? taskId;
  const patchMutation = usePatchFeedback(threadListParams(projectId, rootTaskId));
  const deleteMutation = useDeleteFeedback(threadListParams(projectId, rootTaskId));
  const replyMutation = useReplyFeedback(threadListParams(projectId, rootTaskId));
  const sequence = useIssueSequence({
    projectId,
    taskId: listScope === "project" ? null : taskId,
    current: root,
    enabled: thread.state === "ready" && !!root,
  });
  const threadScrollRef = useRef<HTMLDivElement | null>(null);
  const restoreDistanceRef = useRef<{ owner: string; distance: number } | null>(null);
  const lastSequenceDirectionRef = useRef<SequenceDirection | null>(null);
  const [lastSequenceDirection, setLastSequenceDirection] = useState<SequenceDirection | null>(
    null,
  );
  const replyCount = thread.replies.length;
  const [highlightedReply, setHighlightedReply] = useState<string | null>(null);
  const focusedRequestRef = useRef<string | null>(null);
  const highlightOwnerRef = useRef(ownerKey);

  useEffect(() => {
    setConfirmAction(null);
    setMutationError(null);
    setPendingAction(null);
    setDeleted(false);
    actionRef.current = null;
    lastSequenceDirectionRef.current = null;
    setLastSequenceDirection(null);
  }, [ownerKey]);

  useLayoutEffect(() => {
    if (highlightOwnerRef.current === ownerKey) return;
    highlightOwnerRef.current = ownerKey;
    setHighlightedReply(null);
  }, [ownerKey]);

  useLayoutEffect(() => {
    if (
      !replyFocus ||
      replyFocus.rootId !== rootId ||
      thread.state !== "ready" ||
      focusedRequestRef.current === replyFocus.requestId
    )
      return;
    const container = threadScrollRef.current;
    const row = container?.querySelector<HTMLElement>(`[data-reply-id="${replyFocus.replyId}"]`);
    if (!container || !row) return;
    focusedRequestRef.current = replyFocus.requestId;
    restoreDistanceRef.current = null;
    setHighlightedReply(replyFocus.replyId);
    container.scrollTop += row.getBoundingClientRect().top - container.getBoundingClientRect().top;
    row.focus({ preventScroll: true });
    onReplyFocusHandled?.(replyFocus.requestId);
  }, [replyFocus, rootId, thread.state, replyCount, onReplyFocusHandled]);

  // Preserve the reader's distance from the bottom while an older page is
  // prepended. This keeps the currently visible reply anchored in place.
  useLayoutEffect(() => {
    const pendingRestore = restoreDistanceRef.current;
    const element = threadScrollRef.current;
    if (!pendingRestore || pendingRestore.owner !== ownerKey || !element) return;
    element.scrollTop = Math.max(0, element.scrollHeight - pendingRestore.distance);
    restoreDistanceRef.current = null;
  }, [ownerKey, replyCount]);

  const requestEarlierReplies = useCallback(() => {
    const element = threadScrollRef.current;
    if (element) {
      restoreDistanceRef.current = {
        owner: ownerKey,
        distance: element.scrollHeight - element.scrollTop,
      };
    }
    void thread.loadEarlierReplies();
  }, [ownerKey, thread]);

  const runAction = useCallback(
    async (action: PendingAction) => {
      if (!root || pendingAction || actionRef.current) return;
      const request = { owner: ownerKey, action };
      actionRef.current = request;
      setMutationError(null);
      setPendingAction(action);
      try {
        if (action.kind === "delete") {
          await deleteMutation.mutateAsync({
            id: root.id,
            scope: threadListParams(projectId, rootTaskId),
          });
          if (ownerKeyRef.current === ownerKey && actionRef.current === request) setDeleted(true);
        } else {
          await patchMutation.mutateAsync({ id: root.id, payload: { status: action.status } });
        }
        if (ownerKeyRef.current === ownerKey && actionRef.current === request) {
          setConfirmAction(null);
          setPendingAction(null);
        }
      } catch (error) {
        if (ownerKeyRef.current === ownerKey && actionRef.current === request) {
          setMutationError(
            errorMessage(
              error,
              action.kind === "delete" ? "删除失败，请重试" : "状态更新失败，请重试",
            ),
          );
          setPendingAction(null);
        }
      } finally {
        if (ownerKeyRef.current === ownerKey && actionRef.current === request)
          actionRef.current = null;
      }
    },
    [deleteMutation, ownerKey, patchMutation, pendingAction, projectId, root, rootTaskId],
  );

  const requestAction = () => {
    if (!root || pendingAction) return;
    setMutationError(null);
    setConfirmAction({ kind: "delete" });
  };

  const handleReply = useCallback(
    (payload: DiscussionPayload) =>
      replyMutation.mutateAsync({
        id: root?.id ?? rootId,
        body: payload.body,
        attachments: payload.attachments.map((attachment) => ({ ...attachment })),
      }),
    [replyMutation, root?.id, rootId],
  );

  const handleSequence = useCallback(
    async (direction: SequenceDirection) => {
      const requestOwner = ownerKey;
      lastSequenceDirectionRef.current = direction;
      setLastSequenceDirection(direction);
      const issue = await sequence.go(direction);
      if (issue && ownerKeyRef.current === requestOwner) onOpenIssue(issue);
    },
    [onOpenIssue, ownerKey, sequence],
  );

  const validRoot =
    root && root.kind === "issue" && root.thread_parent_id === null && root.is_active;
  const unavailable =
    deleted || thread.state === "unavailable" || thread.state === "permission-denied";
  const canReply = Boolean(validRoot && issueActionAllowed(root, "reply"));
  const canChangeStatus = Boolean(validRoot && issueActionAllowed(root, "change_status"));
  const canDelete = Boolean(validRoot && issueActionAllowed(root, "delete"));
  const outsideScope = sequence.outsideScope;
  const canRequestProjectScope =
    allowProjectScope && listScope === "task" && Boolean(root?.task_id) && root?.task_id !== taskId;
  const sequenceBlocked = sequence.loading || outsideScope;
  const sequenceError = sequence.error;

  return (
    <div
      className="flex min-h-0 flex-1 flex-col bg-card"
      data-testid="discussion-issue-detail"
      data-issue-id={rootId}
    >
      <div className="flex shrink-0 flex-col gap-1 border-b border-border px-3 py-2">
        <div className="flex min-w-0 items-center gap-2">
          <Button size="sm" variant="ghost" onClick={onBack}>
            <Icon name="chevLeft" size={12} /> 返回问题列表
          </Button>
          <span className="min-w-0 flex-1 truncate text-xs font-semibold text-foreground">
            问题详情
          </span>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-1">
          <Button
            size="sm"
            variant="ghost"
            disabled={sequenceBlocked || sequence.previousState === "unavailable"}
            onClick={() => void handleSequence("previous")}
            aria-label="上一条"
            data-testid={`discussion-issue-previous-${rootId}`}
          >
            <Icon name="chevUp" size={12} /> 上一条
          </Button>
          <Button
            size="sm"
            variant="ghost"
            disabled={sequenceBlocked || sequence.nextState === "unavailable"}
            onClick={() => void handleSequence("next")}
            aria-label="下一条"
            data-testid={`discussion-issue-next-${rootId}`}
          >
            <Icon name="chevDown" size={12} /> 下一条
          </Button>
          {sequence.loading && (
            <>
              <span className="text-2xs text-muted-foreground">正在查找相邻问题…</span>
              <Button size="sm" variant="ghost" onClick={sequence.cancel}>
                取消查找
              </Button>
            </>
          )}
        </div>
      </div>

      {outsideScope && root?.task_id && (
        <div className="flex shrink-0 items-center justify-between gap-2 border-b border-border bg-muted px-3 py-1.5 text-xs text-muted-foreground">
          <span>
            {canRequestProjectScope
              ? "该问题不在当前任务范围内，切换到项目范围后可使用上一条 / 下一条。"
              : "当前问题不在可用的问题序列中。"}
          </span>
          {canRequestProjectScope && onRequestProjectScope && (
            <Button
              size="sm"
              variant="ghost"
              onClick={onRequestProjectScope}
              data-testid={`discussion-issue-switch-project-${rootId}`}
            >
              切换到项目范围
            </Button>
          )}
        </div>
      )}
      {sequenceError && (
        <div className="flex shrink-0 items-center justify-between gap-2 border-b border-status-danger/40 bg-status-danger-soft px-3 py-1.5 text-xs text-status-danger">
          <span>
            问题序列加载失败：
            <CopyableError message={errorMessage(sequenceError, "请重试")} />
          </span>
          <Button
            size="sm"
            variant="ghost"
            disabled={!lastSequenceDirection || sequenceBlocked}
            onClick={() => {
              const direction = lastSequenceDirection;
              if (direction) void handleSequence(direction);
            }}
          >
            重试
          </Button>
        </div>
      )}
      {mutationError && (
        <div
          className="flex shrink-0 items-center justify-between gap-2 border-b border-status-danger/40 bg-status-danger-soft px-3 py-1.5 text-xs text-status-danger"
          role="alert"
          data-testid="discussion-issue-detail-error"
        >
          <span>
            <CopyableError message={mutationError} />
          </span>
          <Button size="sm" variant="ghost" onClick={() => setMutationError(null)}>
            关闭
          </Button>
        </div>
      )}

      <div
        ref={threadScrollRef}
        className="min-h-0 flex-1 overflow-y-auto px-3 py-2.5"
        data-testid="discussion-issue-thread-scroll"
      >
        {thread.state === "loading" && !root && (
          <div className="py-3 text-xs text-muted-foreground">正在加载问题…</div>
        )}
        {thread.state === "permission-denied" && (
          <div
            className="rounded border border-status-danger/40 bg-status-danger-soft px-2.5 py-2 text-xs text-status-danger"
            data-testid="discussion-issue-permission-denied"
          >
            你没有权限查看这个问题。
          </div>
        )}
        {unavailable && (
          <div
            className="rounded border border-border bg-muted px-2.5 py-2 text-xs text-muted-foreground"
            data-testid="discussion-issue-unavailable"
          >
            该问题已删除或当前账号不可见，回复已停止。
          </div>
        )}
        {thread.state === "error" && !unavailable && (
          <div
            className="flex items-center justify-between gap-2 rounded border border-status-danger/40 bg-status-danger-soft px-2.5 py-2 text-xs text-status-danger"
            role="alert"
            data-testid="discussion-issue-thread-error"
          >
            <span>
              <CopyableError
                message={
                  thread.paginationError
                    ? `无法加载更早回复：${errorMessage(thread.paginationError, "请重试")}`
                    : `无法加载问题：${errorMessage(thread.error, "请重试")}`
                }
              />
            </span>
            <Button
              size="sm"
              variant="ghost"
              onClick={() =>
                void (thread.paginationError ? thread.retryEarlierReplies() : thread.retry())
              }
            >
              重试
            </Button>
          </div>
        )}
        {root && !unavailable && (
          <>
            <article
              className="flex flex-col gap-2 border-b border-border pb-3"
              data-testid={`discussion-issue-root-${root.id}`}
            >
              <div className="flex flex-wrap items-center gap-1.5">
                <span
                  className={cn(
                    "rounded-[10px] border px-2 py-px text-2xs",
                    STATUS_CHIP[root.status],
                  )}
                  aria-label={`问题状态：${STATUS_TEXT[root.status]}`}
                >
                  {STATUS_TEXT[root.status]}
                </span>
                <span
                  className={cn(
                    "rounded-[10px] border border-border px-2 py-px text-2xs",
                    root.severity === "blocker"
                      ? "text-status-danger"
                      : root.severity === "info"
                        ? "text-status-info-alt"
                        : "text-status-caution",
                  )}
                  aria-label={`问题严重度：${root.severity ? SEVERITY_TEXT[root.severity] : "警告"}`}
                >
                  {root.severity ? SEVERITY_TEXT[root.severity] : "警告"}
                </span>
                <span className="ml-auto text-2xs text-muted-foreground">
                  {root.author_name ?? "—"} · {new Date(root.created_at).toLocaleString()}
                </span>
              </div>
              {root.title && (
                <h2 className="text-md font-semibold text-foreground">{root.title}</h2>
              )}
              <div className="whitespace-pre-wrap text-xs leading-6 text-foreground">
                {root.body}
              </div>
              {hasPixelAnchor(root) && (
                <div className="flex flex-wrap items-center gap-2 text-2xs text-muted-foreground">
                  <span>
                    <Icon name="crosshair" size={11} /> 位置 ({root.anchor_position.x.toFixed(3)},{" "}
                    {root.anchor_position.y.toFixed(3)})
                  </span>
                  {typeof root.anchor_position.frame === "number" && (
                    <span>源帧 F{root.anchor_position.frame}</span>
                  )}
                </div>
              )}
              {root.annotation_id && (
                <div
                  className="flex items-center gap-1 text-2xs text-muted-foreground"
                  title={root.annotation_id}
                  aria-label={`关联对象：${issueObjectLabel(root.annotation_id)}`}
                  data-testid={`discussion-issue-object-${root.id}`}
                >
                  <Icon name="target" size={11} /> 关联对象：{issueObjectLabel(root.annotation_id)}
                </div>
              )}
              <div className="flex flex-wrap items-center gap-1">
                {onLocate && hasPixelAnchor(root) && (
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => onLocate(root)}
                    aria-label={`定位问题：${root.title ?? root.body}`}
                    data-testid={`discussion-issue-locate-${root.id}`}
                    data-workbench-issue-navigation
                  >
                    <Icon name="crosshair" size={12} /> 定位
                  </Button>
                )}
                {canChangeStatus && root.status !== "resolved" && (
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={Boolean(pendingAction)}
                    onClick={() => void runAction({ kind: "status", status: "resolved" })}
                  >
                    解决
                  </Button>
                )}
                {canChangeStatus && root.status !== "open" && (
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={Boolean(pendingAction)}
                    onClick={() => void runAction({ kind: "status", status: "open" })}
                  >
                    重新打开
                  </Button>
                )}
                {canChangeStatus && root.status !== "wont_fix" && (
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={Boolean(pendingAction)}
                    onClick={() => void runAction({ kind: "status", status: "wont_fix" })}
                  >
                    搁置
                  </Button>
                )}
                {canDelete && (
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={Boolean(pendingAction)}
                    onClick={requestAction}
                  >
                    <Icon name="trash" size={12} /> 删除
                  </Button>
                )}
              </div>
              {confirmAction && (
                <div
                  className="flex flex-wrap items-center gap-1 rounded border border-border bg-muted px-2 py-1.5 text-xs text-muted-foreground"
                  role="group"
                  aria-label="确认问题操作"
                >
                  <span>确认删除这个问题？</span>
                  <Button
                    size="sm"
                    variant="danger"
                    disabled={Boolean(pendingAction)}
                    onClick={() => void runAction(confirmAction)}
                  >
                    {pendingAction ? "处理中…" : "确认"}
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={Boolean(pendingAction)}
                    onClick={() => setConfirmAction(null)}
                  >
                    取消
                  </Button>
                </div>
              )}
            </article>

            <section className="flex flex-col gap-2 pt-3" aria-label="问题回复">
              <div className="flex items-center justify-between text-xs font-semibold text-foreground">
                <span>回复</span>
                {thread.hasNextPage && (
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={thread.isFetchingNextPage}
                    onClick={requestEarlierReplies}
                  >
                    {thread.isFetchingNextPage ? "加载中…" : "加载更早回复"}
                  </Button>
                )}
              </div>
              {thread.replies.length === 0 && !thread.isFetching && (
                <div className="text-xs text-muted-foreground">暂无回复。</div>
              )}
              {thread.replies.map((reply) => (
                <article
                  key={reply.id}
                  className={cn(
                    "rounded border border-border bg-muted px-2.5 py-2 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
                    highlightedReply === reply.id && "border-brand ring-1 ring-brand",
                  )}
                  data-testid={`discussion-issue-reply-${reply.id}`}
                  data-reply-id={reply.id}
                  aria-current={highlightedReply === reply.id ? true : undefined}
                  tabIndex={-1}
                >
                  {reply.parentContext && (
                    <div className="mb-1 border-l-2 border-border pl-2 text-2xs text-muted-foreground">
                      {reply.parentContext.available
                        ? `回复 ${reply.parentContext.authorName ?? "上级回复"}`
                        : "回复尚未加载或已不可见"}
                      ：{reply.parentContext.body}
                    </div>
                  )}
                  <div className="mb-1 flex items-center gap-1 text-2xs text-muted-foreground">
                    <span>{reply.author_name ?? "—"}</span>
                    <span>·</span>
                    <time dateTime={reply.created_at}>
                      {new Date(reply.created_at).toLocaleString()}
                    </time>
                  </div>
                  <div className="whitespace-pre-wrap text-xs leading-6 text-foreground">
                    {reply.body}
                  </div>
                </article>
              ))}
            </section>
          </>
        )}
      </div>

      <div className="shrink-0 border-t border-border px-3 py-2.5">
        {canReply ? (
          <CommentInput
            target={{ projectId, taskId: rootTaskId, kind: "issue", rootIssueId: root!.id }}
            members={[]}
            targetAvailable={thread.state === "ready" && !deleted}
            targetUnavailableReason={unavailable ? "该问题已删除或当前账号不可见" : null}
            onSubmit={handleReply}
          />
        ) : (
          <div className="text-xs text-muted-foreground">当前问题不允许回复。</div>
        )}
      </div>
    </div>
  );
}

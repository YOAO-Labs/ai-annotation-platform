import { FilterGroup, FilterSelect, FilterToggle } from "@/components/filters/FilterControls";
/**
 * DiscussionPanel 的 Issue 列表。
 *
 * Issue status 由服务端过滤；首屏的 status_counts.open 是精确待处理数，
 * 绝不从当前已经加载的卡片数量推导。详情读取和图钉激活独立于当前
 * 列表页/过滤器，定位仍需用户明确点击。
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Icon } from "@/components/ui/Icon";
import { Button } from "@/components/ui/Button";
import { useInfiniteFeedbacks, usePatchFeedback, useDeleteFeedback } from "@/hooks/useFeedbacks";
import {
  hasPixelAnchor,
  type AnnotationFeedback,
  type FeedbackSeverity,
  type FeedbackStatus,
  type ListFeedbacksParams,
} from "@/api/feedbacks";
import { useActiveIssueStore } from "../state/useActiveIssueStore";
import { DiscussionIssueDetail } from "./DiscussionIssueDetail";
import { readVideoIssueContext } from "../state/videoIssueContext";
import { useAuthStore } from "@/stores/authStore";
import type { DiscussionReplyFocus } from "../state/useDiscussionNavigation";
import { issueObjectLabel } from "../stage/issuePinVisuals";

interface Props {
  replyFocus?: DiscussionReplyFocus | null;
  onReplyFocusHandled?: (requestId: string) => void;
  projectId: string;
  taskId: string;
  /** Open the task-only Issue form with an explicit task intent. */
  onCreateTaskIssue?: () => void;
  /** Arm the canvas for an actual confirmed pixel/frame point. */
  onCreatePixelIssue?: () => void;
  allowProjectScope?: boolean;
}

function cn(...xs: Array<string | false | null | undefined>): string {
  return xs.filter(Boolean).join(" ");
}

const STATUS_FILTERS: { key: FeedbackStatus | "all"; label: string }[] = [
  { key: "open", label: "未解决" },
  { key: "all", label: "全部" },
  { key: "resolved", label: "已解决" },
  { key: "wont_fix", label: "搁置" },
];

// 卡片整体减淡(已解决 / 搁置)。
const STATUS_CARD_DIM: Record<FeedbackStatus, string> = {
  open: "",
  resolved: "opacity-60",
  wont_fix: "opacity-55",
};

// status chip:柔底 + 同色描边/文字。
const STATUS_CHIP: Record<FeedbackStatus, string> = {
  open: "border-status-caution/60 bg-status-caution-soft text-status-caution",
  resolved: "border-status-positive/60 bg-status-positive-soft text-status-positive",
  wont_fix: "border-border bg-muted text-muted-foreground",
};

// severity chip:仅文字着色(沿用原设计,描边走默认 border)。
const SEVERITY_CHIP: Record<FeedbackSeverity, string> = {
  info: "text-status-info-alt",
  warn: "text-status-caution",
  blocker: "text-status-danger",
};

function feedbackActions(issue: AnnotationFeedback) {
  // The server is authoritative. Missing capabilities are intentionally
  // fail-closed: an old or malformed response may still be located/read, but
  // must never expose a mutation it cannot prove is permitted.
  return {
    changeStatus: issue.actions?.change_status ?? false,
    delete: issue.actions?.delete ?? false,
  };
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

export function DiscussionIssuesTab({
  replyFocus,
  onReplyFocusHandled,
  projectId,
  taskId,
  onCreateTaskIssue,
  onCreatePixelIssue,
  allowProjectScope = false,
}: Props) {
  const [statusFilter, setStatusFilter] = useState<FeedbackStatus | "all">("open");
  const [scope, setScope] = useState<"task" | "project">("task");
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [pendingMutation, setPendingMutation] = useState<{
    kind: "status" | "delete";
    id: string;
  } | null>(null);
  const [deleteCandidate, setDeleteCandidate] = useState<string | null>(null);
  const [activeDetail, setActiveDetail] = useState<{
    rootId: string;
    snapshot: AnnotationFeedback | null;
  } | null>(null);
  const mutationRequestRef = useRef<object | null>(null);
  const listScrollRef = useRef<HTMLDivElement | null>(null);
  const listScrollTopRef = useRef(0);
  const userId = useAuthStore((state) => state.user?.id ?? null);
  const ownerKey = JSON.stringify([userId, projectId, taskId]);
  const ownerKeyRef = useRef(ownerKey);
  ownerKeyRef.current = ownerKey;

  const params: ListFeedbacksParams = useMemo(
    () => ({
      project_id: projectId,
      task_id: !allowProjectScope || scope === "task" ? taskId : undefined,
      kind: "issue",
      status: statusFilter === "all" ? undefined : statusFilter,
      root_only: true,
      include_counts: true,
      limit: 50,
    }),
    [projectId, taskId, scope, allowProjectScope, statusFilter],
  );
  const query = useInfiniteFeedbacks(params);
  const patchMut = usePatchFeedback(params);
  const deleteMut = useDeleteFeedback(params);
  const highlightId = useActiveIssueStore((s) => s.highlightId);
  const focusIssue = useActiveIssueStore((s) => s.focusIssue);
  const detailRequestTick = useActiveIssueStore((s) => s.detailRequestTick);
  const detailTarget = useActiveIssueStore((s) => s.detailTarget);
  const detailTargetId = useActiveIssueStore((s) => s.detailTargetId);
  const detailOwnerId = useActiveIssueStore((s) => s.detailOwnerId);
  const detailScope = useActiveIssueStore((s) => s.detailScope);
  const openIssueDetail = useActiveIssueStore((s) => s.openIssueDetail);
  const closeIssueDetail = useActiveIssueStore((s) => s.closeIssueDetail);

  useEffect(() => {
    // A task replacement retires local mutation/detail UI; callbacks from the
    // old owner must not surface an error or reopen an old root in the new task.
    setMutationError(null);
    setPendingMutation(null);
    setDeleteCandidate(null);
    setActiveDetail(null);
    mutationRequestRef.current = null;
    return () => {
      mutationRequestRef.current = null;
    };
  }, [ownerKey]);

  // A pin can be activated before the Issues tab mounts. Consume its scoped
  // snapshot when the tab becomes visible, but never replay it after a
  // task/user owner transition.
  const detailOwnerRef = useRef(ownerKey);
  useEffect(() => {
    const ownerChanged = detailOwnerRef.current !== ownerKey;
    detailOwnerRef.current = ownerKey;
    if (ownerChanged) {
      setActiveDetail(null);
      return;
    }
    if (!detailTargetId) {
      setActiveDetail(null);
      return;
    }
    if (detailOwnerId && detailOwnerId !== userId) {
      setActiveDetail(null);
      return;
    }
    const scopeProjectId = detailScope?.projectId ?? detailTarget?.project_id;
    const scopeTaskId = detailScope?.taskId ?? detailTarget?.task_id;
    if (
      (scopeProjectId && scopeProjectId !== projectId) ||
      (scopeTaskId && !allowProjectScope && scopeTaskId !== taskId)
    ) {
      setActiveDetail(null);
      return;
    }
    setActiveDetail({
      rootId: detailTargetId,
      snapshot: detailTarget ? structuredClone(detailTarget) : null,
    });
  }, [
    allowProjectScope,
    detailOwnerId,
    detailRequestTick,
    detailScope,
    detailTarget,
    detailTargetId,
    ownerKey,
    projectId,
    taskId,
    userId,
  ]);

  const activateDetail = useCallback(
    (target: AnnotationFeedback | string, snapshot?: AnnotationFeedback | null) => {
      if (listScrollRef.current) listScrollTopRef.current = listScrollRef.current.scrollTop;
      const nextSnapshot = typeof target === "string" ? (snapshot ?? null) : target;
      const rootId = typeof target === "string" ? target : target.id;
      setActiveDetail({ rootId, snapshot: nextSnapshot ? structuredClone(nextSnapshot) : null });
      openIssueDetail(target, {
        projectId,
        taskId: typeof nextSnapshot?.task_id === "string" ? nextSnapshot.task_id : taskId,
      });
    },
    [openIssueDetail, projectId, taskId],
  );

  const leaveDetail = useCallback(() => {
    setActiveDetail(null);
    closeIssueDetail();
  }, [closeIssueDetail]);

  useLayoutEffect(() => {
    if (activeDetail || !listScrollRef.current) return;
    listScrollRef.current.scrollTop = listScrollTopRef.current;
  }, [activeDetail]);

  const items = useMemo(
    () => query.data?.pages.flatMap((page) => page.items) ?? [],
    [query.data?.pages],
  );
  // The server applies the selected status before pagination. Never filter
  // the loaded rows again: doing so makes a page-one-only match look absent.
  const openIssueCount = query.data?.pages[0]?.status_counts?.open;
  const countState = query.isError
    ? "error"
    : query.isLoading || (query.isFetching && !query.data)
      ? "loading"
      : typeof openIssueCount === "number"
        ? "exact"
        : "unknown";

  const reportMutationError = (error: unknown, fallback: string) => {
    setMutationError(errorMessage(error, fallback));
  };

  const setStatus = (id: string, next: FeedbackStatus) => {
    if (mutationRequestRef.current) return;
    const request = {};
    mutationRequestRef.current = request;
    const requestOwner = ownerKey;
    setMutationError(null);
    setPendingMutation({ kind: "status", id });
    patchMut.mutate(
      { id, payload: { status: next } },
      {
        onError: (error) => {
          if (ownerKeyRef.current === requestOwner && mutationRequestRef.current === request)
            reportMutationError(error, "状态更新失败，请重试");
        },
        onSettled: () => {
          if (ownerKeyRef.current !== requestOwner || mutationRequestRef.current !== request)
            return;
          mutationRequestRef.current = null;
          setPendingMutation(null);
        },
      },
    );
  };

  const deleteIssue = (id: string) => {
    if (mutationRequestRef.current) return;
    const request = {};
    mutationRequestRef.current = request;
    const requestOwner = ownerKey;
    setMutationError(null);
    setPendingMutation({ kind: "delete", id });
    deleteMut.mutate(
      { id, scope: params },
      {
        onError: (error) => {
          if (ownerKeyRef.current === requestOwner && mutationRequestRef.current === request)
            reportMutationError(error, "删除失败，请重试");
        },
        onSettled: () => {
          if (ownerKeyRef.current !== requestOwner || mutationRequestRef.current !== request)
            return;
          mutationRequestRef.current = null;
          setPendingMutation(null);
          setDeleteCandidate(null);
        },
      },
    );
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      {activeDetail ? (
        <DiscussionIssueDetail
          replyFocus={replyFocus?.rootId === activeDetail.rootId ? replyFocus : null}
          onReplyFocusHandled={onReplyFocusHandled}
          rootId={activeDetail.rootId}
          projectId={projectId}
          taskId={taskId}
          // Read permission is global to the video workbench. The list scope
          // only controls the sequence's task filter; a task-scope detail may
          // still be opened from a project-scoped pin/deep link and offer an
          // explicit project-scope switch when its root belongs elsewhere.
          allowProjectScope={allowProjectScope}
          listScope={scope}
          rootSnapshot={activeDetail.snapshot}
          onBack={leaveDetail}
          onOpenIssue={(issue) => activateDetail(issue)}
          onLocate={focusIssue}
          onRequestProjectScope={allowProjectScope ? () => setScope("project") : undefined}
        />
      ) : (
        <div
          ref={listScrollRef}
          className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto px-3 py-2.5"
          data-testid="discussion-issue-list-scroll"
          onScroll={() => {
            if (listScrollRef.current) listScrollTopRef.current = listScrollRef.current.scrollTop;
          }}
        >
          <FilterGroup compact label="问题筛选" hideLabel className="gap-1">
            {allowProjectScope && (
              <FilterSelect
                compact
                aria-label="问题列表范围"
                data-testid="issue-list-scope"
                value={scope}
                onChange={(event) => setScope(event.target.value as "task" | "project")}
              >
                <option value="task">当前任务</option>
                <option value="project">整个项目</option>
              </FilterSelect>
            )}
            {STATUS_FILTERS.map((f) => (
              <FilterToggle
                compact
                active={statusFilter === f.key}
                key={f.key}
                type="button"
                onClick={() => setStatusFilter(f.key)}
                aria-pressed={statusFilter === f.key}
                data-testid={`issue-status-${f.key}`}
              >
                {f.label}
              </FilterToggle>
            ))}
            <span
              className="ml-auto text-2xs text-muted-foreground"
              aria-label={`${!allowProjectScope || scope === "task" ? "当前任务" : "整个项目"}已加载问题数量`}
            >
              已加载 {items.length}
            </span>
            <span
              className="text-2xs text-muted-foreground"
              data-testid="issue-open-count"
              data-state={countState}
            >
              {countState === "exact"
                ? `待处理 ${openIssueCount}`
                : countState === "loading"
                  ? "待处理 …"
                  : "待处理 —"}
            </span>
          </FilterGroup>

          <div className="flex flex-wrap gap-1">
            {onCreateTaskIssue && (
              <Button
                size="sm"
                variant="ghost"
                onClick={onCreateTaskIssue}
                data-testid="issue-create-task"
                data-workbench-issue-navigation
              >
                <Icon name="plus" size={12} /> 任务问题
              </Button>
            )}
            {onCreatePixelIssue && (
              <Button
                size="sm"
                variant="ghost"
                onClick={onCreatePixelIssue}
                data-testid="issue-create-pixel"
                data-workbench-issue-navigation
              >
                <Icon name="crosshair" size={12} /> 在画布选点
              </Button>
            )}
          </div>

          {query.isLoading && (
            <div className="px-1 py-2 text-xs text-muted-foreground">加载中…</div>
          )}
          {query.isError && (
            <div className="flex items-center justify-between gap-2 px-1 py-2 text-xs text-status-danger">
              <span>加载失败：{errorMessage(query.error, "请重试")}</span>
              <Button size="sm" variant="ghost" onClick={() => void query.refetch()}>
                重试
              </Button>
            </div>
          )}
          {mutationError && (
            <div
              className="flex items-center justify-between gap-2 rounded border border-status-danger/40 bg-status-danger-soft px-2 py-1.5 text-xs text-status-danger"
              role="alert"
              data-testid="issue-mutation-error"
            >
              <span>{mutationError}</span>
              <Button size="sm" variant="ghost" onClick={() => setMutationError(null)}>
                关闭
              </Button>
            </div>
          )}
          {!query.isLoading && !query.isError && items.length === 0 && (
            <div className="px-1 py-2 text-xs text-muted-foreground">
              {scope === "task" ? "当前任务" : "当前项目"}暂无 Issue。
            </div>
          )}

          {items.map((it) => {
            const pixelAnchor = hasPixelAnchor(it) ? it.anchor_position : null;
            const hasPin = pixelAnchor !== null;
            const videoContext = readVideoIssueContext(it);
            const actions = feedbackActions(it);
            const severity = it.severity ?? "warn";
            const pending = pendingMutation !== null;
            const isDeleteCandidate = deleteCandidate === it.id;
            const openLabel = `打开问题：${it.title?.trim() || it.body}`;
            return (
              <div
                key={it.id}
                ref={(node) => {
                  if (highlightId === it.id && node) node.scrollIntoView?.({ block: "nearest" });
                }}
                className={cn(
                  "flex flex-col gap-1 rounded-md border border-border bg-muted px-2.5 py-2",
                  STATUS_CARD_DIM[it.status],
                  highlightId === it.id &&
                    "border-status-caution shadow-[0_0_0_1px_var(--sc-caution)]",
                )}
                data-testid={`discussion-issue-card-${it.id}`}
              >
                <div className="flex flex-wrap items-center gap-1.5 text-xs">
                  <span
                    className={cn(
                      "rounded-[10px] border px-2 py-px text-2xs",
                      STATUS_CHIP[it.status],
                    )}
                    aria-label={`问题状态：${it.status === "open" ? "未解决" : it.status === "resolved" ? "已解决" : "搁置"}`}
                  >
                    {it.status === "open" ? "未解决" : it.status === "resolved" ? "已解决" : "搁置"}
                  </span>
                  <span
                    className={cn(
                      "rounded-[10px] border border-border px-2 py-px text-2xs",
                      SEVERITY_CHIP[severity],
                    )}
                    aria-label={`问题严重度：${severity === "blocker" ? "阻断" : severity === "warn" ? "警告" : "提示"}`}
                  >
                    {severity === "blocker" ? "阻断" : severity === "warn" ? "警告" : "提示"}
                  </span>
                  {hasPin && (
                    <span className="text-2xs text-muted-foreground" title="像素锚点">
                      <Icon name="crosshair" size={11} /> ({pixelAnchor.x.toFixed(2)},{" "}
                      {pixelAnchor.y.toFixed(2)})
                    </span>
                  )}
                  {pixelAnchor && typeof pixelAnchor.frame === "number" && (
                    <span className="text-2xs text-muted-foreground" title="所属帧">
                      {videoContext?.frame_range
                        ? `F${videoContext.frame_range.from_frame}–F${videoContext.frame_range.to_frame}`
                        : `F${pixelAnchor.frame}`}
                    </span>
                  )}
                  {it.annotation_id && (
                    <span
                      className="text-2xs text-muted-foreground"
                      title={it.annotation_id}
                      aria-label={`关联对象：${issueObjectLabel(it.annotation_id)}`}
                      data-testid={`discussion-issue-object-${it.id}`}
                    >
                      <Icon name="target" size={11} /> {issueObjectLabel(it.annotation_id)}
                    </span>
                  )}
                  {scope === "project" && it.task_id && it.task_id !== taskId && (
                    <span className="text-2xs text-muted-foreground" title={it.task_id}>
                      其他任务
                    </span>
                  )}
                  <span className="ml-auto text-2xs text-muted-foreground">
                    {it.author_name ?? "—"} · {new Date(it.created_at).toLocaleString()}
                  </span>
                </div>
                <button
                  type="button"
                  className="cursor-pointer appearance-none border-0 bg-transparent p-0 text-left text-xs font-semibold text-foreground underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  onClick={() => activateDetail(it)}
                  data-testid={`discussion-issue-open-${it.id}`}
                  aria-label={openLabel}
                >
                  {it.title?.trim() || "查看问题详情"}
                </button>
                <div className="whitespace-pre-wrap text-xs text-foreground">{it.body}</div>
                <div className="mt-1 flex flex-wrap justify-end gap-1">
                  {hasPin && (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => focusIssue(it)}
                      data-testid={`discussion-issue-locate-${it.id}`}
                      data-workbench-issue-navigation
                      aria-label={`定位问题：${it.title?.trim() || it.body}`}
                    >
                      <Icon name="crosshair" size={11} /> 定位
                    </Button>
                  )}
                  {(actions.changeStatus || actions.delete) && (
                    <>
                      {actions.changeStatus && it.status !== "resolved" && (
                        <Button
                          variant="ghost"
                          size="sm"
                          disabled={pending}
                          onClick={() => setStatus(it.id, "resolved")}
                          title="标为已解决"
                        >
                          <Icon name="check" size={11} /> 解决
                        </Button>
                      )}
                      {actions.changeStatus && it.status !== "wont_fix" && (
                        <Button
                          variant="ghost"
                          size="sm"
                          disabled={pending}
                          onClick={() => setStatus(it.id, "wont_fix")}
                          title="搁置"
                        >
                          搁置
                        </Button>
                      )}
                      {actions.changeStatus && it.status !== "open" && (
                        <Button
                          variant="ghost"
                          size="sm"
                          disabled={pending}
                          onClick={() => setStatus(it.id, "open")}
                          title="重开"
                        >
                          重开
                        </Button>
                      )}
                      {actions.delete && !isDeleteCandidate && (
                        <Button
                          variant="ghost"
                          size="sm"
                          disabled={pending}
                          onClick={() => {
                            setMutationError(null);
                            setDeleteCandidate(it.id);
                          }}
                          title="删除"
                          data-testid={`issue-delete-${it.id}`}
                        >
                          <Icon name="trash" size={11} />
                        </Button>
                      )}
                      {actions.delete && isDeleteCandidate && (
                        <>
                          <span className="self-center text-2xs text-status-danger">
                            确认删除？
                          </span>
                          <Button
                            variant="ghost"
                            size="sm"
                            disabled={pending}
                            onClick={() => deleteIssue(it.id)}
                            data-testid={`issue-delete-confirm-${it.id}`}
                          >
                            确认
                          </Button>
                          <Button
                            variant="ghost"
                            size="sm"
                            disabled={pending}
                            onClick={() => setDeleteCandidate(null)}
                          >
                            取消
                          </Button>
                        </>
                      )}
                    </>
                  )}
                </div>
              </div>
            );
          })}
          {query.hasNextPage && (
            <Button
              size="sm"
              variant="ghost"
              disabled={query.isFetchingNextPage}
              onClick={() => void query.fetchNextPage()}
            >
              {query.isFetchingNextPage ? "加载中…" : "加载更多问题"}
            </Button>
          )}
        </div>
      )}
    </div>
  );
}

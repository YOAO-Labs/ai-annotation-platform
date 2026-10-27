import { CopyableError } from "@/components/ui/CopyableError";
import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ComponentProps,
} from "react";
import { Icon } from "@/components/ui/Icon";
import { CommentsPanel, type DiscussionAnnotationReadRequest } from "./CommentsPanel";
import { DiscussionIssuesTab } from "./DiscussionIssuesTab";
import { MaskQcPanel } from "./MaskQcPanel";
import { useActiveIssueStore } from "../state/useActiveIssueStore";
import { Button } from "@/components/ui/Button";
import { useTaskDiscussion } from "@/hooks/useTaskDiscussion";
import type {
  DiscussionNavigation,
  DiscussionReplyFocus,
  DiscussionCommentFocus,
} from "../state/useDiscussionNavigation";

// 顶部 tab 切换条: 字号/字重与右栏上段"标注详情"标题 (text-sm font-semibold) 对齐,
// 视觉上作为同级标题。v0.20.22 · 拆分中性/激活分支下发, 避免 border-transparent 与
// border-brand 同挂被源顺序覆盖 (memory: "Tailwind 激活态色类冲突")。text 色同理。
const TAB_BUTTON_BASE =
  "shrink-0 cursor-pointer appearance-none border-0 border-b-2 bg-transparent px-2 py-1 text-sm font-semibold focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring [font:inherit]";
const TAB_BUTTON_ACTIVE = "border-brand text-foreground";
const TAB_BUTTON_INACTIVE = "border-transparent text-muted-foreground";

function DiscussionCountBadge({
  count,
  loading,
  error,
  issues = false,
}: {
  count: number | null | undefined;
  loading?: boolean;
  error?: boolean;
  issues?: boolean;
}) {
  if (count === undefined || (count === 0 && !loading && !error)) return null;
  const subject = issues ? "未解决" : "评论";
  const label = error
    ? `${subject}数量暂不可用`
    : loading
      ? `正在加载${subject}数量`
      : count === null
        ? `${subject}数量未知`
        : `${count} ${issues ? "个未解决" : "条评论"}`;
  return (
    <span
      className={`relative -top-1 ml-0.5 inline-flex h-4 min-w-4 items-center justify-center rounded-full px-1 text-2xs font-medium leading-none tabular-nums ${issues ? "bg-status-danger-soft text-status-danger" : "bg-muted text-foreground"}`}
      aria-label={label}
      title={label}
    >
      {error ? "?" : loading ? "…" : count === null ? "?" : count > 9 ? "9+" : count}
    </span>
  );
}

/**
 * v0.11.2-4 · B 组 · 工作台右栏下段统一讨论面板。
 *
 * 三个常驻 tab：
 *   - comments (v0.11.2)：复用 CommentsPanel 的评论能力 (hideTabs + forceTab)，
 *     annotationId 非 null → 标注级评论；null → 任务级评论 (合并 annotation_comments + feedback)。
 *   - history (v0.11.3)：同样复用 CommentsPanel 的历史时间线 (forceTab="history")，
 *     annotation 优先，null 降级查任务级 audit (GET /tasks/{id}/audit-history)。
 *   - issues (v0.11.4)：kind=issue feedback 列表 + status 过滤 + 与 IssueLayer 图钉双向联动。
 *
 * v0.11.5 转正：去 flag 成为右栏下段默认；旧浮层 IssueListPanel 路径已删，
 * CommentsPanel 保留作本面板 comments/history tab 的子渲染器。
 * 边界：只统一 comment + issue + history(audit)；bug / reject 刻意不进。
 */
type DiscussionTab = "comments" | "history" | "issues" | "mask_qc";

const TABS: { key: DiscussionTab; label: string }[] = [
  { key: "comments", label: "评论" },
  { key: "issues", label: "问题" },
  { key: "history", label: "历史" },
  { key: "mask_qc", label: "Mask 质检" },
];

// v0.11.5+ · 评论内画布批注 (live 绘图) + 点评论跳帧 (video) 的桥接 props，
// 从 CommentsPanel 派生以保持同步。原在 AIInspectorPanel 内嵌时透传，去 flag 后
// 在此重新接上 (修复 v0.11.2 复用 CommentsPanel 时漏传导致的功能回退)。
type CommentsBridgeProps = Pick<
  ComponentProps<typeof CommentsPanel>,
  | "backgroundUrl"
  | "imageWidth"
  | "imageHeight"
  | "enableCanvasDrawing"
  | "enableTaskCanvasDrawing"
  | "liveCanvas"
  | "commentAnchor"
  | "onSeekFrame"
  | "annotationClassById"
  | "onSelectAnnotation"
  | "annotationDiscussionRequest"
  | "onAnnotationDiscussionRequestConsumed"
>;

interface DiscussionPanelProps extends CommentsBridgeProps {
  navigation?: DiscussionNavigation;
  onCreateTaskIssue?: () => void;
  onCreatePixelIssue?: () => void;
  openIssueCount?: number | null;
  openIssueCountLoading?: boolean;
  openIssueCountError?: boolean;
  allowProjectIssueScope?: boolean;
  maskQc?: ComponentProps<typeof MaskQcPanel>;
  annotationId: string | null;
  taskId: string | null;
  projectId: string | null;
  currentUserId: string | null;
  onDetach?: () => void;
  floating?: boolean;
  /** v0.20.22 · 完全收起态 (受控, 走 workbench.layout 持久); 缺省回落组件内会话态。
   *  收起时不渲染 tabpanel 内容区, 仅剩 tab 头一条。 */
  collapsed?: boolean;
  onToggleCollapsed?: () => void;
}

export function DiscussionPanel({
  navigation,
  onCreateTaskIssue,
  onCreatePixelIssue,
  openIssueCount,
  openIssueCountLoading,
  openIssueCountError,
  allowProjectIssueScope,
  maskQc,
  annotationId,
  taskId,
  projectId,
  currentUserId,
  backgroundUrl,
  imageWidth,
  imageHeight,
  enableCanvasDrawing,
  enableTaskCanvasDrawing,
  liveCanvas,
  commentAnchor,
  onSeekFrame,
  annotationClassById,
  onSelectAnnotation,
  annotationDiscussionRequest,
  onAnnotationDiscussionRequestConsumed,
  onDetach,
  floating = false,
  collapsed: collapsedProp,
  onToggleCollapsed,
}: DiscussionPanelProps) {
  const id = useId();
  const [tab, setTab] = useState<DiscussionTab>(maskQc?.activeIssue ? "mask_qc" : "comments");
  const availableTabs = TABS.filter((t) => t.key !== "mask_qc" || maskQc);
  // v0.20.22 · 受控优先 (走 workbench.layout 持久), 缺省回落组件内会话态 (测试/独立使用)。
  const [collapsedLocal, setCollapsedLocal] = useState(false);
  const collapsed = collapsedProp ?? collapsedLocal;
  const toggleCollapsedLocal = useCallback(() => setCollapsedLocal((v) => !v), []);
  const toggleCollapsed = onToggleCollapsed ?? toggleCollapsedLocal;
  const [replyFocus, setReplyFocus] = useState<DiscussionReplyFocus | null>(null);
  const [commentFocus, setCommentFocus] = useState<DiscussionCommentFocus | null>(null);
  const [annotationReadRequest, setAnnotationReadRequest] =
    useState<DiscussionAnnotationReadRequest | null>(null);
  const consumedAnnotationRequestRef = useRef<string | null>(null);
  // Share the complete feed's cached total, independent of the active tab or
  // reading filter. This observer also receives existing mutation invalidations.
  const commentCountQuery = useTaskDiscussion(taskId, "all", null, Boolean(projectId), projectId);

  // The panel, not its conditionally mounted Issues tab, owns the activation
  // lifetime. Drafts have a separate authenticated owner and are not cleared.
  const detailOwner = JSON.stringify([currentUserId, projectId, taskId]);
  const detailOwnerRef = useRef(detailOwner);
  const detailLeaseRef = useRef<object | null>(null);
  useLayoutEffect(() => {
    detailLeaseRef.current = {};
    if (detailOwnerRef.current !== detailOwner) {
      useActiveIssueStore.getState().closeIssueDetail();
      setReplyFocus(null);
      setCommentFocus(null);
      setAnnotationReadRequest(null);
      detailOwnerRef.current = detailOwner;
    }
    return () => {
      detailLeaseRef.current = null;
      const { detailRequestTick, detailTargetId } = useActiveIssueStore.getState();
      queueMicrotask(() => {
        // StrictMode immediately reactivates this presentation. A real
        // unmount retires only the request that was present at cleanup, never
        // a newer navigation emitted by the replacement Workbench.
        if (detailLeaseRef.current) return;
        const state = useActiveIssueStore.getState();
        if (
          state.detailRequestTick === detailRequestTick &&
          state.detailTargetId === detailTargetId
        ) {
          state.closeIssueDetail();
        }
      });
    };
  }, [detailOwner]);

  useEffect(() => {
    const request = annotationDiscussionRequest;
    if (
      !request ||
      request.projectId !== projectId ||
      request.taskId !== taskId ||
      !request.annotationId
    )
      return;
    const requestKey = `${currentUserId ?? ""}:${request.projectId}:${request.taskId}:${request.requestId}`;
    if (consumedAnnotationRequestRef.current === requestKey) return;
    consumedAnnotationRequestRef.current = requestKey;
    setReplyFocus(null);
    setCommentFocus(null);
    setAnnotationReadRequest(request);
    setTab("comments");
    if (collapsed) toggleCollapsed();
    onAnnotationDiscussionRequestConsumed?.(request.requestId);
  }, [
    annotationDiscussionRequest,
    collapsed,
    onAnnotationDiscussionRequestConsumed,
    projectId,
    taskId,
    currentUserId,
    toggleCollapsed,
  ]);

  const navigationState = navigation?.state;
  useEffect(() => {
    if (
      !navigationState ||
      navigationState.status === "idle" ||
      navigationState.status === "complete"
    )
      return;
    if (collapsed) toggleCollapsed();
    if (navigationState.status !== "ready") return;
    const { target, requestId } = navigationState;
    if (target.kind === "issue" && projectId && taskId) {
      setCommentFocus(null);
      setAnnotationReadRequest(null);
      setTab("issues");
      useActiveIssueStore
        .getState()
        .openIssueDetail(navigationState.root ?? target.issueId, { projectId, taskId });
      setReplyFocus(
        target.replyId ? { requestId, rootId: target.issueId, replyId: target.replyId } : null,
      );
    } else if (target.kind === "comment") {
      setReplyFocus(null);
      setAnnotationReadRequest(null);
      setTab("comments");
      setCommentFocus({
        requestId,
        annotationId: target.annotationId,
        commentId: target.commentId,
        annotationLabel: navigationState.annotation?.class_name ?? "标注",
        canvasAvailable: navigationState.canvasAvailable === true,
      });
    } else if (target.kind === "task_comment") {
      setReplyFocus(null);
      setAnnotationReadRequest(null);
      setTab("comments");
      setCommentFocus({ requestId, commentId: target.commentId, source: "feedback" });
    }
    navigation?.consume(requestId);
    // Activation is keyed by the owner-produced state, not by callback or
    // presentation changes such as collapsing and reopening the panel.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [navigationState]);

  // v0.11.4 · 单击/hover IssueLayer 图钉 → store.tabRequestTick++ → 自动切到 issues tab。
  const tabRequestTick = useActiveIssueStore((s) => s.tabRequestTick);
  const lastTabRequestRef = useRef(tabRequestTick);
  useEffect(() => {
    if (tabRequestTick !== lastTabRequestRef.current) {
      lastTabRequestRef.current = tabRequestTick;
      setTab("issues");
      // v0.20.22 · IssueLayer 图钉切 tab 时若讨论区处于收起态, 顺手展开让用户看到 issue 列表。
      if (collapsed) toggleCollapsed();
    }
    // toggleCollapsed / collapsed 依赖漂移会引起循环触发, 故按 tick 一路径走。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tabRequestTick]);

  useEffect(() => {
    if (!maskQc && tab === "mask_qc") setTab("comments");
  }, [maskQc, tab]);

  useEffect(() => {
    if (!maskQc?.activeIssue) return;
    setTab("mask_qc");
    if (collapsed) toggleCollapsed();
    // An active QC navigation is an explicit request to expose this tab. The
    // collapsed callbacks are intentionally omitted to avoid replaying it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [maskQc?.activeIssue?.id]);

  return (
    <div
      className={`flex h-full min-h-0 flex-col bg-card ${floating ? "" : "border-t border-border"}`}
      data-workbench-discussion
    >
      <div className="flex shrink-0 items-center justify-between gap-1 px-2 pt-1.5">
        {!floating && (
          // v0.20.22 · 收起 chevron: 只在嵌入布局显示 (浮层已是独立窗口, 无收起语义)。
          <button
            type="button"
            onClick={toggleCollapsed}
            aria-expanded={!collapsed}
            title={collapsed ? "展开讨论" : "收起讨论"}
            data-testid="discussion-toggle-collapsed"
            className="inline-flex h-6 w-6 cursor-pointer appearance-none items-center justify-center rounded border-0 bg-transparent p-0 text-muted-foreground hover:bg-muted hover:text-foreground"
          >
            <Icon name={collapsed ? "chevRight" : "chevDown"} size={13} />
          </button>
        )}
        <div
          className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto"
          role="tablist"
          aria-label="讨论面板"
        >
          {availableTabs.map((t) => (
            <button
              key={t.key}
              type="button"
              role="tab"
              id={`${id}-tab-${t.key}`}
              aria-controls={`${id}-panel-${t.key}`}
              aria-selected={tab === t.key}
              tabIndex={tab === t.key ? 0 : -1}
              className={`${TAB_BUTTON_BASE} ${tab === t.key ? TAB_BUTTON_ACTIVE : TAB_BUTTON_INACTIVE}`}
              onClick={() => {
                setTab(t.key);
                if (collapsed) toggleCollapsed();
              }}
              onKeyDown={(event) => {
                if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
                event.preventDefault();
                event.stopPropagation();
                const index = availableTabs.findIndex((item) => item.key === t.key);
                const next =
                  event.key === "Home"
                    ? 0
                    : event.key === "End"
                      ? availableTabs.length - 1
                      : (index + (event.key === "ArrowRight" ? 1 : -1) + availableTabs.length) %
                        availableTabs.length;
                const nextTab = availableTabs[next].key;
                setTab(nextTab);
                if (collapsed) toggleCollapsed();
                document.getElementById(`${id}-tab-${nextTab}`)?.focus();
              }}
            >
              {t.label}
              {t.key === "comments" && projectId && taskId && (
                <DiscussionCountBadge
                  count={commentCountQuery.data?.pages[0]?.total ?? null}
                  loading={commentCountQuery.isPending}
                  error={commentCountQuery.isError}
                />
              )}
              {t.key === "issues" && (
                <DiscussionCountBadge
                  count={openIssueCount}
                  loading={openIssueCountLoading}
                  error={openIssueCountError}
                  issues
                />
              )}
            </button>
          ))}
        </div>
        {onDetach && (
          <button
            type="button"
            className="inline-flex h-6 w-6 cursor-pointer appearance-none items-center justify-center rounded border border-border bg-background p-0 text-muted-foreground hover:border-brand hover:text-brand"
            onClick={onDetach}
            title="分离讨论面板"
            aria-label="分离讨论面板"
          >
            <Icon name="pictureInPicture2" size={13} />
          </button>
        )}
      </div>
      {navigation &&
        navigationState &&
        ["loading", "error", "cancelled"].includes(navigationState.status) && (
          <div
            className={`mx-2 my-1 flex shrink-0 flex-wrap items-center gap-1.5 rounded px-2 py-1.5 text-xs ${navigationState.status === "error" ? "bg-status-danger-soft text-status-danger" : "bg-muted text-muted-foreground"}`}
            role={navigationState.status === "error" ? "alert" : "status"}
            data-testid="discussion-navigation-status"
          >
            {"message" in navigationState && (
              <span className="min-w-0 flex-1">
                {navigationState.status === "error" ? (
                  <CopyableError message={navigationState.message} />
                ) : (
                  navigationState.message
                )}
                {navigationState.status === "loading" && navigationState.checked > 0
                  ? `（已检查 ${navigationState.checked} 条）`
                  : ""}
              </span>
            )}
            {navigationState.status === "loading" ? (
              <Button size="sm" variant="ghost" onClick={navigation.cancel}>
                取消
              </Button>
            ) : (
              <>
                <Button size="sm" variant="ghost" onClick={navigation.retry}>
                  重试
                </Button>
                <Button size="sm" variant="ghost" onClick={navigation.dismiss}>
                  关闭
                </Button>
              </>
            )}
          </div>
        )}
      {/* v0.20.22 · 完全收起时不渲染 tabpanel, 仅留 tab 头一条; 展开由 chevron 或 IssueLayer 图钉触发。 */}
      {!collapsed && (
        <div
          className={`flex min-h-0 flex-1 flex-col ${tab === "mask_qc" ? "overflow-y-auto" : "overflow-hidden"}`}
          role="tabpanel"
          id={`${id}-panel-${tab}`}
          aria-labelledby={`${id}-tab-${tab}`}
        >
          {tab === "mask_qc" ? (
            maskQc ? (
              <MaskQcPanel {...maskQc} />
            ) : null
          ) : tab === "issues" ? (
            projectId && taskId ? (
              <DiscussionIssuesTab
                replyFocus={replyFocus}
                onReplyFocusHandled={(requestId) =>
                  setReplyFocus((current) => (current?.requestId === requestId ? null : current))
                }
                projectId={projectId}
                taskId={taskId}
                onCreateTaskIssue={onCreateTaskIssue}
                onCreatePixelIssue={onCreatePixelIssue}
                allowProjectScope={allowProjectIssueScope}
              />
            ) : null
          ) : (
            <CommentsPanel
              commentFocus={commentFocus}
              onCommentFocusHandled={(requestId) =>
                setCommentFocus((current) => (current?.requestId === requestId ? null : current))
              }
              annotationId={annotationId}
              taskId={taskId}
              projectId={projectId}
              currentUserId={currentUserId ?? undefined}
              backgroundUrl={backgroundUrl}
              imageWidth={imageWidth}
              imageHeight={imageHeight}
              enableCanvasDrawing={enableCanvasDrawing}
              enableTaskCanvasDrawing={enableTaskCanvasDrawing}
              liveCanvas={liveCanvas}
              commentAnchor={commentAnchor}
              onSeekFrame={onSeekFrame}
              annotationClassById={annotationClassById}
              onSelectAnnotation={onSelectAnnotation}
              annotationDiscussionRequest={annotationReadRequest}
              onAnnotationDiscussionRequestConsumed={(requestId) =>
                setAnnotationReadRequest((current) =>
                  current?.requestId === requestId ? null : current,
                )
              }
              hideTabs
              forceTab={tab}
            />
          )}
        </div>
      )}
    </div>
  );
}

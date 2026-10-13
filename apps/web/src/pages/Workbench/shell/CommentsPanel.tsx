import { FilterGroup, FilterSelect } from "@/components/filters/FilterControls";
import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ComponentProps,
} from "react";
import { useNavigate } from "react-router-dom";
import { Icon } from "@/components/ui/Icon";
import { useProjectMentionCandidates } from "@/hooks/useProjects";
import { CanvasDrawingPreview } from "@/components/CanvasDrawingEditor";
import { useHoveredCommentStore } from "../state/useHoveredCommentStore";
import {
  useAnnotationCommentsInfinite,
  useCreateComment,
  usePatchComment,
  useDeleteComment,
} from "@/hooks/useAnnotationComments";
import { useAnnotationAuditHistory, useTaskAuditHistory } from "@/hooks/useAnnotationAuditHistory";
import { AnnotationHistoryTimeline } from "@/components/AnnotationHistoryTimeline";
import { CommentInput, renderCommentBody } from "./CommentInput";
import type { UserPickerOption } from "@/components/UserPicker";
import {
  commentsApi,
  type AnnotationCommentAnchor,
  type CommentCanvasDrawing,
  type CommentMention,
} from "@/api/comments";
import { isCurrentAuthOwner } from "@/stores/authStore";
import {
  discussionItemKey,
  isFeedbackDiscussionItem,
  isAnnotationDiscussionItem,
  type TaskDiscussionItem,
} from "@/api/discussion";
import { useTaskDiscussion, flattenTaskDiscussion } from "@/hooks/useTaskDiscussion";
import { useCreateFeedback, usePatchFeedback, useDeleteFeedback } from "@/hooks/useFeedbacks";
import type {
  DiscussionPayload,
  DiscussionReadScope,
  DiscussionTarget,
} from "../state/discussionTypes";
import { discussionTargetKey } from "../state/discussionTypes";
import {
  useDiscussionDraftSnapshot,
  useDiscussionDraftStore,
} from "../state/DiscussionDraftProvider";
import type { DiscussionDraftStore } from "../state/useDiscussionDraftStore";
import type { DiscussionCommentFocus } from "../state/useDiscussionNavigation";

type Tab = "comments" | "history";
type CommentInputProps = ComponentProps<typeof CommentInput>;
type CommentSubmit = NonNullable<CommentInputProps["onSubmit"]>;
type LiveCanvas = CommentInputProps["liveCanvas"];

export interface DiscussionAnnotationReadRequest {
  requestId: string;
  projectId: string;
  taskId: string;
  annotationId: string;
}

function cn(...parts: Array<string | false | null | undefined>) {
  return parts.filter(Boolean).join(" ");
}

const TAB_BUTTON =
  "cursor-pointer appearance-none border-0 border-b-2 border-transparent bg-transparent px-2 py-1 text-xs font-semibold text-muted-foreground [font:inherit]";
const TAB_BUTTON_ACTIVE = "border-brand text-foreground";
const ICON_BUTTON =
  "inline-flex min-h-7 min-w-7 cursor-pointer appearance-none items-center justify-center rounded-[3px] border-0 bg-transparent text-muted-foreground active:scale-[0.96]";

interface Props {
  commentFocus?: DiscussionCommentFocus | null;
  onCommentFocusHandled?: (requestId: string) => void;
  annotationId: string | null;
  /** Task context enables the authoritative mixed discussion feed. */
  taskId?: string | null;
  /** Legacy annotation-only callers can still provide task ownership for drafts. */
  annotationTaskId?: string | null;
  projectId?: string | null;
  currentUserId?: string;
  backgroundUrl?: string | null;
  imageWidth?: number | null;
  imageHeight?: number | null;
  enableCanvasDrawing?: boolean;
  /** Image tasks may draw on the task target; video task comments stay text-only. */
  enableTaskCanvasDrawing?: boolean;
  liveCanvas?: LiveCanvas;
  commentAnchor?: AnnotationCommentAnchor | null;
  onSeekFrame?: (frameIndex: number) => void;
  annotationClassById?: Record<string, string | undefined>;
  onSelectAnnotation?: (annotationId: string) => void;
  hideTabs?: boolean;
  forceTab?: Tab;
  /** Coordinator can mark a selected annotation unavailable without retargeting its draft. */
  annotationAvailable?: boolean;
  annotationUnavailableReason?: string | null;
  /** One-shot badge activation after the shell has guarded annotation selection. */
  annotationDiscussionRequest?: DiscussionAnnotationReadRequest | null;
  onAnnotationDiscussionRequestConsumed?: (requestId: string) => void;
}

function anchorLabel(anchor: AnnotationCommentAnchor): string {
  const parts = [`F${anchor.frameIndex}`];
  if (anchor.trackId) parts.push(anchor.trackId.slice(0, 8));
  if (anchor.source) parts.push(anchor.source);
  return parts.join(" · ");
}

/** IDs from prediction/candidate/temporary selections must never reach annotation APIs. */
function isPersistedAnnotationId(id: string | null | undefined): id is string {
  if (!id) return false;
  return !/^(?:pred(?:iction)?|temp(?:orary)?|candidate|ai)[-_:/]/i.test(id);
}

function annotationIsKnown(
  annotationId: string | null | undefined,
  annotationClassById: Record<string, string | undefined> | undefined,
): boolean {
  // The coordinator leaves this map undefined while annotations are loading.
  // An empty, defined map means loading completed and the id is unavailable.
  return (
    annotationClassById === undefined ||
    (annotationId !== null &&
      annotationId !== undefined &&
      Object.prototype.hasOwnProperty.call(annotationClassById, annotationId))
  );
}

type CommentDiscussionTarget = Extract<DiscussionTarget, { kind: "task" | "annotation" }>;

function isCommentTarget(
  target: DiscussionTarget | null | undefined,
): target is CommentDiscussionTarget {
  return target?.kind === "task" || target?.kind === "annotation";
}

function targetBelongsToTask(
  target: DiscussionTarget | null | undefined,
  projectId: string | null | undefined,
  taskId: string | null | undefined,
): target is CommentDiscussionTarget {
  return Boolean(
    target && projectId && taskId && target.projectId === projectId && target.taskId === taskId,
  );
}

function formatAttachment(attachment: unknown): {
  key: string | null;
  name: string;
  size: number | null;
} {
  if (!attachment || typeof attachment !== "object") {
    return { key: null, name: "附件", size: null };
  }
  const value = attachment as Record<string, unknown>;
  const key =
    typeof value.storageKey === "string"
      ? value.storageKey
      : typeof value.storage_key === "string"
        ? value.storage_key
        : null;
  const name =
    typeof value.fileName === "string"
      ? value.fileName
      : typeof value.file_name === "string"
        ? value.file_name
        : "附件";
  const size = typeof value.size === "number" ? value.size : null;
  return { key, name, size };
}

type AttachmentDownloadState = { pending: boolean; error: string | null };

function attachmentErrorMessage(error: unknown): string {
  const status =
    error && typeof error === "object" && "status" in error
      ? (error as { status?: unknown }).status
      : undefined;
  const detail = errorMessage(error);
  return typeof status === "number" ? `HTTP ${status}${detail ? `：${detail}` : ""}` : detail;
}

function feedbackStatusLabel(status: string): string {
  if (status === "resolved") return "已解决";
  if (status === "wont_fix") return "已搁置";
  return "待处理";
}

function legacyActions(authorId: string, currentUserId: string | undefined) {
  const isMine = !!currentUserId && authorId === currentUserId;
  return {
    edit: isMine,
    change_status: isMine,
    delete: isMine,
    reply: false,
  };
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function isTargetUnavailableError(error: unknown): boolean {
  if (!error || typeof error !== "object") return false;
  const status = (error as { status?: unknown }).status;
  return status === 403 || status === 404;
}

export function CommentsPanel({
  commentFocus,
  onCommentFocusHandled,
  annotationId,
  taskId,
  annotationTaskId,
  projectId,
  currentUserId,
  backgroundUrl,
  imageWidth,
  imageHeight,
  enableCanvasDrawing,
  enableTaskCanvasDrawing = false,
  liveCanvas,
  commentAnchor,
  onSeekFrame,
  annotationClassById,
  onSelectAnnotation,
  hideTabs,
  forceTab,
  annotationAvailable,
  annotationUnavailableReason,
  annotationDiscussionRequest,
  onAnnotationDiscussionRequestConsumed,
}: Props) {
  const navigate = useNavigate();
  const panelId = useId();
  const [localTab, setTab] = useState<Tab>("comments");
  const tab = forceTab ?? localTab;
  const [readScope, setReadScope] = useState<DiscussionReadScope>("all");
  const [scopeNotice, setScopeNotice] = useState<string | null>(null);
  const [localSendTarget, setLocalSendTarget] = useState<DiscussionTarget | null>(null);
  const [rowActionStates, setRowActionStates] = useState<
    Record<string, { pending: boolean; error: string | null }>
  >({});
  const [attachmentDownloadStates, setAttachmentDownloadStates] = useState<
    Record<string, AttachmentDownloadState>
  >({});
  const [locallyUnavailableTargets, setLocallyUnavailableTargets] = useState<
    Record<string, string>
  >({});
  const rowActionPendingRef = useRef(new Set<string>());
  const attachmentDownloadPendingRef = useRef(new Set<string>());
  const taskScopeKey = `${projectId ?? ""}:${taskId ?? ""}`;
  const [readAnnotationOverride, setReadAnnotationOverride] = useState<{
    owner: string;
    selectedId: string | null;
    annotationId: string;
    label: string;
  } | null>(null);
  const [highlightedComment, setHighlightedComment] = useState<string | null>(null);
  const focusedRequestRef = useRef<string | null>(null);
  const consumedAnnotationRequestRef = useRef<string | null>(null);
  const contentRef = useRef<HTMLDivElement | null>(null);
  const scopeOwnerRef = useRef(taskScopeKey);
  const scopeForTask = scopeOwnerRef.current === taskScopeKey ? readScope : "all";
  // Keep the first render after a retained-panel task switch on the default
  // task scope. The effect below commits the reset for subsequent renders.

  const annotationEligible =
    isPersistedAnnotationId(annotationId) &&
    annotationAvailable !== false &&
    annotationIsKnown(annotationId, annotationClassById);
  const readOverride =
    readAnnotationOverride?.owner === taskScopeKey &&
    readAnnotationOverride.selectedId === annotationId
      ? readAnnotationOverride
      : null;
  const readAnnotationId = readOverride?.annotationId ?? annotationId;
  const readAnnotationEligible = Boolean(readOverride) || annotationEligible;
  const taskContext = Boolean(taskId && projectId);
  const composerTaskId = taskId ?? annotationTaskId;
  const taskTarget = useMemo<Extract<DiscussionTarget, { kind: "task" }> | null>(
    () => (taskId && projectId ? { projectId, taskId, kind: "task" } : null),
    [projectId, taskId],
  );
  const currentAnnotationTarget = useMemo<Extract<DiscussionTarget, { kind: "annotation" }> | null>(
    () =>
      annotationEligible && taskId && projectId
        ? { projectId, taskId, kind: "annotation", annotationId }
        : null,
    [annotationEligible, annotationId, projectId, taskId],
  );

  const draftStore = useDiscussionDraftStore();
  // Subscribe even though the snapshot value is not otherwise needed here:
  // changing the persisted destination must re-render the controlled select.
  useDiscussionDraftSnapshot();
  const storedSendTarget =
    draftStore && projectId && taskId
      ? (() => {
          const stored = draftStore.getSendTarget(projectId, taskId);
          return isCommentTarget(stored) ? stored : undefined;
        })()
      : undefined;

  useLayoutEffect(() => {
    if (draftStore && projectId && taskId) {
      draftStore.followAnnotationSelection(
        projectId,
        taskId,
        currentAnnotationTarget?.annotationId ?? null,
      );
    } else {
      setLocalSendTarget(currentAnnotationTarget ?? taskTarget);
    }
  }, [currentAnnotationTarget, draftStore, projectId, taskId, taskTarget]);

  useLayoutEffect(() => {
    // Reading scope is a task-local view preference. A new task starts at the
    // complete discussion feed even when the panel instance is retained.
    scopeOwnerRef.current = taskScopeKey;
    setReadScope("all");
    setScopeNotice(null);
    setReadAnnotationOverride(null);
    setHighlightedComment(null);
  }, [taskScopeKey]);

  useEffect(() => {
    const request = annotationDiscussionRequest;
    if (
      !request ||
      !taskContext ||
      request.projectId !== projectId ||
      request.taskId !== taskId ||
      request.annotationId !== annotationId ||
      !isPersistedAnnotationId(request.annotationId)
    )
      return;
    const requestKey = `${currentUserId ?? ""}:${taskScopeKey}:${request.requestId}`;
    if (consumedAnnotationRequestRef.current === requestKey) return;
    consumedAnnotationRequestRef.current = requestKey;
    setReadScope("annotation");
    setReadAnnotationOverride({
      owner: taskScopeKey,
      selectedId: annotationId,
      annotationId: request.annotationId,
      label: annotationClassById?.[request.annotationId] ?? "标注",
    });
    setScopeNotice(null);
    setHighlightedComment(null);
    onAnnotationDiscussionRequestConsumed?.(request.requestId);
  }, [
    annotationClassById,
    annotationDiscussionRequest,
    annotationId,
    currentUserId,
    onAnnotationDiscussionRequestConsumed,
    projectId,
    taskContext,
    taskId,
    taskScopeKey,
  ]);

  useEffect(() => {
    if (!commentFocus || focusedRequestRef.current === commentFocus.requestId) return;
    if (commentFocus.source === "feedback") {
      setReadScope("task");
      setReadAnnotationOverride(null);
      setScopeNotice(null);
      return;
    }
    setReadScope("annotation");
    setReadAnnotationOverride({
      owner: taskScopeKey,
      selectedId: annotationId,
      annotationId: commentFocus.annotationId,
      label: commentFocus.annotationLabel,
    });
    setScopeNotice(
      commentFocus.canvasAvailable
        ? null
        : "评论位于当前画布未载入的标注，已打开原评论；不会自动认领视频分段。",
    );
  }, [commentFocus, annotationId, taskScopeKey]);

  useEffect(() => {
    if (commentFocus && focusedRequestRef.current !== commentFocus.requestId) return;
    if (scopeForTask === "annotation" && !readAnnotationEligible) {
      setReadScope("task");
      setScopeNotice("当前没有可用的已保存标注，已切换为仅任务留言。");
    }
  }, [commentFocus, readAnnotationEligible, scopeForTask]);

  useEffect(() => {
    if (readAnnotationOverride && readAnnotationOverride.selectedId !== annotationId) {
      setReadAnnotationOverride(null);
      setScopeNotice(null);
      setHighlightedComment(null);
    }
  }, [annotationId, readAnnotationOverride]);

  const effectiveScope: DiscussionReadScope =
    scopeForTask === "annotation" && readAnnotationEligible ? "annotation" : scopeForTask;

  const taskDiscussionQuery = useTaskDiscussion(
    taskId,
    effectiveScope,
    effectiveScope === "annotation" ? readAnnotationId : null,
    taskContext && (effectiveScope !== "annotation" || readAnnotationEligible),
    projectId,
  );
  // Standalone ReviewWorkbench has no task context and keeps its legacy bounded
  // annotation reader. Passing null for every other case prevents candidate IDs
  // from reaching annotation-comment endpoints.
  const legacyAnnotationQuery = useAnnotationCommentsInfinite(
    !taskContext && annotationEligible ? annotationId : null,
  );

  const discussionItems = useMemo<TaskDiscussionItem[]>(() => {
    if (taskContext) return flattenTaskDiscussion(taskDiscussionQuery.data);
    return (legacyAnnotationQuery.data?.pages ?? []).flatMap((page) =>
      page.items.map((data) => ({
        source: "annotation_comment" as const,
        data,
        actions: legacyActions(data.author_id, currentUserId),
      })),
    );
  }, [currentUserId, legacyAnnotationQuery.data, taskContext, taskDiscussionQuery.data]);

  const activeQuery = taskContext ? taskDiscussionQuery : legacyAnnotationQuery;
  useLayoutEffect(() => {
    const taskCommentFocus = commentFocus?.source === "feedback";
    if (
      !commentFocus ||
      focusedRequestRef.current === commentFocus.requestId ||
      (taskCommentFocus
        ? effectiveScope !== "task"
        : effectiveScope !== "annotation" ||
          readAnnotationId !==
            ("annotationId" in commentFocus ? commentFocus.annotationId : null)) ||
      activeQuery.isPending ||
      activeQuery.isError
    )
      return;
    const container = contentRef.current;
    const row = container?.querySelector<HTMLElement>(
      `[data-comment-key="${taskCommentFocus ? "feedback" : "annotation_comment"}:${commentFocus.commentId}"]`,
    );
    if (!container || !row) return;
    focusedRequestRef.current = commentFocus.requestId;
    setHighlightedComment(
      `${taskCommentFocus ? "feedback" : "annotation_comment"}:${commentFocus.commentId}`,
    );
    container.scrollTop += row.getBoundingClientRect().top - container.getBoundingClientRect().top;
    row.focus({ preventScroll: true });
    onCommentFocusHandled?.(commentFocus.requestId);
  }, [
    commentFocus,
    effectiveScope,
    readAnnotationId,
    activeQuery.isPending,
    activeQuery.isError,
    discussionItems,
    onCommentFocusHandled,
  ]);
  const total = taskContext ? taskDiscussionQuery.data?.pages[0]?.total : discussionItems.length;

  // 后端按 owner → super_admin → member 去重返回，前端只负责展示标签。
  const { data: mentionCandidates } = useProjectMentionCandidates(projectId ?? "");
  const memberOptions = useMemo<UserPickerOption[]>(
    () =>
      (mentionCandidates ?? []).map((candidate) => ({
        id: candidate.user_id,
        name: candidate.user_name,
        email: candidate.user_email ?? null,
        hint:
          candidate.kind === "owner"
            ? "项目负责人"
            : candidate.kind === "super_admin"
              ? "超级管理员"
              : undefined,
      })),
    [mentionCandidates],
  );

  const feedbackParams = useMemo(
    () => ({
      project_id: projectId ?? "",
      task_id: taskId ?? undefined,
      kind: "comment" as const,
      anchor_type: "task" as const,
    }),
    [projectId, taskId],
  );
  const taskLocalSendTarget = targetBelongsToTask(localSendTarget, projectId, taskId)
    ? localSendTarget
    : null;
  const sendTarget = useMemo<DiscussionTarget | null>(
    () =>
      (storedSendTarget ?? taskLocalSendTarget ?? taskTarget) ||
      (annotationId && !taskContext
        ? ({
            projectId: projectId ?? "",
            taskId: composerTaskId ?? "",
            kind: "annotation",
            annotationId,
          } as DiscussionTarget)
        : null),
    [
      annotationId,
      composerTaskId,
      projectId,
      storedSendTarget,
      taskContext,
      taskLocalSendTarget,
      taskTarget,
    ],
  );

  const sendTargetAnnotationAvailable =
    sendTarget?.kind === "annotation"
      ? locallyUnavailableTargets[discussionTargetKey(sendTarget)]
        ? false
        : sendTarget.annotationId === annotationId && annotationAvailable !== undefined
          ? annotationAvailable
          : annotationClassById === undefined
            ? undefined
            : Object.prototype.hasOwnProperty.call(annotationClassById, sendTarget.annotationId)
      : undefined;
  const sendTargetUnavailableReason =
    sendTargetAnnotationAvailable === false
      ? sendTarget?.kind === "annotation" && sendTarget.annotationId === annotationId
        ? (locallyUnavailableTargets[discussionTargetKey(sendTarget)] ??
          annotationUnavailableReason)
        : "该标注已不可用，请返回任务留言。"
      : undefined;

  const createCommentMut = useCreateComment(
    sendTarget?.kind === "annotation" ? sendTarget.annotationId : null,
    composerTaskId,
  );
  const patchCommentMut = usePatchComment(annotationId, composerTaskId);
  const deleteCommentMut = useDeleteComment(annotationId, composerTaskId);
  const createTaskFeedbackMut = useCreateFeedback(feedbackParams);
  const patchTaskFeedbackMut = usePatchFeedback(feedbackParams);
  const deleteTaskFeedbackMut = useDeleteFeedback(feedbackParams);

  const setHoveredShapes = useHoveredCommentStore((state) => state.setHover);
  const togglePinnedComment = useHoveredCommentStore((state) => state.togglePin);
  const clearPinnedComment = useHoveredCommentStore((state) => state.clearPin);
  const pinnedCommentId = useHoveredCommentStore((state) => state.pinnedId);
  const setComposingShapes = useHoveredCommentStore((state) => state.setComposing);

  const activeAnnotationComposer =
    sendTarget?.kind === "annotation" && sendTarget.annotationId === annotationId;
  // Popup drawing belongs to the explicit destination, not current selection.
  // Restored annotation drafts remain editable when SPA return clears selection;
  // the live stage/hover bridge retains its stricter selected-object boundary.
  const popupAnnotationComposer =
    sendTarget?.kind === "annotation" && sendTargetAnnotationAvailable !== false;
  const taskCanvasComposer =
    sendTarget?.kind === "task" && Boolean(enableTaskCanvasDrawing && enableCanvasDrawing);
  const canvasDrawingTargetEnabled = Boolean(popupAnnotationComposer || taskCanvasComposer);
  const reportPendingDrawing = useCallback(
    (drawing: CommentCanvasDrawing | null) =>
      setComposingShapes(
        (activeAnnotationComposer || taskCanvasComposer) &&
          drawing?.shapes &&
          drawing.shapes.length > 0
          ? drawing.shapes
          : null,
      ),
    [activeAnnotationComposer, setComposingShapes, taskCanvasComposer],
  );

  useEffect(() => {
    clearPinnedComment();
    setComposingShapes(null);
    return () => {
      clearPinnedComment();
      setComposingShapes(null);
    };
  }, [annotationId, clearPinnedComment, setComposingShapes, taskId]);

  const annotationHistoryQuery = useAnnotationAuditHistory(
    tab === "history" && annotationEligible ? annotationId : null,
  );
  const taskHistoryQuery = useTaskAuditHistory(
    tab === "history" && !annotationEligible ? (taskId ?? null) : null,
    tab === "history" && !annotationEligible,
  );
  const history = annotationEligible ? annotationHistoryQuery.data : taskHistoryQuery.data;
  const historyLoading = annotationEligible
    ? annotationHistoryQuery.isLoading
    : taskHistoryQuery.isLoading;
  const historyError = annotationEligible ? annotationHistoryQuery.error : taskHistoryQuery.error;
  const retryHistory = annotationEligible
    ? annotationHistoryQuery.refetch
    : taskHistoryQuery.refetch;

  const sendTargetOptions = useMemo(() => {
    const options: CommentDiscussionTarget[] = [];
    if (taskTarget) options.push(taskTarget);
    const knownTargets: CommentDiscussionTarget[] = taskTarget
      ? Object.keys(annotationClassById ?? {})
          .filter(isPersistedAnnotationId)
          .map((id) => ({ ...taskTarget, kind: "annotation", annotationId: id }))
      : [];
    const candidateTargets = [
      currentAnnotationTarget,
      ...knownTargets,
      storedSendTarget,
      taskLocalSendTarget,
    ];
    const seen = new Set<string>();
    for (const candidate of candidateTargets) {
      if (!candidate || candidate.kind !== "annotation") continue;
      const key = discussionTargetKey(candidate);
      if (seen.has(key)) continue;
      seen.add(key);
      options.push(candidate);
    }
    return options;
  }, [
    annotationClassById,
    currentAnnotationTarget,
    storedSendTarget,
    taskLocalSendTarget,
    taskTarget,
  ]);

  const targetLabel = useCallback(
    (target: CommentDiscussionTarget) => {
      if (target.kind === "task") return "当前任务";
      const className = annotationClassById?.[target.annotationId];
      const isCurrent = target.annotationId === annotationId;
      return `${isCurrent ? "当前标注" : "标注"}${className ? ` · ${className}` : ""} · ${target.annotationId.slice(0, 8)}`;
    },
    [annotationClassById, annotationId],
  );

  const handleSendTargetChange = useCallback(
    (value: string) => {
      const next = sendTargetOptions.find((target) => discussionTargetKey(target) === value);
      if (!next) return;
      setLocalSendTarget(next);
      if (draftStore && projectId && taskId) draftStore.setSendTarget(projectId, taskId, next);
    },
    [draftStore, projectId, sendTargetOptions, taskId],
  );

  const handleReturnToTask = useCallback(() => {
    if (!taskTarget) return;
    setLocalSendTarget(taskTarget);
    if (draftStore && projectId && taskId) draftStore.setSendTarget(projectId, taskId, taskTarget);
  }, [draftStore, projectId, taskId, taskTarget]);

  const clearShapesPreviewFor = useCallback(
    (item: TaskDiscussionItem) => {
      const key = discussionItemKey(item);
      setHoveredShapes(null);
      if (pinnedCommentId === key) clearPinnedComment();
    },
    [clearPinnedComment, pinnedCommentId, setHoveredShapes],
  );

  const handleSubmit = useCallback<CommentSubmit>(
    (payload: DiscussionPayload, snapshot) => {
      // Session-backed CommentInput captures the immutable target before its
      // first await. Never retarget a late completion to the currently visible
      // composer; legacy adapters have no snapshot and use the live target.
      const submissionTarget = snapshot?.target ?? sendTarget;
      if (!isCommentTarget(submissionTarget)) return;
      if (submissionTarget.kind === "annotation") {
        if (!isPersistedAnnotationId(submissionTarget.annotationId)) return;
        const request = createCommentMut.mutateAsync({
          annotationId: submissionTarget.annotationId,
          taskId: submissionTarget.taskId,
          payload: {
            body: payload.body,
            mentions: payload.mentions,
            attachments: payload.attachments,
            canvas_drawing: payload.canvas_drawing,
            anchor: payload.anchor,
          },
        });
        return request.catch((error) => {
          if (isTargetUnavailableError(error)) {
            const key = discussionTargetKey(submissionTarget);
            const reason = errorMessage(error);
            setLocallyUnavailableTargets((previous) => ({ ...previous, [key]: reason }));
            try {
              draftStore?.setTargetAvailability(submissionTarget, false, reason);
            } catch {
              // An auth lease may retire the old store while the request fails.
            }
          }
          throw error;
        });
      }
      // Keep a second guard at the write owner so a legacy adapter cannot
      // silently drop structured payload data. Image task comments may carry
      // mentions and, when enabled, their drawing.
      if (
        payload.attachments.length > 0 ||
        (payload.canvas_drawing &&
          (submissionTarget.kind !== "task" || !enableTaskCanvasDrawing || !enableCanvasDrawing)) ||
        payload.anchor
      ) {
        throw new Error("当前任务留言不支持该内容");
      }
      if (!submissionTarget.projectId || !submissionTarget.taskId) return;
      return createTaskFeedbackMut.mutateAsync({
        kind: "comment",
        anchor_type: "task",
        project_id: submissionTarget.projectId,
        task_id: submissionTarget.taskId,
        body: payload.body,
        mentions: payload.mentions,
        canvas_drawing: payload.canvas_drawing,
      });
    },
    [
      createCommentMut,
      createTaskFeedbackMut,
      draftStore,
      enableCanvasDrawing,
      enableTaskCanvasDrawing,
      sendTarget,
    ],
  );

  const mutationScopeKey = JSON.stringify([
    draftStore?.owner?.sessionId ?? null,
    currentUserId ?? draftStore?.owner?.userId ?? null,
    projectId ?? null,
    composerTaskId ?? null,
  ]);
  const handleAttachmentDownload = useCallback(
    async (rowKey: string, rowAnnotationId: string, storageKey: string, fileName: string) => {
      const stateKey = `${mutationScopeKey}:${rowKey}:${storageKey}`;
      if (attachmentDownloadPendingRef.current.has(stateKey)) return;

      // Capture the owner before the first await. A retained panel may finish
      // this flow after logout, account replacement, or a task switch.
      const capturedStore = draftStore;
      const capturedOwner = capturedStore?.getOwner() ?? null;
      const capturedUserId = currentUserId;
      const isOwnerCurrent = () => {
        try {
          return capturedStore && capturedOwner
            ? capturedStore.isOwned(capturedOwner)
            : Boolean(capturedUserId && isCurrentAuthOwner(capturedUserId));
        } catch {
          return false;
        }
      };

      if (!isOwnerCurrent()) return;
      attachmentDownloadPendingRef.current.add(stateKey);
      setAttachmentDownloadStates((previous) => ({
        ...previous,
        [stateKey]: { pending: true, error: null },
      }));

      try {
        const signed = await commentsApi.attachmentDownloadUrl(rowAnnotationId, storageKey);
        if (!isOwnerCurrent()) return;
        if (
          !signed ||
          typeof signed.download_url !== "string" ||
          signed.download_url.length === 0
        ) {
          throw new Error("下载地址无效");
        }

        const response = await fetch(signed.download_url, { credentials: "omit" });
        if (!isOwnerCurrent()) return;
        if (!response.ok) {
          const error = new Error(`HTTP ${response.status}`) as Error & { status: number };
          error.status = response.status;
          throw error;
        }

        const blob = await response.blob();
        if (!isOwnerCurrent()) return;
        if (typeof URL.createObjectURL !== "function") {
          throw new Error("当前浏览器不支持附件下载");
        }
        if (!isOwnerCurrent()) return;

        const objectUrl = URL.createObjectURL(blob);
        let anchor: HTMLAnchorElement | null = null;
        try {
          if (!isOwnerCurrent()) return;
          anchor = document.createElement("a");
          anchor.href = objectUrl;
          anchor.download = fileName || "附件";
          anchor.style.display = "none";
          document.body.appendChild(anchor);
          if (!isOwnerCurrent()) return;
          anchor.click();
        } finally {
          anchor?.remove();
          if (typeof URL.revokeObjectURL === "function") URL.revokeObjectURL(objectUrl);
        }
      } catch (error) {
        if (!isOwnerCurrent()) return;
        setAttachmentDownloadStates((previous) => ({
          ...previous,
          [stateKey]: { pending: false, error: attachmentErrorMessage(error) },
        }));
      } finally {
        attachmentDownloadPendingRef.current.delete(stateKey);
        if (isOwnerCurrent()) {
          setAttachmentDownloadStates((previous) => {
            const current = previous[stateKey];
            if (!current) return previous;
            return { ...previous, [stateKey]: { ...current, pending: false } };
          });
        }
      }
    },
    [currentUserId, draftStore, mutationScopeKey],
  );

  const handleToggleStatus = useCallback(
    async (item: TaskDiscussionItem) => {
      if (!item.actions.change_status) return;
      const key = `${mutationScopeKey}:${discussionItemKey(item)}`;
      if (rowActionPendingRef.current.has(key)) return;
      rowActionPendingRef.current.add(key);
      setRowActionStates((previous) => ({
        ...previous,
        [key]: { pending: true, error: null },
      }));
      try {
        if (isAnnotationDiscussionItem(item)) {
          await patchCommentMut.mutateAsync({
            id: item.data.id,
            payload: { is_resolved: !item.data.is_resolved },
            annotationId: item.data.annotation_id,
            taskId: composerTaskId,
          });
        } else {
          await patchTaskFeedbackMut.mutateAsync({
            id: item.data.id,
            payload: { status: item.data.status === "resolved" ? "open" : "resolved" },
          });
        }
      } catch (error) {
        setRowActionStates((previous) => ({
          ...previous,
          [key]: { pending: false, error: errorMessage(error) },
        }));
      } finally {
        rowActionPendingRef.current.delete(key);
        setRowActionStates((previous) => {
          const current = previous[key];
          if (!current || current.error) return previous;
          const next = { ...previous };
          delete next[key];
          return next;
        });
      }
    },
    [composerTaskId, mutationScopeKey, patchCommentMut, patchTaskFeedbackMut],
  );

  const handleDelete = useCallback(
    async (item: TaskDiscussionItem) => {
      if (!item.actions.delete) return;
      const key = `${mutationScopeKey}:${discussionItemKey(item)}`;
      if (rowActionPendingRef.current.has(key)) return;
      rowActionPendingRef.current.add(key);
      setRowActionStates((previous) => ({
        ...previous,
        [key]: { pending: true, error: null },
      }));
      clearShapesPreviewFor(item);
      try {
        if (isAnnotationDiscussionItem(item)) {
          await deleteCommentMut.mutateAsync({
            id: item.data.id,
            annotationId: item.data.annotation_id,
            taskId: composerTaskId,
          });
        } else {
          await deleteTaskFeedbackMut.mutateAsync(item.data.id);
        }
      } catch (error) {
        setRowActionStates((previous) => ({
          ...previous,
          [key]: { pending: false, error: errorMessage(error) },
        }));
      } finally {
        rowActionPendingRef.current.delete(key);
        setRowActionStates((previous) => {
          const current = previous[key];
          if (!current || current.error) return previous;
          const next = { ...previous };
          delete next[key];
          return next;
        });
      }
    },
    [
      clearShapesPreviewFor,
      composerTaskId,
      deleteCommentMut,
      deleteTaskFeedbackMut,
      mutationScopeKey,
    ],
  );

  if (!annotationId && !taskId) return null;

  const commentsContentClass =
    tab === "history"
      ? "flex min-h-0 flex-1 flex-col overflow-hidden"
      : "flex min-h-0 flex-1 flex-col overflow-y-auto";
  const commentsTabId = `${panelId}-comments-tab`;
  const historyTabId = `${panelId}-history-tab`;
  const commentsPanelId = `${panelId}-comments-panel`;
  const historyPanelId = `${panelId}-history-panel`;
  const contentPanelId = tab === "comments" ? commentsPanelId : historyPanelId;
  const activeTargetKey = sendTarget ? discussionTargetKey(sendTarget) : "";

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-2 border-t border-border px-3 py-2.5">
      {!hideTabs && (
        <div className="flex shrink-0 items-center gap-1" role="tablist" aria-label="讨论内容">
          <button
            type="button"
            role="tab"
            id={commentsTabId}
            aria-selected={tab === "comments"}
            aria-controls={commentsPanelId}
            onClick={() => setTab("comments")}
            className={cn(TAB_BUTTON, tab === "comments" && TAB_BUTTON_ACTIVE)}
          >
            评论{total !== undefined && ` (${total})`}
          </button>
          <button
            type="button"
            role="tab"
            id={historyTabId}
            aria-selected={tab === "history"}
            aria-controls={historyPanelId}
            onClick={() => setTab("history")}
            className={cn(TAB_BUTTON, tab === "history" && TAB_BUTTON_ACTIVE)}
          >
            历史
          </button>
        </div>
      )}

      {tab === "comments" && taskContext && (
        <FilterGroup compact label="阅读范围" className="shrink-0">
          <FilterSelect
            compact
            aria-label="评论阅读范围"
            value={effectiveScope}
            onChange={(event) => {
              const next = event.target.value as DiscussionReadScope;
              if (next === "annotation" && !readAnnotationEligible) {
                setReadScope("task");
                setScopeNotice("当前没有可用的已保存标注，已切换为仅任务留言。");
                return;
              }
              setReadScope(next);
              setScopeNotice(null);
              if (next !== "annotation") setReadAnnotationOverride(null);
            }}
            className="flex-1"
          >
            <option value="all">本任务全部讨论</option>
            <option value="task">仅任务留言</option>
            {readAnnotationEligible && (
              <option value="annotation">
                {readOverride && readOverride.annotationId !== annotationId
                  ? "通知中的标注"
                  : "当前标注"}
              </option>
            )}
          </FilterSelect>
        </FilterGroup>
      )}

      {scopeNotice && tab === "comments" && (
        <div className="shrink-0 text-xs text-status-caution" role="status">
          {scopeNotice}
        </div>
      )}

      <div
        ref={contentRef}
        id={contentPanelId}
        role={hideTabs ? "region" : "tabpanel"}
        aria-label={tab === "comments" ? "评论列表" : "历史"}
        {...(!hideTabs && {
          "aria-labelledby": tab === "comments" ? commentsTabId : historyTabId,
        })}
        className={commentsContentClass}
      >
        {tab === "history" ? (
          <AnnotationHistoryTimeline
            entries={history?.entries ?? []}
            loading={historyLoading}
            error={historyError}
            onRetry={() => void retryHistory()}
            presentation={taskContext ? "fill" : "bounded"}
            scopeLabel={annotationEligible ? "当前标注历史" : "本任务历史"}
          />
        ) : activeQuery.isPending ? (
          <div
            className="flex min-h-24 items-center justify-center text-xs text-muted-foreground"
            role="status"
          >
            正在加载讨论…
          </div>
        ) : activeQuery.isError ? (
          <div
            className="flex min-h-24 flex-col items-center justify-center gap-2 px-2 text-center text-xs text-status-danger"
            role="alert"
          >
            <span>无法加载讨论，请重试。</span>
            <button
              type="button"
              onClick={() => void activeQuery.refetch()}
              className="min-h-8 cursor-pointer rounded border border-border bg-transparent px-2.5 text-xs text-brand [font:inherit] active:scale-[0.96]"
            >
              重试
            </button>
          </div>
        ) : !taskContext && !annotationEligible ? (
          <div className="px-2 py-3 text-xs text-muted-foreground" role="status">
            当前选择不是可评论的已保存标注。
          </div>
        ) : discussionItems.length === 0 ? (
          <div className="px-2 py-3 text-xs text-muted-foreground/80" role="status">
            暂无讨论
          </div>
        ) : (
          <div className="flex flex-col gap-1.5 px-0.5">
            {discussionItems.map((item) => {
              const annotationData = isAnnotationDiscussionItem(item) ? item.data : undefined;
              const feedbackData = isFeedbackDiscussionItem(item) ? item.data : undefined;
              const isAnnotation = annotationData !== undefined;
              const data = annotationData ?? feedbackData;
              if (!data) return null;
              const itemKey = discussionItemKey(item);
              const rowActionKey = `${mutationScopeKey}:${itemKey}`;
              const rowActionState = rowActionStates[rowActionKey];
              const isResolved = annotationData
                ? annotationData.is_resolved
                : feedbackData?.status === "resolved";
              const hoverShapes =
                data.canvas_drawing?.shapes && data.canvas_drawing.shapes.length > 0
                  ? data.canvas_drawing.shapes
                  : null;
              const rowAnnotationId = annotationData?.annotation_id ?? null;
              const isNavigationAnnotation = readOverride?.annotationId === rowAnnotationId;
              const rowAnnotationAvailable = isNavigationAnnotation
                ? Boolean(
                    rowAnnotationId &&
                    Object.prototype.hasOwnProperty.call(
                      annotationClassById ?? {},
                      rowAnnotationId,
                    ),
                  )
                : annotationIsKnown(rowAnnotationId, annotationClassById);
              const attachments = data.attachments ?? [];
              return (
                <div
                  key={itemKey}
                  data-testid="discussion-comment-row"
                  data-comment-key={itemKey}
                  aria-current={highlightedComment === itemKey ? true : undefined}
                  tabIndex={-1}
                  onMouseEnter={() => {
                    if (hoverShapes) setHoveredShapes(hoverShapes);
                  }}
                  onMouseLeave={() => {
                    if (hoverShapes) setHoveredShapes(null);
                  }}
                  onClick={(event) => {
                    if (!hoverShapes || (event.target as HTMLElement).closest("button, a")) return;
                    togglePinnedComment(itemKey, hoverShapes);
                  }}
                  className={cn(
                    "rounded border border-border bg-card p-2",
                    "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
                    highlightedComment === itemKey && "border-brand ring-1 ring-brand",
                    isResolved && "bg-muted opacity-70",
                    hoverShapes && "cursor-crosshair",
                    pinnedCommentId === itemKey &&
                      "border-brand shadow-[inset_2px_0_0_0_var(--sc-brand)]",
                  )}
                >
                  <div className="mb-1 flex items-start justify-between gap-2">
                    <div className="flex min-w-0 flex-wrap items-center gap-1.5">
                      <span className="text-xs font-medium text-foreground">
                        {data.author_name ?? "—"}
                      </span>
                      <span
                        data-testid="discussion-source-chip"
                        className="rounded-[3px] bg-muted px-1 py-px text-2xs text-muted-foreground"
                      >
                        {isAnnotation ? "标注评论" : "任务留言"}
                      </span>
                      {isResolved && (
                        <span className="text-2xs text-status-positive">
                          {isAnnotation
                            ? "已解决"
                            : feedbackStatusLabel(feedbackData?.status ?? "open")}
                        </span>
                      )}
                    </div>
                    {(item.actions.change_status || item.actions.delete) && (
                      <div className="flex shrink-0 gap-0.5">
                        {item.actions.change_status && (
                          <button
                            type="button"
                            title={isResolved ? "重新打开" : "标为已解决"}
                            aria-label={isResolved ? "重新打开评论" : "标为已解决"}
                            onClick={() => void handleToggleStatus(item)}
                            disabled={rowActionState?.pending}
                            className={cn(
                              ICON_BUTTON,
                              "disabled:cursor-default disabled:opacity-50",
                            )}
                          >
                            <Icon name="check" size={12} />
                          </button>
                        )}
                        {item.actions.delete && (
                          <button
                            type="button"
                            title="删除"
                            aria-label="删除评论"
                            onClick={() => void handleDelete(item)}
                            disabled={rowActionState?.pending}
                            className={cn(
                              ICON_BUTTON,
                              "disabled:cursor-default disabled:opacity-50",
                            )}
                          >
                            <Icon name="trash" size={12} />
                          </button>
                        )}
                      </div>
                    )}
                  </div>

                  {rowAnnotationId && (
                    <button
                      type="button"
                      data-testid="comment-annotation-chip"
                      onClick={() => onSelectAnnotation?.(rowAnnotationId)}
                      disabled={!onSelectAnnotation || !rowAnnotationAvailable}
                      className="mb-1 inline-flex max-w-full cursor-pointer appearance-none items-center gap-1 rounded border border-border bg-muted px-1.5 py-px text-2xs text-muted-foreground [font:inherit] hover:border-brand hover:text-foreground disabled:cursor-default"
                      title={
                        rowAnnotationAvailable
                          ? "跳转到该评论绑定的标注框"
                          : isNavigationAnnotation
                            ? "标注尚未载入当前画布；阅读评论不会自动认领视频分段"
                            : "该标注已不可用，评论仍保留在历史中"
                      }
                    >
                      <Icon name="crosshair" size={11} />
                      <span className="overflow-hidden text-ellipsis whitespace-nowrap">
                        {rowAnnotationAvailable
                          ? (annotationClassById?.[rowAnnotationId] ?? "标注框")
                          : isNavigationAnnotation
                            ? `${readOverride?.label ?? "标注"}（未载入画布）`
                            : "标注已不可用"}
                      </span>
                    </button>
                  )}

                  <div className="whitespace-pre-wrap text-xs leading-relaxed text-foreground">
                    {renderCommentBody(
                      data.body,
                      (data.mentions ?? []) as CommentMention[],
                      (userId) => navigate(`/audit?actor=${userId}`),
                    )}
                  </div>

                  {annotationData && annotationData.anchor?.kind === "video_frame" && (
                    <button
                      type="button"
                      data-testid="comment-anchor-chip"
                      onClick={() => onSeekFrame?.(annotationData.anchor!.frameIndex)}
                      className={cn(
                        "mt-1.5 inline-flex appearance-none items-center gap-1.5 rounded border border-border bg-muted px-1.5 py-0.5 text-xs text-muted-foreground [font:inherit]",
                        onSeekFrame ? "cursor-pointer" : "cursor-default",
                      )}
                      title="跳转到评论锚定的视频帧"
                    >
                      <Icon name="film" size={12} />
                      <span className="mono">{anchorLabel(annotationData.anchor)}</span>
                    </button>
                  )}

                  {data.canvas_drawing?.shapes?.length ? (
                    <div className="mt-1.5">
                      <CanvasDrawingPreview
                        drawing={data.canvas_drawing}
                        width={220}
                        backgroundUrl={backgroundUrl}
                        imageWidth={imageWidth}
                        imageHeight={imageHeight}
                      />
                    </div>
                  ) : null}

                  {rowActionState?.error && (
                    <div
                      className="mt-1 text-2xs text-status-danger"
                      role="alert"
                      data-testid="discussion-row-error"
                    >
                      操作失败：{rowActionState.error}
                    </div>
                  )}

                  {attachments.length > 0 && (
                    <div className="mt-1.5 flex flex-wrap gap-1">
                      {attachments.map((attachment, index) => {
                        const meta = formatAttachment(attachment);
                        const storageKey = meta.key;
                        const attachmentStateKey = storageKey
                          ? `${mutationScopeKey}:${itemKey}:${storageKey}`
                          : null;
                        const attachmentDownloadState = attachmentStateKey
                          ? attachmentDownloadStates[attachmentStateKey]
                          : undefined;
                        const label =
                          meta.size !== null
                            ? `${meta.name} · ${(meta.size / 1024).toFixed(1)} KB`
                            : meta.name;
                        return isAnnotation && rowAnnotationId && storageKey ? (
                          <button
                            type="button"
                            key={`${meta.key}-${index}`}
                            data-testid="comment-attachment-download"
                            aria-label={`${attachmentDownloadState?.pending ? "正在下载" : "下载"}附件 ${meta.name}`}
                            onClick={() =>
                              void handleAttachmentDownload(
                                itemKey,
                                rowAnnotationId,
                                storageKey,
                                meta.name,
                              )
                            }
                            disabled={attachmentDownloadState?.pending}
                            className="inline-flex max-w-full cursor-pointer appearance-none items-center gap-1 rounded-[3px] border border-border bg-muted px-1.5 py-0.5 text-xs text-foreground [font:inherit] disabled:cursor-default disabled:opacity-60"
                            title={label}
                          >
                            <Icon name="folder" size={11} />
                            <span className="max-w-[140px] overflow-hidden text-ellipsis whitespace-nowrap">
                              {meta.name}
                            </span>
                          </button>
                        ) : (
                          <span
                            key={`${meta.name}-${index}`}
                            className="inline-flex max-w-full items-center gap-1 rounded-[3px] border border-dashed border-border bg-muted px-1.5 py-0.5 text-xs text-muted-foreground"
                            title="当前来源暂不支持附件下载"
                          >
                            <Icon name="folder" size={11} />
                            <span className="max-w-[140px] overflow-hidden text-ellipsis whitespace-nowrap">
                              {meta.name}（暂不支持下载）
                            </span>
                          </span>
                        );
                      })}
                    </div>
                  )}

                  {attachments.some((attachment) => {
                    const key = formatAttachment(attachment).key;
                    return Boolean(
                      isAnnotation &&
                      rowAnnotationId &&
                      key &&
                      attachmentDownloadStates[`${mutationScopeKey}:${itemKey}:${key}`]?.error,
                    );
                  }) && (
                    <div
                      className="mt-1 text-2xs text-status-danger"
                      role="alert"
                      data-testid="discussion-attachment-error"
                    >
                      {attachments
                        .map((attachment) => {
                          const key = formatAttachment(attachment).key;
                          return key
                            ? attachmentDownloadStates[`${mutationScopeKey}:${itemKey}:${key}`]
                                ?.error
                            : null;
                        })
                        .filter(Boolean)
                        .map((error) => `附件下载失败：${error}`)
                        .join("；")}
                    </div>
                  )}

                  <div className="mt-1 text-2xs text-muted-foreground/70">
                    {new Date(data.created_at).toLocaleString()}
                  </div>
                </div>
              );
            })}
            {activeQuery.hasNextPage && (
              <button
                type="button"
                onClick={() => void activeQuery.fetchNextPage()}
                disabled={activeQuery.isFetchingNextPage}
                data-testid="comments-load-more"
                className="mt-1 min-h-8 cursor-pointer appearance-none self-center rounded-[3px] border border-border bg-transparent px-2.5 py-1 text-xs text-muted-foreground [font:inherit] active:scale-[0.96]"
              >
                {activeQuery.isFetchingNextPage ? "加载中…" : "加载更早评论"}
              </button>
            )}
          </div>
        )}
      </div>

      {tab === "comments" && (taskContext || annotationId) && (
        <div className="flex shrink-0 flex-col gap-1.5 border-t border-border pt-2">
          {taskContext && sendTarget && sendTargetOptions.length > 0 && (
            <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <span>发送到</span>
              <select
                aria-label="发送目标"
                value={activeTargetKey}
                onChange={(event) => handleSendTargetChange(event.target.value)}
                className="min-h-7 min-w-0 flex-1 cursor-pointer rounded border border-border bg-background px-1.5 text-xs text-foreground [font:inherit] focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-brand"
              >
                {sendTargetOptions.map((target) => (
                  <option key={discussionTargetKey(target)} value={discussionTargetKey(target)}>
                    {targetLabel(target)}
                  </option>
                ))}
              </select>
            </label>
          )}
          <CommentInput
            annotationId={!taskContext ? annotationId : undefined}
            projectId={projectId}
            taskId={composerTaskId}
            target={taskContext ? sendTarget : undefined}
            draftStore={draftStore as DiscussionDraftStore | null}
            members={memberOptions}
            busy={
              !draftStore || !composerTaskId
                ? sendTarget?.kind === "annotation"
                  ? createCommentMut.isPending
                  : createTaskFeedbackMut.isPending
                : false
            }
            backgroundUrl={canvasDrawingTargetEnabled ? backgroundUrl : null}
            imageWidth={imageWidth}
            imageHeight={imageHeight}
            enableCanvasDrawing={canvasDrawingTargetEnabled ? enableCanvasDrawing : false}
            liveCanvas={activeAnnotationComposer || taskCanvasComposer ? liveCanvas : undefined}
            anchor={
              sendTarget?.kind === "annotation" && sendTarget.annotationId === annotationId
                ? commentAnchor
                : null
            }
            onPendingDrawingChange={reportPendingDrawing}
            targetAvailable={sendTargetAnnotationAvailable}
            targetUnavailableReason={sendTargetUnavailableReason}
            onReturnToTask={handleReturnToTask}
            onSubmit={handleSubmit}
          />
        </div>
      )}
    </div>
  );
}

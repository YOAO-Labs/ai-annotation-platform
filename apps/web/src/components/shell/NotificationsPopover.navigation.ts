import { ApiError } from "@/api/client";
import { commentsApi, type AnnotationCommentResponse } from "@/api/comments";
import {
  feedbacksApi,
  type AnnotationFeedback,
  type AnnotationFeedbackThreadPage,
} from "@/api/feedbacks";
import { resolveActiveDiscussionAnnotation } from "@/api/discussionTargets";
import { discussionApi, type TaskDiscussionItem } from "@/api/discussion";
import { tasksApi } from "@/api/tasks";
import type { TaskResponse } from "@/types";
import type { NotificationItem } from "@/api/notifications";
import type { WorkbenchDiscussionTarget } from "@/utils/workbenchNavigation";
import { notificationPayloadRecord } from "./NotificationsPopover.helpers";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const PAGE_LIMIT = 50;

export type DiscussionNotificationKind = "feedback" | "annotation_comment";

export class DiscussionNotificationError extends Error {
  constructor(
    message: string,
    public readonly kind: "invalid" | "unavailable" | "permission-denied" | "network",
    options?: { cause?: unknown },
  ) {
    super(message);
    if (options?.cause !== undefined) {
      Object.defineProperty(this, "cause", {
        configurable: true,
        value: options.cause,
      });
    }
    this.name = "DiscussionNotificationError";
  }
}

export interface ResolvedDiscussionNotification {
  projectId: string;
  task: TaskResponse;
  target: WorkbenchDiscussionTarget;
  kind: DiscussionNotificationKind;
}

function isUuid(value: unknown): value is string {
  return typeof value === "string" && UUID.test(value);
}

function payloadString(item: NotificationItem, key: string): string {
  const value = notificationPayloadRecord(item.payload)[key];
  return typeof value === "string" ? value : "";
}

function requiredUuid(value: unknown, message: string): string {
  if (!isUuid(value)) throw new DiscussionNotificationError(message, "invalid");
  return value;
}

function requiredPayloadUuid(item: NotificationItem, key: string, message: string): string {
  return requiredUuid(notificationPayloadRecord(item.payload)[key], message);
}

function requirePayloadValue(
  item: NotificationItem,
  key: string,
  expected: string,
  message: string,
) {
  if (payloadString(item, key) !== expected) {
    throw new DiscussionNotificationError(message, "invalid");
  }
}

function wrapLookupError(
  error: unknown,
  unavailableMessage: string,
  networkMessage: string,
): never {
  if (error instanceof DiscussionNotificationError) throw error;
  if (error instanceof Error && error.name === "AbortError") throw error;
  if (error instanceof ApiError && error.status === 403) {
    throw new DiscussionNotificationError(
      "通知目标当前不可访问，请从当前任务列表重新查找。",
      "permission-denied",
      { cause: error },
    );
  }
  if (error instanceof ApiError && [404, 410].includes(error.status)) {
    throw new DiscussionNotificationError(unavailableMessage, "unavailable", { cause: error });
  }
  throw new DiscussionNotificationError(networkMessage, "network", { cause: error });
}

function assertNotAborted(signal: AbortSignal) {
  if (!signal.aborted) return;
  const error = new Error("通知目标查找已取消");
  error.name = "AbortError";
  throw error;
}

function validateTask(
  task: TaskResponse,
  expectedTaskId: string,
  expectedProjectId: string,
): TaskResponse {
  if (
    !task ||
    task.id !== expectedTaskId ||
    task.project_id !== expectedProjectId ||
    !isUuid(task.id) ||
    !isUuid(task.project_id)
  ) {
    throw new DiscussionNotificationError(
      "通知目标所属任务已变更或当前账号不可见。",
      "unavailable",
    );
  }
  return task;
}

function validateIssueRoot(
  root: AnnotationFeedback | null | undefined,
  rootId: string,
  projectId: string,
  taskId: string,
): AnnotationFeedback & { task_id: string } {
  if (
    !root ||
    root.id !== rootId ||
    root.kind !== "issue" ||
    root.thread_parent_id !== null ||
    !root.is_active ||
    typeof root.task_id !== "string" ||
    root.project_id !== projectId ||
    root.task_id !== taskId
  ) {
    throw new DiscussionNotificationError("问题已删除、转派或当前账号不可见。", "unavailable");
  }
  return root as AnnotationFeedback & { task_id: string };
}

function validateThreadPage(page: AnnotationFeedbackThreadPage): AnnotationFeedbackThreadPage {
  if (
    !page ||
    typeof page !== "object" ||
    !Array.isArray(page.items) ||
    !page.items.every((item) => item && typeof item === "object" && typeof item.id === "string") ||
    (page.next_cursor !== null &&
      page.next_cursor !== undefined &&
      (typeof page.next_cursor !== "string" || page.next_cursor.length === 0))
  ) {
    throw new DiscussionNotificationError("通知返回的问题线程格式无效。", "invalid");
  }
  return page;
}

function validateIssueReply(
  value: AnnotationFeedback | undefined,
  root: AnnotationFeedback,
  replyId: string,
): AnnotationFeedback | null {
  if (!value || value.id !== replyId) return null;
  if (
    value.id === root.id ||
    value.thread_parent_id === null ||
    !value.is_active ||
    value.project_id !== root.project_id ||
    value.task_id !== root.task_id
  ) {
    throw new DiscussionNotificationError(
      "该问题回复已删除、移出线程或当前账号不可见。",
      "unavailable",
    );
  }
  return value;
}

async function resolveFeedbackNotification(
  item: NotificationItem,
  signal: AbortSignal,
): Promise<ResolvedDiscussionNotification> {
  const rootId = requiredUuid(item.target_id, "通知中的问题 ID 格式无效。");
  const projectHint = requiredPayloadUuid(item, "project_id", "通知中的问题所属项目 ID 格式无效。");
  const taskHint = requiredPayloadUuid(item, "task_id", "通知中的问题所属任务 ID 格式无效。");
  requirePayloadValue(item, "source", "feedback", "通知中的问题来源无效。");
  const replyId =
    item.type === "feedback.reply_created"
      ? requiredUuid(notificationPayloadRecord(item.payload).reply_id, "通知中的回复 ID 格式无效。")
      : null;

  let cursor: string | undefined;
  const seenCursors = new Set<string>();
  let root: AnnotationFeedback | null = null;
  let task: TaskResponse | null = null;
  let foundReply = false;

  while (true) {
    assertNotAborted(signal);
    let page: AnnotationFeedbackThreadPage;
    try {
      page = validateThreadPage(
        await feedbacksApi.thread(
          rootId,
          { limit: PAGE_LIMIT, ...(cursor ? { cursor } : {}) },
          signal,
        ),
      );
    } catch (error) {
      wrapLookupError(
        error,
        "问题线程已删除或当前账号不可见。",
        "暂时无法读取问题线程，请检查网络后重试。",
      );
    }
    assertNotAborted(signal);

    if (!root) {
      const pageRoot = page.root;
      if (!pageRoot || typeof pageRoot !== "object") {
        throw new DiscussionNotificationError("通知返回的问题根记录无效。", "invalid");
      }
      const validatedRoot = validateIssueRoot(pageRoot, rootId, projectHint, taskHint);
      root = validatedRoot;
      try {
        task = validateTask(
          await tasksApi.get(validatedRoot.task_id, { signal }),
          validatedRoot.task_id,
          validatedRoot.project_id,
        );
      } catch (error) {
        wrapLookupError(
          error,
          "问题所属任务已删除、转派或当前账号不可见。",
          "暂时无法核对问题所属任务，请检查网络后重试。",
        );
      }
    } else {
      validateIssueRoot(page.root, root.id, projectHint, taskHint);
    }

    if (replyId) {
      foundReply = Boolean(
        validateIssueReply(
          page.items.find((value) => value.id === replyId),
          root,
          replyId,
        ),
      );
    }
    const nextCursor = page.next_cursor;
    if (!replyId || foundReply || nextCursor == null) break;
    if (typeof nextCursor !== "string" || nextCursor.length === 0 || seenCursors.has(nextCursor)) {
      throw new DiscussionNotificationError("通知返回的问题游标无效。", "invalid");
    }
    seenCursors.add(nextCursor);
    cursor = nextCursor;
  }

  if (replyId && !foundReply) {
    throw new DiscussionNotificationError(
      "该问题回复已删除、移出线程或当前账号不可见。",
      "unavailable",
    );
  }
  if (!root || !task) {
    throw new DiscussionNotificationError("通知目标问题不可用。", "unavailable");
  }
  return {
    projectId: task.project_id,
    task,
    kind: "feedback",
    target: { kind: "issue", issueId: root.id, ...(replyId ? { replyId } : {}) },
  };
}

function validateComment(
  value: AnnotationCommentResponse | undefined,
  commentId: string,
  annotationId: string,
): boolean {
  return Boolean(
    value &&
    value.id === commentId &&
    value.annotation_id === annotationId &&
    value.is_active === true,
  );
}

function validateTaskComment(
  value: TaskDiscussionItem | undefined,
  commentId: string,
  projectId: string,
  taskId: string,
): boolean {
  return Boolean(
    value &&
    value.source === "feedback" &&
    value.data.id === commentId &&
    value.data.kind === "comment" &&
    value.data.anchor_type === "task" &&
    value.data.thread_parent_id === null &&
    value.data.is_active === true &&
    value.data.project_id === projectId &&
    value.data.task_id === taskId,
  );
}

async function resolveTaskCommentNotification(
  item: NotificationItem,
  signal: AbortSignal,
): Promise<ResolvedDiscussionNotification> {
  const commentId = requiredUuid(item.target_id, "通知中的任务留言 ID 格式无效。");
  const projectHint = requiredPayloadUuid(
    item,
    "project_id",
    "通知中的任务留言所属项目 ID 格式无效。",
  );
  const taskHint = requiredPayloadUuid(item, "task_id", "通知中的任务留言所属任务 ID 格式无效。");
  requirePayloadValue(item, "source", "feedback", "通知中的任务留言来源无效。");

  let task: TaskResponse;
  try {
    task = validateTask(await tasksApi.get(taskHint, { signal }), taskHint, projectHint);
  } catch (error) {
    wrapLookupError(
      error,
      "任务留言所属任务已删除、转派或当前账号不可见。",
      "暂时无法核对任务留言所属任务，请检查网络后重试。",
    );
  }

  let cursor: string | undefined;
  const seenCursors = new Set<string>();
  let foundComment = false;
  while (true) {
    assertNotAborted(signal);
    let page: Awaited<ReturnType<typeof discussionApi.listTaskDiscussion>>;
    try {
      page = await discussionApi.listTaskDiscussion(
        task.id,
        { scope: "task", limit: PAGE_LIMIT, ...(cursor ? { cursor } : {}) },
        signal,
      );
    } catch (error) {
      wrapLookupError(
        error,
        "该任务留言已删除或当前账号不可见。",
        "暂时无法读取任务留言，请检查网络后重试。",
      );
    }
    assertNotAborted(signal);
    if (
      !page ||
      typeof page !== "object" ||
      !Array.isArray(page.items) ||
      (page.next_cursor !== null &&
        page.next_cursor !== undefined &&
        (typeof page.next_cursor !== "string" || page.next_cursor.length === 0))
    ) {
      throw new DiscussionNotificationError("通知返回的任务留言列表格式无效。", "invalid");
    }
    const matching = page.items.find(
      (value) => value && typeof value === "object" && value.data?.id === commentId,
    );
    if (matching) {
      if (!validateTaskComment(matching, commentId, task.project_id, task.id)) {
        throw new DiscussionNotificationError(
          "该任务留言已删除、移出任务或当前账号不可见。",
          "unavailable",
        );
      }
      foundComment = true;
      break;
    }
    const nextCursor = page.next_cursor;
    if (!nextCursor) break;
    if (seenCursors.has(nextCursor)) {
      throw new DiscussionNotificationError("通知返回的任务留言游标无效。", "invalid");
    }
    seenCursors.add(nextCursor);
    cursor = nextCursor;
  }
  if (!foundComment) {
    throw new DiscussionNotificationError("该任务留言已删除或当前账号不可见。", "unavailable");
  }
  return {
    projectId: task.project_id,
    task,
    kind: "feedback",
    target: { kind: "task_comment", commentId },
  };
}

async function resolveAnnotationCommentNotification(
  item: NotificationItem,
  signal: AbortSignal,
): Promise<ResolvedDiscussionNotification> {
  const commentId = requiredUuid(item.target_id, "通知中的评论 ID 格式无效。");
  const annotationId = requiredUuid(
    notificationPayloadRecord(item.payload).annotation_id,
    "通知中的标注 ID 格式无效。",
  );
  const projectHint = requiredPayloadUuid(item, "project_id", "通知中的评论所属项目 ID 格式无效。");
  const taskHint = requiredPayloadUuid(item, "task_id", "通知中的评论所属任务 ID 格式无效。");
  requirePayloadValue(item, "source", "annotation_comment", "通知中的评论来源无效。");

  let task: TaskResponse;
  try {
    task = validateTask(await tasksApi.get(taskHint, { signal }), taskHint, projectHint);
  } catch (error) {
    wrapLookupError(
      error,
      "评论所属任务已删除、转派或当前账号不可见。",
      "暂时无法核对评论所属任务，请检查网络后重试。",
    );
  }

  let annotation: Awaited<ReturnType<typeof resolveActiveDiscussionAnnotation>>;
  try {
    annotation = await resolveActiveDiscussionAnnotation(task, annotationId, signal);
  } catch (error) {
    wrapLookupError(
      error,
      "评论所属标注已删除或当前账号不可见。",
      "暂时无法核对评论所属标注，请检查网络后重试。",
    );
  }
  if (
    !annotation ||
    annotation.id !== annotationId ||
    annotation.task_id !== task.id ||
    annotation.is_active !== true ||
    (annotation.project_id !== null && annotation.project_id !== task.project_id)
  ) {
    throw new DiscussionNotificationError("评论所属标注已删除或当前账号不可见。", "unavailable");
  }

  let cursor: string | undefined;
  const seenCursors = new Set<string>();
  let foundComment = false;
  while (true) {
    assertNotAborted(signal);
    let page: Awaited<ReturnType<typeof commentsApi.listByAnnotationKeyset>>;
    try {
      page = await commentsApi.listByAnnotationKeyset(
        annotationId,
        { limit: PAGE_LIMIT, ...(cursor ? { cursor } : {}) },
        signal,
      );
    } catch (error) {
      wrapLookupError(
        error,
        "该标注评论已删除或当前账号不可见。",
        "暂时无法读取标注评论，请检查网络后重试。",
      );
    }
    assertNotAborted(signal);
    if (
      !page ||
      typeof page !== "object" ||
      !Array.isArray(page.items) ||
      !page.items.every(
        (item) => item && typeof item === "object" && typeof item.id === "string",
      ) ||
      (page.next_cursor !== null &&
        page.next_cursor !== undefined &&
        (typeof page.next_cursor !== "string" || page.next_cursor.length === 0))
    ) {
      throw new DiscussionNotificationError("通知返回的评论列表格式无效。", "invalid");
    }
    foundComment = validateComment(
      page.items.find((value) => value.id === commentId),
      commentId,
      annotationId,
    );
    const nextCursor = page.next_cursor;
    if (foundComment || nextCursor == null) break;
    if (typeof nextCursor !== "string" || nextCursor.length === 0 || seenCursors.has(nextCursor)) {
      throw new DiscussionNotificationError("通知返回的评论游标无效。", "invalid");
    }
    seenCursors.add(nextCursor);
    cursor = nextCursor;
  }
  if (!foundComment) {
    throw new DiscussionNotificationError("该标注评论已删除或当前账号不可见。", "unavailable");
  }
  return {
    projectId: task.project_id,
    task,
    kind: "annotation_comment",
    target: { kind: "comment", annotationId, commentId },
  };
}

export async function resolveDiscussionNotification(
  item: NotificationItem,
  signal: AbortSignal,
): Promise<ResolvedDiscussionNotification> {
  if (item.target_type === "feedback") {
    if (item.type === "feedback.comment_mentioned") {
      return resolveTaskCommentNotification(item, signal);
    }
    if (item.type !== "feedback.reply_created" && item.type !== "feedback.status_changed") {
      throw new DiscussionNotificationError("通知中的问题事件类型无效。", "invalid");
    }
    return resolveFeedbackNotification(item, signal);
  }
  if (item.target_type === "annotation_comment") {
    if (item.type !== "annotation.comment_mentioned") {
      throw new DiscussionNotificationError("通知中的评论事件类型无效。", "invalid");
    }
    return resolveAnnotationCommentNotification(item, signal);
  }
  throw new DiscussionNotificationError("通知目标类型不支持讨论跳转。", "invalid");
}

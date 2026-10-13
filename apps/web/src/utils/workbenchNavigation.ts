const LAST_TASK_BY_BATCH_KEY = "anno.workbench.lastTaskByBatch.v1";

/** Returning false keeps a navigation source open after a cancelled leave check. */
export type GuardedNavigate = (to: string) => void | boolean | Promise<void | boolean>;

type StorageLike = Pick<Storage, "getItem" | "setItem">;
type RememberedTaskValue = string | { taskId: string; lastOpenedAt: number };

/** A discussion destination is data, never an arbitrary redirect URL. */
export type WorkbenchDiscussionTarget =
  | { kind: "issue"; issueId: string; replyId?: string | null }
  | { kind: "comment"; annotationId: string; commentId: string }
  | { kind: "task_comment"; commentId: string };

export type WorkbenchDiscussionRequest =
  | { status: "none" }
  | { status: "invalid"; message: string }
  | { status: "valid"; taskId: string; target: WorkbenchDiscussionTarget };

const DISCUSSION_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const DISCUSSION_PARAMETERS = ["discussion", "issue", "reply", "comment", "task_comment"] as const;

/** URL identities remain hints; callers must recheck task and record access. */
export function parseWorkbenchDiscussionRequest(
  search: string | URLSearchParams,
): WorkbenchDiscussionRequest {
  const q = typeof search === "string" ? new URLSearchParams(search) : search;
  if (!DISCUSSION_PARAMETERS.some((key) => q.has(key))) return { status: "none" };
  const invalid = (): WorkbenchDiscussionRequest => ({
    status: "invalid",
    message: "讨论链接不完整或格式无效",
  });
  if ([...DISCUSSION_PARAMETERS, "task", "focus"].some((key) => q.getAll(key).length > 1)) {
    return invalid();
  }
  const taskId = q.get("task") ?? "";
  if (!DISCUSSION_UUID.test(taskId)) return invalid();
  if (q.get("discussion") === "issues") {
    const issueId = q.get("issue") ?? "";
    const replyId = q.get("reply");
    if (
      !DISCUSSION_UUID.test(issueId) ||
      (replyId !== null && !DISCUSSION_UUID.test(replyId)) ||
      q.has("comment") ||
      q.has("task_comment") ||
      q.has("focus") ||
      q.has("track") ||
      q.has("frame")
    )
      return invalid();
    return {
      status: "valid",
      taskId,
      target: { kind: "issue", issueId, ...(replyId ? { replyId } : {}) },
    };
  }
  if (q.get("discussion") === "comments") {
    const taskCommentId = q.get("task_comment");
    if (taskCommentId !== null) {
      if (
        !DISCUSSION_UUID.test(taskCommentId) ||
        q.has("focus") ||
        q.has("comment") ||
        q.has("issue") ||
        q.has("reply") ||
        q.has("track") ||
        q.has("frame")
      )
        return invalid();
      return {
        status: "valid",
        taskId,
        target: { kind: "task_comment", commentId: taskCommentId },
      };
    }
    const annotationId = q.get("focus") ?? "";
    const commentId = q.get("comment") ?? "";
    if (
      !DISCUSSION_UUID.test(annotationId) ||
      !DISCUSSION_UUID.test(commentId) ||
      q.has("issue") ||
      q.has("reply") ||
      q.has("task_comment") ||
      q.has("track") ||
      q.has("frame")
    )
      return invalid();
    return { status: "valid", taskId, target: { kind: "comment", annotationId, commentId } };
  }
  return invalid();
}

function setDiscussionTarget(q: URLSearchParams, target: WorkbenchDiscussionTarget | null) {
  const previousMode = q.get("discussion");
  for (const key of DISCUSSION_PARAMETERS) q.delete(key);
  if (!target) {
    if (previousMode === "comments") q.delete("focus");
    return;
  }
  // Explicit discussion activation must not race a second entity/frame request.
  q.delete("focus");
  q.delete("track");
  q.delete("frame");
  if (target.kind === "issue") {
    q.set("discussion", "issues");
    q.set("issue", target.issueId);
    if (target.replyId !== null && target.replyId !== undefined) q.set("reply", target.replyId);
  } else if (target.kind === "comment") {
    q.set("discussion", "comments");
    q.set("focus", target.annotationId);
    q.set("comment", target.commentId);
  } else {
    q.set("discussion", "comments");
    q.set("task_comment", target.commentId);
  }
  if (parseWorkbenchDiscussionRequest(q).status !== "valid") {
    throw new TypeError("Invalid Workbench discussion destination");
  }
}

function getStorage(): StorageLike | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function readLastTaskMap(storage: StorageLike | null): Record<string, RememberedTaskValue> {
  if (!storage) return {};
  try {
    const raw = storage.getItem(LAST_TASK_BY_BATCH_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as Record<string, RememberedTaskValue>)
      : {};
  } catch {
    return {};
  }
}

function scopedBatchKey(batchId: string, scope?: string | null) {
  return scope ? `${scope}:${batchId}` : batchId;
}

export function getRememberedWorkbenchTask(
  batchId: string | null | undefined,
  storage = getStorage(),
  scope?: string | null,
) {
  return getRememberedWorkbenchTaskRecord(batchId, storage, scope)?.taskId ?? null;
}

/** Return the remembered task and its last real selection time.
 * Legacy string entries remain readable with an unknown (zero) timestamp. */
export function getRememberedWorkbenchTaskRecord(
  batchId: string | null | undefined,
  storage = getStorage(),
  scope?: string | null,
) {
  if (!batchId) return null;
  const value = readLastTaskMap(storage)[scopedBatchKey(batchId, scope)];
  if (typeof value === "string") {
    return value ? { taskId: value, lastOpenedAt: 0 } : null;
  }
  if (
    value &&
    typeof value === "object" &&
    typeof value.taskId === "string" &&
    value.taskId &&
    typeof value.lastOpenedAt === "number" &&
    Number.isFinite(value.lastOpenedAt)
  ) {
    return value;
  }
  return null;
}

export function rememberWorkbenchTask(
  batchId: string | null | undefined,
  taskId: string | null | undefined,
  storage = getStorage(),
  scope?: string | null,
) {
  if (!batchId || !taskId || !storage) return;
  const next = readLastTaskMap(storage);
  next[scopedBatchKey(batchId, scope)] = { taskId, lastOpenedAt: Date.now() };
  try {
    storage.setItem(LAST_TASK_BY_BATCH_KEY, JSON.stringify(next));
  } catch {
    // Ignore storage quota / private mode failures; navigation should still work.
  }
}

export function currentWorkbenchReturnTo(location: {
  pathname: string;
  search?: string;
  hash?: string;
}) {
  return `${location.pathname}${location.search ?? ""}${location.hash ?? ""}`;
}

export function resolveWorkbenchReturnTo(
  raw: string | null | undefined,
  currentPath: string,
  fallback = "/dashboard",
) {
  if (!raw || !raw.startsWith("/") || raw.startsWith("//")) return fallback;
  try {
    const url = new URL(raw, "http://app.local");
    const target = `${url.pathname}${url.search}${url.hash}`;
    return target !== currentPath ? target : fallback;
  } catch {
    return fallback;
  }
}

export function buildWorkbenchUrl(
  projectId: string,
  opts: {
    batchId?: string | null;
    taskId?: string | null;
    annotationId?: string | null;
    trackId?: string | null;
    frameIndex?: number | null;
    returnTo?: string | null;
    discussion?: WorkbenchDiscussionTarget | null;
  } = {},
) {
  const q = new URLSearchParams();
  if (opts.batchId) q.set("batch", opts.batchId);
  if (opts.taskId) q.set("task", opts.taskId);
  if (opts.annotationId) q.set("focus", opts.annotationId);
  if (opts.trackId) q.set("track", opts.trackId);
  if (opts.frameIndex !== null && opts.frameIndex !== undefined) {
    q.set("frame", String(opts.frameIndex));
  }
  if (opts.returnTo) q.set("returnTo", opts.returnTo);
  if (opts.discussion) setDiscussionTarget(q, opts.discussion);
  const qs = q.toString();
  return `/projects/${projectId}/annotate${qs ? `?${qs}` : ""}`;
}

export function buildReviewWorkbenchUrl(
  projectId: string,
  opts: {
    batchId?: string | null;
    taskId?: string | null;
    returnTo?: string | null;
    discussion?: WorkbenchDiscussionTarget | null;
  } = {},
) {
  const q = new URLSearchParams();
  if (opts.batchId) q.set("batch", opts.batchId);
  if (opts.taskId) q.set("task", opts.taskId);
  if (opts.returnTo) q.set("returnTo", opts.returnTo);
  if (opts.discussion) setDiscussionTarget(q, opts.discussion);
  const qs = q.toString();
  return `/projects/${projectId}/review${qs ? `?${qs}` : ""}`;
}

/** List-entry hint only; destination routes still resolve current capabilities. */
export function buildProjectEntryUrl(
  projectId: string,
  projectRole: "annotator" | "reviewer" | "viewer" | null | undefined,
  opts: { returnTo?: string | null } = {},
) {
  if (projectRole === "viewer") return `/projects/${projectId}/data-manager`;
  return projectRole === "reviewer"
    ? buildReviewWorkbenchUrl(projectId, opts)
    : buildWorkbenchUrl(projectId, opts);
}

export function updateWorkbenchUrlSearch(
  location: { pathname: string; search?: string; hash?: string },
  opts: {
    batchId?: string | null;
    taskId?: string | null;
    annotationId?: string | null;
    trackId?: string | null;
    frameIndex?: number | null;
    discussion?: WorkbenchDiscussionTarget | null;
  } = {},
) {
  const q = new URLSearchParams(location.search ?? "");
  if ("batchId" in opts) {
    if (opts.batchId) q.set("batch", opts.batchId);
    else q.delete("batch");
  }
  if ("taskId" in opts) {
    if (opts.taskId) q.set("task", opts.taskId);
    else q.delete("task");
    if (!("annotationId" in opts)) q.delete("focus");
    if (!("trackId" in opts)) q.delete("track");
    if (!("frameIndex" in opts)) q.delete("frame");
    if (!("discussion" in opts)) {
      for (const key of DISCUSSION_PARAMETERS) q.delete(key);
    }
  }
  if ("annotationId" in opts) {
    if (opts.annotationId) q.set("focus", opts.annotationId);
    else q.delete("focus");
  }
  if ("trackId" in opts) {
    if (opts.trackId) q.set("track", opts.trackId);
    else q.delete("track");
  }
  if ("frameIndex" in opts) {
    if (opts.frameIndex !== null && opts.frameIndex !== undefined) {
      q.set("frame", String(opts.frameIndex));
    } else {
      q.delete("frame");
    }
  }
  if ("discussion" in opts) setDiscussionTarget(q, opts.discussion ?? null);
  const qs = q.toString();
  return `${location.pathname}${qs ? `?${qs}` : ""}${location.hash ?? ""}`;
}

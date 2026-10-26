/**
 * Central request-error classification for workbench E2E suites.
 *
 * One place decides which captured failure signals are legitimate lifecycle
 * cancellations. Every allowance is an exact method + path pair with a stated
 * reason, organized into product-domain rule groups so each suite composes
 * exactly the cancellations observed on its own flows; membership in another
 * suite's group never widens a whitelist. The engine only classifies
 * `net::ERR_ABORTED` *request* failures, and only two write-shaped shapes are
 * allowed: keep-alive session telemetry POSTs (heartbeat, task-event batches)
 * whose body the API may already have accepted, and the best-effort keepalive
 * DELETE that revokes a native playback lease while the document leaves. Both
 * happen after unload already cancelled the socket. Console/page errors, HTTP
 * responses and every other failure stay errors; suites keep intended business
 * HTTP failures explicit through their own expected-error registries instead of
 * this engine.
 */

/** A normalized failure signal recorded from page / console / request events. */
export interface RequestErrorSignal {
  kind: "page" | "console" | "http" | "request";
  message: string;
  method?: string;
  path?: string;
  status?: number;
  body?: string;
}

/**
 * One allow entry: a `net::ERR_ABORTED` request failure is expected only when
 * the method matches exactly and the URL pathname matches the given exact
 * string or fully-anchored regular expression.
 */
export interface AbortAllowRule {
  method: "DELETE" | "GET" | "PATCH" | "POST" | "PUT";
  path: string | RegExp;
  /** Why retiring this request is a legitimate lifecycle cancellation. */
  reason: string;
}

const UUID = "[0-9a-f-]{36}";

/**
 * Keep-alive session telemetry may be reported as aborted on unload.
 * Session telemetry stays restricted to these two POST routes.
 */
export const sessionTelemetryAborts: readonly AbortAllowRule[] = [
  {
    method: "POST",
    path: "/api/v1/auth/me/heartbeat",
    reason: "keepalive session heartbeat aborted on unload after the API accepted it",
  },
  {
    method: "POST",
    path: "/api/v1/auth/me/task-events:batch",
    reason: "keepalive session telemetry batch aborted on unload after the API accepted it",
  },
];

/** Bootstrap reads observed on both image and video workbench flows. */
export const coreBootstrapAborts: readonly AbortAllowRule[] = [
  {
    method: "GET",
    path: "/api/v1/auth/me",
    reason: "bootstrap session query retired by reload/leave",
  },
  { method: "GET", path: "/api/v1/feedbacks", reason: "Issue list query retired by reload/leave" },
  { method: "GET", path: "/api/v1/tasks", reason: "task queue query retired by reload/leave" },
  {
    method: "GET",
    path: new RegExp(`^/api/v1/tasks/${UUID}$`),
    reason: "task detail query retired when switching or reloading tasks",
  },
];

/**
 * Shell/dashboard navigation reads observed on image-workbench flows, which
 * enter through the dashboard; direct workbench entry never fetches them.
 */
export const dashboardViewAborts: readonly AbortAllowRule[] = [
  {
    method: "GET",
    path: "/api/v1/auth/registration-status",
    reason: "bootstrap registration-status query retired by reload/leave",
  },
  { method: "GET", path: "/api/v1/projects", reason: "project list query retired by reload/leave" },
  {
    method: "GET",
    path: "/api/v1/audit-logs",
    reason: "audit-log page query retired by reload/leave",
  },
];

/** Per-task context queries retired by switching tasks, reloading or leaving. */
export const taskContextAborts: readonly AbortAllowRule[] = [
  {
    method: "GET",
    path: new RegExp(`^/api/v1/tasks/${UUID}/annotations$`),
    reason: "task annotation query retired when switching or reloading tasks",
  },
  {
    method: "GET",
    path: new RegExp(`^/api/v1/tasks/${UUID}/discussion/page$`),
    reason: "leaving the comments tab retires its abortable task-discussion query",
  },
  {
    method: "GET",
    path: new RegExp(`^/api/v1/tasks/${UUID}/discussion/annotation-counts$`),
    reason: "retiring the task also cancels its annotation comment badge query",
  },
  {
    method: "GET",
    path: new RegExp(`^/api/v1/projects/${UUID}/access$`),
    reason: "leaving or reloading the workbench retires the project access query",
  },
];

/** Task-scoped context reads only exercised by the video Issue suites. */
export const videoTaskContextAborts: readonly AbortAllowRule[] = [
  {
    method: "GET",
    path: new RegExp(`^/api/v1/tasks/${UUID}/predictions$`),
    reason: "task prediction query retired when switching or reloading tasks",
  },
  {
    method: "GET",
    path: new RegExp(`^/api/v1/projects/${UUID}/mention-candidates$`),
    reason: "retiring the discussion composer cancels its mention-candidate query",
  },
  {
    method: "GET",
    path: new RegExp(`^/api/v1/feedbacks/${UUID}/thread$`),
    reason: "closing/replacing an Issue detail retires its root-bound thread query",
  },
];

/** Object comments panel read retired when the panel's owner unmounts. */
export const objectCommentsAborts: readonly AbortAllowRule[] = [
  {
    method: "GET",
    path: new RegExp(`^/api/v1/annotations/${UUID}/comments/page$`),
    reason: "closing the object comments panel retires its paged comment query",
  },
];

/** Mask content reads retire with their canvas or editor owner. */
export const maskEditorAborts: readonly AbortAllowRule[] = [
  {
    method: "GET",
    path: new RegExp(`^/api/v1/annotations/${UUID}/mask-content$`),
    reason: "retiring the mask canvas or editor cancels its in-flight content fetch",
  },
];

/**
 * Verified original reads retire with their task, camera or dataset preview owner.
 */
const originalMediaAborts: readonly AbortAllowRule[] = [
  {
    method: "GET",
    path: new RegExp(`^/api/v1/files/tasks/${UUID}(?:/links/${UUID})?/original(?:-identity)?$`),
    reason: "task or camera retirement/reload cancels its owned original read",
  },
  {
    method: "GET",
    path: new RegExp(`^/api/v1/datasets/${UUID}/items/${UUID}/original(?:-identity)?$`),
    reason: "closing or reloading the dataset preview cancels its owned original read",
  },
];

/**
 * A leaving document can only revoke its native playback lease with a keepalive
 * DELETE, and the browser reports that request as aborted once the document is
 * discarded. Lease expiry and the registry reaper stay the final owner, so the
 * cancelled revoke is a documented lifecycle cancellation rather than a lost
 * write; HTTP failures and every other method stay errors.
 */
export const originalPlaybackReleaseAborts: readonly AbortAllowRule[] = [
  {
    method: "DELETE",
    path: new RegExp(`^/api/v1/files/tasks/${UUID}/original-playback/${UUID}$`),
    reason: "reload/leave aborts the best-effort keepalive lease revoke; expiry owns cleanup",
  },
  {
    method: "DELETE",
    path: new RegExp(`^/api/v1/datasets/${UUID}/items/${UUID}/original-playback/${UUID}$`),
    reason: "closing the dataset preview aborts its best-effort lease revoke",
  },
];

/**
 * Video media and manifest reads retired by switching tasks or reloading.
 * Both the standard and the precise video manifest queries are retired, and
 * chunk/chapter/segment prefetches abort with their owning task.
 */
export const videoMediaAborts: readonly AbortAllowRule[] = [
  {
    method: "GET",
    path: new RegExp(`^/api/v1/tasks/${UUID}/video/manifest(?:-v2)?$`),
    reason: "switching tasks or reloading retires both standard and precise manifest queries",
  },
  {
    method: "GET",
    path: new RegExp(`^/api/v1/tasks/${UUID}/video/frames/\\d+$`),
    reason: "leaving the frame preview retires its in-flight frame fetch",
  },
  {
    method: "GET",
    path: new RegExp(`^/api/v1/videos/${UUID}/chunks/\\d+(?:/samples)?$`),
    reason: "retiring a video owner may cancel chunk metadata and sample fetches",
  },
  {
    method: "GET",
    path: new RegExp(`^/api/v1/tasks/${UUID}/video/(?:segments|frame-timetable)$`),
    reason: "task retirement/reload aborts read-only segment and timetable context queries",
  },
  {
    method: "GET",
    path: new RegExp(`^/api/v1/videos/${UUID}/chapters$`),
    reason: "task retirement/reload aborts the read-only chapter query",
  },
];

/**
 * Image-workbench flows (dashboard → workbench → reload/leave/task switch).
 * Exactly the cancellations the image workbench specs observed before the
 * central policy existed.
 */
export const imageWorkbenchAborts: readonly AbortAllowRule[] = [
  ...sessionTelemetryAborts,
  ...coreBootstrapAborts,
  ...dashboardViewAborts,
  ...taskContextAborts,
  ...originalMediaAborts,
  ...maskEditorAborts,
];

/**
 * Video workbench flows, which enter the workbench directly. Exactly the
 * cancellations `isVideoLifecycleCancellation` used to allow.
 */
export const videoWorkbenchAborts: readonly AbortAllowRule[] = [
  ...sessionTelemetryAborts,
  ...coreBootstrapAborts,
  ...taskContextAborts,
  ...videoTaskContextAborts,
  ...videoMediaAborts,
  ...originalMediaAborts,
  ...originalPlaybackReleaseAborts,
];

/** Whether `signal`'s method + pathname satisfy one allow rule exactly. */
export function matchesAbortRule(
  signal: Pick<RequestErrorSignal, "method" | "path">,
  rule: AbortAllowRule,
): boolean {
  if (signal.method !== rule.method || !signal.path) return false;
  return typeof rule.path === "string" ? rule.path === signal.path : rule.path.test(signal.path);
}

/**
 * True only for a `net::ERR_ABORTED` request failure matched by one of the
 * given rule groups. HTTP failures, other network errors, console/page errors
 * and every other write request stay errors unless a rule names it exactly.
 */
export function isExpectedRequestAbort(
  error: RequestErrorSignal,
  ...ruleGroups: ReadonlyArray<readonly AbortAllowRule[]>
): boolean {
  if (error.kind !== "request") return false;
  if (error.message !== "net::ERR_ABORTED" || !error.path) return false;
  return ruleGroups.some((rules) => rules.some((rule) => matchesAbortRule(error, rule)));
}

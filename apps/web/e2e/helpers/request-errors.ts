export interface RequestErrorSignal {
  kind: "page" | "console" | "http" | "request";
  message: string;
  method?: string;
  path?: string;
  status?: number;
  body?: string;
}

export interface AbortAllowRule {
  method: "DELETE" | "GET" | "PATCH" | "POST" | "PUT";
  path: string | RegExp;
  reason: string;
}

const UUID = "[0-9a-f-]{36}";

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

export const objectCommentsAborts: readonly AbortAllowRule[] = [
  {
    method: "GET",
    path: new RegExp(`^/api/v1/annotations/${UUID}/comments/page$`),
    reason: "closing the object comments panel retires its paged comment query",
  },
];

export const maskEditorAborts: readonly AbortAllowRule[] = [
  {
    method: "GET",
    path: new RegExp(`^/api/v1/annotations/${UUID}/mask-content$`),
    reason: "retiring the mask canvas or editor cancels its in-flight content fetch",
  },
];

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
    method: "POST",
    path: new RegExp(`^/api/v1/tasks/${UUID}/video/frames:prefetch$`),
    reason: "retiring the video owner cancels its read-only batch frame prefetch",
  },
  {
    method: "GET",
    path: new RegExp(`^/api/v1/videos/${UUID}/chunks/\\d+(?:/samples)?$`),
    reason: "retiring a video owner may cancel chunk metadata and sample fetches",
  },
  {
    method: "GET",
    path: new RegExp(`^/api/v1/media-cache/video-derivative/${UUID}/chunk\\.mp4$`),
    reason: "retiring native playback cancels its owned saved-derivative media stream",
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

export const imageWorkbenchAborts: readonly AbortAllowRule[] = [
  ...sessionTelemetryAborts,
  ...coreBootstrapAborts,
  ...dashboardViewAborts,
  ...taskContextAborts,
  ...originalMediaAborts,
  ...maskEditorAborts,
];

export const videoWorkbenchAborts: readonly AbortAllowRule[] = [
  ...sessionTelemetryAborts,
  ...coreBootstrapAborts,
  ...taskContextAborts,
  ...videoTaskContextAborts,
  ...videoMediaAborts,
  ...originalMediaAborts,
  ...originalPlaybackReleaseAborts,
];

export function matchesAbortRule(
  signal: Pick<RequestErrorSignal, "method" | "path">,
  rule: AbortAllowRule,
): boolean {
  if (signal.method !== rule.method || !signal.path) return false;
  return typeof rule.path === "string" ? rule.path === signal.path : rule.path.test(signal.path);
}

export function isExpectedRequestAbort(
  error: RequestErrorSignal,
  ...ruleGroups: ReadonlyArray<readonly AbortAllowRule[]>
): boolean {
  if (error.kind !== "request") return false;
  if (error.message !== "net::ERR_ABORTED" || !error.path) return false;
  return ruleGroups.some((rules) => rules.some((rule) => matchesAbortRule(error, rule)));
}

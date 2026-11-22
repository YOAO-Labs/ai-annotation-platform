export type NotificationNavigationDecision =
  | { kind: "task"; taskId: string; batchId: string | null }
  | { kind: "batch"; batchId: string }
  | { kind: "direct-url"; url: string }
  | { kind: "guarded-route"; url: string };

const DISCUSSION_PARAMETERS = ["discussion", "issue", "reply", "comment", "task_comment"] as const;

export function planNotificationNavigation(
  url: string,
  context: {
    projectId: string | null | undefined;
    mode: "annotate" | "review";
    selectedBatchId: string | null;
  },
  origin = "http://workbench.local",
): NotificationNavigationDecision | null {
  let parsed: URL;
  try {
    parsed = new URL(url, origin);
  } catch {
    return null;
  }
  const workbenchMatch = parsed.pathname.match(/^\/projects\/([^/]+)\/(annotate|review)$/);
  if (!workbenchMatch) {
    return { kind: "guarded-route", url };
  }
  const [, targetProjectId, targetMode] = workbenchMatch;
  const isDiscussion = DISCUSSION_PARAMETERS.some((key) => parsed.searchParams.has(key));
  const isSameWorkbench =
    targetProjectId === context.projectId && targetMode === context.mode && !!context.projectId;
  if (!isSameWorkbench) {
    return { kind: "guarded-route", url };
  }
  if (isDiscussion) {
    return { kind: "direct-url", url };
  }
  const taskId = parsed.searchParams.get("task");
  const batchId = parsed.searchParams.get("batch");
  if (taskId) return { kind: "task", taskId, batchId };
  if (batchId && batchId !== context.selectedBatchId) return { kind: "batch", batchId };
  return { kind: "direct-url", url };
}

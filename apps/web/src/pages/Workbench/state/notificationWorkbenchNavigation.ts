/**
 * 通知面板目标 URL → 工作台导航决策（纯函数，便于单测）。
 *
 * - 同项目同模式 + 任务直达：交给 selectTask（视频/Mask 离开检查 + URL 提交）。
 * - 同项目同模式 + 讨论链接：只改路由，讨论 URL 水合自带单次准入，避免二次确认。
 * - 同项目同模式 + 仅批次：交给 handleSelectBatch（现有批次切换准入）。
 * - 跨项目或非工作台目标：先跑视频 + Mask 离开检查，再改路由。
 */

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
    // 同项目讨论链接：讨论 URL 水合（useDiscussionNavigation + selectTask
    // fromUrl）自带单次准入与取消还原；这里不再叠加一次离开检查。
    return { kind: "direct-url", url };
  }
  const taskId = parsed.searchParams.get("task");
  const batchId = parsed.searchParams.get("batch");
  if (taskId) return { kind: "task", taskId, batchId };
  if (batchId && batchId !== context.selectedBatchId) return { kind: "batch", batchId };
  return { kind: "direct-url", url };
}

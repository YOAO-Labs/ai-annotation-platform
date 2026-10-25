import type { NotificationItem } from "@/api/notifications";

export type NotificationFilter =
  | "all"
  | "task"
  | "batch"
  | "bug_report"
  | "feedback"
  | "annotation_comment"
  | "async_job"
  | "export";

export type NotificationGroupKey = "today" | "week" | "earlier";

export const FILTERS: Array<{ key: NotificationFilter; label: string }> = [
  { key: "all", label: "全部" },
  { key: "task", label: "任务" },
  { key: "batch", label: "批次" },
  { key: "bug_report", label: "反馈" },
  { key: "feedback", label: "问题" },
  { key: "annotation_comment", label: "标注评论" },
  { key: "async_job", label: "后台任务" },
  { key: "export", label: "导出" },
];

export const GROUP_LABELS: Record<NotificationGroupKey, string> = {
  today: "今天",
  week: "本周",
  earlier: "更早",
};

export const GROUP_ORDER: NotificationGroupKey[] = ["today", "week", "earlier"];

export function notificationPayloadRecord(payload: unknown): Record<string, unknown> {
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) return {};
  return payload as Record<string, unknown>;
}

function startOfToday(now: Date): Date {
  return new Date(now.getFullYear(), now.getMonth(), now.getDate());
}

function startOfWeek(now: Date): Date {
  const today = startOfToday(now);
  const day = today.getDay();
  const mondayOffset = day === 0 ? -6 : 1 - day;
  const monday = new Date(today);
  monday.setDate(today.getDate() + mondayOffset);
  return monday;
}

function groupKeyForCreatedAt(iso: string, now = new Date()): NotificationGroupKey {
  const ts = new Date(iso);
  if (Number.isNaN(ts.getTime())) return "earlier";
  if (ts >= startOfToday(now)) return "today";
  if (ts >= startOfWeek(now)) return "week";
  return "earlier";
}

export function filterNotificationItems(
  items: NotificationItem[],
  filter: NotificationFilter,
): NotificationItem[] {
  if (filter === "all") return items;
  return items.filter((item) => item.target_type === filter);
}

export function groupNotificationItems(
  items: NotificationItem[],
  now = new Date(),
): Record<NotificationGroupKey, NotificationItem[]> {
  return items.reduce<Record<NotificationGroupKey, NotificationItem[]>>(
    (groups, item) => {
      groups[groupKeyForCreatedAt(item.created_at, now)].push(item);
      return groups;
    },
    { today: [], week: [], earlier: [] },
  );
}

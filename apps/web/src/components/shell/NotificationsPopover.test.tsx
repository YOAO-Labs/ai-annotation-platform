import { describe, expect, it } from "vitest";
import {
  filterNotificationItems,
  groupNotificationItems,
  notificationPayloadRecord,
} from "./NotificationsPopover.helpers";
import { jobSnippet } from "./NotificationsPopover";
import type { NotificationItem } from "@/api/notifications";

function makeNotification(overrides: Partial<NotificationItem>): NotificationItem {
  return {
    id: "n-1",
    type: "task.rejected",
    target_type: "task",
    target_id: "target-1",
    payload: {},
    read_at: null,
    created_at: "2026-05-24T08:00:00Z",
    ...overrides,
  };
}

describe("NotificationsPopover helpers", () => {
  it.each([null, [], "legacy", 42, true])(
    "treats non-object payload %j as having no UI fields",
    (payload) => {
      expect(notificationPayloadRecord(payload)).toEqual({});
      expect(
        jobSnippet(makeNotification({ type: "job.completed", target_type: "async_job", payload })),
      ).toBe("");
    },
  );

  it("returns object payloads for existing UI fields without copying them", () => {
    const payload = { project_id: "project-1", reason: "all_predicted", skipped_count: 32 };
    expect(notificationPayloadRecord(payload)).toBe(payload);
    expect(
      jobSnippet(makeNotification({ type: "job.completed", target_type: "async_job", payload })),
    ).toBe("已全部预标，跳过 32 个");
  });

  it("filters loaded notifications by target_type", () => {
    const items = [
      makeNotification({ id: "task", target_type: "task" }),
      makeNotification({ id: "batch", target_type: "batch" }),
      makeNotification({ id: "feedback", target_type: "feedback" }),
      makeNotification({ id: "comment", target_type: "annotation_comment" }),
      makeNotification({ id: "job", target_type: "async_job" }),
    ];

    expect(filterNotificationItems(items, "all")).toHaveLength(5);
    expect(filterNotificationItems(items, "batch").map((item) => item.id)).toEqual(["batch"]);
    expect(filterNotificationItems(items, "feedback").map((item) => item.id)).toEqual(["feedback"]);
    expect(filterNotificationItems(items, "annotation_comment").map((item) => item.id)).toEqual([
      "comment",
    ]);
    expect(filterNotificationItems(items, "async_job").map((item) => item.id)).toEqual(["job"]);
  });

  it("groups notifications into today, this week and earlier", () => {
    const now = new Date("2026-05-24T12:00:00");
    const grouped = groupNotificationItems(
      [
        makeNotification({ id: "today", created_at: "2026-05-24T01:00:00" }),
        makeNotification({ id: "week", created_at: "2026-05-20T01:00:00" }),
        makeNotification({ id: "earlier", created_at: "2026-05-10T01:00:00" }),
      ],
      now,
    );

    expect(grouped.today.map((item) => item.id)).toEqual(["today"]);
    expect(grouped.week.map((item) => item.id)).toEqual(["week"]);
    expect(grouped.earlier.map((item) => item.id)).toEqual(["earlier"]);
  });

  it("jobSnippet: 全已预标跳过显示明确文案, 而非误导的成功 0", () => {
    const allPredicted = makeNotification({
      type: "job.completed",
      payload: { reason: "all_predicted", skipped_count: 32, success_count: 0 },
    });
    expect(jobSnippet(allPredicted)).toBe("已全部预标，跳过 32 个");

    const normal = makeNotification({
      type: "job.completed",
      payload: { success_count: 30, failed_count: 2 },
    });
    expect(jobSnippet(normal)).toBe("成功 30 / 失败 2");
  });
});

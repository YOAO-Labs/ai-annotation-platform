import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/api/client";
import type { AnnotationCommentResponse } from "@/api/comments";
import type { AnnotationFeedback } from "@/api/feedbacks";
import type { NotificationItem } from "@/api/notifications";
import type { AnnotationResponse, TaskResponse } from "@/types";
import {
  DiscussionNotificationError,
  resolveDiscussionNotification,
} from "./NotificationsPopover.navigation";
import { notificationPayloadRecord } from "./NotificationsPopover.helpers";

const mocks = vi.hoisted(() => ({
  task: vi.fn(),
  thread: vi.fn(),
  discussion: vi.fn(),
  annotation: vi.fn(),
  comments: vi.fn(),
}));

vi.mock("@/api/tasks", () => ({ tasksApi: { get: mocks.task } }));
vi.mock("@/api/feedbacks", () => ({ feedbacksApi: { thread: mocks.thread } }));
vi.mock("@/api/discussion", () => ({ discussionApi: { listTaskDiscussion: mocks.discussion } }));
vi.mock("@/api/discussionTargets", () => ({
  resolveActiveDiscussionAnnotation: mocks.annotation,
}));
vi.mock("@/api/comments", () => ({
  commentsApi: { listByAnnotationKeyset: mocks.comments },
}));

const ids = {
  project: "11111111-1111-4111-8111-111111111111",
  task: "22222222-2222-4222-8222-222222222222",
  issue: "33333333-3333-4333-8333-333333333333",
  reply: "44444444-4444-4444-8444-444444444444",
  otherReply: "77777777-7777-4777-8777-777777777777",
  annotation: "55555555-5555-4555-8555-555555555555",
  comment: "66666666-6666-4666-8666-666666666666",
  otherComment: "88888888-8888-4888-8888-888888888888",
};

const task = {
  id: ids.task,
  project_id: ids.project,
  batch_id: "batch-1",
} as TaskResponse;

const root = {
  id: ids.issue,
  kind: "issue",
  project_id: ids.project,
  task_id: ids.task,
  thread_parent_id: null,
  is_active: true,
} as AnnotationFeedback;

const reply = {
  id: ids.reply,
  kind: "comment",
  project_id: ids.project,
  task_id: ids.task,
  thread_parent_id: ids.issue,
  is_active: true,
} as AnnotationFeedback;

const annotation = {
  id: ids.annotation,
  task_id: ids.task,
  project_id: ids.project,
  is_active: true,
} as AnnotationResponse;

function notification(
  overrides: Partial<NotificationItem> & { type: string; target_type: string },
): NotificationItem {
  return {
    id: "notification-1",
    target_id: ids.issue,
    payload: {
      project_id: ids.project,
      task_id: ids.task,
      source: "feedback",
    },
    read_at: null,
    created_at: "2026-09-11T00:00:00Z",
    ...overrides,
  };
}

function feedbackNotification(type: "feedback.reply_created" | "feedback.status_changed") {
  return notification({
    type,
    target_type: "feedback",
    payload: {
      project_id: ids.project,
      task_id: ids.task,
      source: "feedback",
      ...(type === "feedback.reply_created" ? { reply_id: ids.reply } : {}),
    },
  });
}

function commentNotification(): NotificationItem {
  return notification({
    type: "annotation.comment_mentioned",
    target_type: "annotation_comment",
    target_id: ids.comment,
    payload: {
      project_id: ids.project,
      task_id: ids.task,
      source: "annotation_comment",
      annotation_id: ids.annotation,
    },
  });
}

function taskCommentNotification(): NotificationItem {
  return notification({
    type: "feedback.comment_mentioned",
    target_type: "feedback",
    target_id: ids.comment,
    payload: {
      project_id: ids.project,
      task_id: ids.task,
      source: "feedback",
    },
  });
}

beforeEach(() => {
  vi.resetAllMocks();
  mocks.task.mockResolvedValue(task);
  mocks.annotation.mockResolvedValue(annotation);
});

describe("notification discussion target resolution", () => {
  it("accepts historical nested reply kinds through the authoritative root-bound reader", async () => {
    mocks.thread.mockResolvedValue({
      root,
      items: [{ ...reply, kind: "issue", thread_parent_id: ids.otherReply }],
      next_cursor: null,
      total: 1,
    });
    await expect(
      resolveDiscussionNotification(
        feedbackNotification("feedback.reply_created"),
        new AbortController().signal,
      ),
    ).resolves.toMatchObject({ target: { replyId: ids.reply } });
  });

  it("revalidates root ownership on later pages", async () => {
    mocks.thread
      .mockResolvedValueOnce({ root, items: [], next_cursor: "older", total: 1 })
      .mockResolvedValueOnce({
        root: { ...root, task_id: ids.otherReply },
        items: [reply],
        next_cursor: null,
        total: 1,
      });
    await expect(
      resolveDiscussionNotification(
        feedbackNotification("feedback.reply_created"),
        new AbortController().signal,
      ),
    ).rejects.toMatchObject({ kind: "unavailable" });
  });

  it("finds a reply on a later thread page instead of treating the first page as missing", async () => {
    const item = feedbackNotification("feedback.reply_created");
    const signal = new AbortController().signal;
    mocks.thread
      .mockResolvedValueOnce({
        root,
        items: [{ ...reply, id: ids.otherReply }],
        next_cursor: "older",
        total: 2,
      })
      .mockResolvedValueOnce({ root, items: [reply], next_cursor: null, total: 2 });

    const resolved = await resolveDiscussionNotification(item, signal);

    expect(resolved.target).toEqual({
      kind: "issue",
      issueId: ids.issue,
      replyId: ids.reply,
    });
    expect(mocks.thread.mock.calls).toEqual([
      [ids.issue, { limit: 50 }, signal],
      [ids.issue, { limit: 50, cursor: "older" }, signal],
    ]);
    expect(mocks.task).toHaveBeenCalledWith(ids.task, { signal });
  });

  it("resolves a status notification from the active root", async () => {
    const item = feedbackNotification("feedback.status_changed");
    const signal = new AbortController().signal;
    mocks.thread.mockResolvedValue({ root, items: [], next_cursor: null, total: 0 });

    await expect(resolveDiscussionNotification(item, signal)).resolves.toMatchObject({
      projectId: ids.project,
      kind: "feedback",
      target: { kind: "issue", issueId: ids.issue },
    });
    expect(mocks.thread).toHaveBeenCalledTimes(1);
  });

  it("resolves a mentioned native task comment through the task feed", async () => {
    const item = taskCommentNotification();
    const signal = new AbortController().signal;
    mocks.discussion.mockResolvedValue({
      items: [
        {
          source: "feedback",
          data: {
            id: ids.comment,
            kind: "comment",
            anchor_type: "task",
            project_id: ids.project,
            task_id: ids.task,
            thread_parent_id: null,
            is_active: true,
          },
        },
      ],
      next_cursor: null,
      total: 1,
    });

    await expect(resolveDiscussionNotification(item, signal)).resolves.toEqual({
      projectId: ids.project,
      task,
      kind: "feedback",
      target: { kind: "task_comment", commentId: ids.comment },
    });
    expect(mocks.discussion).toHaveBeenCalledWith(ids.task, { scope: "task", limit: 50 }, signal);
    expect(mocks.thread).not.toHaveBeenCalled();
  });

  it("finds a mentioned comment on a later keyset page only after active annotation validation", async () => {
    const item = commentNotification();
    const signal = new AbortController().signal;
    mocks.comments
      .mockResolvedValueOnce({
        items: [{ id: ids.otherComment, annotation_id: ids.annotation, is_active: true }],
        next_cursor: "older-comments",
      })
      .mockResolvedValueOnce({
        items: [{ id: ids.comment, annotation_id: ids.annotation, is_active: true }],
        next_cursor: null,
      });

    const resolved = await resolveDiscussionNotification(item, signal);

    expect(resolved.target).toEqual({
      kind: "comment",
      annotationId: ids.annotation,
      commentId: ids.comment,
    });
    expect(mocks.annotation).toHaveBeenCalledWith(task, ids.annotation, signal);
    expect(mocks.comments.mock.calls).toEqual([
      [ids.annotation, { limit: 50 }, signal],
      [ids.annotation, { limit: 50, cursor: "older-comments" }, signal],
    ]);
  });

  it("does not use comment history to prove an inactive annotation is visible", async () => {
    const item = commentNotification();
    mocks.annotation.mockResolvedValue(null);
    await expect(
      resolveDiscussionNotification(item, new AbortController().signal),
    ).rejects.toMatchObject({
      name: "DiscussionNotificationError",
      kind: "unavailable",
    });
    expect(mocks.comments).not.toHaveBeenCalled();
  });

  it("classifies malformed payloads and permission failures without issuing a guessed lookup", async () => {
    const malformed = feedbackNotification("feedback.status_changed");
    malformed.payload = {
      ...notificationPayloadRecord(malformed.payload),
      source: "annotation_comment",
    };
    await expect(
      resolveDiscussionNotification(malformed, new AbortController().signal),
    ).rejects.toMatchObject({
      name: "DiscussionNotificationError",
      kind: "invalid",
    });
    expect(mocks.thread).not.toHaveBeenCalled();

    mocks.thread.mockRejectedValue(new ApiError(403, "forbidden"));
    await expect(
      resolveDiscussionNotification(
        feedbackNotification("feedback.status_changed"),
        new AbortController().signal,
      ),
    ).rejects.toMatchObject({ kind: "permission-denied" });
  });

  it.each([null, [], "legacy", 42, true])(
    "fails closed on non-object discussion payload %j without guessing a lookup",
    async (payload) => {
      const item = feedbackNotification("feedback.status_changed");
      item.payload = payload;
      await expect(
        resolveDiscussionNotification(item, new AbortController().signal),
      ).rejects.toMatchObject({ kind: "invalid" });
      expect(mocks.thread).not.toHaveBeenCalled();
    },
  );

  it.each([
    ["inactive root", { ...root, is_active: false }],
    ["mismatched project", { ...root, project_id: ids.task }],
  ])("keeps %s unavailable", async (_label, invalidRoot) => {
    mocks.thread.mockResolvedValue({ root: invalidRoot, items: [], next_cursor: null, total: 0 });
    await expect(
      resolveDiscussionNotification(
        feedbackNotification("feedback.status_changed"),
        new AbortController().signal,
      ),
    ).rejects.toMatchObject({ kind: "unavailable" });
    expect(mocks.task).not.toHaveBeenCalled();
  });

  it("stops the lookup after the caller aborts", async () => {
    const controller = new AbortController();
    let finish!: (value: unknown) => void;
    mocks.thread.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    const pending = resolveDiscussionNotification(
      feedbackNotification("feedback.status_changed"),
      controller.signal,
    );
    controller.abort();
    finish({ root, items: [], next_cursor: null, total: 0 });
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
    expect(mocks.task).not.toHaveBeenCalled();
  });

  it("rejects a comment page entry that belongs to another annotation", async () => {
    mocks.comments.mockResolvedValue({
      items: [
        {
          id: ids.comment,
          annotation_id: ids.otherReply,
          is_active: true,
        } as unknown as AnnotationCommentResponse,
      ],
      next_cursor: null,
    });
    await expect(
      resolveDiscussionNotification(commentNotification(), new AbortController().signal),
    ).rejects.toMatchObject({ kind: "unavailable" });
  });

  it("preserves the typed error class for callers that need a specific message", async () => {
    mocks.thread.mockResolvedValue({ root, items: [], next_cursor: null, total: 0 });
    const error = (await resolveDiscussionNotification(
      notification({ type: "feedback.unknown", target_type: "feedback" }),
      new AbortController().signal,
    ).catch((value) => value)) as DiscussionNotificationError;
    expect(error).toBeInstanceOf(DiscussionNotificationError);
    expect(error.kind).toBe("invalid");
  });
});

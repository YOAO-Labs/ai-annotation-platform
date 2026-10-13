import { describe, expect, it } from "vitest";
import {
  buildProjectEntryUrl,
  buildReviewWorkbenchUrl,
  buildWorkbenchUrl,
  getRememberedWorkbenchTask,
  parseWorkbenchDiscussionRequest,
  rememberWorkbenchTask,
  resolveWorkbenchReturnTo,
  updateWorkbenchUrlSearch,
} from "./workbenchNavigation";

function memoryStorage(initial?: Record<string, string>) {
  const data = new Map(Object.entries(initial ?? {}));
  return {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => {
      data.set(key, value);
    },
  } as Pick<Storage, "getItem" | "setItem">;
}

describe("workbenchNavigation", () => {
  it.each([
    ["annotator", "annotate"],
    ["reviewer", "review"],
    ["viewer", "data-manager"],
    [null, "annotate"],
  ] as const)("opens a project using its %s membership, not the platform role", (role, path) => {
    const url = new URL(
      buildProjectEntryUrl("p", role, { returnTo: "/dashboard" }),
      "http://app.local",
    );
    expect(url.pathname).toBe(`/projects/p/${path}`);
    if (role !== "viewer") expect(url.searchParams.get("returnTo")).toBe("/dashboard");
  });

  it("remembers the last task per batch", () => {
    const storage = memoryStorage();
    rememberWorkbenchTask("batch-1", "task-2", storage);

    expect(getRememberedWorkbenchTask("batch-1", storage)).toBe("task-2");
    expect(getRememberedWorkbenchTask("batch-2", storage)).toBeNull();
  });

  it("keeps remembered tasks separate across workbench modes", () => {
    const storage = memoryStorage();
    rememberWorkbenchTask("batch-1", "task-anno", storage, "annotate");
    rememberWorkbenchTask("batch-1", "task-review", storage, "review");

    expect(getRememberedWorkbenchTask("batch-1", storage, "annotate")).toBe("task-anno");
    expect(getRememberedWorkbenchTask("batch-1", storage, "review")).toBe("task-review");
  });

  it("builds workbench URLs with batch, task, and return target", () => {
    expect(
      buildWorkbenchUrl("project-1", {
        batchId: "batch-1",
        taskId: "task-2",
        returnTo: "/ai-pre/jobs?status=failed",
      }),
    ).toBe(
      "/projects/project-1/annotate?batch=batch-1&task=task-2&returnTo=%2Fai-pre%2Fjobs%3Fstatus%3Dfailed",
    );
  });

  it("builds workbench URLs with exact entity and frame focus", () => {
    const url = buildWorkbenchUrl("project-1", {
      taskId: "task-2",
      annotationId: "annotation / 中文",
      trackId: "track / 中文",
      frameIndex: 42,
      returnTo: "/projects/project-1/data-manager?lens=tracks",
    });
    const parsed = new URL(url, "http://app.local");
    expect(parsed.searchParams.get("focus")).toBe("annotation / 中文");
    expect(parsed.searchParams.get("track")).toBe("track / 中文");
    expect(parsed.searchParams.get("frame")).toBe("42");
  });

  it("builds review workbench URLs with batch, task, and return target", () => {
    expect(
      buildReviewWorkbenchUrl("project-1", {
        batchId: "batch-1",
        taskId: "task-2",
        returnTo: "/review?project=project-1&batch=batch-1",
      }),
    ).toBe(
      "/projects/project-1/review?batch=batch-1&task=task-2&returnTo=%2Freview%3Fproject%3Dproject-1%26batch%3Dbatch-1",
    );
  });

  it("rejects unsafe return targets", () => {
    expect(resolveWorkbenchReturnTo("https://example.com", "/projects/p/annotate")).toBe(
      "/dashboard",
    );
    expect(resolveWorkbenchReturnTo("//example.com/path", "/projects/p/annotate")).toBe(
      "/dashboard",
    );
    expect(resolveWorkbenchReturnTo("/ai-pre", "/projects/p/annotate")).toBe("/ai-pre");
    expect(resolveWorkbenchReturnTo("/projects/p/annotate", "/projects/p/annotate")).toBe(
      "/dashboard",
    );
  });

  it("updates workbench batch/task query while preserving return target", () => {
    expect(
      updateWorkbenchUrlSearch(
        {
          pathname: "/projects/project-1/review",
          search: "?batch=batch-1&task=task-1&returnTo=%2Freview%3Fbatch%3Dbatch-1",
          hash: "#stage",
        },
        { batchId: "batch-2", taskId: "task-2" },
      ),
    ).toBe(
      "/projects/project-1/review?batch=batch-2&task=task-2&returnTo=%2Freview%3Fbatch%3Dbatch-1#stage",
    );
  });

  it("can clear workbench batch/task query", () => {
    expect(
      updateWorkbenchUrlSearch(
        {
          pathname: "/projects/project-1/review",
          search: "?batch=batch-1&task=task-1&returnTo=%2Freview",
        },
        { batchId: null, taskId: null },
      ),
    ).toBe("/projects/project-1/review?returnTo=%2Freview");
  });

  it("clears stale entity focus when switching tasks", () => {
    expect(
      updateWorkbenchUrlSearch(
        {
          pathname: "/projects/project-1/annotate",
          search: "?task=old&focus=a&track=t&frame=8&returnTo=%2Fdashboard",
        },
        { taskId: "next" },
      ),
    ).toBe("/projects/project-1/annotate?task=next&returnTo=%2Fdashboard");
  });
});

describe("discussion navigation contract", () => {
  const taskId = "10000000-0000-0000-0000-000000000001";
  const issueId = "20000000-0000-0000-0000-000000000002";
  const replyId = "30000000-0000-0000-0000-000000000003";
  const annotationId = "40000000-0000-0000-0000-000000000004";
  const commentId = "50000000-0000-0000-0000-000000000005";

  it.each([buildWorkbenchUrl, buildReviewWorkbenchUrl])(
    "builds validated root/reply and original-comment destinations",
    (build) => {
      for (const target of [
        { kind: "issue" as const, issueId },
        { kind: "issue" as const, issueId, replyId },
        { kind: "comment" as const, annotationId, commentId },
        { kind: "task_comment" as const, commentId },
      ]) {
        const url = new URL(
          build("project", { taskId, discussion: target, returnTo: "/dashboard" }),
          "http://app.local",
        );
        expect(parseWorkbenchDiscussionRequest(url.searchParams)).toEqual({
          status: "valid",
          taskId,
          target,
        });
        expect(url.searchParams.get("returnTo")).toBe("/dashboard");
      }
    },
  );

  it("an explicit discussion target replaces competing annotation, track and frame focus", () => {
    const url = new URL(
      buildWorkbenchUrl("project", {
        taskId,
        annotationId: "old-annotation",
        trackId: "old-track",
        frameIndex: 8,
        discussion: { kind: "issue", issueId, replyId },
      }),
      "http://app.local",
    );
    expect(url.searchParams.has("focus")).toBe(false);
    expect(url.searchParams.has("track")).toBe(false);
    expect(url.searchParams.has("frame")).toBe(false);
    expect(parseWorkbenchDiscussionRequest(url.search)).toMatchObject({
      status: "valid",
      target: { kind: "issue", issueId, replyId },
    });
  });

  it("does not interpret existing annotation and frame links as discussion activations", () => {
    expect(parseWorkbenchDiscussionRequest("?task=legacy&focus=legacy-annotation&frame=8")).toEqual(
      { status: "none" },
    );
  });

  it.each([
    "discussion=issues",
    `discussion=issues&issue=${issueId}&task=bad`,
    `discussion=issues&task=${taskId}&issue=bad`,
    `discussion=issues&task=${taskId}&issue=${issueId}&reply=`,
    `discussion=issues&task=${taskId}&issue=${issueId}&reply=https://evil.invalid`,
    `discussion=issues&task=${taskId}&issue=${issueId}&comment=${commentId}`,
    `discussion=issues&task=${taskId}&issue=${issueId}&focus=${annotationId}`,
    `discussion=issues&task=${taskId}&issue=${issueId}&frame=8`,
    `discussion=issues&task=${taskId}&issue=${issueId}&track=t`,
    `discussion=issues&task=${taskId}&task=${taskId}&issue=${issueId}`,
    `discussion=issues&discussion=comments&task=${taskId}&issue=${issueId}`,
    `discussion=issues&task=${taskId}&issue=${issueId}&issue=${issueId}`,
    `discussion=comments&task=${taskId}&comment=${commentId}`,
    `discussion=comments&task=${taskId}&focus=${annotationId}&comment=bad`,
    `discussion=comments&task=${taskId}&focus=${annotationId}&comment=${commentId}&reply=${replyId}`,
    `discussion=comments&task=${taskId}&focus=${annotationId}&comment=${commentId}&focus=${annotationId}`,
    `discussion=unknown&task=${taskId}&issue=${issueId}`,
    `task=${taskId}&issue=${issueId}`,
    `task=${taskId}&reply=${replyId}`,
    `task=${taskId}&comment=${commentId}`,
  ])("rejects incomplete, mixed, duplicate or malformed identity: %s", (search) => {
    expect(parseWorkbenchDiscussionRequest(search)).toEqual({
      status: "invalid",
      message: "讨论链接不完整或格式无效",
    });
  });

  it("rejects invalid builder inputs rather than emitting an ambiguous URL", () => {
    expect(() => buildWorkbenchUrl("project", { discussion: { kind: "issue", issueId } })).toThrow(
      TypeError,
    );
    expect(() =>
      buildReviewWorkbenchUrl("project", { taskId, discussion: { kind: "issue", issueId: "bad" } }),
    ).toThrow(TypeError);
  });

  it("clears stale discussion identity together with entity focus on task navigation", () => {
    for (const discussion of [
      { kind: "issue" as const, issueId, replyId },
      { kind: "comment" as const, annotationId, commentId },
      { kind: "task_comment" as const, commentId },
    ]) {
      const before = new URL(
        buildWorkbenchUrl("project", { taskId, discussion, returnTo: "/dashboard" }),
        "http://app.local",
      );
      for (const nextTask of ["next", taskId, null]) {
        const after = new URL(
          updateWorkbenchUrlSearch(before, { taskId: nextTask }),
          "http://app.local",
        );
        expect(parseWorkbenchDiscussionRequest(after.search)).toEqual({ status: "none" });
        expect(after.searchParams.has("focus")).toBe(false);
        expect(after.searchParams.get("returnTo")).toBe("/dashboard");
      }
    }
  });

  it("allows an explicit replacement destination and can consume it without a leftover comment focus", () => {
    const after = new URL(
      updateWorkbenchUrlSearch(
        {
          pathname: "/projects/project/review",
          search: "?task=old&discussion=issues&issue=old",
          hash: "#stage",
        },
        {
          taskId,
          discussion: { kind: "comment", annotationId, commentId },
        },
      ),
      "http://app.local",
    );
    expect(parseWorkbenchDiscussionRequest(after.search)).toEqual({
      status: "valid",
      taskId,
      target: { kind: "comment", annotationId, commentId },
    });
    expect(after.hash).toBe("#stage");
    const consumed = new URL(
      updateWorkbenchUrlSearch(after, { discussion: null }),
      "http://app.local",
    );
    expect(consumed.searchParams.has("focus")).toBe(false);
    expect(parseWorkbenchDiscussionRequest(consumed.search)).toEqual({ status: "none" });
  });

  it("encodes native task-comment destinations separately from annotation comments", () => {
    const url = new URL(
      buildWorkbenchUrl("project", {
        taskId,
        discussion: { kind: "task_comment", commentId },
      }),
      "http://app.local",
    );
    expect(url.searchParams.get("discussion")).toBe("comments");
    expect(url.searchParams.get("task_comment")).toBe(commentId);
    expect(url.searchParams.has("focus")).toBe(false);
    expect(url.searchParams.has("comment")).toBe(false);
    expect(parseWorkbenchDiscussionRequest(url.search)).toEqual({
      status: "valid",
      taskId,
      target: { kind: "task_comment", commentId },
    });
  });
});

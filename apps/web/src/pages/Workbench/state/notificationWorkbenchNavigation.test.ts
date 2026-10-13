import { describe, expect, it } from "vitest";
import { planNotificationNavigation } from "./notificationWorkbenchNavigation";

const ctx = { projectId: "p1", mode: "annotate" as const, selectedBatchId: "b1" };

describe("planNotificationNavigation", () => {
  it("同项目同模式任务直达走 selectTask", () => {
    expect(
      planNotificationNavigation("/projects/p1/annotate?task=t9&batch=b2&returnTo=/x", ctx),
    ).toEqual({ kind: "task", taskId: "t9", batchId: "b2" });
  });

  it("a task without a batch does not inherit the current batch", () => {
    expect(planNotificationNavigation("/projects/p1/annotate?task=t9", ctx)).toEqual({
      kind: "task",
      taskId: "t9",
      batchId: null,
    });
  });

  it("同项目同模式仅切换批次走现有批次准入", () => {
    expect(planNotificationNavigation("/projects/p1/annotate?batch=b2", ctx)).toEqual({
      kind: "batch",
      batchId: "b2",
    });
  });

  it("同批次同项目的 URL 直接应用", () => {
    expect(planNotificationNavigation("/projects/p1/annotate?batch=b1", ctx)).toEqual({
      kind: "direct-url",
      url: "/projects/p1/annotate?batch=b1",
    });
  });

  it("同项目讨论链接直接改路由，由讨论水合单次准入", () => {
    const url =
      "/projects/p1/annotate?task=t9&discussion=issues&issue=33333333-3333-4333-8333-333333333333";
    expect(planNotificationNavigation(url, ctx)).toEqual({ kind: "direct-url", url });
  });

  it("跨项目讨论链接先跑离开检查", () => {
    const url =
      "/projects/p2/annotate?task=t9&discussion=issues&issue=33333333-3333-4333-8333-333333333333";
    expect(planNotificationNavigation(url, ctx)).toEqual({ kind: "guarded-route", url });
  });

  it("跨项目与跨模式目标先跑离开检查", () => {
    expect(planNotificationNavigation("/projects/p2/annotate?task=t9", ctx)).toEqual({
      kind: "guarded-route",
      url: "/projects/p2/annotate?task=t9",
    });
    expect(planNotificationNavigation("/projects/p1/review?task=t9", ctx)).toEqual({
      kind: "guarded-route",
      url: "/projects/p1/review?task=t9",
    });
  });

  it("非工作台目标（如 /bugs）先跑离开检查", () => {
    expect(planNotificationNavigation("/bugs", ctx)).toEqual({
      kind: "guarded-route",
      url: "/bugs",
    });
  });

  it("非法 URL 返回 null", () => {
    expect(planNotificationNavigation("http://[", ctx)).toBeNull();
  });

  it("projectId 未就绪时视作跨项目", () => {
    expect(
      planNotificationNavigation("/projects/p1/annotate?task=t9", { ...ctx, projectId: null }),
    ).toEqual({ kind: "guarded-route", url: "/projects/p1/annotate?task=t9" });
  });
});

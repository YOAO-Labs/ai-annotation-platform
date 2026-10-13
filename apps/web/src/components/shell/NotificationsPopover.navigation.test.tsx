import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/api/client";
import type { NotificationItem } from "@/api/notifications";
import { useAuthStore } from "@/stores/authStore";
import type { MeResponse } from "@/api/auth";

const mocks = vi.hoisted(() => ({
  task: vi.fn(),
  batch: vi.fn(),
  job: vi.fn(),
  resolveDiscussion: vi.fn(),
  access: vi.fn(),
  read: vi.fn(),
  delete: vi.fn(),
  notifications: [] as NotificationItem[],
  unread: 1 as number | null,
}));
vi.mock("@/api/tasks", () => ({ tasksApi: { get: mocks.task } }));
vi.mock("@/api/batches", () => ({ batchesApi: { get: mocks.batch } }));
vi.mock("@/api/projects", () => ({ projectsApi: { getAccess: mocks.access } }));
vi.mock("@/api/asyncJobs", () => ({ asyncJobsApi: { get: mocks.job } }));
vi.mock("@/api/discussionTargets", () => ({
  resolveActiveDiscussionAnnotation: vi.fn(),
}));
vi.mock("./NotificationsPopover.navigation", () => ({
  DiscussionNotificationError: class DiscussionNotificationError extends Error {},
  resolveDiscussionNotification: mocks.resolveDiscussion,
}));
vi.mock("@/hooks/useNotifications", () => ({
  useNotifications: () => ({
    data: { pages: [{ items: mocks.notifications }] },
    hasNextPage: false,
  }),
  useUnreadCount: () => ({
    data: mocks.unread === null ? undefined : { unread: mocks.unread },
    isError: mocks.unread === null,
  }),
  useMarkRead: () => ({ mutate: mocks.read }),
  useMarkAllRead: () => ({ mutate: vi.fn() }),
  useClearReadNotifications: () => ({ mutate: vi.fn() }),
  useDeleteNotification: () => ({ mutate: mocks.delete }),
}));
import { NotificationsPopover } from "./NotificationsPopover";

const notification: NotificationItem = {
  id: "n1",
  type: "task.rejected",
  target_type: "task",
  target_id: "t1",
  read_at: null,
  created_at: "2026-09-09T00:00:00Z",
  payload: { project_id: "old-project", task_display_id: "T-1", reject_reason: "请修正边框" },
};
const discussionIds = {
  project: "11111111-1111-4111-8111-111111111111",
  task: "22222222-2222-4222-8222-222222222222",
  issue: "33333333-3333-4333-8333-333333333333",
  reply: "44444444-4444-4444-8444-444444444444",
  annotation: "55555555-5555-4555-8555-555555555555",
  comment: "66666666-6666-4666-8666-666666666666",
  taskComment: "77777777-7777-4777-8777-777777777777",
};
const discussionTask = {
  id: discussionIds.task,
  project_id: discussionIds.project,
  batch_id: "batch-discussion",
};
const feedbackNotification: NotificationItem = {
  id: "n-feedback",
  type: "feedback.reply_created",
  target_type: "feedback",
  target_id: discussionIds.issue,
  read_at: null,
  created_at: "2026-09-09T00:00:00Z",
  payload: {
    project_id: discussionIds.project,
    task_id: discussionIds.task,
    source: "feedback",
    actor_name: "Bob",
    reply_id: discussionIds.reply,
  },
};
const commentNotification: NotificationItem = {
  id: "n-comment",
  type: "annotation.comment_mentioned",
  target_type: "annotation_comment",
  target_id: discussionIds.comment,
  read_at: null,
  created_at: "2026-09-09T00:00:00Z",
  payload: {
    project_id: discussionIds.project,
    task_id: discussionIds.task,
    source: "annotation_comment",
    actor_name: "Carol",
    annotation_id: discussionIds.annotation,
  },
};
const taskCommentNotification: NotificationItem = {
  id: "n-task-comment",
  type: "feedback.comment_mentioned",
  target_type: "feedback",
  target_id: discussionIds.taskComment,
  read_at: null,
  created_at: "2026-09-09T00:00:00Z",
  payload: {
    project_id: discussionIds.project,
    task_id: discussionIds.task,
    source: "feedback",
    actor_name: "Dana",
  },
};
function Location() {
  const location = useLocation();
  return (
    <output data-testid="location">
      {location.pathname}
      {location.search}
    </output>
  );
}

function access(capabilities: string[]) {
  return {
    project_id: "p1",
    user_id: "u1",
    platform_role: "employee" as const,
    project_role: "annotator" as const,
    access_kind: "member" as const,
    is_manager: false,
    capabilities,
  };
}
const BOTH_WRITE = ["project.read", "task.read", "annotation.write", "review.write"];
function renderUI(navigateExternal?: (to: string) => void) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/dashboard"]}>
        <Location />
        <NotificationsPopover navigate={navigateExternal} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
async function clickTaskNotification() {
  fireEvent.click(screen.getByTitle("通知，1 条未读"));
  fireEvent.click(await screen.findByRole("button", { name: "打开通知：退回了任务 T-1" }));
}

describe("通知直达当前目标", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.notifications = [notification];
    mocks.unread = 1;
    useAuthStore.getState().setAuth("test-token", { id: "u1", role: "employee" } as MeResponse);
    mocks.task.mockResolvedValue({
      id: "t1",
      project_id: "p1",
      batch_id: "b1",
      status: "rejected",
      reject_reason: "最新理由",
    });
    mocks.access.mockResolvedValue(access(BOTH_WRITE));
  });
  afterEach(() => {
    act(() => useAuthStore.getState().logout());
  });

  it("一次点击按服务端当前任务项目导航，包含具体任务、批次与返回入口", async () => {
    renderUI();
    fireEvent.click(screen.getByTitle("通知，1 条未读"));
    expect(await screen.findByText("仅显示已加载通知")).toBeInTheDocument();
    expect(await screen.findByText('"请修正边框"')).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "打开通知：退回了任务 T-1" }));
    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent(
        "/projects/p1/annotate?batch=b1&task=t1&returnTo=%2Fdashboard",
      ),
    );
    expect(mocks.task).toHaveBeenCalledWith("t1", { signal: expect.any(AbortSignal) });
    expect(mocks.read).toHaveBeenCalledWith("n1");
  });

  it("deletes a notification without opening or marking its target read", async () => {
    renderUI();
    fireEvent.click(screen.getByTitle("通知，1 条未读"));
    const open = await screen.findByRole("button", { name: "打开通知：退回了任务 T-1" });
    const remove = screen.getByRole("button", { name: "删除通知" });
    expect(open.contains(remove)).toBe(false);
    fireEvent.click(remove);
    expect(mocks.delete).toHaveBeenCalledWith("n1");
    expect(mocks.task).not.toHaveBeenCalled();
    expect(mocks.read).not.toHaveBeenCalled();
  });

  it.each([403, 404])("目标 %s 保留说明且不误开首条任务", async (status) => {
    mocks.task.mockRejectedValue(new ApiError(status, "gone"));
    renderUI();
    await clickTaskNotification();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "任务已被删除、转派或访问权限已变更",
    );
    expect(screen.getByTestId("location")).toHaveTextContent("/dashboard");
    fireEvent.click(screen.getByRole("button", { name: "查看当前任务" }));
    expect(screen.getByTestId("location")).toHaveTextContent("/dashboard");
  });

  it("网络失败可重试同一目标", async () => {
    mocks.task.mockRejectedValueOnce(new ApiError(503, "unavailable"));
    renderUI();
    await clickTaskNotification();
    fireEvent.click(await screen.findByRole("button", { name: "重新打开" }));
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("task=t1"));
    expect(mocks.task).toHaveBeenCalledTimes(2);
  });

  it("切账号后原通知的迟到响应不能改变新账号页面", async () => {
    let resolve!: (value: unknown) => void;
    mocks.task.mockImplementation(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    renderUI();
    await clickTaskNotification();
    act(() =>
      useAuthStore.getState().setAuth("other-token", { id: "u2", role: "employee" } as MeResponse),
    );
    await act(async () => resolve({ id: "t1", project_id: "p1", batch_id: "b1" }));
    expect(screen.getByTestId("location").textContent).toBe("/dashboard");
    expect(screen.queryByText("打开通知目标")).not.toBeInTheDocument();
  });

  it("后台作业通知直接读取该次导入结果", async () => {
    mocks.notifications = [
      {
        ...notification,
        type: "job.completed",
        target_type: "async_job",
        target_id: "j1",
        payload: { kind: "dataset_import", dataset_name: "样本" },
      },
    ];
    mocks.job.mockResolvedValue({
      id: "j1",
      kind: "dataset_import",
      status: "completed",
      progress_pct: 100,
      project_id: null,
      payload: { dataset_id: "d1", dataset_name: "样本" },
      result: { imported: 3, skipped: 0, error_count: 0 },
      created_at: "2026-09-09T00:00:00Z",
      started_at: null,
      completed_at: null,
    });
    renderUI();
    fireEvent.click(screen.getByTitle("通知，1 条未读"));
    fireEvent.click(await screen.findByRole("button", { name: "打开通知：数据集导入完成" }));
    expect(await screen.findByText("导入 3 / 跳过 0 / 错误 0")).toBeInTheDocument();
    expect(mocks.job).toHaveBeenCalledWith("j1");
    expect(screen.getByTestId("location").textContent).toBe("/dashboard");
  });

  it("问题通知核对当前任务后生成带回复定位的标注链接", async () => {
    mocks.notifications = [feedbackNotification];
    mocks.task.mockResolvedValue(discussionTask);
    mocks.resolveDiscussion.mockResolvedValue({
      projectId: discussionIds.project,
      task: discussionTask,
      kind: "feedback",
      target: { kind: "issue", issueId: discussionIds.issue, replyId: discussionIds.reply },
    });
    renderUI();
    fireEvent.click(screen.getByTitle("通知，1 条未读"));
    fireEvent.click(await screen.findByRole("button", { name: "打开通知：回复了问题" }));
    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent(
        `/projects/${discussionIds.project}/annotate?batch=batch-discussion&task=${discussionIds.task}&returnTo=%2Fdashboard&discussion=issues&issue=${discussionIds.issue}&reply=${discussionIds.reply}`,
      ),
    );
    expect(mocks.resolveDiscussion).toHaveBeenCalledWith(
      feedbackNotification,
      expect.any(AbortSignal),
    );
    expect(mocks.read).toHaveBeenCalledWith(feedbackNotification.id);
  });

  it("标注评论通知按审核角色生成评论定位链接", async () => {
    useAuthStore
      .getState()
      .setAuth("review-token", { id: "reviewer-1", role: "employee" } as MeResponse);
    mocks.access.mockResolvedValue(access(["project.read", "task.read", "review.write"]));
    mocks.notifications = [commentNotification];
    mocks.resolveDiscussion.mockResolvedValue({
      projectId: discussionIds.project,
      task: discussionTask,
      kind: "annotation_comment",
      target: {
        kind: "comment",
        annotationId: discussionIds.annotation,
        commentId: discussionIds.comment,
      },
    });
    renderUI();
    fireEvent.click(screen.getByTitle("通知，1 条未读"));
    fireEvent.click(await screen.findByRole("button", { name: "打开通知：在标注评论中提到了你" }));
    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent(
        `/projects/${discussionIds.project}/review?batch=batch-discussion&task=${discussionIds.task}&returnTo=%2Fdashboard&discussion=comments&focus=${discussionIds.annotation}&comment=${discussionIds.comment}`,
      ),
    );
  });

  it("任务留言提及通知生成任务留言定位链接而不打开 Issue", async () => {
    mocks.notifications = [taskCommentNotification];
    mocks.resolveDiscussion.mockResolvedValue({
      projectId: discussionIds.project,
      task: discussionTask,
      kind: "feedback",
      target: { kind: "task_comment", commentId: discussionIds.taskComment },
    });
    renderUI();
    fireEvent.click(screen.getByTitle("通知，1 条未读"));
    fireEvent.click(await screen.findByRole("button", { name: "打开通知：在任务留言中提到了你" }));
    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent(
        `/projects/${discussionIds.project}/annotate?batch=batch-discussion&task=${discussionIds.task}&returnTo=%2Fdashboard&discussion=comments&task_comment=${discussionIds.taskComment}`,
      ),
    );
    expect(mocks.read).toHaveBeenCalledWith(taskCommentNotification.id);
  });

  it("新的讨论通知点击会中止较早的查找请求", async () => {
    let firstSignal!: AbortSignal;
    let finishFirst!: () => void;
    mocks.notifications = [feedbackNotification, commentNotification];
    mocks.resolveDiscussion.mockImplementation((item: NotificationItem, signal: AbortSignal) => {
      if (item.id === feedbackNotification.id) {
        firstSignal = signal;
        return new Promise((resolve) => {
          finishFirst = () => resolve(null);
        });
      }
      return Promise.resolve({
        projectId: discussionIds.project,
        task: discussionTask,
        kind: "annotation_comment",
        target: {
          kind: "comment",
          annotationId: discussionIds.annotation,
          commentId: discussionIds.comment,
        },
      });
    });
    renderUI();
    fireEvent.click(screen.getByTitle("通知，1 条未读"));
    fireEvent.click(await screen.findByRole("button", { name: "打开通知：回复了问题" }));
    expect(await screen.findByRole("status")).toHaveTextContent("正在核对任务和访问权限");

    fireEvent.click(screen.getByTitle("通知，1 条未读"));
    fireEvent.click(await screen.findByRole("button", { name: "打开通知：在标注评论中提到了你" }));
    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent(
        `/projects/${discussionIds.project}/annotate?batch=batch-discussion&task=${discussionIds.task}&returnTo=%2Fdashboard&discussion=comments&focus=${discussionIds.annotation}&comment=${discussionIds.comment}`,
      ),
    );
    expect(firstSignal.aborted).toBe(true);
    finishFirst();
  });
});

describe("通知入口角标与工作台导航回调", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.notifications = [notification];
    mocks.unread = 1;
    useAuthStore.getState().setAuth("test-token", { id: "u1", role: "employee" } as MeResponse);
    mocks.task.mockResolvedValue({
      id: "t1",
      project_id: "p1",
      batch_id: "b1",
      status: "rejected",
    });
    mocks.access.mockResolvedValue(access(BOTH_WRITE));
  });
  afterEach(() => {
    act(() => useAuthStore.getState().logout());
  });

  it.each([
    [0, null, "通知，0 条未读"],
    [1, "1", "通知，1 条未读"],
    [99, "99", "通知，99 条未读"],
    [100, "99+", "通知，100 条未读"],
    [128, "99+", "通知，128 条未读"],
  ])("未读 %i 显示 %s 且暴露精确标签", (unread, badge, label) => {
    mocks.unread = unread;
    renderUI();
    const trigger = screen.getByRole("button", { name: label });
    expect(trigger).toHaveAttribute("title", label);
    const badgeNode = screen.queryByTestId("notifications-unread-badge");
    if (badge === null) {
      expect(badgeNode).not.toBeInTheDocument();
    } else {
      expect(badgeNode).toHaveTextContent(badge);
    }
  });

  it("初始加载失败不显示确认的 0，而提示未读数不可用", () => {
    mocks.unread = null;
    renderUI();
    const trigger = screen.getByRole("button", { name: "通知，未读数暂不可用" });
    expect(screen.queryByTestId("notifications-unread-badge")).not.toBeInTheDocument();
    expect(trigger).toHaveAttribute("title", "通知，未读数暂不可用");
  });

  it("提供导航回调时，任务直达 URL 交给回调而不是路由", async () => {
    const navigateExternal = vi.fn();
    renderUI(navigateExternal);
    fireEvent.click(screen.getByTitle("通知，1 条未读"));
    fireEvent.click(await screen.findByRole("button", { name: "打开通知：退回了任务 T-1" }));
    await waitFor(() => expect(navigateExternal).toHaveBeenCalledTimes(1));
    expect(navigateExternal).toHaveBeenCalledWith(
      "/projects/p1/annotate?batch=b1&task=t1&returnTo=%2Fdashboard",
    );
    // 回调路径不改路由
    expect(screen.getByTestId("location")).toHaveTextContent("/dashboard");
  });

  it("守卫导航被离开检查取消时保留目标与重试说明", async () => {
    const navigateExternal = vi.fn(async () => false);
    renderUI(navigateExternal);
    await clickTaskNotification();
    await waitFor(() => expect(navigateExternal).toHaveBeenCalledTimes(1));
    // 取消后不静默关闭：保留弹窗并给出可重试说明。
    expect(await screen.findByRole("alert")).toHaveTextContent("跳转已取消");
    expect(screen.getByRole("dialog", { name: "打开通知目标" })).toBeVisible();
    expect(screen.getByTestId("location")).toHaveTextContent("/dashboard");
  });

  it("管理员打开 Bug 反馈通知时，/bugs 也交给导航回调", async () => {
    useAuthStore.getState().setAuth("t", { id: "a1", role: "super_admin" } as MeResponse);
    mocks.notifications = [
      {
        ...notification,
        type: "bug_report.commented",
        target_type: "bug_report",
        target_id: "b-1",
        payload: {},
      },
    ];
    const navigateExternal = vi.fn();
    renderUI(navigateExternal);
    fireEvent.click(screen.getByTitle("通知，1 条未读"));
    fireEvent.click(await screen.findByRole("button", { name: "打开通知：评论了反馈" }));
    expect(navigateExternal).toHaveBeenCalledWith("/bugs");
    expect(screen.getByTestId("location")).toHaveTextContent("/dashboard");
  });

  it("错误恢复的「查看当前任务」链接也走导航回调", async () => {
    mocks.task.mockRejectedValue(new ApiError(503, "down"));
    const navigateExternal = vi.fn();
    renderUI(navigateExternal);
    fireEvent.click(screen.getByTitle("通知，1 条未读"));
    fireEvent.click(await screen.findByRole("button", { name: "打开通知：退回了任务 T-1" }));
    fireEvent.click(await screen.findByRole("button", { name: "查看当前任务" }));
    expect(navigateExternal).toHaveBeenCalledWith("/dashboard");
  });

  it("a derived export detail forwards navigation and remains open when leave is cancelled", async () => {
    mocks.notifications = [
      {
        ...notification,
        type: "export.ready",
        target_type: "export",
        target_id: "j1",
        payload: {},
      },
    ];
    mocks.job.mockResolvedValue({
      id: "j1",
      kind: "export",
      status: "completed",
      progress_pct: 100,
      project_id: "p1",
      payload: {},
      result: {},
      created_at: "2026-09-09T00:00:00Z",
      started_at: null,
      completed_at: null,
    });
    const navigate = vi.fn(async () => false);
    renderUI(navigate);
    fireEvent.click(screen.getByTitle("通知，1 条未读"));
    fireEvent.click(await screen.findByRole("button", { name: "打开通知：导出完成" }));
    fireEvent.click(await screen.findByRole("button", { name: "返回项目列表" }));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/dashboard"));
    expect(screen.getByRole("dialog", { name: "后台任务详情" })).toBeVisible();
    expect(screen.getByTestId("location").textContent).toBe("/dashboard");
  });
});

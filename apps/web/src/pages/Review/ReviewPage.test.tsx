/**
 * ReviewPage 单测 — 加载态 / 空态 / 正常渲染 / 批次选择 / 全选交互.
 * plan 1789527942 · T2：整批退回 / 批量退回迁移 decisionDialog（inputDialog /
 * choiceDialog 两步流），测试渲染真实 <DecisionDialogHost /> 驱动完整弹窗交互.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { ApiError } from "@/api/client";

const mockUseReviewerStats = vi.fn();
const mockUseTaskList = vi.fn();
const mockUseAnnotations = vi.fn();
const mockUseApproveTask = vi.fn();
const mockUseRejectTask = vi.fn();
const mockUseRejectBatch = vi.fn();
const mockPushToast = vi.fn();
const mockAuthState = vi.hoisted(() => ({
  token: "token-a" as string | null,
  user: { id: "user-a" } as { id: string } | null,
}));

vi.mock("@/hooks/useDashboard", () => ({
  useReviewerStats: () => mockUseReviewerStats(),
}));

vi.mock("@/hooks/useTasks", () => ({
  useTaskList: (...args: unknown[]) => mockUseTaskList(...args),
  flattenTaskPages: (pages: any[] | undefined) => {
    const seen = new Set<string>();
    return (
      pages
        ?.flatMap((page) => page.items)
        .filter((task) => {
          if (seen.has(task.id)) return false;
          seen.add(task.id);
          return true;
        }) ?? []
    );
  },
  useAnnotations: () => mockUseAnnotations(),
  useApproveTask: () => mockUseApproveTask(),
  useRejectTask: () => mockUseRejectTask(),
}));

vi.mock("@/hooks/useBatches", () => ({
  useRejectBatch: () => mockUseRejectBatch(),
}));

vi.mock("@/stores/authStore", () => ({
  useAuthStore: (selector: (state: typeof mockAuthState) => unknown) => selector(mockAuthState),
}));

vi.mock("./ReviewSidebar", () => ({
  ReviewSidebar: ({ batches, selectedBatchId, onSelect }: any) => (
    <div data-testid="review-sidebar">
      {batches.map((b: any) => (
        <button key={b.batch_id} data-testid={`batch-${b.batch_id}`} onClick={() => onSelect(b)}>
          {b.batch_name}
        </button>
      ))}
      {selectedBatchId && (
        <button data-testid="deselect-batch" onClick={() => onSelect(null)}>
          取消选择
        </button>
      )}
    </div>
  ),
}));

vi.mock("@/components/Thumbnail", () => ({
  Thumbnail: () => <div data-testid="thumbnail" />,
}));

vi.mock("@/utils/workbenchNavigation", () => ({
  buildReviewWorkbenchUrl: (_pid: string, opts: any) => `/workbench/review/${opts?.batchId ?? ""}`,
  currentWorkbenchReturnTo: () => "/review",
}));

vi.mock("@/components/ui/Toast", async () => {
  const actual = await vi.importActual<any>("@/components/ui/Toast");
  return {
    ...actual,
    useToastStore: <T,>(sel: (s: any) => T) => sel({ push: mockPushToast }),
  };
});

import { ReviewPage } from "./ReviewPage";
import { DecisionDialogHost } from "@/components/ui/DecisionDialogHost";
import { useDecisionDialogStore } from "@/components/ui/decisionDialog";

const idleMutation = { mutate: vi.fn(), isPending: false };

const sampleTask = {
  id: "t1",
  display_id: "T-1",
  file_name: "cat.jpg",
  thumbnail_url: null,
  blurhash: null,
  total_annotations: 3,
  total_predictions: 1,
  status: "review",
  skip_reason: null,
};

function renderUI(initialPath = "/review") {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <ReviewPage />
      <DecisionDialogHost />
    </MemoryRouter>,
  );
}

function NavigateTo({ path }: { path: string }) {
  const navigate = useNavigate();
  return (
    <button type="button" onClick={() => navigate(path)}>
      导航
    </button>
  );
}

describe("ReviewPage", () => {
  beforeEach(() => {
    mockPushToast.mockReset();
    mockAuthState.token = "token-a";
    mockAuthState.user = { id: "user-a" };
    mockUseApproveTask.mockReturnValue(idleMutation);
    mockUseRejectTask.mockReturnValue(idleMutation);
    mockUseRejectBatch.mockReturnValue(idleMutation);
    mockUseAnnotations.mockReturnValue({ data: [] });
    mockUseReviewerStats.mockReturnValue({ data: { reviewing_batches: [] } });
    mockUseTaskList.mockReturnValue({ data: undefined, isLoading: false });
  });

  // vitest afterEach 先于 RTL cleanup 跑,此刻 Host 仍挂载;清空 decisionDialog 队列防跨用例残留。
  afterEach(() => {
    act(() => {
      useDecisionDialogStore.setState({ queue: [] });
    });
  });

  it("初始态 → 显示侧边栏 + 「质检审核」标题 + 引导文案", () => {
    renderUI();
    expect(screen.getByTestId("review-sidebar")).toBeInTheDocument();
    expect(screen.getByText("质检审核")).toBeInTheDocument();
    expect(screen.getByText(/选择一个批次开始审核/)).toBeInTheDocument();
  });

  it("审核批次首屏离线暂停 → 显示等待网络连接而不是暂无任务", () => {
    mockUseReviewerStats.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: false,
      isPaused: true,
      fetchStatus: "paused",
      refetch: vi.fn(),
    });
    renderUI();
    expect(screen.getAllByRole("status")[0]).toHaveTextContent(
      "网络连接已断开，审核批次会在恢复后自动继续",
    );
    expect(screen.queryByText("暂无待审核任务")).not.toBeInTheDocument();
  });

  it("待审核任务首屏离线暂停 → 显示等待网络连接而不是空队列", () => {
    mockUseReviewerStats.mockReturnValue({
      data: {
        reviewing_batches: [
          {
            batch_id: "b1",
            batch_name: "批次A",
            batch_display_id: "B-1",
            project_id: "p1",
            project_name: "项目X",
            total_tasks: 5,
            review_tasks: 2,
            completed_tasks: 1,
          },
        ],
      },
    });
    mockUseTaskList.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: false,
      isPaused: true,
      fetchStatus: "paused",
      refetch: vi.fn(),
    });
    renderUI("/review?project=p1&batch=b1");
    expect(screen.getByRole("status")).toHaveTextContent(
      "网络连接已断开，待审核任务会在恢复后自动继续",
    );
    expect(screen.queryByText("该批次暂无待审核任务")).not.toBeInTheDocument();
  });

  it("isLoading=true → 显示「加载中...」", () => {
    mockUseTaskList.mockReturnValue({ data: undefined, isLoading: true });
    renderUI();
    expect(screen.getByText("加载中...")).toBeInTheDocument();
  });

  it("无任务空态 → 显示「暂无待审核任务」", () => {
    renderUI();
    expect(screen.getByText("暂无待审核任务")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "返回全部批次" })).not.toBeInTheDocument();
  });

  it.each(["project=p1", "batch=b1", "assignee=u1"])(
    "空队列有 %s 选择时仍可返回全部批次",
    (selection) => {
      renderUI(`/review?${selection}`);
      fireEvent.click(screen.getByRole("button", { name: "返回全部批次" }));
      expect(screen.getByText("暂无待审核任务")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "返回全部批次" })).not.toBeInTheDocument();
      expect(mockUseTaskList).toHaveBeenLastCalledWith(undefined, { status: "review" });
    },
  );

  it.each([
    { data: undefined, isLoading: true },
    { data: undefined, isError: true, error: new Error("暂时不可用"), refetch: vi.fn() },
  ])("无选择的审核批次加载或失败状态不显示返回入口", (queryState) => {
    mockUseReviewerStats.mockReturnValue(queryState);
    renderUI();
    expect(screen.queryByRole("button", { name: "返回全部批次" })).not.toBeInTheDocument();
  });

  it("有任务 → 渲染任务行 + 全选 checkbox", () => {
    mockUseReviewerStats.mockReturnValue({
      data: {
        reviewing_batches: [
          {
            batch_id: "b1",
            batch_name: "批次A",
            batch_display_id: "B-1",
            project_id: "p1",
            project_name: "项目X",
            total_tasks: 5,
            review_tasks: 2,
            completed_tasks: 1,
          },
        ],
      },
    });
    mockUseTaskList.mockReturnValue({
      data: { pages: [{ items: [sampleTask] }] },
      isLoading: false,
    });
    renderUI("/review?project=p1&batch=b1");
    expect(screen.getByText("T-1")).toBeInTheDocument();
    expect(screen.getByText("cat.jpg")).toBeInTheDocument();
    // 批量操作栏：共 X 个待审核任务
    expect(screen.getByText(/共 1 个待审核任务/)).toBeInTheDocument();
  });

  it("整批退回：必填原因空提交被拦下，补齐后按去空白值提交（不再依赖 window.prompt）", async () => {
    const mutate = vi.fn();
    mockUseRejectBatch.mockReturnValue({ mutate, isPending: false });
    mockUseReviewerStats.mockReturnValue({
      data: {
        reviewing_batches: [
          {
            batch_id: "b1",
            batch_name: "批次A",
            batch_display_id: "B-1",
            project_id: "p1",
            project_name: "项目X",
            total_tasks: 5,
            review_tasks: 2,
            completed_tasks: 1,
          },
        ],
      },
    });
    mockUseTaskList.mockReturnValue({
      data: { pages: [{ items: [sampleTask] }] },
      isLoading: false,
    });
    const promptSpy = vi.spyOn(window, "prompt").mockReturnValue(null);
    renderUI("/review?project=p1&batch=b1");

    fireEvent.click(screen.getByRole("button", { name: "整批退回" }));
    const dialog = await screen.findByRole("alertdialog", { name: "驳回批次 B-1" });
    const textarea = within(dialog).getByPlaceholderText(/请说明需要标注员重做的具体问题/);
    // inputDialog 的 textarea 强制 maxLength=500（服务统一行为）
    expect(textarea).toHaveAttribute("maxlength", "500");

    // 原因为空时点确认：行内报错、不关窗、mutation 不触发
    fireEvent.click(within(dialog).getByRole("button", { name: "确认驳回" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/请填写/);
    expect(mutate).not.toHaveBeenCalled();

    // 补齐后确认：mutation 只在确认后收到去空白的原因
    fireEvent.change(textarea, { target: { value: "  重新标注车辆框  " } });
    fireEvent.click(within(dialog).getByRole("button", { name: "确认驳回" }));
    await waitFor(() =>
      expect(mutate).toHaveBeenCalledWith(
        { batchId: "b1", feedback: "重新标注车辆框" },
        expect.objectContaining({ onSuccess: expect.any(Function) }),
      ),
    );
    expect(promptSpy).not.toHaveBeenCalled();
    promptSpy.mockRestore();
  });

  it("打开整批退回弹窗后切换批次作用域，确认后被作用域守卫拦下不误退回", async () => {
    mockUseReviewerStats.mockReturnValue({
      data: {
        reviewing_batches: [
          {
            batch_id: "b1",
            batch_name: "批次A",
            batch_display_id: "B-1",
            project_id: "p1",
            project_name: "项目X",
            total_tasks: 5,
            review_tasks: 2,
            completed_tasks: 1,
          },
          {
            batch_id: "b2",
            batch_name: "批次B",
            batch_display_id: "B-2",
            project_id: "p1",
            project_name: "项目X",
            total_tasks: 4,
            review_tasks: 2,
            completed_tasks: 0,
          },
        ],
      },
    });
    mockUseTaskList.mockReturnValue({
      data: { pages: [{ items: [sampleTask] }] },
      isLoading: false,
    });
    const mutate = vi.fn();
    mockUseRejectBatch.mockReturnValue({ mutate, isPending: false });
    renderUI("/review?project=p1&batch=b1");

    fireEvent.click(screen.getByRole("button", { name: "整批退回" }));
    const dialog = await screen.findByRole("alertdialog", { name: "驳回批次 B-1" });

    // decisionDialog 不感知页面作用域，弹窗仍打开；确认后由 queueScopeKeyRef 复查拦下
    fireEvent.click(screen.getByTestId("batch-b2"));
    const textarea = within(dialog).getByPlaceholderText(/请说明需要标注员重做的具体问题/);
    fireEvent.change(textarea, { target: { value: "重做" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "确认驳回" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(mutate).not.toHaveBeenCalled();
  });

  it("全选 checkbox → 已选 N/N 文案出现 + 批量操作按钮显示", () => {
    mockUseReviewerStats.mockReturnValue({
      data: {
        reviewing_batches: [
          {
            batch_id: "b1",
            batch_name: "批次A",
            batch_display_id: "B-1",
            project_id: "p1",
            project_name: "项目X",
            total_tasks: 5,
            review_tasks: 2,
            completed_tasks: 1,
          },
        ],
      },
    });
    mockUseTaskList.mockReturnValue({
      data: {
        pages: [
          {
            items: [
              sampleTask,
              { ...sampleTask, id: "t2", display_id: "T-2", file_name: "dog.jpg" },
            ],
          },
        ],
      },
      isLoading: false,
    });
    renderUI("/review?project=p1&batch=b1");

    // 点击全选 checkbox
    const checkboxes = screen.getAllByRole("checkbox");
    // 第一个是全选 checkbox（bulkBar 中的）
    fireEvent.click(checkboxes[0]);
    expect(screen.getByText(/已选 2\/2/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /批量通过/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /批量退回/ })).toBeInTheDocument();
  });

  it("批量退回按钮 → 打开退回原因 choiceDialog（含选项 label）", async () => {
    mockUseReviewerStats.mockReturnValue({
      data: {
        reviewing_batches: [
          {
            batch_id: "b1",
            batch_name: "批次A",
            batch_display_id: "B-1",
            project_id: "p1",
            project_name: "项目X",
            total_tasks: 5,
            review_tasks: 2,
            completed_tasks: 1,
          },
        ],
      },
    });
    mockUseTaskList.mockReturnValue({
      data: { pages: [{ items: [sampleTask] }] },
      isLoading: false,
    });
    renderUI("/review?project=p1&batch=b1");

    const checkboxes = screen.getAllByRole("checkbox");
    fireEvent.click(checkboxes[0]);
    fireEvent.click(screen.getByRole("button", { name: /批量退回/ }));
    const dialog = await screen.findByRole("alertdialog", { name: "退回原因（1 个任务）" });
    for (const label of ["漏标", "多标", "类别错误", "位置或尺寸不准"]) {
      expect(within(dialog).getByRole("button", { name: label })).toBeInTheDocument();
    }
  });

  it("批量退回单任务被跳过时，choiceDialog 描述透传 skip 提示", async () => {
    mockUseReviewerStats.mockReturnValue({
      data: {
        reviewing_batches: [
          {
            batch_id: "b1",
            batch_name: "批次A",
            batch_display_id: "B-1",
            project_id: "p1",
            project_name: "项目X",
            total_tasks: 5,
            review_tasks: 2,
            completed_tasks: 1,
          },
        ],
      },
    });
    mockUseTaskList.mockReturnValue({
      data: { pages: [{ items: [{ ...sampleTask, skip_reason: "no_target" }] }] },
      isLoading: false,
    });
    renderUI("/review?project=p1&batch=b1");

    fireEvent.click(screen.getAllByRole("checkbox")[0]);
    fireEvent.click(screen.getByRole("button", { name: /批量退回/ }));
    const dialog = await screen.findByRole("alertdialog", { name: "退回原因（1 个任务）" });
    expect(within(dialog).getByText(/此任务被标注员跳过：no_target/)).toBeInTheDocument();
  });

  it("切换 URL 批次后，迟到的旧退回结果不会清空新选择", async () => {
    let settled: (() => void) | undefined;
    const rejectMutate = vi.fn((_variables: unknown, options: { onSettled: () => void }) => {
      settled = options.onSettled;
    });
    mockUseRejectTask.mockReturnValue({ mutate: rejectMutate, isPending: false });
    mockUseReviewerStats.mockReturnValue({
      data: {
        reviewing_batches: [
          {
            batch_id: "b1",
            batch_name: "批次A",
            batch_display_id: "B-1",
            project_id: "p1",
            project_name: "项目X",
            total_tasks: 1,
            review_tasks: 1,
            completed_tasks: 0,
          },
          {
            batch_id: "b2",
            batch_name: "批次B",
            batch_display_id: "B-2",
            project_id: "p2",
            project_name: "项目Y",
            total_tasks: 1,
            review_tasks: 1,
            completed_tasks: 0,
          },
        ],
      },
    });
    mockUseTaskList.mockReturnValue({
      data: { pages: [{ items: [sampleTask] }] },
      isLoading: false,
    });
    render(
      <MemoryRouter initialEntries={["/review?project=p1&batch=b1"]}>
        <NavigateTo path="/review?project=p2&batch=b2" />
        <ReviewPage />
        <DecisionDialogHost />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getAllByRole("checkbox")[0]);
    fireEvent.click(screen.getByRole("button", { name: /批量退回/ }));
    let dialog = await screen.findByRole("alertdialog", { name: "退回原因（1 个任务）" });
    fireEvent.click(within(dialog).getByRole("button", { name: "漏标" }));
    dialog = await screen.findByRole("alertdialog", { name: "补充说明" });
    fireEvent.click(within(dialog).getByRole("button", { name: "确认退回" }));
    await waitFor(() => expect(rejectMutate).toHaveBeenCalledOnce());

    fireEvent.click(screen.getByRole("button", { name: "导航" }));
    await waitFor(() => expect(screen.getByRole("heading", { name: "批次B" })).toBeInTheDocument());
    fireEvent.click(screen.getAllByRole("checkbox")[0]);
    expect(screen.getByText("已选 1/1")).toBeInTheDocument();

    await act(async () => settled?.());
    expect(screen.getByText("已选 1/1")).toBeInTheDocument();
  });

  it("切换 URL 批次后，迟到的旧通过结果不会清空新选择", async () => {
    let settled: (() => void) | undefined;
    const approveMutate = vi.fn((_taskId: unknown, options: { onSettled: () => void }) => {
      settled = options.onSettled;
    });
    mockUseApproveTask.mockReturnValue({ mutate: approveMutate, isPending: false });
    mockUseReviewerStats.mockReturnValue({
      data: {
        reviewing_batches: [
          {
            batch_id: "b1",
            batch_name: "批次A",
            batch_display_id: "B-1",
            project_id: "p1",
            project_name: "项目X",
            total_tasks: 1,
            review_tasks: 1,
            completed_tasks: 0,
          },
          {
            batch_id: "b2",
            batch_name: "批次B",
            batch_display_id: "B-2",
            project_id: "p2",
            project_name: "项目Y",
            total_tasks: 1,
            review_tasks: 1,
            completed_tasks: 0,
          },
        ],
      },
    });
    mockUseTaskList.mockReturnValue({
      data: { pages: [{ items: [sampleTask] }] },
      isLoading: false,
    });
    render(
      <MemoryRouter initialEntries={["/review?project=p1&batch=b1"]}>
        <NavigateTo path="/review?project=p2&batch=b2" />
        <ReviewPage />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getAllByRole("checkbox")[0]);
    fireEvent.click(screen.getByRole("button", { name: /批量通过/ }));
    expect(approveMutate).toHaveBeenCalledOnce();

    fireEvent.click(screen.getByRole("button", { name: "导航" }));
    await waitFor(() => expect(screen.getByRole("heading", { name: "批次B" })).toBeInTheDocument());
    fireEvent.click(screen.getAllByRole("checkbox")[0]);
    expect(screen.getByText("已选 1/1")).toBeInTheDocument();

    await act(async () => settled?.());
    expect(screen.getByText("已选 1/1")).toBeInTheDocument();
  });

  it("同一批次内选择集合变化后，迟到的旧通过结果不会清空新选择", async () => {
    let settled: (() => void) | undefined;
    const approveMutate = vi.fn((_taskId: unknown, options: { onSettled: () => void }) => {
      settled = options.onSettled;
    });
    mockUseApproveTask.mockReturnValue({ mutate: approveMutate, isPending: false });
    mockUseReviewerStats.mockReturnValue({
      data: {
        reviewing_batches: [
          {
            batch_id: "b1",
            batch_name: "批次A",
            batch_display_id: "B-1",
            project_id: "p1",
            project_name: "项目X",
            total_tasks: 2,
            review_tasks: 2,
            completed_tasks: 0,
          },
        ],
      },
    });
    mockUseTaskList.mockReturnValue({
      data: {
        pages: [
          {
            items: [
              sampleTask,
              { ...sampleTask, id: "t2", display_id: "T-2", file_name: "dog.jpg" },
            ],
          },
        ],
      },
      isLoading: false,
    });
    renderUI("/review?project=p1&batch=b1");

    let checkboxes = screen.getAllByRole("checkbox");
    fireEvent.click(checkboxes[1]);
    fireEvent.click(screen.getByRole("button", { name: /批量通过/ }));
    checkboxes = screen.getAllByRole("checkbox");
    fireEvent.click(checkboxes[2]);
    expect(screen.getByText("已选 2/2")).toBeInTheDocument();

    await act(async () => settled?.());
    expect(screen.getByText("已选 2/2")).toBeInTheDocument();
  });

  it("同一路由切换认证 owner 后，迟到的旧通过结果不会清空新选择", async () => {
    let settled: (() => void) | undefined;
    const approveMutate = vi.fn((_taskId: unknown, options: { onSettled: () => void }) => {
      settled = options.onSettled;
    });
    mockUseApproveTask.mockReturnValue({ mutate: approveMutate, isPending: false });
    mockUseReviewerStats.mockReturnValue({
      data: {
        reviewing_batches: [
          {
            batch_id: "b1",
            batch_name: "批次A",
            batch_display_id: "B-1",
            project_id: "p1",
            project_name: "项目X",
            total_tasks: 1,
            review_tasks: 1,
            completed_tasks: 0,
          },
        ],
      },
    });
    mockUseTaskList.mockReturnValue({
      data: { pages: [{ items: [sampleTask] }] },
      isLoading: false,
    });
    const view = renderUI("/review?project=p1&batch=b1");

    fireEvent.click(screen.getAllByRole("checkbox")[0]);
    fireEvent.click(screen.getByRole("button", { name: /批量通过/ }));
    expect(approveMutate).toHaveBeenCalledOnce();

    mockAuthState.user = { id: "user-b" };
    mockAuthState.token = "token-b";
    view.rerender(
      <MemoryRouter initialEntries={["/review?project=p1&batch=b1"]}>
        <ReviewPage />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.queryByText("已选 1/1")).not.toBeInTheDocument());
    fireEvent.click(screen.getAllByRole("checkbox")[0]);
    expect(screen.getByText("已选 1/1")).toBeInTheDocument();

    await act(async () => settled?.());
    expect(screen.getByText("已选 1/1")).toBeInTheDocument();
  });

  it("任务首页请求失败 → 显示错误态而不是空队列，并可重试", () => {
    const refetch = vi.fn();
    mockUseReviewerStats.mockReturnValue({
      data: {
        reviewing_batches: [
          {
            batch_id: "b1",
            batch_name: "批次A",
            batch_display_id: "B-1",
            project_id: "p1",
            project_name: "项目X",
            total_tasks: 5,
            review_tasks: 2,
            completed_tasks: 1,
          },
        ],
      },
    });
    mockUseTaskList.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: new Error("server unavailable"),
      refetch,
    });
    renderUI("/review?project=p1&batch=b1");
    expect(screen.getByRole("alert")).toHaveTextContent("无法加载待审核任务");
    expect(screen.queryByText("该批次暂无待审核任务")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
    expect(refetch).toHaveBeenCalledOnce();
  });

  it("403 任务查询 → 显示权限提示，不伪装成空队列", () => {
    mockUseReviewerStats.mockReturnValue({
      data: {
        reviewing_batches: [
          {
            batch_id: "b1",
            batch_name: "批次A",
            batch_display_id: "B-1",
            project_id: "p1",
            project_name: "项目X",
            total_tasks: 5,
            review_tasks: 2,
            completed_tasks: 1,
          },
        ],
      },
    });
    mockUseTaskList.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: new ApiError(403, "forbidden"),
      refetch: vi.fn(),
    });
    renderUI("/review?project=p1&batch=b1");
    expect(screen.getByRole("alert")).toHaveTextContent("没有权限查看待审核任务");
    expect(screen.queryByText("该批次暂无待审核任务")).not.toBeInTheDocument();
  });

  it("500 任务查询 → 显示服务不可用，不混淆为网络或空队列", () => {
    mockUseReviewerStats.mockReturnValue({
      data: {
        reviewing_batches: [
          {
            batch_id: "b1",
            batch_name: "批次A",
            batch_display_id: "B-1",
            project_id: "p1",
            project_name: "项目X",
            total_tasks: 5,
            review_tasks: 2,
            completed_tasks: 1,
          },
        ],
      },
    });
    mockUseTaskList.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: new ApiError(500, "server error"),
      refetch: vi.fn(),
    });
    renderUI("/review?project=p1&batch=b1");
    expect(screen.getByRole("alert")).toHaveTextContent("服务暂时不可用");
    expect(screen.queryByText("该批次暂无待审核任务")).not.toBeInTheDocument();
  });

  it("刷新失败时保留已加载任务，并可继续加载下一页", () => {
    const refetch = vi.fn();
    const fetchNextPage = vi.fn();
    mockUseReviewerStats.mockReturnValue({
      data: {
        reviewing_batches: [
          {
            batch_id: "b1",
            batch_name: "批次A",
            batch_display_id: "B-1",
            project_id: "p1",
            project_name: "项目X",
            total_tasks: 250,
            review_tasks: 250,
            completed_tasks: 0,
          },
        ],
      },
    });
    mockUseTaskList.mockReturnValue({
      data: { pages: [{ items: [sampleTask], total: 250 }] },
      isLoading: false,
      isError: true,
      error: new Error("network"),
      refetch,
      hasNextPage: true,
      isFetchingNextPage: false,
      fetchNextPage,
    });
    renderUI("/review?project=p1&batch=b1");
    expect(screen.getByText("T-1")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("当前内容已保留");
    expect(screen.getByText("共 250 个待审核任务（已加载 1）")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "加载更多" }));
    expect(fetchNextPage).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
    expect(refetch).toHaveBeenCalledOnce();
  });

  it("跨页任务去重并保留总数口径", () => {
    mockUseReviewerStats.mockReturnValue({
      data: {
        reviewing_batches: [
          {
            batch_id: "b1",
            batch_name: "批次A",
            batch_display_id: "B-1",
            project_id: "p1",
            project_name: "项目X",
            total_tasks: 3,
            review_tasks: 3,
            completed_tasks: 0,
          },
        ],
      },
    });
    mockUseTaskList.mockReturnValue({
      data: {
        pages: [
          { items: [sampleTask], total: 3 },
          { items: [sampleTask, { ...sampleTask, id: "t2", display_id: "T-2" }] },
        ],
      },
      isLoading: false,
      hasNextPage: false,
    });
    renderUI("/review?project=p1&batch=b1");
    expect(screen.getByText("共 3 个待审核任务（已加载 2）")).toBeInTheDocument();
    expect(screen.getAllByText("T-1")).toHaveLength(1);
    expect(screen.getByText("T-2")).toBeInTheDocument();
  });

  it("从浏览器 URL 导航恢复项目和批次筛选", async () => {
    mockUseReviewerStats.mockReturnValue({
      data: {
        reviewing_batches: [
          {
            batch_id: "b1",
            batch_name: "批次A",
            batch_display_id: "B-1",
            project_id: "p1",
            project_name: "项目X",
            total_tasks: 1,
            review_tasks: 1,
            completed_tasks: 0,
          },
          {
            batch_id: "b2",
            batch_name: "批次B",
            batch_display_id: "B-2",
            project_id: "p2",
            project_name: "项目Y",
            total_tasks: 1,
            review_tasks: 1,
            completed_tasks: 0,
          },
        ],
      },
    });
    mockUseTaskList.mockReturnValue({
      data: { pages: [{ items: [sampleTask], total: 1 }] },
      isLoading: false,
      hasNextPage: false,
    });
    render(
      <MemoryRouter initialEntries={["/review?project=p1&batch=b1"]}>
        <NavigateTo path="/review?project=p2&batch=b2" />
        <ReviewPage />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("button", { name: "导航" }));
    await waitFor(() => {
      expect(mockUseTaskList).toHaveBeenLastCalledWith("p2", {
        status: "review",
        batch_id: "b2",
      });
    });
    expect(screen.getByRole("heading", { name: "批次B" })).toBeInTheDocument();
  });
});

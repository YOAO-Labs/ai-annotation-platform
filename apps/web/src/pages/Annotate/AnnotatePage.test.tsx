import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ApiError } from "@/api/client";

const mockUseMyBatches = vi.fn();
const mockUseTaskList = vi.fn();
const mockPushToast = vi.fn();

vi.mock("@/hooks/useDashboard", () => ({
  useMyBatches: () => mockUseMyBatches(),
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
}));

vi.mock("./AnnotateSidebar", () => ({
  AnnotateSidebar: ({ batches, onSelect }: any) => (
    <div data-testid="annotate-sidebar">
      {batches.map((batch: any) => (
        <button key={batch.batch_id} onClick={() => onSelect(batch)}>
          选择 {batch.batch_id}
        </button>
      ))}
    </div>
  ),
}));

vi.mock("./BatchCardGrid", () => ({
  BatchCardGrid: () => <div data-testid="batch-card-grid" />,
}));

vi.mock("@/components/Thumbnail", () => ({
  Thumbnail: () => <div data-testid="thumbnail" />,
}));

vi.mock("@/components/ui/Toast", () => ({
  useToastStore: <T,>(selector: (state: { push: typeof mockPushToast }) => T) =>
    selector({ push: mockPushToast }),
}));

vi.mock("@/utils/workbenchNavigation", () => ({
  buildWorkbenchUrl: (_projectId: string, options: { taskId?: string }) =>
    `/workbench/${options.taskId ?? ""}`,
  currentWorkbenchReturnTo: () => "/annotate",
}));

import { AnnotatePage } from "./AnnotatePage";

const batch = {
  batch_id: "b1",
  batch_display_id: "B-1",
  batch_name: "批次一",
  project_id: "p1",
  project_name: "项目一",
  status: "annotating",
  total_tasks: 250,
  completed_tasks: 0,
  review_tasks: 0,
  in_progress_tasks: 0,
  approved_tasks: 0,
  rejected_tasks: 0,
  progress_pct: 0,
  thumbnail_url: null,
  cover_blurhash: null,
  review_feedback: null,
  reviewed_at: null,
  reviewer: null,
};

const secondBatch = { ...batch, batch_id: "b2", batch_display_id: "B-2", batch_name: "批次二" };

function task(id: string, displayId = id) {
  return {
    id,
    project_id: "p1",
    display_id: displayId,
    file_name: `${displayId}.jpg`,
    thumbnail_url: null,
    blurhash: null,
    status: "pending",
    total_annotations: 0,
    total_predictions: 0,
  };
}

function taskQuery(overrides: Record<string, unknown> = {}) {
  return {
    data: { pages: [{ items: [], total: 0 }] },
    isLoading: false,
    isError: false,
    error: null,
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: vi.fn().mockResolvedValue(undefined),
    refetch: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };
}

function renderUI(initialPath = "/annotate") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialPath]}>
        <AnnotatePage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("AnnotatePage", () => {
  beforeEach(() => {
    mockUseMyBatches.mockReset();
    mockUseTaskList.mockReset();
    mockPushToast.mockReset();
    mockUseMyBatches.mockReturnValue({
      data: [batch],
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });
    mockUseTaskList.mockReturnValue(taskQuery());
  });

  it("批次首页请求失败 → 显示错误态而不是暂无分派批次，并可重试", () => {
    const refetch = vi.fn();
    mockUseMyBatches.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: new Error("server unavailable"),
      refetch,
    });
    renderUI();
    expect(screen.getAllByRole("alert")[0]).toHaveTextContent("无法加载分派批次");
    expect(screen.queryByText("暂无分派批次")).not.toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("button", { name: "重新加载" })[0]);
    expect(refetch).toHaveBeenCalledOnce();
  });

  it("批次首屏离线暂停 → 显示等待网络连接而不是暂无分派批次", () => {
    mockUseMyBatches.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: false,
      isPaused: true,
      fetchStatus: "paused",
      refetch: vi.fn(),
    });
    renderUI();
    expect(screen.getAllByRole("status")[0]).toHaveTextContent(
      "网络连接已断开，分派批次会在恢复后自动继续",
    );
    expect(screen.queryByText("暂无分派批次")).not.toBeInTheDocument();
  });

  it("任务首屏离线暂停 → 显示等待网络连接而不是暂无任务", () => {
    mockUseTaskList.mockReturnValue(
      taskQuery({
        data: undefined,
        isPaused: true,
        fetchStatus: "paused",
      }),
    );
    renderUI("/annotate?batch=b1");
    expect(screen.getByRole("status")).toHaveTextContent(
      "网络连接已断开，任务列表会在恢复后自动继续",
    );
    expect(screen.queryByText("该批次暂无任务")).not.toBeInTheDocument();
  });

  it("加载更多离线暂停 → 提示更多任务会在恢复后继续", () => {
    const fetchNextPage = vi.fn().mockResolvedValue(undefined);
    mockUseTaskList.mockReturnValue(
      taskQuery({
        data: { pages: [{ items: [task("t1"), task("t2")], total: 3 }] },
        hasNextPage: true,
        fetchNextPage,
        isPaused: true,
        fetchStatus: "paused",
      }),
    );
    renderUI("/annotate?batch=b1");
    fireEvent.click(screen.getByRole("button", { name: "加载更多" }));
    expect(screen.getByRole("status")).toHaveTextContent(
      "网络连接已断开，更多任务当前内容已保留，恢复连接后会自动更新",
    );
    expect(fetchNextPage).toHaveBeenCalledOnce();
  });

  it("任务查询 403 → 显示权限提示而不是暂无任务", () => {
    mockUseTaskList.mockReturnValue(
      taskQuery({
        data: undefined,
        isError: true,
        error: new ApiError(403, "forbidden"),
      }),
    );
    renderUI("/annotate?batch=b1");
    expect(screen.getByRole("alert")).toHaveTextContent("没有权限查看任务列表");
    expect(screen.queryByText("该批次暂无任务")).not.toBeInTheDocument();
  });

  it("任务刷新失败时保留已加载内容并提供重试", () => {
    const refetch = vi.fn();
    const currentTask = task("t1", "T-1");
    mockUseTaskList.mockReturnValue(
      taskQuery({
        data: { pages: [{ items: [currentTask], total: 1 }] },
        isError: true,
        error: new Error("network"),
        refetch,
      }),
    );
    renderUI("/annotate?batch=b1");
    expect(screen.getByText("T-1")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("当前内容已保留");
    expect(screen.queryByText("该批次暂无任务")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
    expect(refetch).toHaveBeenCalledOnce();
  });

  it("成功返回零条任务 → 显示真正的空队列", () => {
    mockUseTaskList.mockReturnValue(taskQuery());
    renderUI("/annotate?batch=b1");
    expect(screen.getByText("该批次暂无任务")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("reviewing 批次仍有未送审任务时保留整批提交入口", () => {
    mockUseMyBatches.mockReturnValue({
      data: [
        {
          ...batch,
          status: "reviewing",
          total_tasks: 3,
          review_tasks: 2,
          completed_tasks: 0,
          rejected_tasks: 0,
        },
      ],
      isLoading: false,
      isError: false,
    });
    renderUI("/annotate?batch=b1");
    expect(screen.getByText("提交质检")).toBeInTheDocument();
  });

  it("批次全部送审后隐藏整批提交入口", () => {
    mockUseMyBatches.mockReturnValue({
      data: [
        {
          ...batch,
          status: "reviewing",
          total_tasks: 3,
          review_tasks: 3,
          completed_tasks: 0,
          rejected_tasks: 0,
        },
      ],
      isLoading: false,
      isError: false,
    });
    renderUI("/annotate?batch=b1");
    expect(screen.queryByText("提交质检")).not.toBeInTheDocument();
  });

  it("退回入口过滤批次和任务，切批次及返回后保留状态条件", () => {
    mockUseMyBatches.mockReturnValue({
      data: [{ ...batch, rejected_tasks: 2 }, secondBatch],
      isLoading: false,
      isError: false,
    });
    renderUI("/annotate?status=rejected");
    expect(screen.getByRole("button", { name: "选择 b1" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "选择 b2" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "选择 b1" }));
    expect(mockUseTaskList).toHaveBeenLastCalledWith("p1", { batch_id: "b1", status: "rejected" });
    expect(screen.getByText("该批次没有待重做任务")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "返回全部批次" }));
    expect(screen.queryByRole("button", { name: "选择 b2" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "全部任务" }));
    expect(screen.getByRole("button", { name: "选择 b2" })).toBeInTheDocument();
  });

  it("250 条任务可加载到末页、跨页去重，并在切批次后丢弃旧列表", () => {
    const firstHundred = Array.from({ length: 100 }, (_, index) =>
      task(`t-${index + 1}`, `T-${index + 1}`),
    );
    const secondPage = [task("t-100", "T-100"), task("t-101", "T-101")];
    const lastPage = [task("t-250", "T-250")];
    const firstQuery = taskQuery({
      data: {
        pages: [{ items: firstHundred, total: 250 }, { items: secondPage }],
      },
      hasNextPage: true,
    });
    const lastQuery = taskQuery({
      data: {
        pages: [{ items: firstHundred, total: 250 }, { items: secondPage }, { items: lastPage }],
      },
      hasNextPage: false,
    });
    const secondBatchQuery = taskQuery({
      data: { pages: [{ items: [task("b2-task", "B2-1")], total: 1 }] },
    });
    let loadedLastPage = false;
    mockUseMyBatches.mockReturnValue({
      data: [batch, secondBatch],
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });
    mockUseTaskList.mockImplementation((_projectId: string, params: { batch_id?: string }) => {
      if (params?.batch_id === "b2") return secondBatchQuery;
      return loadedLastPage ? lastQuery : firstQuery;
    });

    const view = renderUI("/annotate?batch=b1");
    expect(screen.getByText("T-1")).toBeInTheDocument();
    expect(screen.getByText("T-101")).toBeInTheDocument();
    expect(screen.getAllByText("T-100")).toHaveLength(1);
    expect(screen.getByText("共 250 个任务（已加载 101）")).toBeInTheDocument();

    loadedLastPage = true;
    fireEvent.click(screen.getByRole("button", { name: "加载更多" }));
    view.rerender(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={["/annotate?batch=b1"]}>
          <AnnotatePage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(screen.getByText("T-250")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "选择 b2" }));
    expect(screen.getByText("B2-1")).toBeInTheDocument();
    expect(screen.queryByText("T-1")).not.toBeInTheDocument();
  });
});

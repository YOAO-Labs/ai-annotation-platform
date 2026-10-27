import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { MeResponse } from "@/api/auth";
import type { DatasetWorkbenchProject } from "@/api/datasets";
import { taskViewsApi, type DataManagerSummary } from "@/api/taskViews";
import { useAuthStore } from "@/stores/authStore";
import { SUMMARY } from "@/test/dataManagerApi";
import { DatasetProjectStatistics } from "./DatasetProjectStatistics";

vi.mock("@/api/taskViews", () => ({ taskViewsApi: { summary: vi.fn() } }));

const projects: DatasetWorkbenchProject[] = [
  {
    id: "p-one",
    name: "检测项目",
    display_id: "P1",
    type_label: "检测",
    total_tasks: 20,
    labeled_tasks: 8,
    completed_tasks: 5,
    review_tasks: 3,
    can_manage: true,
  },
  {
    id: "p-two",
    name: "复查项目",
    display_id: "P2",
    type_label: "检测",
    total_tasks: 10,
    labeled_tasks: 4,
    completed_tasks: 3,
    review_tasks: 1,
    can_manage: false,
  },
];
function summary(labels: Record<string, number>, matched = 4): DataManagerSummary {
  return {
    ...SUMMARY,
    scope: { visible_task_total: 999, matched_task_total: matched },
    annotations: {
      ...SUMMARY.annotations,
      total: Object.values(labels).reduce((a, b) => a + b, 0),
      by_class: labels,
    },
  };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
const clients: QueryClient[] = [];
function mount(initial: Partial<Parameters<typeof DatasetProjectStatistics>[0]> = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 30_000 } },
  });
  clients.push(client);
  const props = {
    datasetId: "dataset-one",
    projects,
    selectedProjectId: undefined,
    onProjectChange: vi.fn(),
    ...initial,
  };
  const view = (value: typeof props) => (
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <DatasetProjectStatistics {...value} />
      </MemoryRouter>
    </QueryClientProvider>
  );
  const result = render(view(props));
  return {
    ...result,
    client,
    onProjectChange: props.onProjectChange,
    update: (patch: Partial<typeof props>) => result.rerender(view({ ...props, ...patch })),
  };
}
beforeEach(() => {
  vi.clearAllMocks();
  useAuthStore.getState().setAuth("first", { id: "reader" } as MeResponse);
  vi.mocked(taskViewsApi.summary).mockResolvedValue(summary({ 汽车: 7, 行人: 2 }));
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
  useAuthStore.getState().logout();
});

describe("Dataset project statistics", () => {
  it("uses the first linked project only when absent and counts saved objects in the dataset", async () => {
    const mounted = mount();
    expect(await screen.findByRole("cell", { name: "汽车" })).toBeVisible();
    expect(taskViewsApi.summary).toHaveBeenCalledWith(
      "p-one",
      { field: "dataset.dataset_id", op: "eq", value: "dataset-one" },
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(screen.getByRole("combobox", { name: "统计项目" })).toHaveValue("p-one");
    expect(screen.queryByText("999")).not.toBeInTheDocument();
    const count = screen.getByText("已保存标注对象").closest("div")!;
    expect(within(count).getByText("9")).toBeVisible();
    expect(screen.getByText(/不代表样本数、视频帧数或预测数量/)).toBeVisible();
    const select = screen.getByRole("combobox", { name: "统计项目" });
    expect(document.getElementById(select.getAttribute("aria-describedby")!)).toHaveClass(
      "text-xs",
      "text-muted-foreground",
    );
    await userEvent.setup().selectOptions(select, "p-two");
    expect(mounted.onProjectChange).toHaveBeenCalledWith("p-two");
  });

  it("links raw class names with the dataset and class filters together", async () => {
    const name = '汽车 / "白色" & <特种>';
    vi.mocked(taskViewsApi.summary).mockResolvedValue(summary({ [name]: 1 }));
    mount({ selectedProjectId: "p-two" });
    const link = await screen.findByRole("link", { name: "查看含该标签的任务" });
    const url = new URL(link.getAttribute("href")!, "http://localhost");
    expect(url.pathname).toBe("/projects/p-two/data-manager");
    expect(JSON.parse(url.searchParams.get("filter")!).value).toEqual({
      op: "and",
      rules: [
        { field: "dataset.dataset_id", op: "eq", value: "dataset-one" },
        { field: "annotation.class_name", op: "eq", value: name },
      ],
    });
  });

  it.each(["", "unlinked"])(
    "keeps explicit invalid selection %j closed",
    async (selectedProjectId) => {
      mount({ selectedProjectId });
      expect(screen.getByRole("alert")).toHaveTextContent("所选项目不可用");
      expect(taskViewsApi.summary).not.toHaveBeenCalled();
      expect(screen.queryByRole("table")).not.toBeInTheDocument();
    },
  );

  it("shows an empty project state without issuing a summary request", () => {
    mount({ projects: [] });
    expect(screen.getByText("暂无可统计的关联项目。")).toBeVisible();
    expect(taskViewsApi.summary).not.toHaveBeenCalled();
  });

  it("aborts an old project request and rejects a late response", async () => {
    const old = deferred<DataManagerSummary>();
    let oldSignal: AbortSignal | undefined;
    vi.mocked(taskViewsApi.summary).mockImplementation((id, _filter, init) => {
      if (id === "p-one") {
        oldSignal = init?.signal as AbortSignal;
        return old.promise;
      }
      return Promise.resolve(summary({ 新项目标签: 2 }));
    });
    const mounted = mount();
    await waitFor(() => expect(oldSignal).toBeDefined());
    mounted.update({ selectedProjectId: "p-two" });
    expect(await screen.findByRole("cell", { name: "新项目标签" })).toBeVisible();
    expect(oldSignal!.aborted).toBe(true);
    await act(async () => old.resolve(summary({ 迟到旧项目标签: 500 })));
    expect(screen.queryByText("迟到旧项目标签")).not.toBeInTheDocument();
  });

  it("hides previous data immediately during a dataset switch", async () => {
    const next = deferred<DataManagerSummary>();
    const mounted = mount();
    await screen.findByRole("cell", { name: "汽车" });
    vi.mocked(taskViewsApi.summary).mockReturnValue(next.promise);
    mounted.update({ datasetId: "dataset-two" });
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.getByText("正在加载标签统计…")).toBeVisible();
    await act(async () => next.resolve(summary({ 第二数据集标签: 1 })));
    expect(await screen.findByRole("cell", { name: "第二数据集标签" })).toBeVisible();
  });

  it("does not show cached results while returning to a project is being revalidated", async () => {
    const mounted = mount();
    await screen.findByRole("cell", { name: "汽车" });
    mounted.update({ selectedProjectId: "p-two" });
    await waitFor(() => expect(taskViewsApi.summary).toHaveBeenCalledTimes(2));
    const refresh = deferred<DataManagerSummary>();
    vi.mocked(taskViewsApi.summary).mockReturnValue(refresh.promise);
    mounted.update({ selectedProjectId: "p-one" });
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    await act(async () => refresh.resolve(summary({ 已刷新标签: 1 })));
    expect(await screen.findByRole("cell", { name: "已刷新标签" })).toBeVisible();
  });

  it("hides old data on credentials change and ignores the retired request", async () => {
    const old = deferred<DataManagerSummary>();
    vi.mocked(taskViewsApi.summary)
      .mockReturnValueOnce(old.promise)
      .mockResolvedValueOnce(summary({ 当前会话标签: 1 }));
    mount();
    await waitFor(() => expect(taskViewsApi.summary).toHaveBeenCalledTimes(1));
    const oldSignal = vi.mocked(taskViewsApi.summary).mock.calls[0][2]!.signal!;
    await act(async () =>
      useAuthStore.getState().setAuth("second", { id: "reader" } as MeResponse),
    );
    expect(await screen.findByRole("cell", { name: "当前会话标签" })).toBeVisible();
    expect(oldSignal.aborted).toBe(true);
    await act(async () => old.resolve(summary({ 旧会话标签: 99 })));
    expect(screen.queryByText("旧会话标签")).not.toBeInTheDocument();
  });

  it("removes statistics and does not fall back when the explicit project is unlinked", async () => {
    const mounted = mount({ selectedProjectId: "p-one" });
    await screen.findByRole("cell", { name: "汽车" });
    mounted.update({ projects: [projects[1]] });
    expect(screen.getByRole("alert")).toHaveTextContent("所选项目不可用");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(taskViewsApi.summary).toHaveBeenCalledTimes(1);
  });

  it("shows an error instead of old counts after revocation, then offers retry", async () => {
    const mounted = mount();
    await screen.findByRole("cell", { name: "汽车" });
    vi.mocked(taskViewsApi.summary).mockRejectedValueOnce(new Error("没有项目访问权限"));
    await act(async () =>
      mounted.client.invalidateQueries({ queryKey: ["dataset-project-statistics"] }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("没有项目访问权限");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    vi.mocked(taskViewsApi.summary).mockResolvedValue(summary({ 重试标签: 3 }));
    await userEvent.setup().click(screen.getByRole("button", { name: "重试标签统计" }));
    expect(await screen.findByRole("cell", { name: "重试标签" })).toBeVisible();
  });

  it("renders a real empty label state separately from failed requests", async () => {
    vi.mocked(taskViewsApi.summary).mockResolvedValue(summary({}));
    mount();
    expect(await screen.findByText("当前范围暂无已保存的标签标注。")).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("paginates many labels without truncating the complete distribution", async () => {
    vi.mocked(taskViewsApi.summary).mockResolvedValue(
      summary(
        Object.fromEntries(
          Array.from({ length: 23 }, (_, i) => [`标签${String(i).padStart(2, "0")}`, 23 - i]),
        ),
      ),
    );
    mount();
    const table = await screen.findByRole("table", { name: "项目标签数量" });
    expect(within(table).getAllByRole("row")).toHaveLength(21);
    await userEvent.setup().click(
      within(screen.getByRole("navigation", { name: "项目标签分页" })).getByRole("button", {
        name: "下一页",
      }),
    );
    expect(within(table).getAllByRole("row")).toHaveLength(4);
    expect(within(table).getByRole("cell", { name: "标签22" })).toBeVisible();
  });
});

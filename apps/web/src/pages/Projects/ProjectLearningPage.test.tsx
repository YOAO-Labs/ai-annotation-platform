import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { trainingDatasetsApi } from "@/api/trainingDatasets";
import { batchesApi } from "@/api/batches";
import { tasksApi } from "@/api/tasks";
import { asyncJobsApi } from "@/api/asyncJobs";
import { ProjectLearningPage } from "./ProjectLearningPage";
import { useAuthStore } from "@/stores/authStore";
import type { MeResponse } from "@/api/auth";
import { ApiError } from "@/api/client";
import { useProject } from "@/hooks/useProjects";

vi.mock("./TrainingDatasetInsights", () => ({
  TrainingDatasetInsights: ({ version }: { version: { name: string } }) => (
    <p>冻结详情 {version.name}</p>
  ),
}));

vi.mock("./ProjectTrainingPanel", () => ({
  ProjectTrainingPanel: ({
    onAttemptSelect,
  }: {
    onAttemptSelect: (id: string, runId: string) => void;
  }) => (
    <button onClick={() => onAttemptSelect("historical-attempt", "source-run")}>
      测试历史执行
    </button>
  ),
}));
vi.mock("./ProjectModelsPanel", () => ({ ProjectModelsPanel: () => null }));
vi.mock("./ProjectLearningPolicyPanel", () => ({ ProjectLearningPolicyPanel: () => null }));

vi.mock("@/hooks/useProjects", () => ({
  useProject: vi.fn(),
}));
vi.mock("@/api/batches", () => ({ batchesApi: { list: vi.fn().mockResolvedValue([]) } }));
vi.mock("@/api/asyncJobs", () => ({ asyncJobsApi: { cancel: vi.fn() } }));
vi.mock("@/api/tasks", () => ({
  tasksApi: { listByProject: vi.fn().mockResolvedValue({ items: [], total: 0 }) },
}));
vi.mock("@/api/trainingDatasets", () => ({
  trainingDatasetsApi: {
    taskDatasets: vi.fn(),
    unrecognizedVersions: vi.fn(),
    list: vi.fn(),
    get: vi.fn(),
    getSummary: vi.fn(),
    preflight: vi.fn(),
    create: vi.fn(),
    retry: vi.fn(),
    download: vi.fn(),
    remove: vi.fn(),
  },
}));

const version = {
  id: "dataset-1",
  project_id: "project-1",
  task: "detect",
  contract_profile: {
    schema: 3,
    task: "detect",
    media_type: "image",
    source_media_type: "image",
    format: "ultralytics-yolo",
    geometry: "normalized-bbox",
    bridge: "ultralytics-bbox-1",
  },
  name: "第一轮",
  status: "ready",
  progress_pct: 100,
  created_at: "2026-10-14T12:00:00Z",
  manifest: {},
  snapshot_digest: "a".repeat(64),
  error_message: null,
};
const report = {
  ready: true,
  digest: "b".repeat(64),
  classes: ["car"],
  summary: { positives: 3, negatives: 0, excluded: 0 },
  issues: [],
  excluded: [],
};
const clients: QueryClient[] = [];

function Location() {
  const location = useLocation();
  return (
    <p>
      {location.pathname}
      {location.search}
    </p>
  );
}
function mount(path = "/projects/project-1/learning?tab=data") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  clients.push(client);
  const tree = () => (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route
            path="/projects/:id/learning"
            element={
              <>
                <ProjectLearningPage />
                <Location />
              </>
            }
          />
          <Route path="/projects/:id/learning/new" element={<Location />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
  const rendered = render(tree());
  return { ...rendered, refresh: () => rendered.rerender(tree()) };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(useProject).mockReturnValue({
    data: {
      name: "路口车辆",
      data_type: "image",
      tool_bindings: {
        bbox: { enabled: true, classes: ["car"] },
        region: { enabled: true, classes: ["shape"] },
      },
    },
  } as never);
  useAuthStore.getState().setAuth("session-a", { id: "user-a" } as MeResponse);
  vi.mocked(trainingDatasetsApi.taskDatasets).mockResolvedValue({
    items: [],
    pending_membership_count: 0,
    unrecognized_version_count: 0,
  });
  vi.mocked(trainingDatasetsApi.unrecognizedVersions).mockResolvedValue({ items: [], total: 0 });
  vi.mocked(batchesApi.list).mockResolvedValue([]);
  vi.mocked(tasksApi.listByProject).mockResolvedValue({ items: [], total: 0 } as never);
  vi.mocked(trainingDatasetsApi.list).mockResolvedValue({ items: [], total: 0 });
  vi.mocked(trainingDatasetsApi.get).mockResolvedValue(version as never);
  vi.mocked(trainingDatasetsApi.getSummary).mockResolvedValue(version as never);
  vi.mocked(trainingDatasetsApi.preflight).mockResolvedValue(report);
  vi.mocked(trainingDatasetsApi.create).mockResolvedValue(version as never);
});

afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
  vi.restoreAllMocks();
  useAuthStore.getState().logout();
});

describe("project learning data versions", () => {
  it("defaults to the actual project task when no qualified image tools are enabled", async () => {
    vi.mocked(useProject).mockReturnValue({
      data: {
        name: "关键点项目",
        data_type: "image",
        tool_bindings: { keypoint: { enabled: true, classes: ["person"] } },
      },
    } as never);
    vi.mocked(trainingDatasetsApi.taskDatasets).mockResolvedValue({
      items: [
        {
          task: "pose",
          status_counts: {},
          version_count: 0,
          pending_membership_count: 0,
          capabilities: Object.fromEntries(
            [
              "create_version",
              "qualified_training",
              "overview_statistics",
              "task_statistics",
              "sample_preview",
              "semantic_comparison",
            ].map((key) => [key, { supported: false, reason: "关键点链路尚未资格验证" }]),
          ),
        },
      ],
      pending_membership_count: 0,
      unrecognized_version_count: 0,
    } as never);
    mount();
    await waitFor(() => expect(screen.getByLabelText("数据集任务")).toHaveValue("pose"));
    expect(screen.getByRole("button", { name: "准备新版本" })).toBeDisabled();
    expect(trainingDatasetsApi.list).toHaveBeenLastCalledWith(
      "project-1",
      0,
      undefined,
      expect.anything(),
      { task: "pose" },
    );
  });
  it("shows unavailable task capabilities and diagnoses unrecognized history without opening it as detection", async () => {
    const capabilities = Object.fromEntries(
      [
        "create_version",
        "qualified_training",
        "overview_statistics",
        "task_statistics",
        "sample_preview",
        "semantic_comparison",
      ].map((key) => [key, { supported: false, reason: "关键点链路尚未资格验证" }]),
    );
    vi.mocked(trainingDatasetsApi.taskDatasets).mockResolvedValue({
      items: [
        {
          id: null,
          project_id: "project-1",
          task: "pose",
          version_count: 0,
          status_counts: {},
          pending_membership_count: 0,
          latest_ready_version: null,
          capabilities,
        },
      ],
      pending_membership_count: 0,
      unrecognized_version_count: 1,
    } as never);
    vi.mocked(trainingDatasetsApi.unrecognizedVersions).mockResolvedValue({
      items: [
        {
          id: "unknown-version",
          name: "未识别契约",
          status: "ready",
          created_at: "2026-10-16T00:00:00Z",
          raw_task: "future-task",
          error_code: "unknown_data_contract",
          reason: "冻结任务契约未被识别",
        },
      ],
      total: 1,
    });
    const user = userEvent.setup();
    mount("/projects/project-1/learning?tab=data&data_task=pose");
    await screen.findByText("关键点链路尚未资格验证", { selector: "p" });
    expect(screen.getByRole("button", { name: "准备新版本" })).toBeDisabled();
    await waitFor(() =>
      expect(trainingDatasetsApi.list).toHaveBeenCalledWith(
        "project-1",
        0,
        undefined,
        expect.anything(),
        { task: "pose" },
      ),
    );
    await user.click(screen.getByRole("button", { name: /未识别的历史版本/ }));
    expect(await screen.findByText("未识别契约")).toBeVisible();
    expect(trainingDatasetsApi.getSummary).not.toHaveBeenCalled();
  });
  it("filters task versions before pagination and preserves a separate draft for each task", async () => {
    const user = userEvent.setup();
    mount();
    await waitFor(() =>
      expect(trainingDatasetsApi.list).toHaveBeenCalledWith(
        "project-1",
        0,
        undefined,
        expect.anything(),
        { task: "detect" },
      ),
    );
    await user.click(screen.getByRole("button", { name: "准备新版本" }));
    await user.clear(screen.getByLabelText("版本名称"));
    await user.type(screen.getByLabelText("版本名称"), "检测迭代");
    await user.keyboard("{Escape}");
    expect(screen.getByRole("button", { name: "准备新版本" })).toHaveFocus();
    await user.selectOptions(screen.getByLabelText("数据集任务"), "segment");
    await waitFor(() =>
      expect(trainingDatasetsApi.list).toHaveBeenCalledWith(
        "project-1",
        0,
        undefined,
        expect.anything(),
        { task: "segment" },
      ),
    );
    await user.click(screen.getByRole("button", { name: "准备新版本" }));
    expect(screen.getByLabelText("训练任务")).toHaveValue("segment");
    expect(screen.getByLabelText("版本名称")).not.toHaveValue("检测迭代");
    await user.clear(screen.getByLabelText("版本名称"));
    await user.type(screen.getByLabelText("版本名称"), "分割迭代");
    await user.keyboard("{Escape}");
    await user.selectOptions(screen.getByLabelText("数据集任务"), "detect");
    await user.click(screen.getByRole("button", { name: "准备新版本" }));
    expect(screen.getByLabelText("版本名称")).toHaveValue("检测迭代");
    expect(trainingDatasetsApi.create).not.toHaveBeenCalled();
  });

  it("loads eligible parent versions across pages without losing the selected parent", async () => {
    vi.mocked(trainingDatasetsApi.list).mockImplementation(async (_project, offset, status) =>
      status === "ready"
        ? {
            items: [
              {
                ...version,
                id: offset ? "parent-older" : "parent-newer",
                name: offset ? "更早父版" : "近期父版",
              } as never,
            ],
            total: 21,
          }
        : { items: [], total: 0 },
    );
    const user = userEvent.setup();
    mount();
    await user.click(screen.getByRole("button", { name: "准备新版本" }));
    await user.selectOptions(await screen.findByLabelText("继承数据划分"), "parent-newer");
    await user.click(screen.getByRole("button", { name: "下一页父版本" }));
    await screen.findByRole("option", { name: "更早父版" });
    expect(screen.getByLabelText("继承数据划分")).toHaveValue("parent-newer");
    expect(trainingDatasetsApi.list).toHaveBeenCalledWith(
      "project-1",
      20,
      "ready",
      expect.anything(),
      { task: "detect" },
    );
    await user.selectOptions(screen.getByLabelText("继承数据划分"), "parent-older");
    await user.click(screen.getByRole("button", { name: "检查训练数据" }));
    await waitFor(() =>
      expect(trainingDatasetsApi.preflight).toHaveBeenCalledWith(
        "project-1",
        expect.objectContaining({ parent_id: "parent-older", task: "detect" }),
      ),
    );
  });
  it("prefills a timestamp and preserves an explicitly cleared name when scope changes", async () => {
    vi.mocked(batchesApi.list).mockResolvedValue([{ id: "batch-1", name: "批次一" }] as never);
    const user = userEvent.setup();
    mount();
    await user.click(screen.getByRole("button", { name: "准备新版本" }));
    const name = screen.getByLabelText("版本名称") as HTMLInputElement;
    expect(name.value).toMatch(/^\d{4}-\d{4}-\d{4} $/);
    await user.clear(name);
    await screen.findByRole("option", { name: "批次一" });
    await user.selectOptions(screen.getByLabelText("数据范围"), "batch-1");
    expect(name).toHaveValue("");
    expect(trainingDatasetsApi.create).not.toHaveBeenCalled();
  });
  it("focuses the version name from the data list preparation entry", async () => {
    const user = userEvent.setup();
    mount();
    await user.click(screen.getByRole("button", { name: "准备新版本" }));
    expect(screen.getByLabelText("版本名称")).toHaveFocus();
    expect(trainingDatasetsApi.create).not.toHaveBeenCalled();
  });
  it("retries a failed version list without losing access to preparation fields", async () => {
    vi.mocked(trainingDatasetsApi.list).mockRejectedValueOnce(new Error("版本列表读取失败"));
    const user = userEvent.setup();
    mount();
    expect(await screen.findByRole("alert")).toHaveTextContent("版本列表读取失败");
    expect(screen.getByRole("button", { name: "准备新版本" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByRole("heading", { name: "尚无训练数据版本" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "准备新版本" }));
    expect(screen.getByLabelText("版本名称")).toBeVisible();
  });
  it("redirects legacy creation links with controlled framework and return experiment context", async () => {
    mount("/projects/project-1/learning?tab=training&new=1&framework=libreyolo&run=run-1");
    expect(
      await screen.findByText(
        "/projects/project-1/learning/new?framework=libreyolo&return_run=run-1",
      ),
    ).toBeVisible();
    expect(trainingDatasetsApi.list).not.toHaveBeenCalled();
  });
  it("retains the exact request after a lost response even if source data changes while closed", async () => {
    vi.mocked(trainingDatasetsApi.create).mockRejectedValueOnce(new Error("连接中断"));
    const user = userEvent.setup();
    mount();
    await user.click(screen.getByRole("button", { name: "准备新版本" }));
    await user.clear(screen.getByLabelText("版本名称"));
    await user.type(screen.getByLabelText("版本名称"), "车辆首轮");
    await user.click(screen.getByRole("button", { name: "检查训练数据" }));
    await screen.findByText("数据检查通过");
    await user.click(screen.getByRole("button", { name: "创建数据版本" }));
    await screen.findByText("连接中断");
    const first = vi.mocked(trainingDatasetsApi.create).mock.calls[0][1];
    vi.mocked(trainingDatasetsApi.preflight).mockResolvedValue({
      ...report,
      digest: "e".repeat(64),
    });
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "准备新版本" }));
    expect(screen.getByLabelText("版本名称")).toHaveValue("车辆首轮");
    expect(screen.getByLabelText("版本名称")).toBeDisabled();
    expect(screen.getByRole("button", { name: "检查训练数据" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "确认原创建请求" }));
    await waitFor(() => expect(trainingDatasetsApi.create).toHaveBeenCalledTimes(2));
    expect(vi.mocked(trainingDatasetsApi.create).mock.calls[1][1]).toEqual(first);
    expect(trainingDatasetsApi.preflight).toHaveBeenCalledTimes(1);
  });
  it("unlocks a definitively rejected create so the user can recheck changed data", async () => {
    vi.mocked(trainingDatasetsApi.create).mockRejectedValueOnce(
      new ApiError(409, "数据已变化，请重新预检"),
    );
    const user = userEvent.setup();
    mount();
    await user.click(screen.getByRole("button", { name: "准备新版本" }));
    await user.click(screen.getByRole("button", { name: "检查训练数据" }));
    await screen.findByText("数据检查通过");
    await user.click(screen.getByRole("button", { name: "创建数据版本" }));
    await screen.findByText("数据已变化，请重新预检");
    expect(screen.getByLabelText("版本名称")).toBeEnabled();
    expect(screen.getByRole("button", { name: "检查训练数据" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "确认原创建请求" })).not.toBeInTheDocument();
  });
  it("allows confirming the original pending request after project creation eligibility is removed", async () => {
    vi.mocked(trainingDatasetsApi.create).mockRejectedValueOnce(new Error("连接中断"));
    const user = userEvent.setup();
    const rendered = mount();
    await user.click(screen.getByRole("button", { name: "准备新版本" }));
    await user.click(screen.getByRole("button", { name: "检查训练数据" }));
    await screen.findByText("数据检查通过");
    await user.click(screen.getByRole("button", { name: "创建数据版本" }));
    await screen.findByText("连接中断");
    const original = vi.mocked(trainingDatasetsApi.create).mock.calls[0][1];
    await user.keyboard("{Escape}");
    vi.mocked(useProject).mockReturnValue({
      data: { name: "路口车辆", data_type: "image", tool_bindings: {} },
    } as never);
    rendered.refresh();
    expect(screen.getByRole("button", { name: "准备新版本" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "准备新版本" }));
    expect(screen.getByRole("button", { name: "检查训练数据" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "确认原创建请求" }));
    await waitFor(() => expect(trainingDatasetsApi.create).toHaveBeenCalledTimes(2));
    expect(vi.mocked(trainingDatasetsApi.create).mock.calls[1][1]).toEqual(original);
  });
  it("rejects a late preflight across a batched session A to B to A transition", async () => {
    let resolve!: (value: typeof report) => void;
    vi.mocked(trainingDatasetsApi.preflight).mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    const user = userEvent.setup();
    mount();
    await user.click(screen.getByRole("button", { name: "准备新版本" }));
    await user.click(screen.getByRole("button", { name: "检查训练数据" }));
    act(() => {
      useAuthStore.getState().setAuth("session-b", { id: "user-b" } as MeResponse);
      useAuthStore.getState().setAuth("session-a", { id: "user-a" } as MeResponse);
    });
    await act(async () => {
      resolve(report);
    });
    await user.click(screen.getByRole("button", { name: "准备新版本" }));
    expect(screen.queryByText("数据检查通过")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "创建数据版本" })).toBeDisabled();
  });
  it("requires a successful current preflight and a name before creating", async () => {
    const user = userEvent.setup();
    mount();
    await user.click(screen.getByRole("button", { name: "准备新版本" }));
    expect(screen.getByRole("button", { name: "创建数据版本" })).toBeDisabled();
    await user.clear(screen.getByLabelText("版本名称"));
    await user.type(screen.getByLabelText("版本名称"), "车辆首轮");
    await user.click(screen.getByRole("button", { name: "检查训练数据" }));
    await screen.findByText("数据检查通过");
    await user.click(screen.getByRole("button", { name: "创建数据版本" }));
    await waitFor(() =>
      expect(trainingDatasetsApi.create).toHaveBeenCalledWith(
        "project-1",
        expect.objectContaining({
          name: "车辆首轮",
          preflight_digest: report.digest,
          validation_fraction: 0.2,
          seed: 42,
          mode: "reviewed",
        }),
      ),
    );
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "新建数据版本" })).not.toBeInTheDocument(),
    );
    await user.click(screen.getByRole("button", { name: "准备新版本" }));
    await waitFor(() =>
      expect((screen.getByLabelText("版本名称") as HTMLInputElement).value).toMatch(
        /^\d{4}-\d{4}-\d{4} $/,
      ),
    );
  });

  it("invalidates the previous preflight when split settings change", async () => {
    const user = userEvent.setup();
    mount();
    await user.click(screen.getByRole("button", { name: "准备新版本" }));
    await user.type(screen.getByLabelText("版本名称"), "第二轮");
    await user.click(screen.getByRole("button", { name: "检查训练数据" }));
    await screen.findByText("数据检查通过");
    await user.clear(screen.getByLabelText("划分随机种子"));
    await user.type(screen.getByLabelText("划分随机种子"), "99");
    expect(screen.queryByText("数据检查通过")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "创建数据版本" })).toBeDisabled();
    expect(trainingDatasetsApi.create).not.toHaveBeenCalled();
  });

  it("keeps explicit manager confirmation disabled without selected tasks", async () => {
    const user = userEvent.setup();
    mount();
    await user.click(screen.getByRole("button", { name: "准备新版本" }));
    await user.selectOptions(screen.getByLabelText("标注纳入方式"), "manager_confirmed");
    expect(screen.getByRole("button", { name: "检查训练数据" })).toBeDisabled();
    expect(await screen.findByText("已选择 0 个任务")).toBeVisible();
  });

  it("shows preflight failures and allows rechecking", async () => {
    vi.mocked(trainingDatasetsApi.preflight).mockRejectedValueOnce(new Error("素材不可用"));
    const user = userEvent.setup();
    mount();
    await user.click(screen.getByRole("button", { name: "准备新版本" }));
    await user.click(screen.getByRole("button", { name: "检查训练数据" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("素材不可用");
    await user.click(screen.getByRole("button", { name: "检查训练数据" }));
    await screen.findByText("数据检查通过");
  });

  it("restores a selected version outside the current list page without loading its heavy manifest", async () => {
    mount(
      "/projects/project-1/learning?tab=data&dataset=dataset-1&data_view=compare&data_baseline=older",
    );
    expect(await screen.findByText("冻结详情 第一轮")).toBeVisible();
    expect(trainingDatasetsApi.getSummary).toHaveBeenCalledWith(
      "project-1",
      "dataset-1",
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(trainingDatasetsApi.get).not.toHaveBeenCalled();
  });

  it("downloads through the authorized version endpoint", async () => {
    vi.mocked(trainingDatasetsApi.list).mockResolvedValue({ items: [version as never], total: 1 });
    vi.mocked(trainingDatasetsApi.download).mockResolvedValue({
      url: "/minio/training/package.zip",
      sha256: "checksum",
    });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("button", { name: "下载训练包" }));
    await waitFor(() =>
      expect(trainingDatasetsApi.download).toHaveBeenCalledWith("project-1", "dataset-1"),
    );
    await waitFor(() => expect(click).toHaveBeenCalledOnce());
  });

  it("cancels the current build through its associated job", async () => {
    vi.mocked(trainingDatasetsApi.list).mockResolvedValue({
      items: [{ ...version, status: "running", async_job_id: "job-1" } as never],
      total: 1,
    });
    vi.mocked(asyncJobsApi.cancel).mockResolvedValue({ status: "cancelled", id: "job-1" });
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("button", { name: "取消构建" }));
    await waitFor(() => expect(asyncJobsApi.cancel).toHaveBeenCalledWith("job-1"));
  });
  it("pins the owning run when selecting an attempt from the default experiment", async () => {
    const user = userEvent.setup();
    mount("/projects/project-1/learning?tab=training");
    await user.click(await screen.findByRole("button", { name: "测试历史执行" }));
    expect(
      screen.getByText(/tab=training&attempt=historical-attempt&run=source-run/),
    ).toBeInTheDocument();
  });
});

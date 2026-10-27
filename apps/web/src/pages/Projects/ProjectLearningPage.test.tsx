import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor, cleanup, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { trainingDatasetsApi } from "@/api/trainingDatasets";
import { trainingRunsApi } from "@/api/trainingRuns";
import { batchesApi } from "@/api/batches";
import { tasksApi } from "@/api/tasks";
import { asyncJobsApi } from "@/api/asyncJobs";
import { ProjectLearningPage } from "./ProjectLearningPage";
import { TrainingDatasetPage } from "../Training/TrainingDatasetPage";
import { useAuthStore } from "@/stores/authStore";
import type { MeResponse } from "@/api/auth";
import { ApiError } from "@/api/client";
import { useProject } from "@/hooks/useProjects";
import { useProjectDatasets } from "@/hooks/useDatasets";
import { confirmDialog } from "@/components/ui/decisionDialog";
import { trainingDatasetDraftKey } from "../Training/trainingDatasetDraftStorage";

vi.mock("@/components/ui/decisionDialog", () => ({ confirmDialog: vi.fn() }));

vi.mock("./TrainingDatasetInsights", () => ({
  TrainingDatasetInsights: ({
    version,
    onClose,
  }: {
    version: { name: string };
    onClose: () => void;
  }) => (
    <>
      <p>冻结详情 {version.name}</p>
      <button onClick={onClose}>收起数据详情</button>
    </>
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

vi.mock("@/hooks/useDatasets", () => ({ useProjectDatasets: vi.fn() }));

vi.mock("@/hooks/useProjects", () => ({
  useProject: vi.fn(),
}));
vi.mock("@/api/trainingRuns", () => ({ trainingRunsApi: { catalog: vi.fn() } }));
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
  const navigate = useNavigate();
  return (
    <>
      <button
        onClick={() => {
          const next = new URLSearchParams(location.search);
          next.set("dataset_filter", JSON.stringify({ extension: "jpg" }));
          navigate(`${location.pathname}?${next}`);
        }}
      >
        切换训练筛选
      </button>
      <p>
        {location.pathname}
        {location.search}
      </p>
    </>
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
          <Route
            path="/projects/:id/learning/data/:datasetId"
            element={
              <>
                <TrainingDatasetPage />
                <Location />
              </>
            }
          />
          <Route path="/projects/:id/learning/experiments/:runId" element={<Location />} />
          <Route path="/projects/:id/learning/models/:modelId" element={<Location />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
  const rendered = render(tree());
  return { ...rendered, refresh: () => rendered.rerender(tree()) };
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
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
  vi.mocked(useProjectDatasets).mockReturnValue({ data: [], isLoading: false } as never);
  useAuthStore.getState().setAuth("session-a", { id: "user-a" } as MeResponse);
  vi.mocked(trainingRunsApi.catalog).mockResolvedValue({
    items: [
      {
        framework: "ultralytics",
        base_models: [
          {
            id: "yolo11n-seg",
            task: "segment",
            capabilities: { instance_rle: { bridge: "ultralytics-instance-rle-1" } },
          },
        ],
      },
    ],
  } as never);
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
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  useAuthStore.getState().logout();
  localStorage.clear();
});

describe("project learning data versions", () => {
  it("requires explicit dataset scope confirmation and sends server-side dataset selection", async () => {
    const datasetId = "11111111-2222-4333-8444-555555555555";
    vi.mocked(useProjectDatasets).mockReturnValue({
      data: [{ id: datasetId, name: "目录样本" }],
      isLoading: false,
    } as never);
    const user = userEvent.setup();
    mount(`/projects/project-1/learning?tab=data&dataset_id=${datasetId}`);
    await user.click(await screen.findByRole("button", { name: "准备新版本" }));
    expect(screen.getByRole("combobox", { name: "限定数据集" })).toHaveValue(datasetId);
    expect(screen.getByRole("button", { name: "检查训练数据" })).toBeDisabled();
    await user.click(
      screen.getByRole("checkbox", { name: "我已确认所选数据集与当前项目的冻结范围" }),
    );
    await user.click(screen.getByRole("button", { name: "检查训练数据" }));
    await waitFor(() =>
      expect(trainingDatasetsApi.preflight).toHaveBeenCalledWith(
        "project-1",
        expect.objectContaining({ dataset_id: datasetId }),
      ),
    );
    await user.click(screen.getByRole("button", { name: "创建数据版本" }));
    await waitFor(() =>
      expect(trainingDatasetsApi.create).toHaveBeenCalledWith(
        "project-1",
        expect.objectContaining({ dataset_id: datasetId }),
      ),
    );
  });
  it("keeps an unavailable dataset restriction instead of broadening the selection to all tasks", async () => {
    const user = userEvent.setup();
    mount("/projects/project-1/learning?tab=data&dataset_id=11111111-2222-4333-8444-555555555555");
    await user.click(await screen.findByRole("button", { name: "准备新版本" }));
    expect(screen.getByText(/此数据集未关联当前项目/)).toBeVisible();
    expect(screen.getByRole("button", { name: "检查训练数据" })).toBeDisabled();
    expect(trainingDatasetsApi.preflight).not.toHaveBeenCalled();
  });
  it("paginates manager confirmation within the selected dataset on the server", async () => {
    const datasetId = "11111111-2222-4333-8444-555555555555";
    vi.mocked(useProjectDatasets).mockReturnValue({
      data: [{ id: datasetId, name: "目录样本" }],
      isLoading: false,
    } as never);
    const user = userEvent.setup();
    mount(`/projects/project-1/learning?tab=data&dataset_id=${datasetId}`);
    await user.click(await screen.findByRole("button", { name: "准备新版本" }));
    await user.selectOptions(
      screen.getByRole("combobox", { name: "标注纳入方式" }),
      "manager_confirmed",
    );
    await waitFor(() =>
      expect(tasksApi.listByProject).toHaveBeenCalledWith(
        "project-1",
        expect.objectContaining({ dataset_id: datasetId, limit: 25, offset: 0 }),
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      ),
    );
  });

  it("previews the retained copy size for reference originals before creation", async () => {
    vi.mocked(trainingDatasetsApi.preflight).mockResolvedValueOnce({
      ...report,
      summary: { ...report.summary, reference_samples: 2, original_copy_bytes: 2048 },
    });
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("button", { name: "准备新版本" }));
    await user.click(screen.getByRole("button", { name: "检查训练数据" }));
    expect(await screen.findByText(/引用图片 2 张；预计新增原图副本/)).toHaveTextContent(
      "2,048 字节",
    );
    expect(trainingDatasetsApi.create).not.toHaveBeenCalled();
  });
  it("opens training data and creates UUID requests on plain HTTP without randomUUID", async () => {
    vi.stubGlobal("crypto", { getRandomValues: crypto.getRandomValues.bind(crypto) });
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("button", { name: "准备新版本" }));
    await user.click(screen.getByRole("button", { name: "检查训练数据" }));
    await user.click(await screen.findByRole("button", { name: "创建数据版本" }));
    await waitFor(() => expect(trainingDatasetsApi.create).toHaveBeenCalledOnce());
    expect(vi.mocked(trainingDatasetsApi.create).mock.calls[0][1].request_id).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
  });

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
    await waitFor(() => expect(screen.getByLabelText("数据类型筛选")).toHaveValue("pose"));
    expect(screen.getByRole("button", { name: "准备新版本" })).toBeDisabled();
    await waitFor(() =>
      expect(trainingDatasetsApi.list).toHaveBeenLastCalledWith(
        "project-1",
        0,
        undefined,
        expect.anything(),
        { task: "pose" },
        20,
      ),
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
    vi.mocked(trainingDatasetsApi.unrecognizedVersions).mockResolvedValueOnce({
      items: [
        {
          id: "unknown-version-1",
          name: "未识别契约",
          status: "ready",
          created_at: "2026-10-16T00:00:00Z",
          raw_task: "future-task",
          error_code: "unknown_data_contract",
          reason: "冻结任务契约未被识别",
        },
        {
          id: "unknown-version-2",
          name: "未识别契约",
          status: "ready",
          created_at: "2026-10-16T00:00:00Z",
          raw_task: "future-task",
          error_code: "unknown_data_contract",
          reason: "冻结任务契约未被识别",
        },
      ],
      total: 21,
    });
    vi.mocked(trainingDatasetsApi.unrecognizedVersions).mockResolvedValueOnce({
      items: [
        {
          id: "unknown-version-21",
          name: "未识别契约",
          status: "ready",
          created_at: "2026-10-16T00:00:00Z",
          raw_task: "future-task",
          error_code: "unknown_data_contract",
          reason: "冻结任务契约未被识别",
        },
      ],
      total: 21,
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
        20,
      ),
    );
    await user.click(screen.getByRole("button", { name: /未识别的历史版本/ }));
    expect(await screen.findAllByText("未识别契约")).toHaveLength(2);
    expect(screen.getByText("记录 1")).toBeVisible();
    expect(screen.getByText("记录 2")).toBeVisible();
    expect(screen.getAllByRole("button", { name: "复制历史数据版本 ID" })).toHaveLength(2);
    expect(document.body).not.toHaveTextContent("unknown-version-1");
    expect(document.body).not.toHaveTextContent("unknown-version-2");
    await user.click(screen.getByRole("button", { name: "下一页诊断" }));
    expect(await screen.findByText("记录 21")).toBeVisible();
    expect(trainingDatasetsApi.unrecognizedVersions).toHaveBeenCalledWith(
      "project-1",
      20,
      expect.anything(),
      20,
    );
    expect(trainingDatasetsApi.getSummary).not.toHaveBeenCalled();
  });

  it("distinguishes repeated preflight task issues without showing task IDs", async () => {
    const taskA = "task-uuid-a";
    const taskB = "task-uuid-b";
    vi.mocked(trainingDatasetsApi.preflight).mockResolvedValueOnce({
      ...report,
      ready: false,
      issues: [
        { code: "invalid", message: "相同预检失败", task_id: taskA },
        { code: "invalid", message: "相同预检失败", task_id: taskB },
        { code: "invalid", message: "相同预检失败", task_id: taskA },
      ],
    } as never);
    const user = userEvent.setup();
    mount();
    await user.click(screen.getByRole("button", { name: "准备新版本" }));
    await user.click(screen.getByRole("button", { name: "检查训练数据" }));

    const copyButtons = await screen.findAllByRole("button", { name: "复制问题任务 ID" });
    expect(copyButtons).toHaveLength(3);
    expect(copyButtons.map((button) => button.closest("p")?.textContent?.trim())).toEqual([
      "相同预检失败任务 1",
      "相同预检失败任务 2",
      "相同预检失败任务 1",
    ]);
    expect(document.body).not.toHaveTextContent(taskA);
    expect(document.body).not.toHaveTextContent(taskB);
    expect(
      screen.queryByRole("link", { name: "处理原图来源（新标签页）" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/入口会在新标签页打开样本检查器/)).not.toBeInTheDocument();
  });
  it("opens authorized reference issues in a new tab while retaining the preparation form and report", async () => {
    const datasetId = "11111111-2222-4333-8444-555555555555";
    const itemId = "66666666-7777-4888-9999-aaaaaaaaaaaa";
    vi.mocked(trainingDatasetsApi.preflight).mockResolvedValueOnce({
      ...report,
      ready: false,
      issues: [
        {
          code: "agent_offline",
          message: "来源 Agent 暂时离线",
          dataset_id: datasetId,
          dataset_item_id: itemId,
        },
      ],
    } as never);
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("button", { name: "准备新版本" }));
    const name = screen.getByLabelText("版本名称");
    await user.clear(name);
    await user.type(name, "引用图片首轮");
    await user.click(screen.getByRole("button", { name: "检查训练数据" }));
    const link = await screen.findByRole("link", { name: "处理原图来源（新标签页）" });
    expect(link).toHaveAttribute("href", `/datasets/${datasetId}?tab=data&item=${itemId}`);
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.getByText(/可核验原图、关联备用来源/)).toHaveTextContent(
      "联系来源管理员恢复连接",
    );
    expect(screen.getByText(/当前准备表单和预检报告会保留/)).toHaveTextContent(
      "返回此页点击“检查训练数据”重试",
    );
    await user.click(link);
    expect(screen.getByText("/projects/project-1/learning?tab=data")).toBeVisible();
    expect(name).toHaveValue("引用图片首轮");
    expect(screen.getByText("来源 Agent 暂时离线")).toBeVisible();
    expect(screen.getByText("需要处理数据问题")).toBeVisible();
    expect(trainingDatasetsApi.create).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "检查训练数据" }));
    expect(await screen.findByText("数据检查通过")).toBeVisible();
    expect(name).toHaveValue("引用图片首轮");
    expect(trainingDatasetsApi.preflight).toHaveBeenCalledTimes(2);
    expect(vi.mocked(trainingDatasetsApi.preflight).mock.calls[1][1]).toEqual(
      vi.mocked(trainingDatasetsApi.preflight).mock.calls[0][1],
    );
  });
  it("only constructs sample paths from complete UUID strings and never from issue URLs or text", async () => {
    const datasetId = "11111111-2222-4333-8444-555555555555";
    const itemId = "66666666-7777-4888-9999-aaaaaaaaaaaa";
    const invalid = [
      {},
      { dataset_id: datasetId, task_id: itemId },
      { dataset_item_id: itemId },
      { dataset_id: "/cephfs/images", dataset_item_id: itemId },
      { dataset_id: datasetId, dataset_item_id: "https://example.invalid/sample" },
      { dataset_id: datasetId, dataset_item_id: `${itemId}&tab=sources` },
      { dataset_id: `${datasetId}/../other`, dataset_item_id: itemId },
      { dataset_id: [datasetId], dataset_item_id: itemId },
      { dataset_id: datasetId, dataset_item_id: { id: itemId } },
      { dataset_id: ` ${datasetId}`, dataset_item_id: itemId },
      { dataset_id: datasetId, dataset_item_id: itemId.slice(1) },
      { dataset_id: null, dataset_item_id: 123 },
    ];
    vi.mocked(trainingDatasetsApi.preflight).mockResolvedValueOnce({
      ...report,
      ready: false,
      issues: [
        ...invalid.map((fields, index) => ({
          code: "invalid",
          message: `旧问题 ${index}`,
          ...fields,
          url: "https://example.invalid/sample",
          path: "/cephfs/images",
        })),
        {
          code: "agent_offline",
          message: "https://example.invalid/not-a-destination",
          dataset_id: datasetId,
          dataset_item_id: itemId.toUpperCase(),
        },
      ],
    } as never);
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("button", { name: "准备新版本" }));
    await user.click(screen.getByRole("button", { name: "检查训练数据" }));
    const links = await screen.findAllByRole("link", { name: "处理原图来源（新标签页）" });
    expect(links).toHaveLength(1);
    expect(links[0]).toHaveAttribute(
      "href",
      `/datasets/${datasetId}?tab=data&item=${itemId.toUpperCase()}`,
    );
    invalid.forEach((_, index) => expect(screen.getByText(`旧问题 ${index}`)).toBeVisible());
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
        20,
      ),
    );
    await user.click(screen.getByRole("button", { name: "准备新版本" }));
    await user.clear(screen.getByLabelText("版本名称"));
    await user.type(screen.getByLabelText("版本名称"), "检测迭代");
    await user.keyboard("{Escape}");
    expect(screen.getByRole("button", { name: "准备新版本" })).toHaveFocus();
    await user.selectOptions(screen.getByLabelText("数据类型筛选"), "segment");
    await waitFor(() =>
      expect(trainingDatasetsApi.list).toHaveBeenCalledWith(
        "project-1",
        0,
        undefined,
        expect.anything(),
        { task: "segment" },
        20,
      ),
    );
    await user.click(screen.getByRole("button", { name: "准备新版本" }));
    expect(screen.getByLabelText("训练任务")).toHaveValue("segment");
    expect(screen.getByLabelText("版本名称")).not.toHaveValue("检测迭代");
    await user.clear(screen.getByLabelText("版本名称"));
    await user.type(screen.getByLabelText("版本名称"), "分割迭代");
    await user.keyboard("{Escape}");
    await user.selectOptions(screen.getByLabelText("数据类型筛选"), "detect");
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
  it.each(["network", "server", "in-flight", "408", "429"])(
    "restores the exact %s request and editing guards after remount",
    async (failure) => {
      if (failure === "in-flight")
        vi.mocked(trainingDatasetsApi.create).mockImplementationOnce(() => new Promise(() => {}));
      else
        vi.mocked(trainingDatasetsApi.create).mockRejectedValueOnce(
          failure === "server"
            ? new ApiError(503, "服务暂不可用")
            : failure === "408" || failure === "429"
              ? new ApiError(Number(failure), "响应尚未确定")
              : new Error("连接中断"),
        );
      const user = userEvent.setup();
      const rendered = mount();
      await user.click(screen.getByRole("button", { name: "准备新版本" }));
      await user.clear(screen.getByLabelText("版本名称"));
      await user.type(screen.getByLabelText("版本名称"), "车辆首轮");
      await user.click(screen.getByRole("button", { name: "检查训练数据" }));
      await screen.findByText("数据检查通过");
      await user.click(screen.getByRole("button", { name: "创建数据版本" }));
      await waitFor(() => expect(trainingDatasetsApi.create).toHaveBeenCalledOnce());
      const first = structuredClone(vi.mocked(trainingDatasetsApi.create).mock.calls[0][1]);
      rendered.unmount();
      await act(async () => {
        await useAuthStore.persist.rehydrate();
      });
      mount();
      await user.click(screen.getByRole("button", { name: "准备新版本" }));
      expect(screen.getByLabelText("版本名称")).toHaveValue("车辆首轮");
      expect(screen.getByLabelText("版本名称")).toBeDisabled();
      expect(screen.getByRole("button", { name: "检查训练数据" })).toBeDisabled();
      await user.click(screen.getByRole("button", { name: "确认原创建请求" }));
      await waitFor(() => expect(trainingDatasetsApi.create).toHaveBeenCalledTimes(2));
      expect(vi.mocked(trainingDatasetsApi.create).mock.calls[1][1]).toEqual(first);
      expect(trainingDatasetsApi.preflight).toHaveBeenCalledOnce();
      await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
      cleanup();
      mount();
      await user.click(screen.getByRole("button", { name: "准备新版本" }));
      expect(screen.getByLabelText("版本名称")).toBeEnabled();
      expect(screen.queryByRole("button", { name: "确认原创建请求" })).not.toBeInTheDocument();
    },
  );

  it("restores a pending request when automatic token refresh remounts the view", async () => {
    vi.mocked(trainingDatasetsApi.create).mockRejectedValueOnce(new Error("连接中断"));
    const user = userEvent.setup();
    mount();
    await user.click(screen.getByRole("button", { name: "准备新版本" }));
    await user.click(screen.getByRole("button", { name: "检查训练数据" }));
    await screen.findByText("数据检查通过");
    await user.click(screen.getByRole("button", { name: "创建数据版本" }));
    await screen.findByText("连接中断");
    const original = structuredClone(vi.mocked(trainingDatasetsApi.create).mock.calls[0][1]);
    act(() => useAuthStore.getState().setToken("refreshed-token"));
    await user.click(screen.getByRole("button", { name: "准备新版本" }));
    expect(screen.getByLabelText("版本名称")).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "确认原创建请求" }));
    await waitFor(() => expect(trainingDatasetsApi.create).toHaveBeenCalledTimes(2));
    expect(vi.mocked(trainingDatasetsApi.create).mock.calls[1][1]).toEqual(original);
  });

  it("keeps pending requests isolated by task and project across remount", async () => {
    vi.mocked(trainingDatasetsApi.create).mockRejectedValue(new Error("连接中断"));
    const user = userEvent.setup();
    mount();
    await user.click(screen.getByRole("button", { name: "准备新版本" }));
    await user.clear(screen.getByLabelText("版本名称"));
    await user.type(screen.getByLabelText("版本名称"), "仅检测的请求");
    await user.click(screen.getByRole("button", { name: "检查训练数据" }));
    await screen.findByText("数据检查通过");
    await user.click(screen.getByRole("button", { name: "创建数据版本" }));
    await screen.findByText("连接中断");
    cleanup();
    mount("/projects/project-2/learning?tab=data");
    await user.click(screen.getByRole("button", { name: "准备新版本" }));
    expect(screen.getByLabelText("版本名称")).toBeEnabled();
    expect(screen.getByLabelText("版本名称")).not.toHaveValue("仅检测的请求");
    cleanup();
    mount("/projects/project-1/learning?tab=data&data_task=segment");
    await user.click(screen.getByRole("button", { name: "准备新版本" }));
    expect(screen.getByLabelText("版本名称")).toBeEnabled();
    await user.keyboard("{Escape}");
    await user.selectOptions(screen.getByLabelText("数据类型筛选"), "detect");
    await user.click(screen.getByRole("button", { name: "准备新版本" }));
    expect(screen.getByLabelText("版本名称")).toHaveValue("仅检测的请求");
    expect(screen.getByLabelText("版本名称")).toBeDisabled();
  });

  it.each(["different-account", "same-account-new-token", "same-account-relogin"])(
    "does not expose a prior session request after %s",
    async (change) => {
      vi.mocked(trainingDatasetsApi.create).mockRejectedValueOnce(new Error("连接中断"));
      const user = userEvent.setup();
      mount();
      await user.click(screen.getByRole("button", { name: "准备新版本" }));
      await user.clear(screen.getByLabelText("版本名称"));
      await user.type(screen.getByLabelText("版本名称"), "上一会话私有名称");
      await user.click(screen.getByRole("button", { name: "检查训练数据" }));
      await screen.findByText("数据检查通过");
      await user.click(screen.getByRole("button", { name: "创建数据版本" }));
      await screen.findByText("连接中断");
      act(() => {
        if (change === "same-account-relogin") useAuthStore.getState().logout();
        useAuthStore
          .getState()
          .setAuth(change === "same-account-relogin" ? "session-a" : "session-b", {
            id: change === "different-account" ? "user-b" : "user-a",
          } as MeResponse);
      });
      await user.click(screen.getByRole("button", { name: "准备新版本" }));
      expect(screen.getByLabelText("版本名称")).not.toHaveValue("上一会话私有名称");
      expect(screen.getByLabelText("版本名称")).toBeEnabled();
      cleanup();
      mount();
      await user.click(screen.getByRole("button", { name: "准备新版本" }));
      expect(screen.getByLabelText("版本名称")).not.toHaveValue("上一会话私有名称");
      expect(screen.queryByRole("button", { name: "确认原创建请求" })).not.toBeInTheDocument();
    },
  );

  it.each(["project_busy", "account_busy", "membership_busy"])(
    "retains a lost-response request across a transient %s conflict and remount",
    async (reason) => {
      vi.mocked(trainingDatasetsApi.create)
        .mockRejectedValueOnce(new Error("连接中断"))
        .mockRejectedValueOnce(new ApiError(409, "正在变更，请重试", { reason }));
      const user = userEvent.setup();
      mount();
      await user.click(screen.getByRole("button", { name: "准备新版本" }));
      await user.click(screen.getByRole("button", { name: "检查训练数据" }));
      await screen.findByText("数据检查通过");
      await user.click(screen.getByRole("button", { name: "创建数据版本" }));
      await screen.findByText("连接中断");
      const original = structuredClone(vi.mocked(trainingDatasetsApi.create).mock.calls[0][1]);
      await user.click(screen.getByRole("button", { name: "确认原创建请求" }));
      await screen.findByText("正在变更，请重试");
      cleanup();
      mount();
      await user.click(screen.getByRole("button", { name: "准备新版本" }));
      expect(screen.getByLabelText("版本名称")).toBeDisabled();
      await user.click(screen.getByRole("button", { name: "确认原创建请求" }));
      await waitFor(() => expect(trainingDatasetsApi.create).toHaveBeenCalledTimes(3));
      expect(vi.mocked(trainingDatasetsApi.create).mock.calls[2][1]).toEqual(original);
      expect(trainingDatasetsApi.preflight).toHaveBeenCalledOnce();
    },
  );

  it("blocks replacement of malformed persisted requests without revealing their contents", async () => {
    const key = trainingDatasetDraftKey({
      userId: "user-a",
      sessionId: useAuthStore.getState().sessionId!,
      projectId: "project-1",
      task: "detect",
    });
    localStorage.setItem(key, '{"private":"不可显示的损坏记录"');
    const user = userEvent.setup();
    mount();
    await user.click(screen.getByRole("button", { name: "准备新版本" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("无法恢复本地创建请求");
    expect(screen.queryByText(/不可显示的损坏记录/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "检查训练数据" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "创建数据版本" })).toBeDisabled();
    expect(trainingDatasetsApi.create).not.toHaveBeenCalled();
    expect(localStorage.getItem(key)).toBe('{"private":"不可显示的损坏记录"');
  });

  it("does not send a POST when the original request cannot be persisted", async () => {
    const user = userEvent.setup();
    mount();
    await user.click(screen.getByRole("button", { name: "准备新版本" }));
    await user.click(screen.getByRole("button", { name: "检查训练数据" }));
    await screen.findByText("数据检查通过");
    const setItem = Storage.prototype.setItem;
    const storage = vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (
      this: Storage,
      key,
      value,
    ) {
      if (key.startsWith("aap-training-dataset-request:"))
        throw new DOMException("Quota exceeded", "QuotaExceededError");
      setItem.call(this, key, value);
    });
    await user.click(screen.getByRole("button", { name: "创建数据版本" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("请求尚未发送");
    expect(trainingDatasetsApi.create).not.toHaveBeenCalled();
    expect(screen.getByLabelText("版本名称")).toBeEnabled();
    storage.mockRestore();
    await user.click(screen.getByRole("button", { name: "创建数据版本" }));
    await waitFor(() => expect(trainingDatasetsApi.create).toHaveBeenCalledOnce());
  });

  it.each(["token", "session"])(
    "blocks a stale owner's pending retry before another tab's %s is adopted",
    async (changed) => {
      vi.mocked(trainingDatasetsApi.create).mockRejectedValueOnce(new Error("连接中断"));
      const user = userEvent.setup();
      mount();
      await user.click(screen.getByRole("button", { name: "准备新版本" }));
      await user.click(screen.getByRole("button", { name: "检查训练数据" }));
      await screen.findByText("数据检查通过");
      await user.click(screen.getByRole("button", { name: "创建数据版本" }));
      await screen.findByText("连接中断");
      if (changed === "token") localStorage.setItem("token", "another-tab-session");
      else {
        const saved = JSON.parse(localStorage.getItem("auth-storage")!);
        saved.state.sessionId = crypto.randomUUID();
        localStorage.setItem("auth-storage", JSON.stringify(saved));
      }
      await user.click(screen.getByRole("button", { name: "确认原创建请求" }));
      await waitFor(() =>
        expect(screen.getByRole("button", { name: "确认原创建请求" })).toBeEnabled(),
      );
      expect(trainingDatasetsApi.create).toHaveBeenCalledOnce();
    },
  );

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
    cleanup();
    mount();
    await user.click(screen.getByRole("button", { name: "准备新版本" }));
    expect(screen.getByLabelText("版本名称")).toBeEnabled();
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
    await user.click(await screen.findByRole("link", { name: "返回数据版本列表" }));
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

  it("opens the exact version from its visible details action on the first visit", async () => {
    const older = { ...version, id: "dataset-older", name: "旧版" };
    vi.mocked(trainingDatasetsApi.list).mockResolvedValue({
      items: [version as never, older as never],
      total: 2,
    });
    vi.mocked(trainingDatasetsApi.getSummary).mockImplementation(
      async (_project, id) => (id === older.id ? older : version) as never,
    );
    const user = userEvent.setup();
    mount();
    expect(trainingDatasetsApi.getSummary).not.toHaveBeenCalled();
    await user.click(await screen.findByRole("link", { name: "查看详情：旧版" }));
    expect(await screen.findByText("冻结详情 旧版")).toBeVisible();
    expect(trainingDatasetsApi.getSummary).toHaveBeenCalledWith(
      "project-1",
      "dataset-older",
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(screen.getByText(/learning\/data\/dataset-older\?tab=data/)).toBeVisible();
    expect(trainingDatasetsApi.download).not.toHaveBeenCalled();
  });

  it("starts with the table and returns from independent details preserving filters and pagination", async () => {
    vi.mocked(trainingDatasetsApi.list).mockResolvedValue({ items: [version as never], total: 1 });
    const user = userEvent.setup();
    mount("/projects/project-1/learning?tab=data&data_list_status=ready&data_list_page=1");
    await user.click(await screen.findByRole("link", { name: "查看详情：第一轮" }));
    expect(await screen.findByText("冻结详情 第一轮")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "收起数据详情" }));
    expect(await screen.findByRole("table", { name: "训练数据版本列表" })).toBeVisible();
    expect(screen.getByRole("combobox", { name: "数据版本状态筛选" })).toHaveValue("ready");
    expect(screen.getByText(/data_list_status=ready&data_list_page=1/)).toBeVisible();
    expect(screen.queryByText("冻结详情 第一轮")).not.toBeInTheDocument();
  });

  it("does not offer a details shortcut when the task has no versions", async () => {
    mount();
    expect(await screen.findByText("尚无训练数据版本")).toBeVisible();
    expect(screen.queryByRole("button", { name: "查看最新版本" })).not.toBeInTheDocument();
    expect(trainingDatasetsApi.getSummary).not.toHaveBeenCalled();
  });
  it("keeps the models tab accessible when a legacy URL also contains an experiment", async () => {
    mount("/projects/project-1/learning?tab=models&run=source-run");
    expect(await screen.findByRole("tab", { name: "模型与发布" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(
      screen.getByText("/projects/project-1/learning?tab=models&run=source-run"),
    ).toBeVisible();
  });
  it("filters all versions before pagination and preserves the selected state in details links", async () => {
    vi.mocked(trainingDatasetsApi.list).mockResolvedValue({ items: [version as never], total: 21 });
    const user = userEvent.setup();
    mount("/projects/project-1/learning?tab=data&data_list_page=1");
    await user.type(screen.getByRole("textbox", { name: "搜索数据版本名称" }), "第一轮");
    await user.selectOptions(screen.getByRole("combobox", { name: "数据版本状态筛选" }), "ready");
    await waitFor(() =>
      expect(trainingDatasetsApi.list).toHaveBeenLastCalledWith(
        "project-1",
        0,
        undefined,
        expect.anything(),
        { task: "detect", q: "第一轮", status: "ready" },
        20,
      ),
    );
    const link = await screen.findByRole("link", { name: "查看详情：第一轮" });
    expect(link).toHaveAttribute(
      "href",
      "/projects/project-1/learning/data/dataset-1?tab=data&data_list_q=%E7%AC%AC%E4%B8%80%E8%BD%AE&data_list_status=ready",
    );
    await user.click(screen.getByRole("button", { name: "下一页数据版本" }));
    await waitFor(() =>
      expect(trainingDatasetsApi.list).toHaveBeenLastCalledWith(
        "project-1",
        20,
        undefined,
        expect.anything(),
        { task: "detect", q: "第一轮", status: "ready" },
        20,
      ),
    );
    await user.selectOptions(screen.getByLabelText("数据类型筛选"), "segment");
    await waitFor(() =>
      expect(trainingDatasetsApi.list).toHaveBeenLastCalledWith(
        "project-1",
        0,
        undefined,
        expect.anything(),
        { task: "segment", q: "第一轮", status: "ready" },
        20,
      ),
    );
    await user.selectOptions(screen.getByLabelText("数据类型筛选"), "all");
    await waitFor(() =>
      expect(trainingDatasetsApi.list).toHaveBeenLastCalledWith(
        "project-1",
        0,
        undefined,
        expect.anything(),
        { q: "第一轮", status: "ready" },
        20,
      ),
    );
    expect(trainingDatasetsApi.getSummary).not.toHaveBeenCalled();
  });
  it("restores all data types from the URL and resets filters without hiding either task", async () => {
    const segment = { ...version, id: "segment-version", task: "segment", name: "第一轮分割" };
    vi.mocked(trainingDatasetsApi.list).mockImplementation(
      async (_project, _offset, _status, _options, filters) => ({
        items: (filters?.task === "detect" ? [version] : [version, segment]) as never[],
        total: 22,
      }),
    );
    vi.mocked(useProject).mockReturnValue({
      data: {
        data_type: "image",
        tool_bindings: {
          bbox: { enabled: true, classes: ["car"] },
          region: { enabled: true, classes: ["car"] },
        },
      },
    } as never);
    const user = userEvent.setup();
    mount(
      "/projects/project-1/learning?tab=data&data_task=all&data_list_page=1&data_list_q=第一轮&data_list_status=ready",
    );
    expect(await screen.findByRole("link", { name: "查看详情：第一轮分割" })).toBeVisible();
    expect(screen.getByLabelText("数据类型筛选")).toHaveValue("all");
    expect(screen.getByRole("columnheader", { name: "数据类型" })).toBeVisible();
    expect(screen.getByRole("link", { name: "查看详情：第一轮" })).toBeVisible();
    expect(trainingDatasetsApi.list).toHaveBeenLastCalledWith(
      "project-1",
      20,
      undefined,
      expect.anything(),
      { q: "第一轮", status: "ready" },
      20,
    );
    await user.selectOptions(screen.getByLabelText("数据类型筛选"), "detect");
    await waitFor(() =>
      expect(trainingDatasetsApi.list).toHaveBeenLastCalledWith(
        "project-1",
        0,
        undefined,
        expect.anything(),
        { task: "detect", q: "第一轮", status: "ready" },
        20,
      ),
    );
    await user.click(screen.getByRole("button", { name: "重置筛选" }));
    await waitFor(() =>
      expect(trainingDatasetsApi.list).toHaveBeenLastCalledWith(
        "project-1",
        0,
        undefined,
        expect.anything(),
        {},
        20,
      ),
    );
    expect(screen.getByLabelText("数据类型筛选")).toHaveValue("all");
    expect(screen.getByRole("textbox", { name: "搜索数据版本名称" })).toHaveValue("");
    expect(screen.getByRole("combobox", { name: "数据版本状态筛选" })).toHaveValue("");
    expect(await screen.findByRole("link", { name: "查看详情：第一轮分割" })).toBeVisible();
  });

  it("retains data rows without a loading placeholder during background refresh", async () => {
    vi.mocked(trainingDatasetsApi.list).mockResolvedValueOnce({
      items: [version as never],
      total: 1,
    });
    let resolve!: (value: Awaited<ReturnType<typeof trainingDatasetsApi.list>>) => void;
    vi.mocked(trainingDatasetsApi.list).mockImplementation(
      () =>
        new Promise((yes) => {
          resolve = yes;
        }),
    );
    mount();
    const link = await screen.findByRole("link", { name: "查看详情：第一轮" });
    await act(async () => {
      void clients[clients.length - 1].refetchQueries({ queryKey: ["training-datasets"] });
    });
    await waitFor(() => expect(trainingDatasetsApi.list).toHaveBeenCalledTimes(2));
    expect(screen.queryByText("正在加载数据版本…")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "查看详情：第一轮" })).toBe(link);
    await act(async () =>
      resolve({ items: [{ ...version, summary: { samples: 16 } } as never], total: 1 }),
    );
    expect(await screen.findByText("16")).toBeVisible();
    expect(screen.getByRole("link", { name: "查看详情：第一轮" })).toBe(link);
  });

  it("does not restore a deleted deep-linked version from a stale list response", async () => {
    vi.mocked(trainingDatasetsApi.list).mockResolvedValue({ items: [version as never], total: 1 });
    vi.mocked(confirmDialog).mockResolvedValueOnce(true);
    const user = userEvent.setup();
    mount("/projects/project-1/learning?tab=data&dataset=dataset-1");
    await screen.findByText("冻结详情 第一轮");
    await user.click(await screen.findByRole("button", { name: "删除" }));
    await waitFor(() =>
      expect(trainingDatasetsApi.remove).toHaveBeenCalledWith("project-1", "dataset-1"),
    );
    expect(await screen.findByRole("table", { name: "训练数据版本列表" })).toBeVisible();
    expect(screen.getByText("/projects/project-1/learning?tab=data")).toBeVisible();
    expect(screen.queryByText("冻结详情 第一轮")).not.toBeInTheDocument();
  });

  it("lists the actual project task without automatically loading any details", async () => {
    const segment = { ...version, id: "segment-latest", task: "segment", name: "分割最新版本" };
    vi.mocked(useProject).mockReturnValue({
      data: {
        data_type: "image",
        tool_bindings: { region: { enabled: true, classes: ["shape"] } },
      },
    } as never);
    vi.mocked(trainingDatasetsApi.list).mockImplementation(
      async (_project, _offset, _status, _options, filters) => ({
        items: [(filters?.task === "segment" ? segment : version) as never],
        total: 1,
      }),
    );
    vi.mocked(trainingDatasetsApi.getSummary).mockResolvedValue(segment as never);
    mount();
    expect(await screen.findByRole("link", { name: "查看详情：分割最新版本" })).toBeVisible();
    expect(screen.getByLabelText("数据类型筛选")).toHaveValue("segment");
    expect(trainingDatasetsApi.getSummary).not.toHaveBeenCalled();
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
      await screen.findByText(
        /learning\/experiments\/source-run\?tab=training&attempt=historical-attempt/,
      ),
    ).toBeInTheDocument();
  });
});

it("redirects legacy model detail links and preserves the model list context", async () => {
  mount(
    "/projects/project-1/learning?tab=models&model=model-1&model_q=候选&model_page=2&archive=only&run=old-source",
  );
  expect(
    await screen.findByText(
      "/projects/project-1/learning/models/model-1?tab=models&model_q=%E5%80%99%E9%80%89&model_page=2&archive=only",
    ),
  ).toBeVisible();
});

it("selects native instance masks explicitly, preserves each bridge draft and prevents cross-bridge parents", async () => {
  const nativeBridge = "ultralytics-instance-rle-1";
  const polygon = {
    ...version,
    task: "segment" as const,
    name: "多边形父版本",
    contract_profile: {
      ...version.contract_profile,
      task: "segment",
      geometry: "normalized-simple-polygon",
      bridge: "ultralytics-simple-polygon-1",
    },
  };
  const native = {
    ...polygon,
    id: "native",
    name: "掩码父版本",
    contract_profile: {
      ...polygon.contract_profile,
      schema: 4,
      format: "aap-instance-rle",
      geometry: "instance-mask",
      bridge: nativeBridge,
    },
  };
  vi.mocked(trainingDatasetsApi.list).mockResolvedValue({
    items: [polygon, native] as never,
    total: 2,
  });
  const user = userEvent.setup();
  mount("/projects/project-1/learning?tab=data&data_task=segment");
  await user.click(await screen.findByRole("button", { name: "准备新版本" }));
  await user.clear(screen.getByLabelText("版本名称"));
  await user.type(screen.getByLabelText("版本名称"), "多边形草稿");
  await user.selectOptions(screen.getByLabelText("分割训练桥接"), nativeBridge);
  expect(screen.getByLabelText("版本名称")).not.toHaveValue("多边形草稿");
  expect(screen.getByRole("option", { name: /多边形父版本.*桥接不同/ })).toBeDisabled();
  expect(screen.getByRole("option", { name: "掩码父版本" })).toBeEnabled();
  await user.selectOptions(screen.getByLabelText("分割训练桥接"), "polygon");
  expect(screen.getByLabelText("版本名称")).toHaveValue("多边形草稿");
  await user.selectOptions(screen.getByLabelText("分割训练桥接"), nativeBridge);
  await user.click(screen.getByRole("button", { name: "检查训练数据" }));
  await waitFor(() =>
    expect(trainingDatasetsApi.preflight).toHaveBeenCalledWith(
      "project-1",
      expect.objectContaining({ task: "segment", bridge: nativeBridge }),
    ),
  );
  await user.click(await screen.findByRole("button", { name: "创建数据版本" }));
  await waitFor(() =>
    expect(trainingDatasetsApi.create).toHaveBeenCalledWith(
      "project-1",
      expect.objectContaining({ task: "segment", bridge: nativeBridge }),
    ),
  );
});

it("ignores a late polygon preflight after switching to the native mask bridge", async () => {
  let resolve!: (value: typeof report) => void;
  vi.mocked(trainingDatasetsApi.preflight).mockReturnValueOnce(
    new Promise((done) => {
      resolve = done;
    }),
  );
  const user = userEvent.setup();
  mount("/projects/project-1/learning?tab=data&data_task=segment");
  await user.click(await screen.findByRole("button", { name: "准备新版本" }));
  await user.click(screen.getByRole("button", { name: "检查训练数据" }));
  await user.selectOptions(screen.getByLabelText("分割训练桥接"), "ultralytics-instance-rle-1");
  await act(async () => resolve(report));
  expect(screen.getByRole("button", { name: "创建数据版本" })).toBeDisabled();
  expect(trainingDatasetsApi.create).not.toHaveBeenCalled();
  expect(screen.getByLabelText("分割训练桥接")).toHaveValue("ultralytics-instance-rle-1");
});

it("keeps the native mask bridge unavailable when an older catalog omits qualification", async () => {
  vi.mocked(trainingRunsApi.catalog).mockResolvedValue({
    items: [
      {
        framework: "ultralytics",
        base_models: [{ id: "yolo11n-seg", task: "segment", capabilities: { tasks: ["segment"] } }],
      },
    ],
  } as never);
  const user = userEvent.setup();
  mount("/projects/project-1/learning?tab=data&data_task=segment");
  await user.click(await screen.findByRole("button", { name: "准备新版本" }));
  expect(
    await screen.findByRole("option", { name: "原生实例掩码训练（当前执行器尚未取得资格）" }),
  ).toBeDisabled();
});

it("passes the same dataset filter to preflight, creation and manager task pages", async () => {
  const datasetId = "11111111-2222-4333-8444-555555555555";
  const filter = { extension: "png", min_width: 640 };
  vi.mocked(useProjectDatasets).mockReturnValue({
    data: [{ id: datasetId, name: "目录样本" }],
    isLoading: false,
  } as never);
  const user = userEvent.setup();
  mount(
    `/projects/project-1/learning?tab=data&dataset_id=${datasetId}&dataset_filter=${encodeURIComponent(JSON.stringify(filter))}`,
  );
  await user.click(await screen.findByRole("button", { name: "准备新版本" }));
  expect(screen.getByText(/扩展名：png/)).toBeVisible();
  await user.click(
    screen.getByRole("checkbox", { name: "我已确认所选数据集与当前项目的冻结范围" }),
  );
  await user.click(screen.getByRole("button", { name: "检查训练数据" }));
  await screen.findByText("数据检查通过");
  expect(trainingDatasetsApi.preflight).toHaveBeenCalledWith(
    "project-1",
    expect.objectContaining({ dataset_filter: filter }),
  );
  await user.click(screen.getByRole("button", { name: "创建数据版本" }));
  await waitFor(() =>
    expect(trainingDatasetsApi.create).toHaveBeenCalledWith(
      "project-1",
      expect.objectContaining({ dataset_filter: filter }),
    ),
  );
  cleanup();
  mount(
    `/projects/project-1/learning?tab=data&dataset_id=${datasetId}&dataset_filter=${encodeURIComponent(JSON.stringify(filter))}`,
  );
  await user.click(await screen.findByRole("button", { name: "准备新版本" }));
  await user.selectOptions(
    screen.getByRole("combobox", { name: "标注纳入方式" }),
    "manager_confirmed",
  );
  await waitFor(() =>
    expect(tasksApi.listByProject).toHaveBeenCalledWith(
      "project-1",
      expect.objectContaining({ dataset_filter: filter, limit: 25 }),
      expect.anything(),
    ),
  );
});

it("blocks malformed filter URLs until explicitly cleared", async () => {
  const datasetId = "11111111-2222-4333-8444-555555555555";
  vi.mocked(useProjectDatasets).mockReturnValue({
    data: [{ id: datasetId, name: "目录样本" }],
    isLoading: false,
  } as never);
  const user = userEvent.setup();
  mount(`/projects/project-1/learning?tab=data&dataset_id=${datasetId}&dataset_filter=null`);
  await user.click(await screen.findByRole("button", { name: "准备新版本" }));
  expect(screen.getByText(/数据集筛选条件无效/)).toBeVisible();
  await user.click(
    screen.getByRole("checkbox", { name: "我已确认所选数据集与当前项目的冻结范围" }),
  );
  expect(screen.getByRole("button", { name: "检查训练数据" })).toBeDisabled();
  expect(trainingDatasetsApi.preflight).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "清除数据集筛选" }));
  expect(screen.queryByText(/数据集筛选条件无效/)).not.toBeInTheDocument();
});

it("restores a pending filtered request unchanged on a different filter URL", async () => {
  const datasetId = "11111111-2222-4333-8444-555555555555";
  vi.mocked(useProjectDatasets).mockReturnValue({
    data: [{ id: datasetId, name: "目录样本" }],
    isLoading: false,
  } as never);
  vi.mocked(trainingDatasetsApi.create).mockRejectedValueOnce(new Error("连接中断"));
  const path = (extension: string) =>
    `/projects/project-1/learning?tab=data&dataset_id=${datasetId}&dataset_filter=${encodeURIComponent(JSON.stringify({ extension }))}`;
  const user = userEvent.setup();
  const firstPage = mount(path("png"));
  await user.click(await screen.findByRole("button", { name: "准备新版本" }));
  await user.click(
    screen.getByRole("checkbox", { name: "我已确认所选数据集与当前项目的冻结范围" }),
  );
  await user.click(screen.getByRole("button", { name: "检查训练数据" }));
  await screen.findByText("数据检查通过");
  await user.click(screen.getByRole("button", { name: "创建数据版本" }));
  await waitFor(() => expect(trainingDatasetsApi.create).toHaveBeenCalledOnce());
  const original = structuredClone(vi.mocked(trainingDatasetsApi.create).mock.calls[0][1]);
  firstPage.unmount();
  mount(path("jpg"));
  await user.click(await screen.findByRole("button", { name: "准备新版本" }));
  expect(screen.getByText(/扩展名：png/)).toBeVisible();
  expect(screen.queryByText(/扩展名：jpg/)).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "确认原创建请求" }));
  await waitFor(() => expect(trainingDatasetsApi.create).toHaveBeenCalledTimes(2));
  expect(vi.mocked(trainingDatasetsApi.create).mock.calls[1][1]).toEqual(original);
});

it("retires a preflight when only the filter URL changes within the same dataset", async () => {
  const datasetId = "11111111-2222-4333-8444-555555555555";
  vi.mocked(useProjectDatasets).mockReturnValue({
    data: [{ id: datasetId, name: "目录样本" }],
    isLoading: false,
  } as never);
  let finish!: (value: typeof report) => void;
  vi.mocked(trainingDatasetsApi.preflight).mockImplementationOnce(
    () =>
      new Promise((done) => {
        finish = done;
      }),
  );
  const user = userEvent.setup();
  mount(
    `/projects/project-1/learning?tab=data&dataset_id=${datasetId}&dataset_filter=${encodeURIComponent(JSON.stringify({ extension: "png" }))}`,
  );
  await user.click(await screen.findByRole("button", { name: "准备新版本" }));
  await user.click(
    screen.getByRole("checkbox", { name: "我已确认所选数据集与当前项目的冻结范围" }),
  );
  await user.click(screen.getByRole("button", { name: "检查训练数据" }));
  await waitFor(() => expect(trainingDatasetsApi.preflight).toHaveBeenCalledOnce());
  fireEvent.click(screen.getByRole("button", { name: "切换训练筛选", hidden: true }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  await act(async () => finish(report));
  await user.click(screen.getByRole("button", { name: "准备新版本" }));
  expect(screen.getByText(/扩展名：jpg/)).toBeVisible();
  expect(screen.queryByText("数据检查通过")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "创建数据版本" })).toBeDisabled();
});

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Routes, Route, useLocation, useNavigate } from "react-router-dom";
import { trainedModelsApi } from "@/api/trainedModels";
import { modelLineageApi } from "@/api/modelLineage";
import { trainingRunsApi } from "@/api/trainingRuns";
import { mlBackendsApi } from "@/api/ml-backends";
import { tasksApi } from "@/api/tasks";
import { dataManagerTaskActionsApi } from "@/api/dataManagerActions";
import { useAuthStore } from "@/stores/authStore";
import { confirmDialog } from "@/components/ui/decisionDialog";
import { ProjectModelsPanel } from "./ProjectModelsPanel";
import { TrainingModelPage } from "../Training/TrainingModelPage";
import { ModelLineagePage } from "../Training/ModelLineagePage";

vi.mock("@/api/modelLineage", async (original) => ({
  ...(await original<typeof import("@/api/modelLineage")>()),
  modelLineageApi: { get: vi.fn(), decisions: vi.fn(), append: vi.fn() },
}));

vi.mock("@/api/trainedModels", async (original) => ({
  ...(await original<typeof import("@/api/trainedModels")>()),
  trainedModelsApi: {
    list: vi.fn(),
    get: vi.fn(),
    register: vi.fn(),
    deploy: vi.fn(),
    releases: vi.fn(),
    publish: vi.fn(),
    download: vi.fn(),
    trialMaskContent: vi.fn(),
  },
}));
vi.mock("@/api/trainingRuns", () => ({ trainingRunsApi: { list: vi.fn(), get: vi.fn() } }));
vi.mock("@/api/ml-backends", () => ({ mlBackendsApi: { listAvailable: vi.fn() } }));
vi.mock("@/api/tasks", () => ({ tasksApi: { listByProject: vi.fn() } }));
vi.mock("@/api/dataManagerActions", () => ({
  dataManagerTaskActionsApi: { preannotate: vi.fn() },
}));
vi.mock("@/api/asyncJobs", () => ({
  asyncJobsApi: {
    get: vi.fn().mockResolvedValue({ id: "job-1", status: "completed", progress_pct: 100 }),
  },
}));
vi.mock("@/components/ui/decisionDialog", () => ({
  confirmDialog: vi.fn().mockResolvedValue(true),
}));

const model = {
  id: "model-1",
  project_id: "project-1",
  run_id: "run-1",
  attempt_id: "attempt-1",
  name: "第一候选",
  framework: "ultralytics",
  framework_version: "8.4.177",
  artifact: { sha256: "a".repeat(64) },
  classes: ["car", "person"],
  bindings: ["车辆", "行人"],
  binding_digest: "binding-1",
  provenance: { dataset_id: "data-1", validation_digest: "val-1" },
  evaluation: {
    summary: { "metrics/mAP50-95(B)": 0.4 },
    validation_digest: "val-1",
    evaluation: { framework: "ultralytics", split: "val", imgsz: 640, independent_test_set: false },
    per_class: [
      { index: 0, name: "car", precision: 0.5, recall: 0.6, mAP50: 0.7, "mAP50-95": 0.4 },
    ],
  },
  created_at: "2026-10-14T12:00:00Z",
};
const deployment = {
  id: "deployment-1",
  model_version_id: "model-1",
  registry_id: "backend-1",
  status: "ready",
  receipt: {},
  trial: { result: [], inference_time_ms: 10 },
  error_message: null,
  checked_at: null,
  created_at: model.created_at,
};
const releases = { generation: 7, total: 0, items: [], current_release_id: null, current: null };
const clients: QueryClient[] = [];

let navigate: ReturnType<typeof useNavigate>;
function Navigation() {
  navigate = useNavigate();
  const location = useLocation();
  return <output data-testid="location">{location.pathname + location.search}</output>;
}

function mount(
  entry = "/",
  classes = ["车辆", "行人"],
  taskClasses?: { detect?: string[]; segment?: string[] },
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter
        initialEntries={[entry === "/" ? "/projects/project-1/learning?tab=models" : entry]}
      >
        <Navigation />
        <Routes>
          <Route
            path="/projects/:id/learning"
            element={
              <ProjectModelsPanel
                projectId="project-1"
                classes={classes}
                taskClasses={taskClasses}
              />
            }
          />
          <Route path="/projects/:id/learning/models/:modelId" element={<TrainingModelPage />} />
          <Route
            path="/projects/:id/learning/lineage/:kind/:nodeId"
            element={<ModelLineagePage />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  useAuthStore.getState().setAuth("token-a", { id: "owner" } as never);
  vi.mocked(trainingRunsApi.list).mockResolvedValue({
    total: 1,
    items: [{ id: "run-1", name: "成功训练", status: "succeeded" }],
  } as never);
  vi.mocked(trainingRunsApi.get).mockResolvedValue({
    registration_eligibility: { qualified: true, reason: null },
    run: { id: "run-1", config: { framework: "ultralytics", classes: ["car", "person"] } },
    attempts: [{ id: "attempt-1", ordinal: 1, status: "succeeded" }],
  } as never);
  vi.mocked(trainedModelsApi.list).mockResolvedValue({ total: 1, items: [model] });
  vi.mocked(trainedModelsApi.get).mockResolvedValue({ model, deployments: [deployment] });
  vi.mocked(trainedModelsApi.releases).mockResolvedValue(releases);
  vi.mocked(modelLineageApi.get).mockResolvedValue({
    node: {
      kind: "model",
      id: model.id,
      name: model.name,
      created_at: model.created_at,
      facts: {},
    },
    inputs: [],
    sections: ["deployments", "releases"],
    items: [],
  });
  vi.mocked(modelLineageApi.decisions).mockResolvedValue({ items: [] });
  vi.mocked(modelLineageApi.append).mockResolvedValue({ id: "decision-1" } as never);
  vi.mocked(trainedModelsApi.register).mockResolvedValue(model);
  vi.mocked(trainedModelsApi.publish).mockResolvedValue({ id: "release-8" } as never);
  vi.mocked(mlBackendsApi.listAvailable).mockResolvedValue({
    items: [
      {
        enabled: true,
        backend: { id: "backend-1", name: "受控推理", auth_method: "token", state: "connected" },
      },
    ],
  } as never);
  vi.mocked(tasksApi.listByProject).mockResolvedValue({
    total: 1,
    items: [{ id: "task-1", display_id: "T-1", file_name: "car.png" }],
  } as never);
  vi.mocked(dataManagerTaskActionsApi.preannotate).mockResolvedValue({
    job_id: "job-1",
    status: "pending",
  });
});

afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
});

describe("ProjectModelsPanel", () => {
  it.each(["detect", "segment"])(
    "sets a %s model as default from the all-types list after reviewing its sample",
    async (task) => {
      const candidate = { ...model, task };
      vi.mocked(trainedModelsApi.list).mockResolvedValue({ total: 1, items: [candidate] });
      vi.mocked(trainedModelsApi.get).mockResolvedValue({
        model: candidate,
        deployments: [{ ...deployment, trial: { ...deployment.trial, sample_url: "/sample.png" } }],
      });
      vi.mocked(trainedModelsApi.publish).mockImplementation(async (_project, body) => {
        const published = { ...body, id: "release-8", generation: 8, snapshot: {} };
        vi.mocked(trainedModelsApi.list).mockResolvedValue({
          total: 1,
          items: [{ ...candidate, is_current_release: true }],
        });
        vi.mocked(trainedModelsApi.releases).mockResolvedValue({
          ...releases,
          generation: 8,
          current_release_id: published.id,
          current: published,
        } as never);
        return published as never;
      });
      const user = userEvent.setup();
      mount("/projects/project-1/learning?tab=models&model_task=all&model_page=2");
      await user.click(await screen.findByRole("button", { name: "设为默认预标" }));
      const dialog = screen.getByRole("dialog", { name: "设置默认预标模型" });
      const publish = await within(dialog).findByRole("button", { name: "设为默认预标模型" });
      expect(within(dialog).queryByLabelText("对比另一候选")).not.toBeInTheDocument();
      expect(publish).toBeDisabled();
      if (task === "segment") {
        fireEvent.load(within(dialog).getByAltText("冻结样本与逐实例掩码"));
        await waitFor(() => expect(within(dialog).getByRole("checkbox")).toBeEnabled());
      }
      await user.click(within(dialog).getByRole("checkbox"));
      await user.click(publish);
      await waitFor(() =>
        expect(trainedModelsApi.publish).toHaveBeenCalledWith(
          "project-1",
          expect.objectContaining({
            task,
            expected_generation: 7,
            model_version_id: model.id,
            deployment_id: deployment.id,
            sample_confirmed: true,
          }),
        ),
      );
      await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
      expect(screen.getByText("默认预标")).toBeInTheDocument();
      expect(screen.getByLabelText("模型与发布任务")).toHaveValue("all");
      expect(screen.getByTestId("location")).toHaveTextContent("model_page=2");
      expect(screen.getByTestId("location").textContent).not.toContain("/models/");
    },
  );

  it("keeps the list shortcut from publishing a model without a ready deployment", async () => {
    vi.mocked(trainedModelsApi.get).mockResolvedValue({ model, deployments: [] });
    const user = userEvent.setup();
    mount();
    const shortcut = await screen.findByRole("button", { name: "设为默认预标" });
    await user.click(shortcut);
    const dialog = screen.getByRole("dialog", { name: "设置默认预标模型" });
    await within(dialog).findByRole("button", { name: "加载模型并试用" });
    expect(
      within(dialog).queryByRole("button", { name: "设为默认预标模型" }),
    ).not.toBeInTheDocument();
    expect(trainedModelsApi.publish).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "关闭" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(shortcut).toHaveFocus();
  });

  it("ignores a late publication confirmation after closing and reopening the list dialog", async () => {
    let resolve!: (value: boolean) => void;
    vi.mocked(confirmDialog).mockImplementationOnce(
      () =>
        new Promise((yes) => {
          resolve = yes;
        }),
    );
    const user = userEvent.setup();
    mount();
    const shortcut = await screen.findByRole("button", { name: "设为默认预标" });
    await user.click(shortcut);
    const dialog = screen.getByRole("dialog", { name: "设置默认预标模型" });
    await user.click(await within(dialog).findByRole("checkbox"));
    await user.click(within(dialog).getByRole("button", { name: "设为默认预标模型" }));
    await waitFor(() => expect(confirmDialog).toHaveBeenCalled());
    await user.click(within(dialog).getByRole("button", { name: "关闭" }));
    await user.click(shortcut);
    await act(async () => resolve(true));
    expect(trainedModelsApi.publish).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "设置默认预标模型" })).toBeInTheDocument();
  });

  it("lists all model types with their own defaults, resets pagination and preserves the filter through details", async () => {
    const segment = {
      ...model,
      id: "segment-model",
      name: "分割候选",
      task: "segment",
      is_current_release: true,
      evaluation: { ...model.evaluation, summary: { "metrics/mAP50-95(M)": 0.6 } },
    };
    const mixed = [{ ...model, task: "detect", is_current_release: true }, segment];
    vi.mocked(trainedModelsApi.list).mockImplementation(
      async (_project, _offset, _options, _archive, filters) => ({
        total: 100,
        items: mixed.filter((item) => !filters?.task || item.task === filters.task),
      }),
    );
    vi.mocked(trainedModelsApi.get).mockResolvedValue({ model: segment, deployments: [] });
    const user = userEvent.setup();
    mount("/projects/project-1/learning?tab=models&model_task=all&model_page=2");
    const list = screen.getByRole("region", { name: "模型列表" });
    expect(within(list).getByLabelText("模型与发布任务")).toHaveValue("all");
    for (const [name, type, score] of [
      [model.name, "目标检测", "0.4000"],
      [segment.name, "实例分割", "0.6000"],
    ]) {
      const row = (await within(list).findByRole("link", { name })).closest("tr")!;
      expect(within(row).getByText(type)).toBeInTheDocument();
      expect(within(row).getByText(score)).toBeInTheDocument();
      expect(within(row).getByText("默认预标")).toBeInTheDocument();
    }
    expect(trainedModelsApi.list).toHaveBeenLastCalledWith(
      "project-1",
      40,
      expect.any(Object),
      "exclude",
      expect.objectContaining({ task: undefined }),
      20,
    );
    expect(trainedModelsApi.releases).not.toHaveBeenCalled();
    expect(within(list).queryByRole("button", { name: "重置筛选" })).not.toBeInTheDocument();
    expect(within(list).queryByRole("button", { name: "停用默认预标" })).not.toBeInTheDocument();
    expect(within(list).queryByText("默认预标切换记录")).not.toBeInTheDocument();
    await user.selectOptions(within(list).getByLabelText("模型与发布任务"), "detect");
    await waitFor(() =>
      expect(screen.queryByRole("link", { name: segment.name })).not.toBeInTheDocument(),
    );
    expect(screen.getByTestId("location").textContent).not.toContain("model_page");
    await user.selectOptions(screen.getByLabelText("模型与发布任务"), "all");
    await screen.findByRole("link", { name: segment.name });
    expect(trainedModelsApi.list).toHaveBeenLastCalledWith(
      "project-1",
      0,
      expect.any(Object),
      "exclude",
      expect.objectContaining({ task: undefined }),
      20,
    );
    await user.click(screen.getByRole("link", { name: segment.name }));
    await screen.findByLabelText("模型版本详情");
    expect(trainedModelsApi.releases).toHaveBeenLastCalledWith("project-1", 0, "segment", 20);
    await user.click(screen.getByRole("link", { name: "返回模型列表" }));
    await screen.findByRole("link", { name: segment.name });
    expect(screen.getByLabelText("模型与发布任务")).toHaveValue("all");
    expect(screen.getByRole("link", { name: model.name })).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("模型与发布任务"), "segment");
    await waitFor(() =>
      expect(screen.queryByRole("link", { name: model.name })).not.toBeInTheDocument(),
    );
    await user.click(screen.getByRole("button", { name: "重置筛选" }));
    await screen.findByRole("link", { name: model.name });
    expect(screen.getByLabelText("模型与发布任务")).toHaveValue("all");
    expect(screen.queryByRole("button", { name: "重置筛选" })).not.toBeInTheDocument();
  });

  it.each([
    { task: "detect", spec: "yolo11n", label: "目标检测" },
    { task: "segment", spec: "yolo11n-seg", label: "实例分割" },
  ])(
    "shows each $task model's frozen identity and scopes the default to its own row",
    async ({ task, spec, label }) => {
      vi.mocked(trainedModelsApi.list).mockResolvedValue({
        total: 2,
        items: [model, { ...model, id: "model-2", name: "第二模型" }].map((item) => ({
          ...item,
          task,
          provenance: { ...item.provenance, base_model: { id: spec } },
        })),
      });
      vi.mocked(trainedModelsApi.releases).mockResolvedValue({
        ...releases,
        current: { model_version_id: model.id, generation: 7, snapshot: {} } as never,
      });
      mount(`/projects/project-1/learning?tab=models&model_task=${task}`);
      const row = (await screen.findByRole("link", { name: model.name })).closest("tr")!;
      const other = screen.getByRole("link", { name: "第二模型" }).closest("tr")!;
      expect(within(row).getByText(spec)).toBeInTheDocument();
      expect(within(row).getByText(label)).toBeInTheDocument();
      await waitFor(() => expect(within(row).getByText("默认预标")).toBeInTheDocument());
      const status = within(row).getByText("默认预标").closest("td")!;
      expect(within(status).getByRole("button", { name: "停用默认预标" })).toBeEnabled();
      expect(within(other).getByText(spec)).toBeInTheDocument();
      expect(within(other).getByText(label)).toBeInTheDocument();
      expect(within(other).getByText("已注册")).toBeInTheDocument();
      expect(within(other).queryByRole("button", { name: "停用默认预标" })).not.toBeInTheDocument();
    },
  );
  it("keeps the detection per-class columns and metric values", async () => {
    const user = userEvent.setup();
    mount("/projects/project-1/learning/models/model-1");
    await screen.findByLabelText("模型版本详情");
    await user.click(screen.getByText("分类别结果（仅列出验证集中出现的类别）"));
    const table = screen.getByRole("table");
    expect(
      within(table)
        .getAllByRole("columnheader")
        .map((header) => header.textContent),
    ).toEqual(["类别", "Precision", "Recall", "mAP50", "mAP50-95"]);
    expect(
      within(within(table).getAllByRole("row")[1])
        .getAllByRole("cell")
        .map((cell) => cell.textContent),
    ).toEqual(["car", "0.5000", "0.6000", "0.7000", "0.4000"]);
  });
  it("shows distinct box and mask per-class metrics from parsed segmentation keys, including real zeros and gaps", async () => {
    vi.mocked(trainedModelsApi.get).mockResolvedValue({
      model: {
        ...model,
        task: "segment",
        evaluation: {
          ...model.evaluation,
          summary: { "metrics/mAP50-95(B)": 0.82, "metrics/mAP50-95(M)": 0 },
          per_class: [
            {
              index: 0,
              name: "shape",
              "precision(B)": 0.91,
              "recall(B)": 0.88,
              "mAP50(B)": 0.86,
              "mAP50-95(B)": 0.82,
              "precision(M)": 0,
              "recall(M)": 0,
              "mAP50(M)": 0,
              "mAP50-95(M)": 0,
            },
            {
              index: 1,
              name: "person",
              "precision(B)": 0.61,
              "recall(B)": 0.52,
              "mAP50(B)": 0.43,
              "mAP50-95(B)": 0.34,
              "precision(M)": null,
              "mAP50(M)": 0.12,
              "mAP50-95(M)": 0,
            },
          ],
        },
      },
      deployments: [],
    });
    const user = userEvent.setup();
    mount("/projects/project-1/learning/models/model-1");
    await screen.findByLabelText("模型版本详情");
    await user.click(screen.getByText("分类别结果（仅列出验证集中出现的类别）"));
    const table = screen.getByRole("table");
    expect(
      within(table)
        .getAllByRole("columnheader")
        .map((header) => header.textContent),
    ).toEqual([
      "类别",
      "框 Precision",
      "框 Recall",
      "框 mAP50",
      "框 mAP50-95",
      "掩码 Precision",
      "掩码 Recall",
      "掩码 mAP50",
      "掩码 mAP50-95",
    ]);
    const rows = within(table).getAllByRole("row");
    expect(
      within(rows[1])
        .getAllByRole("cell")
        .map((cell) => cell.textContent),
    ).toEqual([
      "shape",
      "0.9100",
      "0.8800",
      "0.8600",
      "0.8200",
      "0.0000",
      "0.0000",
      "0.0000",
      "0.0000",
    ]);
    expect(
      within(rows[2])
        .getAllByRole("cell")
        .map((cell) => cell.textContent),
    ).toEqual(["person", "0.6100", "0.5200", "0.4300", "0.3400", "—", "—", "0.1200", "0.0000"]);
  });
  it("opens registration from an experiment with the exact execution outside the first source page", async () => {
    vi.mocked(trainingRunsApi.list).mockResolvedValue({ items: [], total: 40 });
    vi.mocked(trainingRunsApi.get).mockResolvedValue({
      registration_eligibility: { qualified: true, reason: null },
      run: {
        id: "older-run",
        name: "较早的实验",
        config: { framework: "ultralytics", classes: ["car", "person"] },
      },
      attempts: [
        { id: "latest-attempt", ordinal: 2, status: "succeeded" },
        { id: "older-attempt", ordinal: 1, status: "succeeded" },
      ],
    } as never);
    const user = userEvent.setup();
    mount(
      "/projects/project-1/learning?tab=models&register_run=older-run&register_attempt=older-attempt",
    );
    const source = await screen.findByRole("option", { name: "较早的实验" });
    expect(source).toHaveValue("older-run");
    expect(screen.getByLabelText("来源训练实验")).toHaveValue("older-run");
    expect(screen.getByLabelText("成功执行尝试")).toHaveValue("older-attempt");
    expect(trainedModelsApi.register).not.toHaveBeenCalled();
    await user.selectOptions(screen.getByLabelText("原生类别 0 · car"), "车辆");
    await user.selectOptions(screen.getByLabelText("原生类别 1 · person"), "行人");
    await user.click(screen.getByRole("button", { name: "从训练结果添加模型" }));
    await waitFor(() => expect(trainedModelsApi.register).toHaveBeenCalledOnce());
    expect(trainedModelsApi.register).toHaveBeenCalledWith(
      "project-1",
      expect.objectContaining({ run_id: "older-run", attempt_id: "older-attempt" }),
    );
    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent("/models/model-1"),
    );
    expect(screen.getByTestId("location").textContent).not.toContain("register_");
  });
  it("blocks duplicate return from a saved registration URL and allows another successful execution", async () => {
    vi.mocked(trainingRunsApi.get).mockResolvedValue({
      registration_eligibility: { qualified: true, reason: null },
      registered_models: [{ id: "model-1", attempt_id: "attempt-1", name: "已有模型" }],
      run: { id: "run-1", config: { framework: "ultralytics", classes: ["car", "person"] } },
      attempts: [
        { id: "attempt-2", ordinal: 2, status: "succeeded" },
        { id: "attempt-1", ordinal: 1, status: "succeeded" },
      ],
    } as never);
    const user = userEvent.setup();
    mount("/projects/project-1/learning?tab=models&register_run=run-1&register_attempt=attempt-1");
    expect(await screen.findByText(/本次执行已回流为项目模型“已有模型”/)).toBeVisible();
    expect(screen.getByRole("button", { name: "从训练结果添加模型" })).toBeDisabled();
    expect(screen.queryByLabelText("原生类别 0 · car")).not.toBeInTheDocument();
    expect(trainedModelsApi.register).not.toHaveBeenCalled();
    await user.selectOptions(screen.getByLabelText("成功执行尝试"), "attempt-2");
    await user.selectOptions(screen.getByLabelText("原生类别 0 · car"), "车辆");
    await user.selectOptions(screen.getByLabelText("原生类别 1 · person"), "行人");
    expect(screen.getByRole("button", { name: "从训练结果添加模型" })).toBeEnabled();
  });
  it("keeps a linked registration draft when closed without leaving an auto-open URL", async () => {
    const user = userEvent.setup();
    mount("/projects/project-1/learning?tab=models&register_run=run-1&register_attempt=attempt-1");
    await screen.findByLabelText("原生类别 0 · car");
    await user.clear(screen.getByLabelText("模型名称"));
    await user.type(screen.getByLabelText("模型名称"), "训练回流草稿");
    await user.keyboard("{Escape}");
    expect(screen.getByTestId("location").textContent).not.toContain("register_");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "从训练结果添加模型" }));
    expect(screen.getByLabelText("模型名称")).toHaveValue("训练回流草稿");
    expect(screen.getByLabelText("来源训练实验")).toHaveValue("run-1");
    expect(trainedModelsApi.register).not.toHaveBeenCalled();
  });
  it("keeps a successful historical source selectable after a later execution failed", async () => {
    vi.mocked(trainingRunsApi.list).mockResolvedValue({
      items: [{ id: "run-1", name: "成功后续训失败", status: "failed" }],
      total: 1,
    } as never);
    vi.mocked(trainingRunsApi.get).mockResolvedValue({
      registration_eligibility: { qualified: true, reason: null },
      run: {
        id: "run-1",
        name: "成功后续训失败",
        status: "failed",
        config: { framework: "ultralytics", classes: ["car", "person"] },
      },
      attempts: [
        { id: "failed-attempt", ordinal: 2, status: "failed" },
        { id: "attempt-1", ordinal: 1, status: "succeeded" },
      ],
    } as never);
    mount("/projects/project-1/learning?tab=models&register_run=run-1&register_attempt=attempt-1");
    await screen.findByRole("option", { name: "成功后续训失败" });
    expect(screen.getByLabelText("来源训练实验")).toHaveValue("run-1");
    expect(screen.getByLabelText("成功执行尝试")).toHaveValue("attempt-1");
    expect(trainedModelsApi.register).not.toHaveBeenCalled();
  });
  it("rejects an unavailable execution from a registration URL until a successful one is selected", async () => {
    const user = userEvent.setup();
    mount("/projects/project-1/learning?tab=models&register_run=run-1&register_attempt=missing");
    await screen.findByText("指定的执行记录不可用于回流，请选择成功的执行记录。");
    await user.selectOptions(screen.getByLabelText("原生类别 0 · car"), "车辆");
    await user.selectOptions(screen.getByLabelText("原生类别 1 · person"), "行人");
    expect(screen.getByRole("button", { name: "从训练结果添加模型" })).toBeDisabled();
    expect(trainedModelsApi.register).not.toHaveBeenCalled();
    await user.selectOptions(screen.getByLabelText("成功执行尝试"), "attempt-1");
    expect(screen.getByRole("button", { name: "从训练结果添加模型" })).toBeEnabled();
  });
  it("registers all 80 bindings in original order even when the editor is filtered", async () => {
    const user = userEvent.setup();
    const classes = Array.from({ length: 80 }, (_, index) => `类别${index}`);
    vi.mocked(trainingRunsApi.get).mockResolvedValue({
      registration_eligibility: { qualified: true, reason: null },
      run: { id: "run-1", config: { framework: "ultralytics", classes } },
      attempts: [{ id: "attempt-1", ordinal: 1, status: "succeeded" }],
    } as never);
    mount("/", classes);
    await user.click(screen.getByRole("button", { name: "从训练结果添加模型" }));
    await screen.findByRole("option", { name: "成功训练" });
    await user.selectOptions(screen.getByLabelText("来源训练实验"), "run-1");
    await user.selectOptions(await screen.findByLabelText("原生类别 0 · 类别0"), "类别1");
    const search = screen.getByRole("searchbox", { name: "搜索类别绑定" });
    await user.type(search, "类别79{Enter}");
    expect(trainedModelsApi.register).not.toHaveBeenCalled();
    await user.selectOptions(screen.getByLabelText("原生类别 79 · 类别79"), "类别0");
    await user.click(screen.getByRole("button", { name: "按同名绑定" }));
    await user.click(screen.getByRole("button", { name: "从训练结果添加模型" }));
    await waitFor(() => expect(trainedModelsApi.register).toHaveBeenCalledOnce());
    expect(vi.mocked(trainedModelsApi.register).mock.calls[0][1].bindings).toEqual(
      classes.map((name, index) => (index === 0 ? "类别1" : index === 79 ? "类别0" : name)),
    );
  });
  it("starts with the source experiment and preserves edits to the timestamp name", async () => {
    const user = userEvent.setup();
    mount();
    await user.click(screen.getByRole("button", { name: "从训练结果添加模型" }));
    const source = screen.getByLabelText("来源训练实验");
    const name = screen.getByLabelText("模型名称") as HTMLInputElement;
    expect(source.compareDocumentPosition(name) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(name.value).toMatch(/^\d{4}-\d{4}-\d{4} $/);
    await user.clear(name);
    await screen.findByRole("option", { name: "成功训练" });
    await user.selectOptions(source, "run-1");
    await screen.findByRole("combobox", { name: /原生类别 0/ });
    expect(name).toHaveValue("");
    expect(screen.getByRole("button", { name: "从训练结果添加模型" })).toBeDisabled();
    expect(trainedModelsApi.register).not.toHaveBeenCalled();
  });
  it("keeps loading release and candidate states distinct from an empty model library", async () => {
    vi.mocked(trainedModelsApi.list).mockReturnValue(new Promise(() => {}));
    vi.mocked(trainedModelsApi.releases).mockReturnValue(new Promise(() => {}));
    mount();
    expect(screen.getByText("正在读取默认预标设置…")).toBeVisible();
    expect(screen.getByText("正在加载模型列表…")).toBeInTheDocument();
    expect(screen.queryByText("尚未注册模型版本")).not.toBeInTheDocument();
    expect(screen.queryByText("未设置默认预标模型")).not.toBeInTheDocument();
  });
  it("takes the empty candidate entry to the source experiment without creating a model", async () => {
    vi.mocked(trainedModelsApi.list).mockResolvedValue({ total: 0, items: [] });
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("button", { name: "选择训练结果" }));
    expect(screen.getByLabelText("来源训练实验")).toHaveFocus();
    expect(trainedModelsApi.register).not.toHaveBeenCalled();
  });
  it.each(["ultralytics", "libreyolo"])(
    "registers qualified %s with explicit complete class binding and preserves identity after a lost response",
    async (framework) => {
      const user = userEvent.setup();
      vi.mocked(trainingRunsApi.get).mockResolvedValue({
        registration_eligibility: { qualified: true, reason: null },
        run: { id: "run-1", config: { framework, classes: ["car", "person"] } },
        attempts: [{ id: "attempt-1", ordinal: 1, status: "succeeded" }],
      } as never);
      vi.mocked(trainedModelsApi.register)
        .mockRejectedValueOnce(new Error("响应中断"))
        .mockResolvedValue(model);
      mount();
      await user.click(screen.getByRole("button", { name: "从训练结果添加模型" }));
      await user.type(screen.getByLabelText("模型名称"), "回流候选");
      await user.selectOptions(await screen.findByLabelText("来源训练实验"), "run-1");
      await screen.findByRole("combobox", { name: /原生类别 0/ });
      const button = screen.getByRole("button", { name: "从训练结果添加模型" });
      expect(button).toBeDisabled();
      await user.selectOptions(screen.getByRole("combobox", { name: /原生类别 0/ }), "车辆");
      expect(button).toBeDisabled();
      await user.selectOptions(screen.getByRole("combobox", { name: /原生类别 1/ }), "行人");
      await user.click(button);
      await screen.findByText("响应中断");
      const first = vi.mocked(trainedModelsApi.register).mock.calls[0][1];
      expect(first.bindings).toEqual(["车辆", "行人"]);
      await user.click(button);
      await waitFor(() => expect(trainedModelsApi.register).toHaveBeenCalledTimes(2));
      expect(vi.mocked(trainedModelsApi.register).mock.calls[1][1]).toEqual(first);
    },
  );

  it("requires sample confirmation and publishes against the current generation", async () => {
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("link", { name: "第一候选" }));
    const detail = await screen.findByLabelText("模型版本详情");
    const publish = within(detail).getByRole("button", { name: "设为默认预标模型" });
    expect(publish).toBeDisabled();
    await user.click(within(detail).getByLabelText("已查看固定验证样本，确认用于后续人工试用"));
    await user.click(publish);
    await waitFor(() => expect(trainedModelsApi.publish).toHaveBeenCalledTimes(1));
    expect(vi.mocked(trainedModelsApi.publish).mock.calls[0]).toEqual([
      "project-1",
      expect.objectContaining({
        expected_generation: 7,
        model_version_id: "model-1",
        deployment_id: "deployment-1",
        sample_confirmed: true,
      }),
    ]);
  });

  it("marks the current deployment as published and blocks a redundant historical rollback", async () => {
    vi.mocked(trainedModelsApi.releases).mockResolvedValue({
      ...releases,
      current_release_id: "release-7",
      current: {
        id: "release-7",
        model_version_id: model.id,
        deployment_id: deployment.id,
        generation: 7,
        snapshot: {},
      } as never,
      items: [
        {
          id: "release-6",
          model_version_id: model.id,
          deployment_id: deployment.id,
          generation: 6,
          snapshot: {},
        },
      ] as never,
    });
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByText("默认预标切换记录"));
    const historyButton = await screen.findByRole("button", { name: "已是默认模型" });
    expect(historyButton).toBeDisabled();
    await user.click(screen.getByRole("link", { name: "第一候选" }));
    const detail = await screen.findByLabelText("模型版本详情");
    expect(await within(detail).findByRole("button", { name: "已设为默认预标" })).toBeDisabled();
    expect(
      within(detail).queryByLabelText("已查看固定验证样本，确认用于后续人工试用"),
    ).not.toBeInTheDocument();
    expect(trainedModelsApi.publish).not.toHaveBeenCalled();
  });

  it("allows the current model to be published to a different deployment", async () => {
    vi.mocked(trainedModelsApi.releases).mockResolvedValue({
      ...releases,
      current: {
        model_version_id: model.id,
        deployment_id: "other-deployment",
        generation: 7,
        snapshot: {},
      } as never,
    });
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("link", { name: "第一候选" }));
    const detail = await screen.findByLabelText("模型版本详情");
    await user.click(within(detail).getByLabelText("已查看固定验证样本，确认用于后续人工试用"));
    expect(within(detail).getByRole("button", { name: "设为默认预标模型" })).toBeEnabled();
  });

  it("keeps publishing disabled until the current release refresh completes", async () => {
    const published = {
      id: "release-8",
      project_id: "project-1",
      model_version_id: model.id,
      deployment_id: deployment.id,
      generation: 8,
      snapshot: {},
      previous_release_id: null,
      rollback_from_id: null,
      created_at: model.created_at,
    };
    let resolveRefresh!: (value: Awaited<ReturnType<typeof trainedModelsApi.releases>>) => void;
    const refreshed = new Promise<Awaited<ReturnType<typeof trainedModelsApi.releases>>>(
      (resolve) => {
        resolveRefresh = resolve;
      },
    );
    vi.mocked(trainedModelsApi.publish).mockImplementation(async () => {
      vi.mocked(trainedModelsApi.releases).mockReturnValue(refreshed);
      return published;
    });
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("link", { name: "第一候选" }));
    const detail = await screen.findByLabelText("模型版本详情");
    await user.click(within(detail).getByLabelText("已查看固定验证样本，确认用于后续人工试用"));
    const button = within(detail).getByRole("button", { name: "设为默认预标模型" });
    await user.click(button);
    await waitFor(() => expect(trainedModelsApi.publish).toHaveBeenCalledTimes(1));
    expect(button).toBeDisabled();
    await user.click(button);
    expect(trainedModelsApi.publish).toHaveBeenCalledTimes(1);
    await act(async () =>
      resolveRefresh({
        ...releases,
        generation: 8,
        current_release_id: published.id,
        current: published,
      }),
    );
    expect(await within(detail).findByRole("button", { name: "已设为默认预标" })).toBeDisabled();
  });

  it("retains the exact release request after a network error even if polling sees a newer generation", async () => {
    const user = userEvent.setup();
    vi.mocked(trainedModelsApi.publish)
      .mockRejectedValueOnce(new Error("发布响应丢失"))
      .mockResolvedValue({ id: "release-8" } as never);
    mount();
    await user.click(await screen.findByRole("link", { name: "第一候选" }));
    const detail = await screen.findByLabelText("模型版本详情");
    await user.click(within(detail).getByLabelText("已查看固定验证样本，确认用于后续人工试用"));
    vi.mocked(trainedModelsApi.releases).mockResolvedValue({ ...releases, generation: 8 });
    await user.click(within(detail).getByRole("button", { name: "设为默认预标模型" }));
    await screen.findByText("发布响应丢失");
    const first = vi.mocked(trainedModelsApi.publish).mock.calls[0][1];
    await user.click(within(detail).getByRole("button", { name: "设为默认预标模型" }));
    await waitFor(() => expect(trainedModelsApi.publish).toHaveBeenCalledTimes(2));
    expect(vi.mocked(trainedModelsApi.publish).mock.calls[1][1]).toEqual(first);
    expect(first.expected_generation).toBe(7);
  });

  it("queues selected images through the existing idempotent preannotation action", async () => {
    const user = userEvent.setup();
    vi.mocked(trainedModelsApi.releases).mockResolvedValue({
      ...releases,
      current_release_id: "release-7",
      current: {
        id: "release-7",
        generation: 7,
        model_version_id: "model-1",
        snapshot: { registry_id: "backend-1" },
      },
    } as never);
    mount();
    await user.click(await screen.findByLabelText("T-1 · car.png"));
    await user.click(screen.getByRole("button", { name: "用默认模型预标注" }));
    await waitFor(() => expect(dataManagerTaskActionsApi.preannotate).toHaveBeenCalledTimes(1));
    expect(vi.mocked(dataManagerTaskActionsApi.preannotate).mock.calls[0]).toEqual([
      "project-1",
      expect.objectContaining({
        model_id: "project-release",
        ml_backend_id: "backend-1",
        task_ids: ["task-1"],
        params: { conf: 0.25 },
      }),
      { idempotencyKey: expect.any(String) },
    ]);
    await screen.findByText("预标注完成 · 100%");
  });

  it("separates reports from different validation scopes", async () => {
    const user = userEvent.setup();
    const second = {
      ...model,
      id: "model-2",
      name: "另一基准",
      provenance: { ...model.provenance, validation_digest: "val-2" },
    };
    vi.mocked(trainedModelsApi.list).mockResolvedValue({ total: 2, items: [model, second] });
    mount();
    await user.click(await screen.findByRole("link", { name: "第一候选" }));
    await user.selectOptions(await screen.findByLabelText("对比另一候选"), "model-2");
    await screen.findByText(
      "验证基准、类别绑定或评估配置不同，不计算新旧提升。请分别查看各自报告。",
    );
    expect(screen.queryByText("同一验证范围")).not.toBeInTheDocument();
    vi.mocked(trainedModelsApi.list).mockResolvedValue({
      total: 2,
      items: [model, { ...second, id: "model-3" }],
    });
    await act(async () => {
      await clients[clients.length - 1]!.refetchQueries({
        queryKey: ["trained-models", "project-1"],
      });
    });
    expect(screen.getByLabelText("对比另一候选")).toHaveValue("model-2");
    await waitFor(() =>
      expect(screen.getByLabelText("对比另一候选")).toHaveDisplayValue(
        "已选对比模型（未在当前列表中）",
      ),
    );
    await user.click(screen.getByRole("button", { name: "取消模型对比" }));
    expect(screen.getByLabelText("对比另一候选")).toHaveValue("");
    expect(screen.queryByText(/验证基准、类别绑定或评估配置不同/)).not.toBeInTheDocument();
  });
});

it("explains and disables registration when native return is unqualified", async () => {
  const user = userEvent.setup();
  vi.mocked(trainingRunsApi.get).mockResolvedValue({
    registration_eligibility: { qualified: false, reason: "该原生模型家族尚未通过资格检查" },
    run: { id: "run-1", config: { framework: "libreyolo", classes: ["car", "person"] } },
    attempts: [{ id: "attempt-1", ordinal: 1, status: "succeeded" }],
  } as never);
  mount();
  await user.click(screen.getByRole("button", { name: "从训练结果添加模型" }));
  await user.type(screen.getByLabelText("模型名称"), "Libre候选");
  await user.selectOptions(await screen.findByLabelText("来源训练实验"), "run-1");
  await screen.findByText(/该原生模型家族尚未通过资格检查/);
  expect(screen.getByRole("button", { name: "从训练结果添加模型" })).toBeDisabled();
  expect(screen.queryByLabelText(/原生类别 0/)).not.toBeInTheDocument();
  expect(trainedModelsApi.register).not.toHaveBeenCalled();
});

it("loads a deep-linked archived candidate independently of the list page and preserves return context", async () => {
  vi.mocked(trainedModelsApi.list).mockResolvedValue({ items: [], total: 100 });
  vi.mocked(trainedModelsApi.get).mockResolvedValue({
    model: { ...model, archived: true },
    deployments: [],
  });
  mount("/projects/project-1/learning/models/model-1?tab=models&archive=only&model_page=2");
  expect(await screen.findByRole("heading", { name: "第一候选" })).toBeInTheDocument();
  expect(trainedModelsApi.list).toHaveBeenCalledWith(
    "project-1",
    40,
    expect.objectContaining({ signal: expect.any(AbortSignal) }),
    "only",
    expect.objectContaining({ q: "" }),
    20,
  );
  expect(screen.getByRole("tab", { name: "来源与使用" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "返回模型列表" })).toHaveAttribute(
    "href",
    "/projects/project-1/learning?tab=models&archive=only&model_page=2",
  );
});

it("unifies row details, publication, usage records and model notes without leaving the model route", async () => {
  const user = userEvent.setup();
  mount("/projects/project-1/learning?tab=models&archive=include&model_page=2");
  const action = await screen.findByRole("link", { name: model.name });
  const list = screen.getByRole("region", { name: "模型列表" });
  expect(within(list).getAllByRole("columnheader")).toHaveLength(8);
  expect(within(list).queryByRole("columnheader", { name: "操作" })).not.toBeInTheDocument();
  expect(within(list).queryByRole("link", { name: "详情与操作" })).not.toBeInTheDocument();
  await user.click(action);
  await screen.findByRole("region", { name: "模型版本详情" });
  expect(screen.getByRole("button", { name: "加载模型并试用" })).toBeInTheDocument();
  expect(modelLineageApi.get).not.toHaveBeenCalled();
  expect(modelLineageApi.decisions).not.toHaveBeenCalled();
  await user.click(screen.getByRole("tab", { name: "来源与使用" }));
  await screen.findByRole("heading", { name: "来源记录" });
  expect(screen.getByTestId("location")).toHaveTextContent("/learning/models/model-1?");
  expect(modelLineageApi.decisions).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "发布记录" }));
  await waitFor(() =>
    expect(modelLineageApi.get).toHaveBeenLastCalledWith(
      "project-1",
      "model",
      model.id,
      "releases",
      undefined,
      expect.anything(),
      20,
    ),
  );
  await user.click(screen.getByRole("tab", { name: "标记与备注" }));
  await user.type(await screen.findByLabelText("标记说明"), "保留以供进一步评估");
  await user.click(screen.getByRole("button", { name: "保存标记与说明" }));
  await screen.findByText("标记与说明已保存。默认预标选择未改变。");
  expect(modelLineageApi.append).toHaveBeenCalledWith(
    "project-1",
    model.id,
    expect.objectContaining({ disposition: "research", note: "保留以供进一步评估" }),
  );
  expect(trainedModelsApi.publish).not.toHaveBeenCalled();
  await user.click(screen.getByRole("tab", { name: "评估与发布" }));
  await screen.findByRole("button", { name: "下载模型" });
  await user.click(screen.getByRole("link", { name: "返回模型列表" }));
  expect(screen.getByTestId("location")).toHaveTextContent(
    "/projects/project-1/learning?tab=models&archive=include&model_page=2",
  );
});

it("opens an old model lineage link in the unified page and preserves its return destination", async () => {
  mount(
    "/projects/project-1/learning/lineage/model/model-1?relation=releases&returnTo=%2Fprojects%2Fproject-1%2Flearning%3Ftab%3Dmodels%26archive%3Donly%26model_page%3D2",
  );
  await screen.findByRole("heading", { name: "来源记录" });
  expect(screen.getByRole("tab", { name: "来源与使用", selected: true })).toBeInTheDocument();
  expect(screen.getByTestId("location")).toHaveTextContent("/learning/models/model-1?");
  expect(screen.getByRole("link", { name: "返回模型列表" })).toHaveAttribute(
    "href",
    "/projects/project-1/learning?tab=models&archive=only&model_page=2",
  );
  expect(modelLineageApi.get).toHaveBeenCalledWith(
    "project-1",
    "model",
    model.id,
    "releases",
    undefined,
    expect.anything(),
    20,
  );
});

it("opens model notes directly and keeps records failures within their own tab", async () => {
  const user = userEvent.setup();
  mount("/projects/project-1/learning/models/model-1?tab=models&model_view=notes");
  await screen.findByLabelText("模型标记");
  expect(screen.getByRole("tab", { name: "标记与备注", selected: true })).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "来源记录" })).not.toBeInTheDocument();
  expect(trainedModelsApi.get).not.toHaveBeenCalled();
  vi.mocked(modelLineageApi.get).mockRejectedValueOnce(new Error("来源暂时无法读取"));
  await user.click(screen.getByRole("tab", { name: "来源与使用" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("来源暂时无法读取");
  await user.click(screen.getByRole("tab", { name: "评估与发布" }));
  await screen.findByRole("button", { name: "加载模型并试用" });
  expect(screen.queryByText("来源暂时无法读取")).not.toBeInTheDocument();
});

it("ignores a delayed note acknowledgement after switching model tabs", async () => {
  let resolve!: (value: never) => void;
  vi.mocked(modelLineageApi.append).mockImplementation(
    () =>
      new Promise((yes) => {
        resolve = yes;
      }),
  );
  const user = userEvent.setup();
  mount("/projects/project-1/learning/models/model-1?model_view=notes");
  await user.type(await screen.findByLabelText("标记说明"), "仍需评估");
  await user.click(screen.getByRole("button", { name: "保存标记与说明" }));
  await waitFor(() => expect(modelLineageApi.append).toHaveBeenCalledTimes(1));
  await user.click(screen.getByRole("tab", { name: "评估与发布" }));
  await screen.findByRole("button", { name: "加载模型并试用" });
  const invalidate = vi.spyOn(clients[0], "invalidateQueries");
  await act(async () => resolve({ id: "late" } as never));
  expect(invalidate).not.toHaveBeenCalled();
  expect(screen.queryByText("标记与说明已保存。默认预标选择未改变。")).not.toBeInTheDocument();
});

it("rejects delayed publication confirmation after candidate navigation ABA", async () => {
  const user = userEvent.setup();
  let resolve!: (value: boolean) => void;
  vi.mocked(confirmDialog).mockImplementationOnce(
    () =>
      new Promise((yes) => {
        resolve = yes;
      }),
  );
  mount("/projects/project-1/learning/models/model-1?tab=models");
  await user.click(await screen.findByRole("checkbox", { name: /已查看.*样本/ }));
  await user.click(screen.getByRole("button", { name: "设为默认预标模型" }));
  await waitFor(() => expect(confirmDialog).toHaveBeenCalled());
  await act(async () => navigate("?tab=models&archive=include"));
  await act(async () => navigate(-1));
  await act(async () => resolve(true));
  expect(trainedModelsApi.publish).not.toHaveBeenCalled();
});

it("keeps the default preannotation target explicit when it is outside the filtered model page", async () => {
  vi.mocked(trainedModelsApi.list).mockResolvedValue({ items: [], total: 90 });
  vi.mocked(trainedModelsApi.releases).mockResolvedValue({
    ...releases,
    current: { model_version_id: "model-1", generation: 7, snapshot: {} } as never,
  });
  mount("/projects/project-1/learning?tab=models&model_page=2&model_q=other");
  const list = screen.getByRole("region", { name: "模型列表" });
  expect(within(list).getByLabelText("搜索模型名称")).toHaveValue("other");
  expect(within(list).getByLabelText("模型与发布任务")).toHaveValue("detect");
  const preannotation = await within(list).findByRole("region", { name: "使用默认预标模型" });
  await waitFor(() => expect(preannotation).toHaveTextContent("图片目标检测 · 第一候选"));
  expect(within(list).getByText("默认预标切换记录")).toBeInTheDocument();
  expect(screen.queryByRole("region", { name: "当前项目发布" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "停用默认预标" })).not.toBeInTheDocument();
});

it("keeps registration disabled when the server has not supplied eligibility", async () => {
  const user = userEvent.setup();
  vi.mocked(trainingRunsApi.get).mockResolvedValue({
    run: { id: "run-1", config: { framework: "ultralytics", classes: ["car", "person"] } },
    attempts: [{ id: "attempt-1", ordinal: 1, status: "succeeded" }],
  } as never);
  mount();
  await user.click(screen.getByRole("button", { name: "从训练结果添加模型" }));
  await user.type(screen.getByLabelText("模型名称"), "待核验候选");
  await user.selectOptions(await screen.findByLabelText("来源训练实验"), "run-1");
  await screen.findByText(/尚未取得模型回流资格/);
  expect(screen.getByRole("button", { name: "从训练结果添加模型" })).toBeDisabled();
  expect(trainedModelsApi.register).not.toHaveBeenCalled();
});

it("keeps native metric gaps visible and declines comparisons without evaluator identity", async () => {
  const user = userEvent.setup();
  const native = {
    ...model,
    framework: "libreyolo",
    framework_version: "1.6.0",
    evaluation: {
      summary: { "metrics/mAP50-95": 0, "metrics/AR100": 0.4 },
      per_class: [],
      per_class_available: false,
      comparison_identity_status: "unknown",
      comparison_compatibility_digest: null,
    },
  };
  vi.mocked(trainedModelsApi.list).mockResolvedValue({
    total: 2,
    items: [native, { ...native, id: "model-2", name: "原生对照" }],
  });
  vi.mocked(trainedModelsApi.get).mockResolvedValue({ model: native, deployments: [deployment] });
  mount();
  await user.click(await screen.findByRole("link", { name: "第一候选" }));
  expect(await screen.findByText(/原生报告未提供分类别指标/)).toBeInTheDocument();
  expect(screen.getByText("AR100")).toBeInTheDocument();
  await user.selectOptions(await screen.findByLabelText("对比另一候选"), "model-2");
  expect(await screen.findByText(/不计算新旧提升/)).toBeInTheDocument();
  expect(screen.queryByText("同一验证范围")).not.toBeInTheDocument();
});

it("keeps registration in a dialog, preserves the draft and restores focus", async () => {
  const user = userEvent.setup();
  mount();
  const button = screen.getByRole("button", { name: "从训练结果添加模型" });
  expect(screen.queryByLabelText("模型名称")).not.toBeInTheDocument();
  expect(trainingRunsApi.list).not.toHaveBeenCalled();
  await user.click(button);
  const dialog = screen.getByRole("dialog", { name: "从训练结果添加模型" });
  await user.clear(within(dialog).getByLabelText("模型名称"));
  await user.type(within(dialog).getByLabelText("模型名称"), "未完成候选");
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(button).toHaveFocus();
  await user.click(button);
  expect(screen.getByLabelText("模型名称")).toHaveValue("未完成候选");
  expect(trainedModelsApi.register).not.toHaveBeenCalled();
});

it("preserves model filters and pagination through detail navigation", async () => {
  const user = userEvent.setup();
  mount(
    "/projects/project-1/learning?tab=models&model_q=第一&model_framework=ultralytics&model_created_from=2026-10-14&model_page=2&archive=include",
  );
  const link = await screen.findByRole("link", { name: "第一候选" });
  expect(trainedModelsApi.list).toHaveBeenCalledWith(
    "project-1",
    40,
    expect.any(Object),
    "include",
    {
      task: "detect",
      q: "第一",
      framework: "ultralytics",
      created_from: "2026-10-14",
      created_to: "",
    },
    20,
  );
  await user.click(link);
  await screen.findByLabelText("模型版本详情");
  expect(screen.queryByLabelText("模型列表")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "从训练结果添加模型" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("link", { name: "返回模型列表" }));
  expect(screen.getByTestId("location").textContent).toContain("model_q=%E7%AC%AC%E4%B8%80");
  expect(screen.getByTestId("location").textContent).toContain("model_page=2");
  expect(screen.getByLabelText("模型框架筛选")).toHaveValue("ultralytics");
});

it("resets pagination when filtering and clears archive together with model filters", async () => {
  const user = userEvent.setup();
  mount("/projects/project-1/learning?tab=models&model_page=3&archive=only");
  await user.selectOptions(screen.getByLabelText("模型框架筛选"), "libreyolo");
  await waitFor(() =>
    expect(trainedModelsApi.list).toHaveBeenLastCalledWith(
      "project-1",
      0,
      expect.any(Object),
      "only",
      expect.objectContaining({ framework: "libreyolo" }),
      20,
    ),
  );
  expect(screen.getByTestId("location").textContent).not.toContain("model_page");
  await user.click(screen.getByRole("button", { name: "重置筛选" }));
  expect(screen.getByLabelText("模型框架筛选")).toHaveValue("");
  expect(screen.getByLabelText("模型显示范围")).toHaveValue("exclude");
});

it("retries a failed direct model detail request without displaying the list", async () => {
  const user = userEvent.setup();
  vi.mocked(trainedModelsApi.get)
    .mockRejectedValueOnce(new Error("详情请求失败"))
    .mockResolvedValue({ model, deployments: [] });
  mount("/projects/project-1/learning/models/model-1?model_page=2");
  await screen.findByText("详情请求失败");
  expect(screen.queryByLabelText("模型列表")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "重试" }));
  expect(await screen.findByRole("heading", { name: model.name })).toBeVisible();
});

it("does not expose deployment actions for a mismatched model response", async () => {
  vi.mocked(trainedModelsApi.get).mockResolvedValue({
    model: { ...model, project_id: "other-project" },
    deployments: [deployment],
  });
  mount("/projects/project-1/learning/models/model-1");
  expect(await screen.findByText("模型详情与当前项目或版本不匹配")).toBeVisible();
  expect(screen.queryByRole("button", { name: "设为默认预标模型" })).not.toBeInTheDocument();
  expect(screen.queryByLabelText("模型版本详情")).not.toBeInTheDocument();
});

it("explains unenabled and unauthenticated services instead of silently filtering them out", async () => {
  vi.mocked(mlBackendsApi.listAvailable).mockResolvedValue({
    items: [
      {
        enabled: false,
        backend: { id: "backend-1", name: "yolo-backend", state: "connected", auth_method: "none" },
      },
    ],
  } as never);
  mount("/projects/project-1/learning/models/model-1");
  expect(await screen.findByText("本项目尚未启用推理服务，暂时无法加载此模型。")).toBeVisible();
  const service = screen.getByRole("option", {
    name: /yolo-backend · 项目未启用、未配置推理认证/,
  });
  expect(service).toBeDisabled();
  expect(screen.getByRole("list", { name: "已注册推理服务状态" })).toHaveTextContent(
    "yolo-backend · 项目未启用、未配置推理认证",
  );
  expect(screen.getByRole("button", { name: "加载模型并试用" })).toBeDisabled();
  expect(trainedModelsApi.deploy).not.toHaveBeenCalled();
});

it("selects the only ready inference service and loads the exact model when clicked", async () => {
  vi.mocked(trainedModelsApi.deploy).mockResolvedValue(deployment as never);
  const user = userEvent.setup();
  mount("/projects/project-1/learning/models/model-1");
  const button = await screen.findByRole("button", { name: "加载模型并试用" });
  await waitFor(() => expect(button).toBeEnabled());
  expect(screen.getByLabelText("用于加载模型的推理服务")).toHaveValue("backend-1");
  expect(trainedModelsApi.deploy).not.toHaveBeenCalled();
  await user.click(button);
  await waitFor(() =>
    expect(trainedModelsApi.deploy).toHaveBeenCalledWith("project-1", "model-1", "backend-1"),
  );
  expect(trainedModelsApi.publish).not.toHaveBeenCalled();
});

it("requires an explicit choice between multiple ready services", async () => {
  vi.mocked(mlBackendsApi.listAvailable).mockResolvedValue({
    items: ["backend-1", "backend-2"].map((id) => ({
      enabled: true,
      backend: { id, name: id, auth_method: "token", state: "connected" },
    })),
  } as never);
  const user = userEvent.setup();
  mount("/projects/project-1/learning/models/model-1");
  expect(await screen.findByText("请选择一个在线且已启用的推理服务，再加载此模型。")).toBeVisible();
  const button = screen.getByRole("button", { name: "加载模型并试用" });
  expect(button).toBeDisabled();
  await user.selectOptions(screen.getByLabelText("用于加载模型的推理服务"), "backend-2");
  expect(button).toBeEnabled();
  expect(trainedModelsApi.deploy).not.toHaveBeenCalled();
});

it("explains missing authentication on an enabled and connected service", async () => {
  vi.mocked(mlBackendsApi.listAvailable).mockResolvedValue({
    items: [
      {
        enabled: true,
        backend: { id: "backend-1", name: "YOLO", auth_method: "none", state: "connected" },
      },
    ],
  } as never);
  mount("/projects/project-1/learning/models/model-1");
  expect(
    await screen.findByText("已启用的推理服务尚未配置系统托管或 Token 认证，无法加载训练权重。"),
  ).toBeVisible();
  expect(screen.getByRole("button", { name: "加载模型并试用" })).toBeDisabled();
  expect(trainedModelsApi.deploy).not.toHaveBeenCalled();
});

it("reports service query failures separately from an unconfigured project and offers retry", async () => {
  vi.mocked(mlBackendsApi.listAvailable).mockRejectedValueOnce(new Error("读取服务超时"));
  const user = userEvent.setup();
  mount("/projects/project-1/learning/models/model-1");
  expect(await screen.findByText("推理服务列表读取失败，请重试。")).toBeVisible();
  expect(
    screen.queryByText("本项目尚未启用推理服务，暂时无法加载此模型。"),
  ).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "加载模型并试用" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "重试" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "加载模型并试用" })).toBeEnabled());
});

it("disables loading when the selected service goes offline during polling", async () => {
  const user = userEvent.setup();
  mount("/projects/project-1/learning/models/model-1");
  const select = await screen.findByLabelText("用于加载模型的推理服务");
  await screen.findByRole("option", { name: "受控推理 · 在线" });
  await user.selectOptions(select, "backend-1");
  expect(screen.getByRole("button", { name: "加载模型并试用" })).toBeEnabled();
  vi.mocked(mlBackendsApi.listAvailable).mockResolvedValue({
    items: [
      {
        enabled: true,
        backend: { id: "backend-1", name: "受控推理", auth_method: "token", state: "disconnected" },
      },
    ],
  } as never);
  await act(async () =>
    clients[clients.length - 1].refetchQueries({ queryKey: ["learning-backends"] }),
  );
  expect(
    await screen.findByText("已启用且配置认证的推理服务当前均不可用，请恢复服务后重试。"),
  ).toBeVisible();
  expect(screen.getByRole("button", { name: "加载模型并试用" })).toBeDisabled();
  expect(trainedModelsApi.deploy).not.toHaveBeenCalled();
});

it("preserves a removed service ID instead of visually selecting another live service", async () => {
  const user = userEvent.setup();
  mount("/projects/project-1/learning/models/model-1");
  const select = await screen.findByLabelText("用于加载模型的推理服务");
  await screen.findByRole("option", { name: "受控推理 · 在线" });
  await user.selectOptions(select, "backend-1");
  vi.mocked(mlBackendsApi.listAvailable).mockResolvedValue({
    items: [
      {
        enabled: true,
        backend: {
          id: "backend-2",
          name: "另一推理服务",
          auth_method: "token",
          state: "connected",
        },
      },
    ],
  } as never);
  await act(async () => {
    await clients[clients.length - 1]!.refetchQueries({ queryKey: ["learning-backends"] });
  });
  expect(select).toHaveValue("backend-1");
  await waitFor(() => expect(select).toHaveDisplayValue("已选推理服务（暂不可用）"));
  expect(screen.getByRole("button", { name: "加载模型并试用" })).toBeDisabled();
});

it.each(["super_admin", "project_admin", "employee"])(
  "gives %s the correct inference service setup links",
  async (role) => {
    useAuthStore.getState().setAuth("token-a", { id: "owner", role } as never);
    vi.mocked(mlBackendsApi.listAvailable).mockResolvedValue({ items: [] } as never);
    mount("/projects/project-1/learning/models/model-1");
    const summary = await screen.findByText("配置推理服务");
    await waitFor(() => expect(summary.closest("details")).toHaveAttribute("open"));
    expect(screen.getByText(/模型市场 → 注册管理 → 实例/)).toBeVisible();
    expect(screen.getByText(/项目设置 → ML 模型/)).toBeVisible();
    expect(screen.getByRole("link", { name: "去项目设置启用" })).toHaveAttribute(
      "href",
      "/projects/project-1/settings?section=ml-backends",
    );
    if (role === "employee") {
      expect(
        screen.queryByRole("link", { name: /去注册管理|查看已注册的推理服务/ }),
      ).not.toBeInTheDocument();
    } else {
      expect(
        screen.getByRole("link", {
          name: role === "super_admin" ? "去注册管理" : "查看已注册的推理服务",
        }),
      ).toHaveAttribute("href", "/model-market?tab=registry&registry_view=instances");
    }
    expect(trainedModelsApi.deploy).not.toHaveBeenCalled();
  },
);

it("isolates task release generations, resets pagination and ignores a late detection response", async () => {
  let resolveDetection!: (value: typeof releases) => void;
  const late = new Promise<typeof releases>((resolve) => {
    resolveDetection = resolve;
  });
  vi.mocked(trainedModelsApi.releases).mockImplementation((_project, _offset, task) =>
    task === "segment"
      ? Promise.resolve({
          ...releases,
          generation: 3,
          current: {
            id: "segment-release",
            model_version_id: "segment-model",
            deployment_id: "segment-deploy",
            generation: 3,
            task: "segment",
            snapshot: { registry_id: "backend-1" },
          },
        } as never)
      : late,
  );
  vi.mocked(trainedModelsApi.list).mockImplementation(
    (_project, _offset, _options, _archive, filters) =>
      Promise.resolve({
        total: 100,
        items: [
          {
            ...model,
            id: filters?.task === "segment" ? "segment-model" : model.id,
            name: filters?.task === "segment" ? "分割候选" : "检测候选",
            task: filters?.task,
          },
        ],
      }),
  );
  const user = userEvent.setup();
  mount("/projects/project-1/learning?tab=models&model_page=2");
  await screen.findByRole("link", { name: "检测候选" });
  const list = screen.getByRole("region", { name: "模型列表" });
  await user.selectOptions(within(list).getByLabelText("模型与发布任务"), "segment");
  await screen.findByRole("link", { name: "分割候选" });
  expect(screen.getByTestId("location")).toHaveTextContent("model_task=segment");
  expect(screen.getByTestId("location").textContent).not.toContain("model_page");
  expect(trainedModelsApi.list).toHaveBeenLastCalledWith(
    "project-1",
    0,
    expect.any(Object),
    "exclude",
    expect.objectContaining({ task: "segment" }),
    20,
  );
  expect(trainedModelsApi.releases).toHaveBeenLastCalledWith("project-1", 0, "segment", 20);
  const row = screen.getByRole("link", { name: "分割候选" }).closest("tr")!;
  expect(within(row).getByText("默认预标")).toBeInTheDocument();
  expect(within(row).getByText("实例分割")).toBeInTheDocument();
  await act(async () => resolveDetection({ ...releases, generation: 99 }));
  expect(within(row).getByText("默认预标")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "停用默认预标" }));
  await waitFor(() =>
    expect(trainedModelsApi.publish).toHaveBeenCalledWith(
      "project-1",
      expect.objectContaining({ task: "segment", expected_generation: 3, model_version_id: null }),
    ),
  );
});

it("loads the model's actual task for a direct detail URL before allowing publication", async () => {
  vi.mocked(trainedModelsApi.get).mockResolvedValue({
    model: { ...model, task: "segment" },
    deployments: [{ ...deployment, trial: { ...deployment.trial, sample_url: "/sample.png" } }],
  });
  const user = userEvent.setup();
  mount("/projects/project-1/learning/models/model-1");
  await screen.findByLabelText("模型版本详情");
  expect(trainedModelsApi.releases).toHaveBeenCalledWith("project-1", 0, "segment", 20);
  expect(trainedModelsApi.releases).not.toHaveBeenCalledWith("project-1", 0, "detect");
  fireEvent.load(screen.getByAltText("冻结样本与逐实例掩码"));
  await waitFor(() =>
    expect(screen.getByLabelText("已查看固定验证样本，确认用于后续人工试用")).toBeEnabled(),
  );
  await user.click(screen.getByLabelText("已查看固定验证样本，确认用于后续人工试用"));
  await user.click(screen.getByRole("button", { name: "设为默认预标模型" }));
  await waitFor(() =>
    expect(trainedModelsApi.publish).toHaveBeenCalledWith(
      "project-1",
      expect.objectContaining({ task: "segment", expected_generation: 7 }),
    ),
  );
});

it("uses the segmentation release alias and does not open unqualified segmentation registration", async () => {
  vi.mocked(trainedModelsApi.releases).mockResolvedValue({
    ...releases,
    current: {
      id: "release",
      model_version_id: model.id,
      deployment_id: deployment.id,
      generation: 7,
      task: "segment",
      snapshot: { registry_id: "backend-1" },
    },
  } as never);
  vi.mocked(trainingRunsApi.get).mockResolvedValue({
    run: { id: "run-1", task: "segment", config: { classes: ["car"] } },
    attempts: [{ id: "attempt-1", status: "succeeded", ordinal: 1 }],
    registration_eligibility: { qualified: false, reason: "该分割规格尚未资格验证" },
  } as never);
  const user = userEvent.setup();
  mount("/projects/project-1/learning?tab=models&model_task=segment");
  await user.click(await screen.findByRole("checkbox", { name: "T-1 · car.png" }));
  await user.click(screen.getByRole("button", { name: "用默认模型预标注" }));
  await waitFor(() =>
    expect(dataManagerTaskActionsApi.preannotate).toHaveBeenCalledWith(
      "project-1",
      expect.objectContaining({ model_id: "project-release:segment", task: "segment" }),
      expect.any(Object),
    ),
  );
  await act(async () =>
    navigate("/projects/project-1/learning?tab=models&model_task=segment&register_run=run-1"),
  );
  await screen.findByText("该分割规格尚未资格验证");
  expect(screen.getByRole("button", { name: "从训练结果添加模型" })).toBeDisabled();
  expect(screen.queryByLabelText("原生类别 0 · car")).not.toBeInTheDocument();
});

it.each(["segment", "all"])(
  "binds qualified segmentation runs only to region categories from %s",
  async (task) => {
    vi.mocked(trainingRunsApi.get).mockResolvedValue({
      run: { id: "run-1", task: "segment", config: { classes: ["car"] } },
      attempts: [{ id: "attempt-1", status: "succeeded", ordinal: 1 }],
      registration_eligibility: { qualified: true, reason: null },
    } as never);
    const user = userEvent.setup();
    mount(`/projects/project-1/learning?tab=models&model_task=${task}&register_run=run-1`, [], {
      detect: ["检测车辆"],
      segment: ["分割车辆"],
    });
    const binding = await screen.findByLabelText("原生类别 0 · car");
    expect(within(binding).queryByRole("option", { name: "检测车辆" })).not.toBeInTheDocument();
    await user.selectOptions(binding, "分割车辆");
    await user.click(screen.getByRole("button", { name: "从训练结果添加模型" }));
    await waitFor(() =>
      expect(trainedModelsApi.register).toHaveBeenCalledWith(
        "project-1",
        expect.objectContaining({ bindings: ["分割车辆"] }),
      ),
    );
  },
);

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { trainedModelsApi } from "@/api/trainedModels";
import { trainingRunsApi } from "@/api/trainingRuns";
import { mlBackendsApi } from "@/api/ml-backends";
import { tasksApi } from "@/api/tasks";
import { dataManagerTaskActionsApi } from "@/api/dataManagerActions";
import { useAuthStore } from "@/stores/authStore";
import { confirmDialog } from "@/components/ui/decisionDialog";
import { ProjectModelsPanel } from "./ProjectModelsPanel";

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
  return null;
}

function mount(entry = "/", classes = ["车辆", "行人"]) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[entry]}>
        <Navigation />
        <ProjectModelsPanel projectId="project-1" classes={classes} />
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
  it("registers all 80 bindings in original order even when the editor is filtered", async () => {
    const user = userEvent.setup();
    const classes = Array.from({ length: 80 }, (_, index) => `类别${index}`);
    vi.mocked(trainingRunsApi.get).mockResolvedValue({
      registration_eligibility: { qualified: true, reason: null },
      run: { id: "run-1", config: { framework: "ultralytics", classes } },
      attempts: [{ id: "attempt-1", ordinal: 1, status: "succeeded" }],
    } as never);
    mount("/", classes);
    await screen.findByRole("option", { name: "成功训练" });
    await user.selectOptions(screen.getByLabelText("来源训练实验"), "run-1");
    await user.selectOptions(await screen.findByLabelText("原生类别 0 · 类别0"), "类别1");
    const search = screen.getByRole("searchbox", { name: "搜索类别绑定" });
    await user.type(search, "类别79{Enter}");
    expect(trainedModelsApi.register).not.toHaveBeenCalled();
    await user.selectOptions(screen.getByLabelText("原生类别 79 · 类别79"), "类别0");
    await user.click(screen.getByRole("button", { name: "按同名绑定" }));
    await user.click(screen.getByRole("button", { name: "注册候选模型" }));
    await waitFor(() => expect(trainedModelsApi.register).toHaveBeenCalledOnce());
    expect(vi.mocked(trainedModelsApi.register).mock.calls[0][1].bindings).toEqual(
      classes.map((name, index) => (index === 0 ? "类别1" : index === 79 ? "类别0" : name)),
    );
  });
  it("starts with the source experiment and preserves edits to the timestamp name", async () => {
    const user = userEvent.setup();
    mount();
    const source = screen.getByLabelText("来源训练实验");
    const name = screen.getByLabelText("模型名称") as HTMLInputElement;
    expect(source.compareDocumentPosition(name) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(name.value).toMatch(/^\d{4}-\d{4}-\d{4} $/);
    await user.clear(name);
    await screen.findByRole("option", { name: "成功训练" });
    await user.selectOptions(source, "run-1");
    await screen.findByLabelText(/原生类别 0/);
    expect(name).toHaveValue("");
    expect(screen.getByRole("button", { name: "注册候选模型" })).toBeDisabled();
    expect(trainedModelsApi.register).not.toHaveBeenCalled();
  });
  it("keeps loading release and candidate states distinct from an empty model library", async () => {
    vi.mocked(trainedModelsApi.list).mockReturnValue(new Promise(() => {}));
    vi.mocked(trainedModelsApi.releases).mockReturnValue(new Promise(() => {}));
    mount();
    expect(screen.getByText("正在读取发布状态…")).toBeVisible();
    expect(screen.getByText("正在加载候选模型…")).toBeInTheDocument();
    expect(screen.queryByText("尚未注册模型版本")).not.toBeInTheDocument();
    expect(screen.queryByText("尚未发布受控模型")).not.toBeInTheDocument();
  });
  it("takes the empty candidate entry to the source experiment without creating a model", async () => {
    vi.mocked(trainedModelsApi.list).mockResolvedValue({ total: 0, items: [] });
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("button", { name: "填写候选信息" }));
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
      await user.type(screen.getByLabelText("模型名称"), "回流候选");
      await user.selectOptions(await screen.findByLabelText("来源训练实验"), "run-1");
      await screen.findByLabelText(/原生类别 0/);
      const button = screen.getByRole("button", { name: "注册候选模型" });
      expect(button).toBeDisabled();
      await user.selectOptions(screen.getByLabelText(/原生类别 0/), "车辆");
      expect(button).toBeDisabled();
      await user.selectOptions(screen.getByLabelText(/原生类别 1/), "行人");
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
    await user.click(await screen.findByRole("button", { name: "评估与部署" }));
    const detail = await screen.findByLabelText("模型版本详情");
    const publish = within(detail).getByRole("button", { name: "发布此模型" });
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

  it("retains the exact release request after a network error even if polling sees a newer generation", async () => {
    const user = userEvent.setup();
    vi.mocked(trainedModelsApi.publish)
      .mockRejectedValueOnce(new Error("发布响应丢失"))
      .mockResolvedValue({ id: "release-8" } as never);
    mount();
    await user.click(await screen.findByRole("button", { name: "评估与部署" }));
    const detail = await screen.findByLabelText("模型版本详情");
    await user.click(within(detail).getByLabelText("已查看固定验证样本，确认用于后续人工试用"));
    vi.mocked(trainedModelsApi.releases).mockResolvedValue({ ...releases, generation: 8 });
    await user.click(within(detail).getByRole("button", { name: "发布此模型" }));
    await screen.findByText("发布响应丢失");
    const first = vi.mocked(trainedModelsApi.publish).mock.calls[0][1];
    await user.click(within(detail).getByRole("button", { name: "发布此模型" }));
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
    await user.click(screen.getByRole("button", { name: "用当前发布模型预标注" }));
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
    await user.click((await screen.findAllByRole("button", { name: "评估与部署" }))[0]);
    await user.selectOptions(await screen.findByLabelText("对比另一候选"), "model-2");
    await screen.findByText(
      "验证基准、类别绑定或评估配置不同，不计算新旧提升。请分别查看各自报告。",
    );
    expect(screen.queryByText("同一验证范围")).not.toBeInTheDocument();
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
  await user.type(screen.getByLabelText("模型名称"), "Libre候选");
  await user.selectOptions(await screen.findByLabelText("来源训练实验"), "run-1");
  await screen.findByText(/该原生模型家族尚未通过资格检查/);
  expect(screen.getByRole("button", { name: "注册候选模型" })).toBeDisabled();
  expect(screen.queryByLabelText(/原生类别 0/)).not.toBeInTheDocument();
  expect(trainedModelsApi.register).not.toHaveBeenCalled();
});

it("loads a deep-linked archived candidate independently of the list page and preserves return context", async () => {
  vi.mocked(trainedModelsApi.list).mockResolvedValue({ items: [], total: 100 });
  vi.mocked(trainedModelsApi.get).mockResolvedValue({
    model: { ...model, archived: true },
    deployments: [],
  });
  mount("/projects/project-1/learning?tab=models&archive=only&model_page=2&model=model-1");
  expect(await screen.findByRole("heading", { name: "第一候选" })).toBeInTheDocument();
  expect(trainedModelsApi.list).toHaveBeenCalledWith("project-1", 40, undefined, "only");
  const link = screen.getByRole("link", { name: "谱系与决策" });
  expect(decodeURIComponent(link.getAttribute("href")!)).toContain(
    "returnTo=/projects/project-1/learning?tab=models&archive=only&model_page=2&model=model-1",
  );
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
  mount("/projects/project-1/learning?tab=models&model=model-1");
  await user.click(await screen.findByRole("checkbox", { name: /已查看.*样本/ }));
  await user.click(screen.getByRole("button", { name: "发布此模型" }));
  await waitFor(() => expect(confirmDialog).toHaveBeenCalled());
  await act(async () => navigate("?tab=models&archive=include&model=model-1"));
  await act(async () => navigate(-1));
  await act(async () => resolve(true));
  expect(trainedModelsApi.publish).not.toHaveBeenCalled();
});

it("loads the current release name when its candidate is outside the displayed page", async () => {
  vi.mocked(trainedModelsApi.list).mockResolvedValue({ items: [], total: 90 });
  vi.mocked(trainedModelsApi.releases).mockResolvedValue({
    ...releases,
    current: { model_version_id: "model-1", generation: 7, snapshot: {} } as never,
  });
  mount("/projects/project-1/learning?tab=models&model_page=2");
  expect(await screen.findByText("第一候选 · 发布代次 7")).toBeInTheDocument();
});

it("keeps registration disabled when the server has not supplied eligibility", async () => {
  const user = userEvent.setup();
  vi.mocked(trainingRunsApi.get).mockResolvedValue({
    run: { id: "run-1", config: { framework: "ultralytics", classes: ["car", "person"] } },
    attempts: [{ id: "attempt-1", ordinal: 1, status: "succeeded" }],
  } as never);
  mount();
  await user.type(screen.getByLabelText("模型名称"), "待核验候选");
  await user.selectOptions(await screen.findByLabelText("来源训练实验"), "run-1");
  await screen.findByText(/尚未取得模型回流资格/);
  expect(screen.getByRole("button", { name: "注册候选模型" })).toBeDisabled();
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
  await user.click((await screen.findAllByRole("button", { name: "评估与部署" }))[0]);
  expect(await screen.findByText(/原生报告未提供分类别指标/)).toBeInTheDocument();
  expect(screen.getByText("AR100")).toBeInTheDocument();
  await user.selectOptions(await screen.findByLabelText("对比另一候选"), "model-2");
  expect(await screen.findByText(/不计算新旧提升/)).toBeInTheDocument();
  expect(screen.queryByText("同一验证范围")).not.toBeInTheDocument();
});

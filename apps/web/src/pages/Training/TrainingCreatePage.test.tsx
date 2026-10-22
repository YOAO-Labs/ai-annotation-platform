import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { projectsApi } from "@/api/projects";
import { trainingDatasetsApi } from "@/api/trainingDatasets";
import { trainingRunsApi } from "@/api/trainingRuns";
import { trainedModelsApi } from "@/api/trainedModels";
import { trainingPreviewsApi } from "@/api/trainingPreviews";
import { trainingRecipesApi, type TrainingRecipeOut } from "@/api/trainingRecipes";
import type { TrainingParameterField, TrainingParameterSchema } from "@/api/generated/types.gen";
import { useAuthStore } from "@/stores/authStore";
import { TrainingCreatePage } from "./TrainingCreatePage";
import {
  emptyTrainingDraft,
  saveTrainingDraft,
  trainingDraftKey,
  trainingDraftOwner,
} from "./trainingDraftStorage";

vi.mock("@/hooks/useProjects", () => ({ useProject: () => ({ data: { name: "路口车辆" } }) }));
vi.mock("@/api/projects", () => ({ projectsApi: { getAccess: vi.fn() } }));
vi.mock("@/api/trainingDatasets", () => ({ trainingDatasetsApi: { list: vi.fn(), get: vi.fn() } }));
vi.mock("@/api/trainingRuns", () => ({
  trainingRunsApi: {
    catalog: vi.fn(),
    parameterSchema: vi.fn(),
    preflight: vi.fn(),
    create: vi.fn(),
    initializationSource: vi.fn(),
  },
}));
vi.mock("@/api/trainedModels", () => ({ trainedModelsApi: { list: vi.fn() } }));
vi.mock("./TrainingParameterInventory", () => ({ TrainingParameterInventory: () => null }));
vi.mock("@/api/trainingPreviews", () => ({
  trainingPreviewsApi: {
    capabilities: vi.fn(),
    create: vi.fn(),
    get: vi.fn(),
    cancel: vi.fn(),
    image: vi.fn(),
  },
}));
vi.mock("@/api/trainingRecipes", async (original) => ({
  ...(await original<typeof import("@/api/trainingRecipes")>()),
  trainingRecipesApi: {
    list: vi.fn(),
    get: vi.fn(),
    clone: vi.fn(),
    resolve: vi.fn(),
    revisions: vi.fn(),
    save: vi.fn(),
    archive: vi.fn(),
  },
}));

const field = (
  key: string,
  label: string,
  value: number | boolean,
  rules: Record<string, number | string>,
): TrainingParameterField => ({
  key,
  label,
  default: value,
  native_default: value,
  rules,
  status: "editable",
  group: "basic",
  aliases: [],
  native_type: typeof value === "boolean" ? "bool" : "number",
  defined_in: "native",
  reason: "",
});
const schema: TrainingParameterSchema = {
  adapter_revision: 2,
  framework: "ultralytics",
  framework_version: "8.4.177",
  model_family: "yolo11",
  task: "detect",
  schema_id: "schema-1",
  schema_digest: "a".repeat(64),
  source: "native",
  source_sha256: "b".repeat(64),
  groups: { basic: "基本训练" },
  fields: [
    field("epochs", "训练轮数", 50, { type: "integer", minimum: 1, maximum: 1000 }),
    field("batch", "批大小", 8, { type: "integer", minimum: 1, maximum: 64 }),
    field("lr0", "初始学习率", 0.01, { type: "number", exclusiveMinimum: 0, maximum: 0.1 }),
  ],
};
const dataset = {
  id: "dataset-1",
  project_id: "project-1",
  name: "审核数据",
  task: "detect" as const,
  contract_profile: {
    schema: 3,
    task: "detect" as const,
    media_type: "image",
    source_media_type: "image",
    format: "ultralytics-yolo",
    geometry: "normalized-bbox",
    bridge: "ultralytics-bbox-1",
  },
  status: "ready",
  package_sha256: "d".repeat(64),
  manifest: {},
};
const catalog = {
  enabled: true,
  online: true,
  busy: false,
  resource_id: "gpu",
  items: [
    {
      framework: "ultralytics" as const,
      version: "8.4.177",
      image_digest: `sha256:${"e".repeat(64)}`,
      base_models: [{ id: "yolo11n", filename: "yolo11n.pt", sha256: "f".repeat(64) }],
      capabilities: {
        tasks: ["detect"],
        resume: true,
        artifact_download: true,
        model_registration: true,
      },
    },
  ],
};
const report = {
  config_digest: "c".repeat(64),
  config: {
    framework: "ultralytics",
    dataset_id: dataset.id,
    configuration: {
      effective_values: { epochs: 50, batch: 8, lr0: 0.01 },
      origins: { epochs: "default", batch: "default", lr0: "default" },
      inactive_fields: {},
    },
  },
};
const clients: QueryClient[] = [];
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}
let navigate: ReturnType<typeof useNavigate>;
function Context() {
  const location = useLocation();
  navigate = useNavigate();
  return (
    <p data-testid="location">
      {location.pathname}
      {location.search}
    </p>
  );
}
function mount(entry = "/projects/project-1/learning/new") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route path="/projects/:id/learning/new" element={<TrainingCreatePage />} />
          <Route path="/projects/:id/learning" element={<p>实验列表</p>} />
        </Routes>
        <Context />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  useAuthStore.getState().setAuth("session-1", { id: "user-1" } as never);
  vi.mocked(projectsApi.getAccess).mockImplementation(
    async (id) =>
      ({ project_id: id, user_id: "user-1", capabilities: ["project.manage"] }) as never,
  );
  vi.mocked(trainingDatasetsApi.list).mockResolvedValue({ items: [dataset], total: 1 } as never);
  vi.mocked(trainingDatasetsApi.get).mockResolvedValue(dataset as never);
  vi.mocked(trainingRunsApi.catalog).mockResolvedValue(catalog);
  vi.mocked(trainingRunsApi.parameterSchema).mockResolvedValue(schema);
  vi.mocked(trainingRunsApi.preflight).mockResolvedValue(report);
  vi.mocked(trainingRunsApi.create).mockResolvedValue({ id: "run-1" } as never);
  vi.mocked(trainingPreviewsApi.capabilities).mockResolvedValue({
    qualified: true,
    online: true,
    busy: false,
    resource_id: "cpu",
    policy: {},
    policy_digest: "p",
    reason: null,
  });
  vi.mocked(trainingRecipesApi.list).mockResolvedValue({ total: 0, items: [] });
  vi.mocked(trainedModelsApi.list).mockResolvedValue({ total: 0, items: [] });
  vi.mocked(trainingRecipesApi.resolve).mockImplementation(
    async (_id, body) =>
      ({
        ...reuseSnapshot,
        configuration: {
          ...reuseSnapshot.configuration,
          user_values: body.configuration.values,
          effective_values: { epochs: 50, batch: 8, lr0: 0.01, ...body.configuration.values },
        },
      }) as never,
  );
});

const recipeId = "11111111-1111-4111-8111-111111111111";
const revisionId = "22222222-2222-4222-8222-222222222222";
const reuseSnapshot = {
  framework: "ultralytics" as const,
  framework_version: "8.4.177",
  image_digest: catalog.items[0].image_digest,
  base_model: catalog.items[0].base_models[0],
  configuration: {
    identity: { schema_id: schema.schema_id, schema_digest: schema.schema_digest },
    user_values: { batch: 4, lr0: 0.000001 },
    effective_values: { epochs: 50, batch: 4, lr0: 0.000001 },
  },
};
const recipe = {
  id: recipeId,
  project_id: "project-1",
  head_revision: 1,
  archived: false,
  compatible: true,
  incompatibility: null,
  source_available: true,
  revision: {
    id: revisionId,
    recipe_id: recipeId,
    project_id: "project-1",
    ordinal: 1,
    name: "轻量配方",
    description: "复用测试",
    snapshot: reuseSnapshot,
    snapshot_digest: "1".repeat(64),
    origin: null,
    creator_id: null,
    created_at: "2026-10-15T00:00:00Z",
  },
} satisfies TrainingRecipeOut;

describe("immutable recipe and clone reuse", () => {
  const key = trainingDraftKey(trainingDraftOwner("user-1", "project-1"));
  async function preview() {
    vi.mocked(trainingRecipesApi.list).mockResolvedValue({ total: 1, items: [recipe] });
    vi.mocked(trainingRecipesApi.get).mockResolvedValue(recipe);
    const result = await prepare(false);
    await result.user.click(screen.getByText(/项目训练配方/));
    await result.user.click(await screen.findByRole("button", { name: "核对修订 1" }));
    await screen.findByRole("dialog", { name: "核对复用配置" });
    return result;
  }
  it("cancels without changing the draft and applies all settings with a new request only after confirmation", async () => {
    const { user, view } = await preview();
    const before = localStorage.getItem(key)!;
    expect(screen.getByText("完整生效参数差异：2 项")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "取消复用" }));
    expect(localStorage.getItem(key)).toBe(before);
    await user.click(screen.getByRole("button", { name: "核对修订 1" }));
    await user.click(await screen.findByRole("button", { name: "确认采用配置" }));
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "核对复用配置" })).not.toBeInTheDocument(),
    );
    const after = JSON.parse(localStorage.getItem(key)!);
    expect(after.name).toBe("车辆实验");
    expect(after.datasetId).toBe("dataset-1");
    expect(after.requestId).not.toBe(JSON.parse(before).requestId);
    expect(after.source.reference.revision_id).toBe(revisionId);
    expect(after.fields.lr0.text).toBe("0.000001");
    view.unmount();
    mount();
    await user.click(await screen.findByRole("button", { name: /核对并开始/ }));
    await check();
    expect(vi.mocked(trainingRunsApi.preflight).mock.calls[0][1]).toMatchObject({
      origin: { kind: "recipe", recipe_id: recipeId, revision_id: revisionId },
      configuration: { values: reuseSnapshot.configuration.user_values },
    });
  });
  it("keeps the existing draft when the source is archived between preview and acceptance", async () => {
    const { user } = await preview();
    const before = localStorage.getItem(key);
    vi.mocked(trainingRecipesApi.get).mockResolvedValue({ ...recipe, archived: true });
    await user.click(screen.getByRole("button", { name: "确认采用配置" }));
    await waitFor(() =>
      expect(screen.getAllByText("配方已归档，不能新采用").length).toBeGreaterThan(0),
    );
    expect(localStorage.getItem(key)).toBe(before);
  });
  it("ignores a late source response after a project or session change", async () => {
    const { user } = await preview();
    const late = deferred<TrainingRecipeOut>();
    vi.mocked(trainingRecipesApi.get).mockReturnValue(late.promise);
    await user.click(screen.getByRole("button", { name: "确认采用配置" }));
    act(() => useAuthStore.getState().setAuth("session-2", { id: "user-1" } as never));
    const before = localStorage.getItem(key);
    await act(async () => late.resolve(recipe));
    expect(localStorage.getItem(key)).toBe(before);
    expect(screen.queryByText(/基于 轻量配方/)).not.toBeInTheDocument();
  });
  it("rejects an old URL clone response after the source changes on the same route", async () => {
    const late = deferred<Awaited<ReturnType<typeof trainingRecipesApi.clone>>>();
    vi.mocked(trainingRecipesApi.clone).mockImplementation((_project, run) =>
      run === "first" ? late.promise : Promise.reject(new Error("第二来源不存在")),
    );
    mount("/projects/project-1/learning/new?clone_run=first");
    await waitFor(() => expect(trainingRecipesApi.clone).toHaveBeenCalled());
    act(() => navigate("/projects/project-1/learning/new?clone_run=second"));
    await screen.findByText("第二来源不存在");
    await act(async () =>
      late.resolve({
        snapshot: reuseSnapshot,
        snapshot_digest: "2".repeat(64),
        label: "旧来源",
        origin: { kind: "clone", run_id: recipeId, digest: "3".repeat(64) },
        compatible: true,
        incompatibility: null,
      }),
    );
    expect(screen.queryByRole("dialog", { name: "核对复用配置" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("基础模型")).toHaveValue("");
  });
  it("loads the current URL clone after an obsolete in-flight preview releases its lock", async () => {
    const candidate = {
      snapshot: reuseSnapshot,
      snapshot_digest: "2".repeat(64),
      label: "旧来源",
      origin: { kind: "clone" as const, run_id: recipeId, digest: "3".repeat(64) },
      compatible: true,
      incompatibility: null,
    };
    const late = deferred<typeof candidate>();
    vi.mocked(trainingRecipesApi.clone).mockImplementation((_project, run) => {
      if (run === "first") return Promise.resolve(candidate);
      if (run === recipeId) return late.promise;
      return Promise.reject(new Error("第二来源不存在"));
    });
    mount("/projects/project-1/learning/new?clone_run=first");
    await waitFor(() =>
      expect(trainingRecipesApi.clone).toHaveBeenCalledWith("project-1", recipeId, undefined),
    );
    act(() => navigate("/projects/project-1/learning/new?clone_run=second"));
    expect(trainingRecipesApi.clone).not.toHaveBeenCalledWith("project-1", "second", undefined);
    await act(async () => late.resolve(candidate));
    await screen.findByText("第二来源不存在");
    expect(screen.queryByRole("dialog", { name: "核对复用配置" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("基础模型")).toHaveValue("");
  });
  it("allows explicit clone retry after editing invalidates a pending source response", async () => {
    const candidate = {
      snapshot: reuseSnapshot,
      snapshot_digest: "2".repeat(64),
      label: "克隆来源",
      origin: { kind: "clone" as const, run_id: recipeId, digest: "3".repeat(64) },
      compatible: true,
      incompatibility: null,
    };
    const late = deferred<typeof candidate>();
    vi.mocked(trainingRecipesApi.clone)
      .mockResolvedValue(candidate)
      .mockReturnValueOnce(late.promise);
    const user = userEvent.setup();
    mount("/projects/project-1/learning/new?clone_run=first");
    await waitFor(() => expect(trainingRecipesApi.clone).toHaveBeenCalledTimes(1));
    await user.type(screen.getByLabelText("实验名称"), "保留我的名称");
    await act(async () => late.resolve(candidate));
    expect(screen.queryByRole("dialog", { name: "核对复用配置" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "重新核对克隆来源" }));
    await screen.findByRole("dialog", { name: "核对复用配置" });
    await user.click(screen.getByRole("button", { name: "确认采用配置" }));
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "核对复用配置" })).not.toBeInTheDocument(),
    );
    expect(screen.getByLabelText("实验名称")).toHaveValue("保留我的名称");
    expect(screen.getByLabelText("基础模型")).toHaveValue("yolo11n");
  });
  it("rejects the first A response after URL intent changes A to B to A", async () => {
    const late = deferred<Awaited<ReturnType<typeof trainingRecipesApi.clone>>>();
    vi.mocked(trainingRecipesApi.clone)
      .mockRejectedValue(new Error("当前来源不可用"))
      .mockReturnValueOnce(late.promise);
    mount("/projects/project-1/learning/new?clone_run=first");
    await waitFor(() => expect(trainingRecipesApi.clone).toHaveBeenCalledTimes(1));
    act(() => navigate("/projects/project-1/learning/new?clone_run=second"));
    await screen.findByText("当前来源不可用");
    act(() => navigate("/projects/project-1/learning/new?clone_run=first"));
    await waitFor(() => expect(trainingRecipesApi.clone).toHaveBeenCalledTimes(3));
    await act(async () =>
      late.resolve({
        snapshot: reuseSnapshot,
        snapshot_digest: "2".repeat(64),
        origin: { kind: "clone", run_id: recipeId, digest: "3".repeat(64) },
        label: "过期的第一次 A",
        compatible: true,
        incompatibility: null,
      }),
    );
    expect(screen.queryByRole("dialog", { name: "核对复用配置" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("基础模型")).toHaveValue("");
  });
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
  vi.unstubAllGlobals();
  localStorage.clear();
});
async function prepare(review = true) {
  const user = userEvent.setup();
  const view = mount();
  await user.selectOptions(screen.getByLabelText("实验任务"), "detect");
  await user.clear(screen.getByLabelText("实验名称"));
  await user.type(screen.getByLabelText("实验名称"), "车辆实验");
  await user.selectOptions(await screen.findByLabelText("训练数据"), "dataset-1");
  await waitFor(() =>
    expect(screen.getByRole("option", { name: /^yolo11n ·/ })).toBeInTheDocument(),
  );
  await user.selectOptions(screen.getByLabelText("基础模型"), "yolo11n");
  await waitFor(() => expect(screen.getByRole("button", { name: "下一步" })).toBeEnabled());
  await user.click(screen.getByRole("button", { name: "下一步" }));
  if (review) await user.click(screen.getByRole("button", { name: "下一步" }));
  return { user, view };
}

it.each([true, false])(
  "blocks an unprepared current model and explains node readiness (online=%s)",
  async (online) => {
    vi.mocked(trainingRunsApi.catalog).mockResolvedValue({
      ...catalog,
      online,
      items: [
        {
          ...catalog.items[0],
          version: "8.4.183",
          base_models: [
            {
              ...catalog.items[0].base_models[0],
              compatible: online ? false : null,
              unavailable_reason: online ? "该节点尚未准备此基础模型权重" : "训练节点离线",
            },
          ],
        },
      ],
    });
    vi.mocked(trainingRunsApi.parameterSchema).mockResolvedValue({
      ...schema,
      framework_version: "8.4.183",
    });
    const user = userEvent.setup();
    mount();
    await user.selectOptions(screen.getByLabelText("实验任务"), "detect");
    await user.selectOptions(await screen.findByLabelText("训练数据"), "dataset-1");
    await screen.findByRole("option", { name: /^yolo11n ·/ });
    await user.selectOptions(screen.getByLabelText("基础模型"), "yolo11n");
    await screen.findByText(online ? "该节点尚未准备此基础模型权重" : "训练节点离线");
    expect(screen.getByRole("button", { name: "下一步" })).toBeDisabled();
    expect(trainingRunsApi.preflight).not.toHaveBeenCalled();
    if (!online) expect(screen.getByText(/须在线确认所选模型权重就绪/)).toBeVisible();
  },
);

it("persists scratch initialization and sends optional training evaluation in the frozen request", async () => {
  vi.mocked(trainingRunsApi.parameterSchema).mockResolvedValue({
    ...schema,
    fields: [
      ...schema.fields,
      field("pretrained", "使用预训练权重", true, { type: "boolean" }),
      field("evaluate_train", "逐轮评估训练集", false, { type: "boolean" }),
    ],
  });
  const { user, view } = await prepare(false);
  await user.click(screen.getByRole("button", { name: "上一步" }));
  await user.selectOptions(screen.getByLabelText("初始化权重"), "scratch");
  await user.click(screen.getByRole("button", { name: "下一步" }));
  expect(screen.getByLabelText("逐轮评估训练集")).not.toBeChecked();
  await user.click(screen.getByLabelText("逐轮评估训练集"));
  await user.click(screen.getByRole("button", { name: "下一步" }));
  await user.click(screen.getByRole("button", { name: "检查训练配置" }));
  await waitFor(() => expect(trainingRunsApi.preflight).toHaveBeenCalledTimes(1));
  expect(vi.mocked(trainingRunsApi.preflight).mock.calls[0][1].configuration?.values).toMatchObject(
    {
      pretrained: false,
      evaluate_train: true,
    },
  );
  view.unmount();
  mount();
  await waitFor(() => expect(screen.getByLabelText("初始化权重")).toHaveValue("scratch"));
});
async function check() {
  await userEvent.click(screen.getByRole("button", { name: "检查训练配置" }));
  await screen.findByText("训练检查通过 · 请核对最终配置");
}

it("selects the task first, filters compatible options and updates only generated names", async () => {
  const segmented = { ...dataset, id: "segment-data", name: "分割数据", task: "segment" as const };
  vi.mocked(trainingRunsApi.catalog).mockResolvedValue({
    ...catalog,
    items: [
      {
        ...catalog.items[0],
        base_models: [
          ...catalog.items[0].base_models,
          {
            id: "yolo11n-seg",
            filename: "yolo11n-seg.pt",
            sha256: "c".repeat(64),
            task: "segment",
          },
        ],
      },
      {
        ...catalog.items[0],
        framework: "libreyolo",
        base_models: [
          { id: "LibreYOLO9t", filename: "LibreYOLO9t.pt", sha256: "d".repeat(64), task: "detect" },
        ],
      },
    ],
  } as never);
  vi.mocked(trainingDatasetsApi.list).mockResolvedValue({
    items: [dataset, segmented],
    total: 2,
  } as never);
  vi.mocked(trainingDatasetsApi.get).mockImplementation(async (_p, id) =>
    id === segmented.id ? (segmented as never) : (dataset as never),
  );
  vi.mocked(trainingRunsApi.parameterSchema).mockImplementation(async (_p, _f, base) =>
    base.endsWith("-seg") ? { ...schema, task: "segment" } : schema,
  );
  const user = userEvent.setup();
  const view = mount();
  expect(screen.getByLabelText("实验任务")).toHaveValue("");
  expect(screen.getByLabelText("训练数据")).toBeDisabled();
  expect(screen.getByLabelText("基础模型")).toBeDisabled();
  await user.selectOptions(screen.getByLabelText("实验任务"), "segment");
  expect((screen.getByLabelText("实验名称") as HTMLInputElement).value).toMatch(
    /^segment-\d{8}-\d{6}$/,
  );
  await screen.findByRole("option", { name: /^分割数据/ });
  expect(screen.queryByRole("option", { name: /^审核数据/ })).not.toBeInTheDocument();
  expect(screen.queryByRole("option", { name: /^libreyolo/ })).not.toBeInTheDocument();
  expect(screen.queryByRole("option", { name: /^yolo11n ·/ })).not.toBeInTheDocument();
  expect(trainingDatasetsApi.list).toHaveBeenLastCalledWith(
    "project-1",
    0,
    "ready",
    expect.any(Object),
    { task: "segment" },
  );
  await user.selectOptions(screen.getByLabelText("训练数据"), segmented.id);
  await user.selectOptions(screen.getByLabelText("基础模型"), "yolo11n-seg");
  await waitFor(() => expect(screen.getByLabelText("基础模型")).toHaveValue("yolo11n-seg"));
  await user.selectOptions(screen.getByLabelText("实验任务"), "detect");
  expect((screen.getByLabelText("实验名称") as HTMLInputElement).value).toMatch(/^detect-/);
  expect(screen.getByLabelText("训练数据")).toHaveValue("");
  expect(screen.getByLabelText("基础模型")).toHaveValue("");
  await user.clear(screen.getByLabelText("实验名称"));
  await user.type(screen.getByLabelText("实验名称"), "自定义名称");
  await user.selectOptions(screen.getByLabelText("实验任务"), "segment");
  expect(screen.getByLabelText("实验名称")).toHaveValue("自定义名称");
  view.unmount();
  mount();
  expect(screen.getByLabelText("实验任务")).toHaveValue("segment");
  expect(screen.getByLabelText("实验名称")).toHaveValue("自定义名称");
});

describe("training creation modal", () => {
  it("preflights a training draft on plain HTTP without randomUUID", async () => {
    vi.stubGlobal("crypto", { getRandomValues: crypto.getRandomValues.bind(crypto) });
    await prepare();
    await check();
    expect(trainingRunsApi.preflight).toHaveBeenCalledOnce();
    const draft = JSON.parse(localStorage.getItem(trainingDraftKey("user-1:project-1"))!);
    expect(draft.requestId).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
  });

  it("distinguishes required dataset and weight prompts from selectable options", async () => {
    const user = userEvent.setup();
    mount();
    const select = (await screen.findByLabelText("训练数据")) as HTMLSelectElement;
    expect(select).toHaveClass("text-muted-foreground");
    expect(select.options[0]).toBeDisabled();
    expect(select.options[0].hidden).toBe(true);
    const weights = screen.getByLabelText("基础模型") as HTMLSelectElement;
    expect(weights).toHaveClass("text-muted-foreground");
    expect(weights.options[0]).toBeDisabled();
    expect(weights.options[0].hidden).toBe(true);
    await user.selectOptions(screen.getByLabelText("实验任务"), "detect");
    await user.type(screen.getByLabelText("实验名称"), "权重选择检查");
    await screen.findByRole("option", { name: /^审核数据/ });
    await user.selectOptions(select, "dataset-1");
    expect(select).not.toHaveClass("text-muted-foreground");
    expect(screen.getByRole("button", { name: "下一步" })).toBeDisabled();
    await user.selectOptions(weights, "yolo11n");
    expect(weights).not.toHaveClass("text-muted-foreground");
    await waitFor(() => expect(screen.getByRole("button", { name: "下一步" })).toBeEnabled());
  });

  it.each(["close button", "Escape"])(
    "restores the draft after dismissal by %s",
    async (method) => {
      const user = userEvent.setup();
      mount("/projects/project-1/learning/new?return_run=previous-run");
      const dialog = await screen.findByRole("dialog", { name: "新建训练实验" });
      expect(dialog).toBeVisible();
      await user.type(screen.getByLabelText("实验名称"), "保留草稿");
      if (method === "Escape") await user.keyboard("{Escape}");
      else await user.click(within(dialog).getByRole("button", { name: "关闭" }));
      await screen.findByText("实验列表");
      expect(screen.getByTestId("location")).toHaveTextContent(
        "/projects/project-1/learning?tab=training&run=previous-run",
      );
      expect(trainingRunsApi.create).not.toHaveBeenCalled();
      act(() => navigate("/projects/project-1/learning/new?return_run=previous-run"));
      await screen.findByRole("dialog", { name: "新建训练实验" });
      expect(screen.getByLabelText("实验名称")).toHaveValue("保留草稿");
    },
  );

  it("dismisses a nested YAML preview with Escape while retaining the experiment draft", async () => {
    const { user } = await prepare(false);
    await user.clear(screen.getByLabelText("批大小"));
    await user.type(screen.getByLabelText("批大小"), "4");
    const source = "lr0: 1e-6";
    const file = new File([source], "config.yaml");
    Object.defineProperty(file, "text", { value: () => Promise.resolve(source) });
    fireEvent.change(screen.getByLabelText("导入 YAML 文件"), { target: { files: [file] } });
    await screen.findByRole("dialog", { name: "核对 YAML 导入" });
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "核对 YAML 导入" })).not.toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "新建训练实验" })).toBeVisible();
    expect(screen.getByLabelText("批大小")).toHaveValue("4");
    await user.click(screen.getByRole("button", { name: /数据与模型/ }));
    expect(screen.getByLabelText("实验名称")).toHaveValue("车辆实验");
  });
});

describe("exact frozen-version experiment handoff", () => {
  const targetId = "44444444-4444-4444-8444-444444444444";
  const target = { ...dataset, id: targetId, name: "指定冻结版本" };
  const key = trainingDraftKey(trainingDraftOwner("user-1", "project-1"));
  const entry = `/projects/project-1/learning/new?dataset=${targetId}&tab=data&return_tab=data&return_dataset=${targetId}&data_task=detect`;
  const readTarget = () =>
    vi
      .mocked(trainingDatasetsApi.get)
      .mockImplementation(async (_project, id) => (id === targetId ? target : dataset) as never);

  it("confirms an exact off-page version before selecting its compatible defaults and returns to data context", async () => {
    readTarget();
    const user = userEvent.setup();
    mount(entry);
    const review = await screen.findByRole("dialog", { name: "核对训练数据版本" });
    expect(within(review).getByText("指定冻结版本")).toBeVisible();
    expect(localStorage.getItem(key)).toBeNull();
    expect(trainingRunsApi.create).not.toHaveBeenCalled();
    await user.click(within(review).getByRole("button", { name: "确认采用数据版本" }));
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "核对训练数据版本" })).not.toBeInTheDocument(),
    );
    expect(screen.getByLabelText("训练数据")).toHaveValue(targetId);
    expect(screen.getByLabelText("基础模型")).toHaveValue("yolo11n");
    expect(
      new URL(`http://test${screen.getByTestId("location").textContent}`).searchParams.has(
        "dataset",
      ),
    ).toBe(false);
    expect(JSON.parse(localStorage.getItem(key)!)).toMatchObject({
      datasetId: targetId,
      framework: "ultralytics",
      baseModel: "yolo11n",
      fields: {},
    });
    await user.click(
      within(screen.getByRole("dialog", { name: "新建训练实验" })).getByRole("button", {
        name: "关闭",
      }),
    );
    expect(screen.getByTestId("location")).toHaveTextContent(
      `/projects/project-1/learning?tab=data&dataset=${targetId}&data_task=detect`,
    );
  });

  it("keeps an edited draft unchanged until explicit acceptance and retains compatible parameters", async () => {
    readTarget();
    const { user } = await prepare(false);
    await user.clear(screen.getByLabelText("批大小"));
    await user.type(screen.getByLabelText("批大小"), "4");
    const before = localStorage.getItem(key)!;
    act(() => navigate(entry));
    await screen.findByRole("dialog", { name: "核对训练数据版本" });
    expect(localStorage.getItem(key)).toBe(before);
    await user.click(screen.getByRole("button", { name: "取消采用" }));
    expect(localStorage.getItem(key)).toBe(before);
    act(() => navigate(entry));
    await user.click(await screen.findByRole("button", { name: "确认采用数据版本" }));
    await waitFor(() => expect(JSON.parse(localStorage.getItem(key)!).datasetId).toBe(targetId));
    const after = JSON.parse(localStorage.getItem(key)!);
    expect(after.name).toBe("车辆实验");
    expect(after.fields.batch.text).toBe("4");
    expect(after.requestId).not.toBe(JSON.parse(before).requestId);
    expect(after.revision).toBe(JSON.parse(before).revision + 1);
  });

  it("shows the task-specific model and explicit reset before adopting segmentation", async () => {
    const segmented = {
      ...target,
      task: "segment",
      contract_profile: {
        ...target.contract_profile,
        task: "segment",
        geometry: "normalized-simple-polygon",
        bridge: "ultralytics-simple-polygon-1",
      },
    };
    vi.mocked(trainingDatasetsApi.get).mockImplementation(
      async (_project, id) => (id === targetId ? segmented : dataset) as never,
    );
    vi.mocked(trainingRunsApi.catalog).mockResolvedValue({
      ...catalog,
      items: [
        {
          ...catalog.items[0],
          base_models: [
            ...catalog.items[0].base_models,
            {
              id: "yolo11n-seg",
              filename: "yolo11n-seg.pt",
              sha256: "a".repeat(64),
              task: "segment",
            },
          ],
        },
      ],
    });
    vi.mocked(trainingRunsApi.parameterSchema).mockImplementation(
      async (_project, _framework, base) =>
        base === "yolo11n-seg" ? { ...schema, task: "segment", schema_id: "seg-schema" } : schema,
    );
    const { user } = await prepare(false);
    await user.clear(screen.getByLabelText("批大小"));
    await user.type(screen.getByLabelText("批大小"), "4");
    const before = localStorage.getItem(key);
    act(() => navigate(entry));
    const review = await screen.findByRole("dialog", { name: "核对训练数据版本" });
    expect(within(review).getByText(/yolo11n-seg/)).toBeVisible();
    expect(within(review).getByText(/移除原参数覆盖/)).toBeVisible();
    expect(localStorage.getItem(key)).toBe(before);
    await user.click(within(review).getByRole("button", { name: "确认采用数据版本" }));
    await waitFor(() =>
      expect(JSON.parse(localStorage.getItem(key)!).baseModel).toBe("yolo11n-seg"),
    );
    expect(JSON.parse(localStorage.getItem(key)!)).toMatchObject({
      name: "车辆实验",
      datasetId: targetId,
      fields: {},
      initialization: { kind: "catalog" },
    });
  });

  it.each(["pending", "pendingStudy"])(
    "preserves an unresolved %s request through handoff and modal dismissal",
    async (kind) => {
      vi.mocked(trainingRunsApi.create).mockRejectedValueOnce(new Error("连接中断"));
      const { user, view } = await prepare();
      await check();
      await user.click(screen.getByRole("button", { name: "开始训练" }));
      await screen.findByText("连接中断");
      if (kind === "pendingStudy") {
        const saved = JSON.parse(localStorage.getItem(key)!);
        saveTrainingDraft({
          ...saved,
          pending: undefined,
          pendingStudy: {
            base: saved.pending.body,
            grid: { batch: [4, 8] },
            objective: "metrics/mAP50-95",
            direction: "maximize",
            budgets: { max_trials: 2, max_planned_epochs: 100, max_native_seconds: 3600 },
            preview_digest: "a".repeat(64),
          },
        });
      }
      const before = localStorage.getItem(key);
      view.unmount();
      readTarget();
      mount(entry);
      await screen.findByText(/当前草稿有尚未确认的创建请求/);
      expect(screen.queryByRole("dialog", { name: "核对训练数据版本" })).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "重新核对数据版本" })).toBeDisabled();
      expect(localStorage.getItem(key)).toBe(before);
      const dialog = screen.getByRole("dialog", { name: "新建训练实验" });
      await user.click(within(dialog).getByRole("button", { name: "关闭" }));
      expect(localStorage.getItem(key)).toBe(before);
    },
  );

  it.each([{ project_id: "project-2" }, { status: "running" }, { package_sha256: null }])(
    "rejects an unqualified or foreign authoritative version %j",
    async (patch) => {
      vi.mocked(trainingDatasetsApi.get).mockResolvedValue({ ...target, ...patch } as never);
      mount(entry);
      await screen.findByRole("alert");
      expect(screen.queryByRole("dialog", { name: "核对训练数据版本" })).not.toBeInTheDocument();
      expect(localStorage.getItem(key)).toBeNull();
    },
  );

  it("retries a failed lookup and a failed acceptance without changing the draft", async () => {
    vi.mocked(trainingDatasetsApi.get)
      .mockResolvedValue(target as never)
      .mockRejectedValueOnce(new Error("数据读取失败"));
    const user = userEvent.setup();
    mount(entry);
    await screen.findByText("数据读取失败");
    await user.click(screen.getByRole("button", { name: "重新核对数据版本" }));
    await screen.findByRole("dialog", { name: "核对训练数据版本" });
    vi.mocked(trainingDatasetsApi.get).mockRejectedValueOnce(new Error("重新校验失败"));
    await user.click(screen.getByRole("button", { name: "确认采用数据版本" }));
    await screen.findByText("重新校验失败");
    expect(localStorage.getItem(key)).toBeNull();
    await user.click(screen.getByRole("button", { name: "确认采用数据版本" }));
    await waitFor(() => expect(JSON.parse(localStorage.getItem(key)!).datasetId).toBe(targetId));
  });

  const unqualifiedProfiles = [
    {
      name: "image output from video",
      profile: { ...target.contract_profile, source_media_type: "video" },
      reason: "当前继承仅资格验证图片来源与图片输出",
    },
    {
      name: "unknown source media",
      profile: { ...target.contract_profile, source_media_type: null },
      reason: "当前继承仅资格验证图片来源与图片输出",
    },
    {
      name: "missing contract profile",
      profile: null,
      reason: "冻结契约资料不完整",
    },
    {
      name: "unqualified bridge",
      profile: { ...target.contract_profile, bridge: "unqualified-bbox-bridge" },
      reason: "该几何或桥接契约尚未资格验证",
    },
  ];

  it.each(unqualifiedProfiles)(
    "rejects a direct dataset intent with $name without changing an edited draft",
    async ({ profile, reason }) => {
      const { user } = await prepare(false);
      await user.clear(screen.getByLabelText("批大小"));
      await user.type(screen.getByLabelText("批大小"), "4");
      const before = localStorage.getItem(key);
      vi.mocked(trainingDatasetsApi.get).mockResolvedValue({
        ...target,
        contract_profile: profile,
      } as never);
      act(() => navigate(entry));
      expect(await screen.findByText(`此冻结版本尚未获得训练资格：${reason}`)).toBeVisible();
      expect(screen.queryByRole("dialog", { name: "核对训练数据版本" })).not.toBeInTheDocument();
      expect(localStorage.getItem(key)).toBe(before);
      expect(trainingRunsApi.create).not.toHaveBeenCalled();
      expect(screen.getByTestId("location")).toHaveTextContent(`dataset=${targetId}`);
    },
  );

  it.each(unqualifiedProfiles)(
    "rejects adoption when qualification changes to $name after review",
    async ({ profile, reason }) => {
      readTarget();
      const { user } = await prepare(false);
      const before = localStorage.getItem(key);
      act(() => navigate(entry));
      const review = await screen.findByRole("dialog", { name: "核对训练数据版本" });
      vi.mocked(trainingDatasetsApi.get).mockResolvedValue({
        ...target,
        contract_profile: profile,
      } as never);
      await user.click(within(review).getByRole("button", { name: "确认采用数据版本" }));
      expect(await screen.findByText(`此冻结版本尚未获得训练资格：${reason}`)).toBeVisible();
      expect(screen.getByRole("dialog", { name: "核对训练数据版本" })).toBeVisible();
      expect(localStorage.getItem(key)).toBe(before);
      expect(trainingRunsApi.create).not.toHaveBeenCalled();
      expect(screen.getByTestId("location")).toHaveTextContent(`dataset=${targetId}`);
    },
  );

  it("revalidates ready state before adoption", async () => {
    readTarget();
    const user = userEvent.setup();
    mount(entry);
    await screen.findByRole("dialog", { name: "核对训练数据版本" });
    vi.mocked(trainingDatasetsApi.get).mockResolvedValue({ ...target, status: "failed" } as never);
    await user.click(screen.getByRole("button", { name: "确认采用数据版本" }));
    await screen.findByText("此冻结版本尚未就绪，不能用于训练");
    expect(localStorage.getItem(key)).toBeNull();
  });

  it("rejects the first response after dataset intent changes A to B to A", async () => {
    const late = deferred<Awaited<ReturnType<typeof trainingDatasetsApi.get>>>();
    vi.mocked(trainingDatasetsApi.get)
      .mockRejectedValue(new Error("当前版本不存在"))
      .mockReturnValueOnce(late.promise);
    mount(entry);
    await waitFor(() => expect(trainingDatasetsApi.get).toHaveBeenCalledTimes(1));
    act(() => navigate("/projects/project-1/learning/new?dataset=second"));
    await screen.findByText("当前版本不存在");
    act(() => navigate(entry));
    await waitFor(() => expect(trainingDatasetsApi.get).toHaveBeenCalledTimes(3));
    await act(async () => late.resolve(target as never));
    expect(screen.queryByRole("dialog", { name: "核对训练数据版本" })).not.toBeInTheDocument();
    expect(localStorage.getItem(key)).toBeNull();
  });

  it.each(["project", "session"])("ignores a late acceptance after changing %s", async (kind) => {
    readTarget();
    const user = userEvent.setup();
    mount(entry);
    await screen.findByRole("dialog", { name: "核对训练数据版本" });
    const late = deferred<Awaited<ReturnType<typeof trainingDatasetsApi.get>>>();
    vi.mocked(trainingDatasetsApi.get).mockReturnValueOnce(late.promise);
    await user.click(screen.getByRole("button", { name: "确认采用数据版本" }));
    await waitFor(() => expect(trainingDatasetsApi.get).toHaveBeenCalledTimes(2));
    if (kind === "project") act(() => navigate("/projects/project-2/learning/new"));
    else act(() => useAuthStore.getState().setAuth("session-2", { id: "user-1" } as never));
    const newOwner = trainingDraftOwner("user-1", kind === "project" ? "project-2" : "project-1");
    const newer = emptyTrainingDraft(newOwner);
    saveTrainingDraft(newer);
    await act(async () => late.resolve(target as never));
    expect(localStorage.getItem(trainingDraftKey(newOwner))).toContain(newer.draftId);
    expect(JSON.parse(localStorage.getItem(trainingDraftKey(newOwner))!).datasetId).toBe("");
  });

  it("keeps nested Escape scoped to the handoff and leaves the experiment draft intact", async () => {
    readTarget();
    const user = userEvent.setup();
    mount(entry);
    await screen.findByRole("dialog", { name: "核对训练数据版本" });
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "核对训练数据版本" })).not.toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "新建训练实验" })).toBeVisible();
    expect(localStorage.getItem(key)).toBeNull();
  });
});

it.each(["ultralytics-simple-polygon-1", "ultralytics-instance-rle-1"])(
  "filters segmentation weights for %s and preserves an incompatible selection until explicitly replaced",
  async (bridge) => {
    const task = "segment" as const;
    const segmented = {
      ...dataset,
      task,
      contract_profile: {
        ...dataset.contract_profile,
        task,
        schema: bridge === "ultralytics-instance-rle-1" ? 4 : 3,
        format: bridge === "ultralytics-instance-rle-1" ? "aap-instance-rle" : "ultralytics-yolo",
        geometry:
          bridge === "ultralytics-instance-rle-1" ? "instance-mask" : "normalized-simple-polygon",
        bridge,
      },
    };
    vi.mocked(trainingDatasetsApi.list).mockResolvedValue({
      items: [segmented],
      total: 1,
    } as never);
    vi.mocked(trainingDatasetsApi.get).mockResolvedValue(segmented as never);
    vi.mocked(trainingRunsApi.catalog).mockResolvedValue({
      ...catalog,
      items: [
        {
          ...catalog.items[0],
          base_models: [
            ...catalog.items[0].base_models,
            { id: "yolo11n-seg", filename: "yolo11n-seg.pt", sha256: "a".repeat(64), task },
          ],
        },
      ],
    });
    vi.mocked(trainingRunsApi.parameterSchema).mockImplementation(async (_p, _f, base) =>
      base === "yolo11n-seg" ? { ...schema, task, schema_id: "seg-2" } : schema,
    );
    const initial = {
      ...emptyTrainingDraft(trainingDraftOwner("user-1", "project-1")),
      datasetId: dataset.id,
      name: "分割实验",
      baseModel: "yolo11n",
      schemaId: schema.schema_id,
      schemaDigest: schema.schema_digest,
    };
    saveTrainingDraft(initial);
    mount();
    await screen.findByRole("alert");
    expect(screen.getByLabelText("基础模型")).toHaveValue("yolo11n");
    expect(screen.getByRole("button", { name: "下一步" })).toBeDisabled();
    expect(
      screen.getByRole("option", { name: /yolo11n（目录不可用或与数据任务不匹配）/ }),
    ).toBeDisabled();
    const candidateReads = vi.mocked(trainedModelsApi.list).mock.calls.length;
    await userEvent.selectOptions(screen.getByLabelText("基础模型"), "yolo11n-seg");
    await waitFor(() => expect(screen.getByRole("button", { name: "下一步" })).toBeEnabled());
    expect(screen.getByText(/实例分割支持已资格验证的训练桥接/)).toBeVisible();
    expect(screen.getByLabelText("初始化权重").querySelectorAll("option")).toHaveLength(1);
    expect(screen.queryByText(/尚未取得项目权重初始化资格/)).not.toBeInTheDocument();
    await waitFor(() => expect(trainedModelsApi.list).toHaveBeenCalledTimes(candidateReads + 1));
    expect(trainedModelsApi.list).toHaveBeenLastCalledWith(
      "project-1",
      0,
      expect.any(Object),
      "include",
      { task: "segment", framework: "ultralytics" },
    );
    expect(trainingRunsApi.preflight).not.toHaveBeenCalled();
  },
);

const initializationId = "99999999-9999-4999-8999-999999999999";
const initializationPin = {
  kind: "trained_model",
  model_version_id: initializationId,
  model_name: "项目初始模型",
  source_digest: "9".repeat(64),
  base_model: "yolo11n",
  classes: ["car"],
  dataset_id: "source-data",
  validation_digest: "7".repeat(64),
  run_id: "source-run",
  attempt_id: "source-attempt",
  artifact: { sha256: "8".repeat(64), size_bytes: 1234 },
};

it.each([true, false])(
  "keeps the initialization summary readable when the source pin is available: %s",
  async (eligible) => {
    vi.mocked(trainedModelsApi.list).mockResolvedValue({
      total: 1,
      items: [{ id: initializationId, name: "项目初始模型", framework: "ultralytics" }],
    } as never);
    vi.mocked(trainingRunsApi.initializationSource).mockResolvedValue({
      model_version_id: initializationId,
      eligible,
      reasons: eligible ? [] : ["来源信息不完整"],
      pin: eligible ? initializationPin : null,
    } as never);
    const user = userEvent.setup();
    const writeText = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue(undefined);
    mount();
    await user.selectOptions(screen.getByLabelText("实验任务"), "detect");
    await user.selectOptions(await screen.findByLabelText("训练数据"), "dataset-1");
    await screen.findByRole("option", { name: /^yolo11n ·/ });
    await user.selectOptions(screen.getByLabelText("基础模型"), "yolo11n");
    await screen.findByRole("option", { name: "项目初始模型 · ultralytics" });
    await user.selectOptions(screen.getByLabelText("初始化权重"), initializationId);
    const summary = screen.getByRole("complementary", { name: "实验摘要" });
    expect(
      await within(summary).findByText(eligible ? "项目初始模型" : "所选候选模型"),
    ).toBeVisible();
    expect(summary).not.toHaveTextContent(initializationId);
    await user.click(within(summary).getByRole("button", { name: "复制初始化模型版本 ID" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(initializationId));
    expect(trainingRunsApi.create).not.toHaveBeenCalled();
    expect(trainingRunsApi.preflight).not.toHaveBeenCalled();
  },
);

it("pins an explicitly selected candidate, invalidates review and retries its exact source after refresh", async () => {
  vi.mocked(trainedModelsApi.list).mockResolvedValue({
    total: 1,
    items: [{ id: initializationId, name: "项目初始模型", framework: "ultralytics" }],
  } as never);
  vi.mocked(trainingRunsApi.initializationSource).mockResolvedValue({
    model_version_id: initializationId,
    eligible: true,
    reasons: [],
    pin: initializationPin,
  } as never);
  const { user, view } = await prepare();
  await check();
  const old = vi.mocked(trainingRunsApi.preflight).mock.calls[0][1];
  await user.click(screen.getByRole("button", { name: /数据与模型/ }));
  await user.selectOptions(screen.getByLabelText("初始化权重"), initializationId);
  const key = trainingDraftKey(trainingDraftOwner("user-1", "project-1"));
  await waitFor(() =>
    expect(JSON.parse(localStorage.getItem(key)!).initialization?.source_digest).toBe(
      initializationPin.source_digest,
    ),
  );
  await user.click(screen.getByRole("button", { name: /核对并开始/ }));
  expect(screen.getByRole("button", { name: "开始训练" })).toBeDisabled();
  vi.mocked(trainingRunsApi.preflight).mockResolvedValue({
    ...report,
    config: { ...report.config, initialization: initializationPin },
  });
  await check();
  const next = vi.mocked(trainingRunsApi.preflight).mock.calls[1][1];
  expect(next.request_id).not.toBe(old.request_id);
  expect(next.initialization).toEqual({
    kind: "trained_model",
    model_version_id: initializationId,
    source_digest: initializationPin.source_digest,
  });
  const writeText = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue(undefined);
  const frozen = screen.getByRole("region", { name: "冻结的初始化权重" });
  expect(frozen).not.toHaveTextContent(initializationPin.artifact.sha256);
  await user.click(within(frozen).getByRole("button", { name: "复制初始化权重哈希" }));
  await waitFor(() => expect(writeText).toHaveBeenCalledWith(initializationPin.artifact.sha256));
  vi.mocked(trainingRunsApi.create).mockRejectedValueOnce(new Error("初始化创建响应丢失"));
  await user.click(screen.getByRole("button", { name: "开始训练" }));
  await screen.findByText("初始化创建响应丢失");
  const accepted = vi.mocked(trainingRunsApi.create).mock.calls[0][1];
  view.unmount();
  vi.mocked(trainingRunsApi.initializationSource).mockRejectedValue(new Error("原来源已离线"));
  mount();
  await user.click(await screen.findByRole("button", { name: "重试并确认创建" }));
  await screen.findByText("实验列表");
  expect(vi.mocked(trainingRunsApi.create).mock.calls[1][1]).toEqual(accepted);
});

it("discards an old preflight after authentication changes A to B to A in one render batch", async () => {
  const result = deferred<typeof report>();
  vi.mocked(trainingRunsApi.preflight).mockReturnValue(result.promise);
  const { user } = await prepare();
  await user.click(screen.getByRole("button", { name: "检查训练配置" }));
  await waitFor(() => expect(trainingRunsApi.preflight).toHaveBeenCalled());
  act(() => {
    useAuthStore.getState().setAuth("session-other", { id: "user-other" } as never);
    useAuthStore.getState().setAuth("session-1", { id: "user-1" } as never);
  });
  await act(async () => result.resolve(report));
  expect(screen.queryByText("训练检查通过 · 请核对最终配置")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: /数据与模型/ })).toHaveAttribute(
    "aria-current",
    "step",
  );
});

describe("training creation ownership", () => {
  it("keeps preview controls and failures out of the persisted draft and training requests", async () => {
    vi.mocked(trainingPreviewsApi.create).mockRejectedValue(new Error("preview unavailable"));
    const { user } = await prepare(false);
    const key = trainingDraftKey(trainingDraftOwner("user-1", "project-1"));
    const before = localStorage.getItem(key);
    await user.clear(screen.getByLabelText("预览种子"));
    await user.type(screen.getByLabelText("预览种子"), "97");
    await user.click(screen.getByRole("button", { name: "生成原生预览" }));
    await screen.findByText("preview unavailable");
    expect(localStorage.getItem(key)).toBe(before);
    expect(trainingRunsApi.preflight).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "下一步" }));
    await check();
    expect(vi.mocked(trainingRunsApi.preflight).mock.calls[0][1]).not.toHaveProperty("seed");
    await user.click(screen.getByRole("button", { name: "开始训练" }));
    await screen.findByText("实验列表");
    expect(vi.mocked(trainingRunsApi.create).mock.calls[0][1]).not.toHaveProperty("preview");
  });
  it("invalidates review and rotates the request when an execution input changes", async () => {
    const { user } = await prepare();
    await check();
    expect(screen.getByRole("button", { name: "开始训练" })).toBeEnabled();
    const first = vi.mocked(trainingRunsApi.preflight).mock.calls[0][1];
    await user.click(screen.getByRole("button", { name: "上一步" }));
    await user.clear(screen.getByLabelText("批大小"));
    await user.type(screen.getByLabelText("批大小"), "4");
    await user.click(screen.getByRole("button", { name: "下一步" }));
    expect(screen.getByRole("button", { name: "开始训练" })).toBeDisabled();
    await check();
    const next = vi.mocked(trainingRunsApi.preflight).mock.calls[1][1];
    expect(next.request_id).not.toBe(first.request_id);
    expect(next.parameters).toBeUndefined();
    expect(next.configuration?.values).toEqual({ batch: 4 });
  });
  it("restores a lost creation response after refresh and retries the exact payload even with a changed catalog", async () => {
    vi.mocked(trainingRunsApi.create).mockRejectedValueOnce(new Error("连接中断"));
    const { user, view } = await prepare();
    await check();
    await user.click(screen.getByRole("button", { name: "开始训练" }));
    await screen.findByText("连接中断");
    const first = vi.mocked(trainingRunsApi.create).mock.calls[0][1];
    view.unmount();
    vi.mocked(trainingRunsApi.catalog).mockResolvedValue({ ...catalog, items: [] });
    mount();
    await user.click(await screen.findByRole("button", { name: "重试并确认创建" }));
    await waitFor(() => expect(trainingRunsApi.create).toHaveBeenCalledTimes(2));
    expect(vi.mocked(trainingRunsApi.create).mock.calls[1][1]).toEqual(first);
    expect(first.preflight_digest).toBe(report.config_digest);
    expect(await screen.findByText("实验列表")).toBeVisible();
    expect(
      localStorage.getItem(trainingDraftKey(trainingDraftOwner("user-1", "project-1"))),
    ).toBeNull();
  });
  it.each(["success", "error"])("ignores a late preflight %s after editing", async (outcome) => {
    const late = deferred<typeof report>();
    vi.mocked(trainingRunsApi.preflight).mockReturnValue(late.promise);
    const { user } = await prepare();
    await user.click(screen.getByRole("button", { name: "检查训练配置" }));
    await waitFor(() => expect(trainingRunsApi.preflight).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole("button", { name: "上一步" }));
    await user.clear(screen.getByLabelText("批大小"));
    await user.type(screen.getByLabelText("批大小"), "4");
    await act(async () => {
      if (outcome === "success") late.resolve(report);
      else late.reject(new Error("旧检查错误"));
    });
    await user.click(screen.getByRole("button", { name: "下一步" }));
    expect(screen.queryByText("旧检查错误")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "开始训练" })).toBeDisabled();
  });
  it.each(["project", "session"])(
    "does not navigate or remove a new owner draft after a late create under changed %s",
    async (context) => {
      const late = deferred<Awaited<ReturnType<typeof trainingRunsApi.create>>>();
      vi.mocked(trainingRunsApi.create).mockReturnValue(late.promise);
      const { user } = await prepare();
      await check();
      await user.click(screen.getByRole("button", { name: "开始训练" }));
      await waitFor(() => expect(trainingRunsApi.create).toHaveBeenCalledTimes(1));
      if (context === "project") act(() => navigate("/projects/project-2/learning/new"));
      else act(() => useAuthStore.getState().setAuth("session-2", { id: "user-1" } as never));
      const owner = trainingDraftOwner("user-1", context === "project" ? "project-2" : "project-1");
      const newer = emptyTrainingDraft(owner);
      saveTrainingDraft(newer);
      await act(async () => late.resolve({ id: "old-run" } as never));
      expect(screen.getByTestId("location")).toHaveTextContent(
        `/projects/${context === "project" ? "project-2" : "project-1"}/learning/new`,
      );
      expect(localStorage.getItem(trainingDraftKey(owner))).toContain(newer.draftId);
    },
  );
  it("checks current authority before preflight and create", async () => {
    const { user } = await prepare();
    await check();
    vi.mocked(projectsApi.getAccess).mockResolvedValue({
      project_id: "project-1",
      user_id: "user-1",
      capabilities: [],
    } as never);
    await user.click(screen.getByRole("button", { name: "开始训练" }));
    await screen.findByText("当前账号已失去此项目的管理权限");
    expect(trainingRunsApi.create).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "检查训练配置" }));
    await waitFor(() => expect(projectsApi.getAccess).toHaveBeenCalledTimes(3));
    expect(trainingRunsApi.preflight).toHaveBeenCalledTimes(1);
  });
  it.each(["resource", "filename", "schema"])(
    "retires review when catalog %s changes",
    async (kind) => {
      await prepare();
      await check();
      const item = catalog.items[0];
      const updated =
        kind === "resource"
          ? { ...catalog, resource_id: "new-gpu" }
          : {
              ...catalog,
              items: [
                {
                  ...item,
                  ...(kind === "filename"
                    ? { base_models: [{ ...item.base_models[0], filename: "new-base.pt" }] }
                    : {
                        parameter_schemas: [
                          {
                            ...schema,
                            schema_digest: "7".repeat(64),
                            field_count: 3,
                            editable_count: 3,
                          },
                        ],
                      }),
                },
              ],
            };
      act(() =>
        clients[clients.length - 1].setQueriesData(
          { queryKey: ["training-catalog", "project-1", "user-1"] },
          updated,
        ),
      );
      await waitFor(() => expect(screen.getByRole("button", { name: "开始训练" })).toBeDisabled());
      expect(screen.queryByText("训练检查通过 · 请核对最终配置")).not.toBeInTheDocument();
      expect(trainingRunsApi.create).not.toHaveBeenCalled();
    },
  );
});

describe("configuration import and recovery", () => {
  it("keeps name/data and requires acceptance before dropping incompatible framework overrides", async () => {
    const optimizer: TrainingParameterField = {
      ...schema.fields[0],
      key: "optimizer",
      label: "优化器",
      default: "SGD",
      native_default: "auto",
      rules: { enum: ["SGD", "AdamW"] },
    };
    const ultra = { ...schema, fields: [...schema.fields, optimizer] };
    const libre = {
      ...schema,
      framework: "libreyolo",
      framework_version: "1.6.0",
      model_family: "yolo9",
      schema_id: "libre-schema",
      schema_digest: "6".repeat(64),
      fields: [
        ...schema.fields,
        { ...optimizer, default: "sgd", rules: { enum: ["sgd", "adamw"] } },
      ],
    };
    vi.mocked(trainingRunsApi.parameterSchema).mockImplementation(async (_project, framework) =>
      framework === "libreyolo" ? libre : ultra,
    );
    vi.mocked(trainingRunsApi.catalog).mockResolvedValue({
      ...catalog,
      items: [
        ...catalog.items,
        {
          ...catalog.items[0],
          framework: "libreyolo",
          version: "1.6.0",
          base_models: [{ id: "LibreYOLO9t", sha256: "5".repeat(64), filename: "LibreYOLO9t.pt" }],
        },
      ],
    });
    const { user } = await prepare(false);
    await user.selectOptions(screen.getByLabelText("优化器"), "AdamW");
    await user.clear(screen.getByLabelText("初始学习率"));
    await user.type(screen.getByLabelText("初始学习率"), "1e-6");
    await user.click(screen.getByRole("button", { name: /数据与模型/ }));
    await user.selectOptions(screen.getByLabelText("训练器"), "libreyolo");
    expect(screen.getByLabelText("基础模型")).toHaveValue("");
    await user.selectOptions(screen.getByLabelText("基础模型"), "LibreYOLO9t");
    await screen.findByRole("dialog", { name: "核对参数切换" });
    expect(screen.getByText("移除 1 项：optimizer")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "接受切换" }));
    expect(screen.getByLabelText("实验名称")).toHaveValue("车辆实验");
    expect(screen.getByLabelText("训练数据")).toHaveValue(dataset.id);
    await user.click(screen.getByRole("button", { name: "下一步" }));
    expect(screen.getByLabelText("优化器")).toHaveValue("sgd");
    expect(screen.getByLabelText("初始学习率")).toHaveValue("1e-6");
  });
  it("previews replacement, rejects partial invalid imports, and applies a valid import atomically", async () => {
    const { user } = await prepare(false);
    await user.clear(screen.getByLabelText("批大小"));
    await user.type(screen.getByLabelText("批大小"), "4");
    const upload = (source: string) => {
      const file = new File([source], "config.yaml", { type: "application/yaml" });
      Object.defineProperty(file, "text", { value: () => Promise.resolve(source) });
      fireEvent.change(screen.getByLabelText("导入 YAML 文件"), { target: { files: [file] } });
    };
    upload("lr0: 1e-6\nepochs: 0");
    await screen.findByRole("dialog", { name: "核对 YAML 导入" });
    expect(screen.getByRole("button", { name: "应用导入" })).toBeDisabled();
    expect(screen.getByLabelText("批大小")).toHaveValue("4");
    await user.click(screen.getByRole("button", { name: "取消" }));
    upload("lr0: 1e-6");
    await screen.findByRole("dialog", { name: "核对 YAML 导入" });
    expect(screen.getByText(/4 → 8/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "应用导入" }));
    expect(screen.getByLabelText("批大小")).toHaveValue("8");
    expect(screen.getByLabelText("初始学习率")).toHaveValue("0.000001");
  });
  it("ignores a file read that finishes after its draft changes", async () => {
    const { user } = await prepare(false);
    const late = deferred<string>();
    const file = new File(["lr0: 0.02"], "config.yaml");
    Object.defineProperty(file, "text", { value: () => late.promise });
    fireEvent.change(screen.getByLabelText("导入 YAML 文件"), { target: { files: [file] } });
    await user.clear(screen.getByLabelText("批大小"));
    await user.type(screen.getByLabelText("批大小"), "4");
    await act(async () => late.resolve("lr0: 0.02"));
    expect(screen.queryByRole("dialog", { name: "核对 YAML 导入" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("批大小")).toHaveValue("4");
  });
  it("blocks an import preview when the current schema changes after validation", async () => {
    const { user } = await prepare(false);
    const source = "lr0: 1e-6";
    const file = new File([source], "config.yaml");
    Object.defineProperty(file, "text", { value: () => Promise.resolve(source) });
    fireEvent.change(screen.getByLabelText("导入 YAML 文件"), { target: { files: [file] } });
    await screen.findByRole("dialog", { name: "核对 YAML 导入" });
    const key = trainingDraftKey(trainingDraftOwner("user-1", "project-1"));
    const original = localStorage.getItem(key);
    act(() =>
      clients[clients.length - 1].setQueriesData(
        { queryKey: ["training-parameter-schema", "project-1", "user-1"] },
        { ...schema, schema_digest: "8".repeat(64) },
      ),
    );
    await waitFor(() => expect(screen.getByRole("button", { name: "应用导入" })).toBeDisabled());
    await user.click(screen.getByRole("button", { name: "应用导入" }));
    expect(localStorage.getItem(key)).toBe(original);
  });
  it("restores a selected data version outside the first page and offers older pages", async () => {
    saveTrainingDraft({
      ...emptyTrainingDraft(trainingDraftOwner("user-1", "project-1")),
      name: "恢复实验",
      datasetId: dataset.id,
      baseModel: "yolo11n",
      schemaId: schema.schema_id,
      schemaDigest: schema.schema_digest,
    });
    vi.mocked(trainingDatasetsApi.list).mockResolvedValue({ items: [], total: 21 });
    const user = userEvent.setup();
    mount();
    await waitFor(() => expect(screen.getByRole("button", { name: "下一步" })).toBeEnabled());
    expect(screen.getByLabelText("训练数据")).toHaveValue(dataset.id);
    await user.click(screen.getByRole("button", { name: "加载更早的数据版本" }));
    await waitFor(() =>
      expect(trainingDatasetsApi.list).toHaveBeenCalledWith(
        "project-1",
        20,
        "ready",
        expect.any(Object),
        { task: "detect" },
      ),
    );
  });
  it("requires explicit migration acceptance and retains only compatible fields", async () => {
    const owner = trainingDraftOwner("user-1", "project-1");
    saveTrainingDraft({
      ...emptyTrainingDraft(owner),
      name: "恢复实验",
      datasetId: dataset.id,
      baseModel: "yolo11n",
      schemaId: "old-schema",
      schemaDigest: "9".repeat(64),
      fields: { lr0: { kind: "scalar", text: "1e-6" }, removed: { kind: "scalar", text: "2" } },
    });
    const user = userEvent.setup();
    mount();
    expect(screen.getByRole("button", { name: "下一步" })).toBeDisabled();
    await user.click(await screen.findByRole("button", { name: "核对并迁移草稿" }));
    await screen.findByRole("dialog", { name: "核对参数切换" });
    expect(screen.getByText("移除 1 项：removed")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "接受切换" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "下一步" })).toBeEnabled());
    expect(localStorage.getItem(trainingDraftKey(owner))).not.toContain('"removed"');
    expect(localStorage.getItem(trainingDraftKey(owner))).toContain("1e-6");
  });
});

it("filters catalog models by task, family and native scale and restores the selected family", async () => {
  const base = (
    id: string,
    family: string,
    size: string,
    task: "detect" | "segment" = "detect",
  ) => ({
    id,
    filename: `${id}.pt`,
    sha256: "f".repeat(64),
    model_family: family,
    model_size: size,
    task,
  });
  vi.mocked(trainingRunsApi.catalog).mockResolvedValue({
    ...catalog,
    items: [
      {
        ...catalog.items[0],
        version: "8.4.183",
        base_models: [
          base("yolo11n", "yolo11", "n"),
          base("yolov8n", "yolov8", "n"),
          base("yolov8x", "yolov8", "x"),
          base("yolo26s", "yolo26", "s"),
          base("yolo26m-seg", "yolo26", "m", "segment"),
        ],
      },
    ],
  });
  vi.mocked(trainingRunsApi.parameterSchema).mockImplementation(
    async (_project, _framework, id) => ({
      ...schema,
      framework_version: "8.4.183",
      schema_id: `schema-${id}`,
      model_family: id.startsWith("yolov8")
        ? "yolov8"
        : id.startsWith("yolo26")
          ? "yolo26"
          : "yolo11",
      task: id.endsWith("-seg") ? "segment" : "detect",
    }),
  );
  const user = userEvent.setup();
  const view = mount();
  expect(screen.getByLabelText("模型家族")).toBeDisabled();
  await user.selectOptions(screen.getByLabelText("实验任务"), "detect");
  await screen.findByRole("option", { name: "YOLOv8" });
  await user.selectOptions(screen.getByLabelText("模型家族"), "yolov8");
  const models = screen.getByLabelText("基础模型");
  expect(within(models).getByRole("option", { name: /^yolov8n · Nano/ })).toBeVisible();
  expect(within(models).getByRole("option", { name: /^yolov8x · Extra large/ })).toBeVisible();
  expect(within(models).queryByRole("option", { name: /^yolo11/ })).not.toBeInTheDocument();
  expect(within(models).queryByRole("option", { name: /^yolo26/ })).not.toBeInTheDocument();
  await user.selectOptions(models, "yolov8x");
  await waitFor(() => expect(models).toHaveValue("yolov8x"));
  view.unmount();
  mount();
  await waitFor(() => expect(screen.getByLabelText("模型家族")).toHaveValue("yolov8"));
  expect(screen.getByLabelText("基础模型")).toHaveValue("yolov8x");
  await user.selectOptions(screen.getByLabelText("模型家族"), "yolo26");
  expect(screen.getByLabelText("基础模型")).toHaveValue("");
  expect(
    within(screen.getByLabelText("基础模型")).getByRole("option", { name: /^yolo26s · Small/ }),
  ).toBeVisible();
  const stored = JSON.parse(localStorage.getItem(trainingDraftKey("user-1:project-1"))!);
  expect(stored.baseModel).toBe("");
  expect(stored.initialization).toBeUndefined();
  await user.selectOptions(screen.getByLabelText("实验任务"), "segment");
  expect(screen.getByLabelText("模型家族")).toHaveValue("yolo26");
  expect(screen.queryByRole("option", { name: "YOLOv8" })).not.toBeInTheDocument();
  expect(
    within(screen.getByLabelText("基础模型")).getByRole("option", {
      name: /^yolo26m-seg · Medium/,
    }),
  ).toBeVisible();
});

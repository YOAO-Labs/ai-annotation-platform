import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { projectsApi } from "@/api/projects";
import { trainingDatasetsApi } from "@/api/trainingDatasets";
import { trainingRunsApi } from "@/api/trainingRuns";
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
  },
}));
vi.mock("./TrainingParameterInventory", () => ({ TrainingParameterInventory: () => null }));

const field = (
  key: string,
  label: string,
  value: number,
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
  native_type: "number",
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
        task_types: ["image-detection"],
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
function Context() {
  const location = useLocation();
  const navigate = useNavigate();
  return (
    <>
      <p data-testid="location">
        {location.pathname}
        {location.search}
      </p>
      <button onClick={() => navigate("/projects/project-2/learning/new")}>切换项目</button>
    </>
  );
}
function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/projects/project-1/learning/new"]}>
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
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
  localStorage.clear();
});
async function prepare(review = true) {
  const user = userEvent.setup();
  const view = mount();
  await user.type(screen.getByLabelText("实验名称"), "车辆实验");
  await user.selectOptions(await screen.findByLabelText("训练数据"), "dataset-1");
  await waitFor(() => expect(screen.getByRole("option", { name: "yolo11n" })).toBeInTheDocument());
  await user.selectOptions(screen.getByLabelText("基础权重"), "yolo11n");
  await waitFor(() => expect(screen.getByRole("button", { name: "下一步" })).toBeEnabled());
  await user.click(screen.getByRole("button", { name: "下一步" }));
  if (review) await user.click(screen.getByRole("button", { name: "下一步" }));
  return { user, view };
}
async function check() {
  await userEvent.click(screen.getByRole("button", { name: "检查训练配置" }));
  await screen.findByText("训练检查通过 · 请核对最终配置");
}

describe("training creation ownership", () => {
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
      if (context === "project") await user.click(screen.getByRole("button", { name: "切换项目" }));
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
        clients[clients.length - 1].setQueryData(
          ["training-catalog", "project-1", "user-1"],
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
    expect(screen.getByLabelText("基础权重")).toHaveValue("");
    await user.selectOptions(screen.getByLabelText("基础权重"), "LibreYOLO9t");
    await screen.findByRole("dialog");
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
    await screen.findByRole("dialog");
    expect(screen.getByRole("button", { name: "应用导入" })).toBeDisabled();
    expect(screen.getByLabelText("批大小")).toHaveValue("4");
    await user.click(screen.getByRole("button", { name: "取消" }));
    upload("lr0: 1e-6");
    await screen.findByRole("dialog");
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
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByLabelText("批大小")).toHaveValue("4");
  });
  it("blocks an import preview when the current schema changes after validation", async () => {
    const { user } = await prepare(false);
    const source = "lr0: 1e-6";
    const file = new File([source], "config.yaml");
    Object.defineProperty(file, "text", { value: () => Promise.resolve(source) });
    fireEvent.change(screen.getByLabelText("导入 YAML 文件"), { target: { files: [file] } });
    await screen.findByRole("dialog");
    const key = trainingDraftKey(trainingDraftOwner("user-1", "project-1"));
    const original = localStorage.getItem(key);
    act(() =>
      clients[clients.length - 1].setQueryData(
        ["training-parameter-schema", "project-1", "user-1", "ultralytics", "yolo11n"],
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
      expect(trainingDatasetsApi.list).toHaveBeenCalledWith("project-1", 20, "ready"),
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
    await screen.findByRole("dialog");
    expect(screen.getByText("移除 1 项：removed")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "接受切换" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "下一步" })).toBeEnabled());
    expect(localStorage.getItem(trainingDraftKey(owner))).not.toContain('"removed"');
    expect(localStorage.getItem(trainingDraftKey(owner))).toContain("1e-6");
  });
});

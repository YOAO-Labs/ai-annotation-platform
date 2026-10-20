import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { mlBackendsApi } from "@/api/ml-backends";
import { sampleEvaluationsApi } from "@/api/sampleEvaluations";
import { trainedModelsApi } from "@/api/trainedModels";
import { trainingDatasetsApi } from "@/api/trainingDatasets";
import { useAuthStore } from "@/stores/authStore";
import { SampleEvaluationCreatePage } from "./SampleEvaluationCreate";

vi.mock("@/api/sampleEvaluations", () => ({
  sampleEvaluationsApi: { qualify: vi.fn(), create: vi.fn() },
}));
vi.mock("@/api/ml-backends", () => ({ mlBackendsApi: { list: vi.fn() } }));
vi.mock("@/api/trainedModels", () => ({ trainedModelsApi: { list: vi.fn(), get: vi.fn() } }));
vi.mock("@/api/trainingDatasets", () => ({
  trainingDatasetsApi: { list: vi.fn(), getSummary: vi.fn(), samples: vi.fn() },
}));
const id = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const project = id(1),
  model = id(2),
  deployment = id(3),
  dataset = id(4);
const clients: QueryClient[] = [];
function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/projects/${project}/learning/evaluations/new`]}>
        <Routes>
          <Route
            path="/projects/:id/learning/evaluations/new"
            element={<SampleEvaluationCreatePage />}
          />
          <Route
            path="/projects/:id/learning/evaluations/:evaluationId"
            element={<p>已创建评估</p>}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
beforeEach(() => {
  vi.resetAllMocks();
  useAuthStore.getState().setAuth("creator-token", { id: id(9) } as never);
  vi.mocked(trainedModelsApi.list).mockResolvedValue({
    total: 1,
    items: [{ id: model, name: "候选", framework: "ultralytics", project_id: project }],
  } as never);
  vi.mocked(trainedModelsApi.get).mockResolvedValue({
    model: { id: model, name: "候选", project_id: project, framework: "ultralytics" },
    deployments: [
      {
        id: deployment,
        registry_id: id(5),
        model_version_id: model,
        status: "ready",
        created_at: "2025-01-02T03:04:00Z",
        checked_at: "2025-01-03T03:04:00Z",
      },
    ],
  } as never);
  vi.mocked(mlBackendsApi.list).mockResolvedValue([] as never);
  vi.mocked(trainingDatasetsApi.list).mockResolvedValue({
    total: 1,
    items: [{ id: dataset, name: "冻结数据" }],
  } as never);
  vi.mocked(trainingDatasetsApi.getSummary).mockResolvedValue({
    id: dataset,
    project_id: project,
    status: "ready",
    name: "冻结数据",
  } as never);
  vi.mocked(trainingDatasetsApi.samples).mockImplementation(
    async (_p, _d, filters) =>
      ({
        source_version_id: dataset,
        total: 13,
        limit: 12,
        offset: filters.offset ?? 0,
        items: Array.from({ length: filters.offset ? 1 : 12 }, (_, i) => ({
          task_id: id(100 + (filters.offset ?? 0) + i),
          version_id: dataset,
          split: "val",
          included: true,
          issue: null,
          sha256: "a".repeat(64),
          label_sha256: "b".repeat(64),
          width: 300,
          height: 200,
          instances: 1,
          size_bytes: 1024,
          is_negative: false,
        })),
      }) as never,
  );
  vi.mocked(sampleEvaluationsApi.qualify).mockResolvedValue({
    model_ref: { model_version_id: model, deployment_id: deployment, project_id: project },
    profile: { ultralytics_version: "8.4.177", effective_device: "cpu" },
  } as never);
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((c) => c.clear());
});
async function choose(selectedDeploymentId = deployment) {
  await screen.findByRole("option", { name: "候选 · ultralytics" });
  fireEvent.change(await screen.findByRole("combobox", { name: /已注册模型/ }), {
    target: { value: model },
  });
  const deploymentSelect = screen.getByRole("combobox", {
    name: /已安装部署/,
  }) as HTMLSelectElement;
  await waitFor(() =>
    expect(Array.from(deploymentSelect.options).some((option) => option.value)).toBe(true),
  );
  fireEvent.change(deploymentSelect, {
    target: { value: selectedDeploymentId },
  });
  await screen.findByRole("option", { name: "冻结数据" });
  fireEvent.change(screen.getByRole("combobox", { name: /就绪数据版本/ }), {
    target: { value: dataset },
  });
  fireEvent.change(screen.getByRole("textbox", { name: "评估名称" }), {
    target: { value: "路口误差" },
  });
  await screen.findByRole("checkbox", { name: /^选择验证成员 1（/ });
}
describe("explicit frozen evaluation creation", () => {
  it("selects only segmentation data for a segment candidate and applies its pixel budget", async () => {
    const detail = await vi.mocked(trainedModelsApi.get).getMockImplementation()!(project, model);
    vi.mocked(trainedModelsApi.get).mockResolvedValue({
      ...detail,
      model: { ...detail.model, task: "segment" },
    });
    vi.mocked(trainingDatasetsApi.list).mockResolvedValue({
      total: 2,
      items: [
        { id: dataset, name: "冻结数据", task: "segment" },
        { id: id(45), name: "检测数据", task: "detect" },
      ],
    } as never);
    vi.mocked(trainingDatasetsApi.getSummary).mockResolvedValue({
      id: dataset,
      project_id: project,
      status: "ready",
      name: "冻结数据",
      task: "segment",
    } as never);
    vi.mocked(sampleEvaluationsApi.qualify).mockResolvedValue({
      model_ref: {
        model_version_id: model,
        deployment_id: deployment,
        project_id: project,
        task: "segment",
      },
      profile: { ultralytics_version: "8.4.177", effective_device: "cpu" },
      limits: { pixels: 100, max_dimension: 4096 },
    } as never);
    mount();
    await choose();
    expect(screen.queryByRole("option", { name: "检测数据" })).not.toBeInTheDocument();
    expect(screen.getByRole("spinbutton", { name: "匹配 Mask IoU" })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /^选择验证成员 1（/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: "开始逐样本评估" })).toBeDisabled();
    expect(screen.getByText(/平均 Mask IoU、Dice 仅统计已匹配实例/)).toBeInTheDocument();
    vi.mocked(sampleEvaluationsApi.qualify).mockResolvedValue({
      model_ref: {
        model_version_id: model,
        deployment_id: deployment,
        project_id: project,
        task: "segment",
      },
      profile: { ultralytics_version: "8.4.177", effective_device: "cpu" },
      limits: { pixels: 1_280_000, max_dimension: 4096 },
    } as never);
    await act(async () => {
      await clients[clients.length - 1]!.refetchQueries({
        queryKey: ["evaluation-qualification", project],
      });
    });
    await waitFor(() =>
      expect(screen.getByRole("checkbox", { name: /^选择验证成员 1（/ })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("checkbox", { name: /^选择验证成员 1（/ }));
    expect(screen.getByRole("button", { name: "开始逐样本评估" })).toBeEnabled();
  });
  it("retains a deployment that disappears on refresh and requires another explicit choice", async () => {
    mount();
    await choose();
    fireEvent.click(screen.getByRole("checkbox", { name: /^选择验证成员 1（/ }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "开始逐样本评估" })).toBeEnabled(),
    );
    vi.mocked(trainedModelsApi.get).mockResolvedValue({
      model: { id: model, project_id: project, framework: "ultralytics" },
      deployments: [
        {
          id: id(40),
          registry_id: id(41),
          status: "ready",
          created_at: "2025-01-01",
          checked_at: null,
        },
      ],
    } as never);
    await act(async () => {
      await clients[clients.length - 1]!.refetchQueries({
        queryKey: ["evaluation-model", project],
      });
    });
    const select = screen.getByRole("combobox", { name: /已安装部署/ });
    expect(select).toHaveValue(deployment);
    await waitFor(() => expect(select).toHaveDisplayValue("已选部署（暂不可用，请重新选择）"));
    expect(screen.getByRole("button", { name: "开始逐样本评估" })).toBeDisabled();
    expect(sampleEvaluationsApi.create).not.toHaveBeenCalled();
  });
  it("requires a new deployment choice after changing the candidate model", async () => {
    vi.mocked(trainedModelsApi.list).mockResolvedValue({
      total: 2,
      items: [
        { id: model, name: "候选", framework: "ultralytics", project_id: project },
        { id: id(40), name: "另一候选", framework: "ultralytics", project_id: project },
      ],
    } as never);
    mount();
    await choose();
    const deploymentSelect = screen.getByRole("combobox", { name: /已安装部署/ });
    expect(deploymentSelect).toHaveValue(deployment);
    fireEvent.change(screen.getByRole("combobox", { name: /已注册模型/ }), {
      target: { value: id(40) },
    });
    expect(deploymentSelect).toHaveValue("");
    expect(deploymentSelect.querySelector('option[value=""]')).toBeDisabled();
    expect(deploymentSelect.querySelector('option[value=""]')).toHaveAttribute("hidden");
    expect(screen.getByRole("button", { name: "开始逐样本评估" })).toBeDisabled();
  });
  it("keeps duplicate backend deployment labels stable and submits the selected deployment after reordering", async () => {
    const unnamedRegistry = id(20);
    const registryA = id(21);
    const registryB = id(22);
    const deploymentA = {
      id: id(3),
      registry_id: registryA,
      status: "ready",
      created_at: "2025-01-01T03:04:00Z",
      checked_at: "2025-01-02T03:04:00Z",
    };
    const deploymentB = {
      id: id(6),
      registry_id: registryB,
      status: "ready",
      created_at: "2025-01-01T03:04:00Z",
      checked_at: "2025-01-03T03:04:00Z",
    };
    const unnamedDeployment = {
      id: id(7),
      registry_id: unnamedRegistry,
      status: "ready",
      created_at: "2025-01-04T03:04:00Z",
      checked_at: null,
    };
    const modelResponse = (deployments: unknown[]) => ({
      model: { id: model, name: "候选", project_id: project, framework: "ultralytics" },
      deployments,
    });
    vi.mocked(trainedModelsApi.get)
      .mockResolvedValueOnce(modelResponse([unnamedDeployment, deploymentB, deploymentA]) as never)
      .mockResolvedValue(modelResponse([deploymentA, unnamedDeployment, deploymentB]) as never);
    vi.mocked(mlBackendsApi.list).mockResolvedValue([
      { id: registryA, name: "部署 1" },
      { id: registryB, name: "部署 1" },
    ] as never);
    vi.mocked(sampleEvaluationsApi.qualify).mockImplementation(
      async (_projectId, _modelId, selectedDeploymentId) =>
        ({
          model_ref: {
            model_version_id: model,
            deployment_id: selectedDeploymentId,
            project_id: project,
          },
          profile: { ultralytics_version: "8.4.177", effective_device: "cpu" },
        }) as never,
    );
    vi.mocked(sampleEvaluationsApi.create).mockImplementation(
      async (_projectId, body) =>
        ({
          id: id(10),
          project_id: project,
          current_attempt_id: id(11),
          request_id: body.request_id,
        }) as never,
    );

    mount();
    await choose(deploymentB.id);
    const deploymentSelect = screen.getByRole("combobox", { name: /已安装部署/ });
    const labels = () =>
      Array.from(deploymentSelect.querySelectorAll("option"))
        .filter((option) => option.value)
        .map((option) => option.textContent);
    const expectedLabels = [
      "部署 1 · 就绪 · 创建于 2025-01-04",
      "部署 2 · 部署 1 · 就绪 · 检查于 2025-01-02",
      "部署 3 · 部署 1 · 就绪 · 检查于 2025-01-03",
    ];
    expect(labels()).toEqual(expectedLabels);
    expect(deploymentSelect).toHaveValue(deploymentB.id);
    expect(screen.getByRole("button", { name: "复制推理后端 ID" })).toBeInTheDocument();

    const client = clients[clients.length - 1]!;
    await client.invalidateQueries({ queryKey: ["evaluation-model", project] });
    await waitFor(() => expect(trainedModelsApi.get).toHaveBeenCalledTimes(2));
    expect(labels()).toEqual(expectedLabels);
    expect(deploymentSelect).toHaveValue(deploymentB.id);

    fireEvent.click(screen.getByRole("checkbox", { name: /^选择验证成员 1（/ }));
    const submit = screen.getByRole("button", { name: "开始逐样本评估" });
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);
    await screen.findByText("已创建评估");
    expect(sampleEvaluationsApi.qualify).toHaveBeenCalledWith(
      project,
      model,
      deploymentB.id,
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(sampleEvaluationsApi.create).toHaveBeenCalledWith(
      project,
      expect.objectContaining({ deployment_id: deploymentB.id }),
    );
  });

  it("keeps explicit members across pages and reuses the request after a lost create response", async () => {
    vi.mocked(sampleEvaluationsApi.create)
      .mockRejectedValueOnce(new Error("响应丢失"))
      .mockImplementation(
        async (_p, body) =>
          ({
            id: id(10),
            project_id: project,
            current_attempt_id: id(11),
            request_id: body.request_id,
          }) as never,
      );
    mount();
    await choose();
    fireEvent.click(screen.getByRole("checkbox", { name: /^选择验证成员 1（/ }));
    fireEvent.click(screen.getByRole("button", { name: "成员下一页" }));
    fireEvent.click(await screen.findByRole("checkbox", { name: /选择验证成员 13/ }));
    const submit = screen.getByRole("button", { name: "开始逐样本评估" });
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);
    expect(await screen.findByText(/响应丢失/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "开始逐样本评估" }));
    expect(await screen.findByText("已创建评估")).toBeInTheDocument();
    const calls = vi.mocked(sampleEvaluationsApi.create).mock.calls;
    expect(calls).toHaveLength(2);
    expect(calls[0][1].request_id).toBe(calls[1][1].request_id);
    expect(calls[1][1]).toMatchObject({
      sample_task_ids: [id(100), id(112)],
      role: "validation_selection",
      confidence: 0.25,
      matching_iou: 0.5,
      nms_iou: 0.7,
    });
    expect(trainingDatasetsApi.samples).toHaveBeenCalledWith(
      project,
      dataset,
      expect.objectContaining({ split: "val", offset: 12 }),
      { signal: expect.any(AbortSignal) },
    );
  });
  it("requires an explicit held-out declaration and prevents submission when qualification fails", async () => {
    vi.mocked(sampleEvaluationsApi.qualify).mockRejectedValue(new Error("运行环境不受支持"));
    mount();
    await choose();
    fireEvent.click(screen.getByRole("checkbox", { name: /^选择验证成员 1（/ }));
    fireEvent.change(screen.getByRole("combobox", { name: /数据用途/ }), {
      target: { value: "held_out_test" },
    });
    expect(await screen.findByText(/当前部署不具备评估资格/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "开始逐样本评估" })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: /我确认这些成员未用于/ })).not.toBeChecked();
    fireEvent.click(screen.getByRole("checkbox", { name: /我确认这些成员未用于/ }));
    expect(screen.getByRole("button", { name: "开始逐样本评估" })).toBeDisabled();
    expect(sampleEvaluationsApi.create).not.toHaveBeenCalled();
  });
});

it("explains native model support and blocks LibreYOLO shared evaluation", async () => {
  vi.mocked(trainedModelsApi.list).mockResolvedValue({
    total: 1,
    items: [{ id: model, name: "原生候选", framework: "libreyolo", project_id: project }],
  } as never);
  vi.mocked(trainedModelsApi.get).mockResolvedValue({
    model: { id: model, project_id: project, framework: "libreyolo" },
    deployments: [
      {
        id: deployment,
        registry_id: id(5),
        status: "ready",
        created_at: "2025-01-02T03:04:00Z",
        checked_at: "2025-01-03T03:04:00Z",
      },
    ],
  } as never);
  mount();
  expect(
    await screen.findByRole("option", { name: /原生候选.*尚未通过共享评估资格/ }),
  ).toBeDisabled();
  fireEvent.change(screen.getByRole("combobox", { name: /已注册模型/ }), {
    target: { value: model },
  });
  await waitFor(() => expect(screen.getByRole("combobox", { name: /已安装部署/ })).toBeDisabled());
  expect(screen.getByText(/LibreYOLO 可在模型面板试用/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "开始逐样本评估" })).toBeDisabled();
  expect(sampleEvaluationsApi.qualify).not.toHaveBeenCalled();
  expect(sampleEvaluationsApi.create).not.toHaveBeenCalled();
});

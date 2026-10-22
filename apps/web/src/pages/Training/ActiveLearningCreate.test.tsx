import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { activeLearningApi } from "@/api/activeLearning";
import { mlBackendsApi } from "@/api/ml-backends";
import { trainedModelsApi } from "@/api/trainedModels";
import { useAuthStore } from "@/stores/authStore";
import { ActiveLearningCreatePage } from "./ActiveLearningCreate";

vi.mock("@/api/activeLearning", () => ({
  activeLearningApi: { pool: vi.fn(), preview: vi.fn(), create: vi.fn() },
}));
vi.mock("@/api/ml-backends", () => ({ mlBackendsApi: { list: vi.fn() } }));
vi.mock("@/api/trainedModels", () => ({ trainedModelsApi: { list: vi.fn(), get: vi.fn() } }));
const id = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const project = id(1),
  model = id(2),
  deployment = id(3);
const clients: QueryClient[] = [];
function SwitchProject() {
  const navigate = useNavigate();
  return (
    <button onClick={() => navigate(`/projects/${id(99)}/learning/active-learning/new`)}>
      切换项目
    </button>
  );
}
function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/projects/${project}/learning/active-learning/new`]}>
        <SwitchProject />
        <Routes>
          <Route
            path="/projects/:id/learning/active-learning/new"
            element={<ActiveLearningCreatePage />}
          />
          <Route
            path="/projects/:id/learning/active-learning/:roundId"
            element={<p>已创建选样轮次</p>}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
beforeEach(() => {
  vi.resetAllMocks();
  sessionStorage.clear();
  useAuthStore.getState().setAuth("active-token", { id: id(9) } as never);
  vi.mocked(trainedModelsApi.list).mockImplementation(
    async (pid) =>
      ({
        total: 1,
        items: [{ id: model, name: "固定候选", framework: "ultralytics", project_id: pid }],
      }) as never,
  );
  vi.mocked(trainedModelsApi.get).mockResolvedValue({
    model: { id: model, project_id: project, framework: "ultralytics" },
    deployments: [
      {
        id: deployment,
        registry_id: id(4),
        status: "ready",
        created_at: "2025-01-02T03:04:00Z",
        checked_at: "2025-01-03T03:04:00Z",
      },
    ],
  } as never);
  vi.mocked(mlBackendsApi.list).mockResolvedValue([] as never);
  vi.mocked(activeLearningApi.pool).mockResolvedValue({
    total: 6,
    items: Array.from({ length: 6 }, (_, index) => ({
      task_id: id(100 + index),
      display_id: `T-${index + 1}`,
      file_name: `未标注-${index + 1}.png`,
    })),
  });
  vi.mocked(activeLearningApi.preview).mockResolvedValue({
    preview_digest: "a".repeat(64),
    pool_size: 6,
    candidate_count: 6,
    omitted: [],
    samples: [],
    batch_size: 2,
  } as never);
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
  sessionStorage.clear();
});
async function choose() {
  await screen.findByRole("option", { name: "固定候选 · ultralytics" });
  fireEvent.change(screen.getByRole("combobox", { name: "已注册模型" }), {
    target: { value: model },
  });
  await screen.findByRole("option", { name: /就绪/ });
  fireEvent.change(screen.getByRole("combobox", { name: "就绪部署" }), {
    target: { value: deployment },
  });
  fireEvent.change(screen.getByRole("textbox", { name: "轮次名称" }), {
    target: { value: "道路人工选样" },
  });
  await waitFor(() => expect(screen.getByRole("button", { name: "选择本页候选" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "选择本页候选" }));
  fireEvent.click(screen.getByRole("button", { name: "核验并预览" }));
  await screen.findByRole("heading", { name: "3. 已核验预览" });
}
describe("explicit active learning creation", () => {
  it("previews and creates a ready segmentation candidate using instance mask signals", async () => {
    vi.mocked(trainedModelsApi.list).mockResolvedValue({
      total: 1,
      items: [
        {
          id: model,
          name: "固定候选",
          framework: "ultralytics",
          project_id: project,
          task: "segment",
        },
      ],
    } as never);
    vi.mocked(trainedModelsApi.get).mockResolvedValue({
      model: { id: model, project_id: project, framework: "ultralytics", task: "segmentation" },
      deployments: [
        {
          id: deployment,
          registry_id: id(4),
          status: "ready",
          created_at: "2025-01-02",
          checked_at: null,
        },
      ],
    } as never);
    vi.mocked(activeLearningApi.preview).mockResolvedValue({
      preview_digest: "a".repeat(64),
      pool_size: 6,
      candidate_count: 6,
      omitted: [],
      samples: [],
      batch_size: 2,
      model_ref: { task: "segment", model_version_id: model, deployment_id: deployment },
    } as never);
    vi.mocked(activeLearningApi.create).mockResolvedValue({
      id: id(20),
      project_id: project,
      spec: { round_id: id(20) },
    } as never);
    mount();
    await screen.findByRole("option", { name: "固定候选 · ultralytics · 图片实例分割" });
    fireEvent.change(screen.getByLabelText("已注册模型"), { target: { value: model } });
    await screen.findByRole("option", { name: /就绪/ });
    fireEvent.change(screen.getByLabelText("就绪部署"), { target: { value: deployment } });
    fireEvent.change(screen.getByLabelText("轮次名称"), { target: { value: "原生掩码选样" } });
    fireEvent.click(await screen.findByRole("button", { name: "选择本页候选" }));
    fireEvent.click(screen.getByRole("button", { name: "核验并预览" }));
    await screen.findByRole("heading", { name: "3. 已核验预览" });
    expect(screen.getByText(/每张原图最多 1,280,000 像素/)).toBeVisible();
    expect(screen.getByText(/这些信号不使用像素概率，也不是 AP/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "创建选样轮次" }));
    await screen.findByText("已创建选样轮次");
    expect(vi.mocked(activeLearningApi.create).mock.calls[0][1]).toMatchObject({
      model_version_id: model,
      deployment_id: deployment,
    });
    expect(vi.mocked(activeLearningApi.create).mock.calls[0][1]).not.toHaveProperty("task");
  });
  it("rejects a preview whose task differs from the chosen model", async () => {
    vi.mocked(activeLearningApi.preview).mockResolvedValue({
      model_ref: { task: "segment" },
    } as never);
    mount();
    await screen.findByRole("option", { name: "固定候选 · ultralytics" });
    fireEvent.change(screen.getByLabelText("已注册模型"), { target: { value: model } });
    await screen.findByRole("option", { name: /就绪/ });
    fireEvent.change(screen.getByLabelText("就绪部署"), { target: { value: deployment } });
    fireEvent.click(await screen.findByRole("button", { name: "选择本页候选" }));
    fireEvent.click(screen.getByRole("button", { name: "核验并预览" }));
    expect(await screen.findByText("选样预览与候选模型任务不一致")).toBeVisible();
    expect(screen.getByRole("button", { name: "创建选样轮次" })).toBeDisabled();
  });
  it("retains the exact selected model when moving to a different model page", async () => {
    vi.mocked(trainedModelsApi.list).mockImplementation(
      async (_project, offset) =>
        ({
          total: 21,
          items: [
            {
              id: offset ? id(40) : model,
              name: offset ? "另一页候选" : "固定候选",
              framework: "ultralytics",
              project_id: project,
            },
          ],
        }) as never,
    );
    mount();
    await choose();
    fireEvent.click(screen.getByRole("button", { name: "模型下一页" }));
    await screen.findByRole("option", { name: "另一页候选 · ultralytics" });
    expect(screen.getByRole("combobox", { name: "已注册模型" })).toHaveValue(model);
    expect(screen.getByRole("combobox", { name: "就绪部署" })).toHaveValue(deployment);
  });
  it("retains an unavailable deployment without displaying or submitting a different one", async () => {
    mount();
    await choose();
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
        queryKey: ["active-learning-model", project],
      });
    });
    const select = screen.getByRole("combobox", { name: "就绪部署" });
    expect(select).toHaveValue(deployment);
    await waitFor(() => expect(select).toHaveDisplayValue("已选部署（暂不可用，请重新选择）"));
    expect(screen.getByRole("button", { name: "核验并预览" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "创建选样轮次" })).toBeDisabled();
  });
  it("restores the deployment prompt when a different model is selected", async () => {
    vi.mocked(trainedModelsApi.list).mockResolvedValue({
      total: 2,
      items: [
        { id: model, name: "固定候选", framework: "ultralytics", project_id: project },
        { id: id(40), name: "另一候选", framework: "ultralytics", project_id: project },
      ],
    } as never);
    mount();
    await choose();
    const select = screen.getByRole("combobox", { name: "就绪部署" });
    expect(select).toHaveValue(deployment);
    fireEvent.change(screen.getByRole("combobox", { name: "已注册模型" }), {
      target: { value: id(40) },
    });
    expect(select).toHaveValue("");
    expect(select.querySelector('option[value=""]')).toBeDisabled();
    expect(select.querySelector('option[value=""]')).toHaveAttribute("hidden");
    expect(screen.queryByRole("heading", { name: "3. 已核验预览" })).not.toBeInTheDocument();
  });
  it("keeps duplicate backend names and selected deployment identity stable across API reordering", async () => {
    const namedRegistryA = id(10);
    const namedRegistryB = id(11);
    const unnamedRegistry = id(12);
    const deploymentA = {
      id: id(3),
      registry_id: namedRegistryA,
      status: "ready",
      created_at: "2025-01-01T03:04:00Z",
      checked_at: "2025-01-02T03:04:00Z",
    };
    const deploymentB = {
      id: id(4),
      registry_id: namedRegistryB,
      status: "ready",
      created_at: "2025-01-01T03:04:00Z",
      checked_at: "2025-01-03T03:04:00Z",
    };
    const unnamedDeployment = {
      id: id(5),
      registry_id: unnamedRegistry,
      status: "ready",
      created_at: "2025-01-04T03:04:00Z",
      checked_at: null,
    };
    const modelResponse = (deployments: unknown[]) => ({
      model: { id: model, project_id: project, framework: "ultralytics" },
      deployments,
    });
    vi.mocked(trainedModelsApi.get)
      .mockResolvedValueOnce(modelResponse([unnamedDeployment, deploymentB, deploymentA]) as never)
      .mockResolvedValue(modelResponse([deploymentA, unnamedDeployment, deploymentB]) as never);
    vi.mocked(mlBackendsApi.list).mockResolvedValue([
      { id: namedRegistryA, name: "共享推理后端" },
      { id: namedRegistryB, name: "共享推理后端" },
    ] as never);

    mount();
    await screen.findByRole("option", { name: "固定候选 · ultralytics" });
    fireEvent.change(screen.getByRole("combobox", { name: "已注册模型" }), {
      target: { value: model },
    });
    const deploymentSelect = await screen.findByRole("combobox", { name: "就绪部署" });
    const labels = () =>
      Array.from(deploymentSelect.querySelectorAll("option"))
        .filter((option) => option.value)
        .map((option) => option.textContent);
    const expectedLabels = [
      "部署 1 · 共享推理后端 · 就绪 · 检查于 2025-01-02",
      "部署 2 · 共享推理后端 · 就绪 · 检查于 2025-01-03",
      "部署 3 · 就绪 · 创建于 2025-01-04",
    ];
    await waitFor(() => expect(labels()).toEqual(expectedLabels));

    fireEvent.change(deploymentSelect, { target: { value: deploymentB.id } });
    expect(deploymentSelect).toHaveValue(deploymentB.id);
    expect(screen.getByRole("button", { name: "复制已选部署 ID" })).toBeInTheDocument();

    const client = clients[clients.length - 1]!;
    await client.invalidateQueries({ queryKey: ["active-learning-model", project] });
    await waitFor(() => expect(trainedModelsApi.get).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(labels()).toEqual(expectedLabels));
    expect(deploymentSelect).toHaveValue(deploymentB.id);

    fireEvent.change(screen.getByRole("textbox", { name: "轮次名称" }), {
      target: { value: "稳定部署选样" },
    });
    await waitFor(() => expect(screen.getByRole("button", { name: "选择本页候选" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "选择本页候选" }));
    fireEvent.click(screen.getByRole("button", { name: "核验并预览" }));
    await screen.findByRole("heading", { name: "3. 已核验预览" });
    expect(activeLearningApi.preview).toHaveBeenCalledWith(
      project,
      expect.objectContaining({ deployment_id: deploymentB.id }),
    );
  });

  it("invalidates preview when budgets change", async () => {
    mount();
    await choose();
    expect(screen.getByRole("button", { name: "创建选样轮次" })).toBeEnabled();
    fireEvent.change(screen.getByRole("spinbutton", { name: "推理派发上限" }), {
      target: { value: "31" },
    });
    expect(screen.queryByRole("heading", { name: "3. 已核验预览" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "创建选样轮次" })).toBeDisabled();
    expect(activeLearningApi.create).not.toHaveBeenCalled();
  });
  it("persists exact creation body through a lost response and refresh", async () => {
    vi.mocked(activeLearningApi.create)
      .mockRejectedValueOnce(new Error("回执丢失"))
      .mockResolvedValue({ id: id(20), project_id: project, spec: { round_id: id(20) } } as never);
    const first = mount();
    await choose();
    fireEvent.click(screen.getByRole("button", { name: "创建选样轮次" }));
    await screen.findByText("回执丢失");
    const original = vi.mocked(activeLearningApi.create).mock.calls[0][1];
    first.unmount();
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "重试同一创建请求" }));
    await screen.findByText("已创建选样轮次");
    expect(vi.mocked(activeLearningApi.create).mock.calls[1][1]).toEqual(original);
  });
  it("ignores a late creation acknowledgement after project changes", async () => {
    let resolve!: (value: never) => void;
    vi.mocked(activeLearningApi.create).mockImplementation(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    mount();
    await choose();
    fireEvent.click(screen.getByRole("button", { name: "创建选样轮次" }));
    await waitFor(() => expect(activeLearningApi.create).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: "切换项目" }));
    resolve({ id: id(20), project_id: project, spec: { round_id: id(20) } } as never);
    await waitFor(() => expect(screen.getByRole("textbox", { name: "轮次名称" })).toHaveValue(""));
    expect(screen.queryByText("已创建选样轮次")).not.toBeInTheDocument();
  });
});

it("blocks LibreYOLO ranking until shared evaluator qualification", async () => {
  vi.mocked(trainedModelsApi.list).mockResolvedValue({
    total: 1,
    items: [{ id: model, name: "原生候选", framework: "libreyolo", project_id: project }],
  } as never);
  vi.mocked(trainedModelsApi.get).mockResolvedValue({
    model: { id: model, project_id: project, framework: "libreyolo" },
    deployments: [
      {
        id: deployment,
        registry_id: id(4),
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
  await waitFor(() => expect(screen.getByRole("combobox", { name: "就绪部署" })).toBeDisabled());
  expect(screen.getByText(/LibreYOLO 可在模型面板试用/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "核验并预览" })).toBeDisabled();
  expect(activeLearningApi.preview).not.toHaveBeenCalled();
  expect(activeLearningApi.create).not.toHaveBeenCalled();
});

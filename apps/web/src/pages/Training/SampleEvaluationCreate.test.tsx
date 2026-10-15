import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { sampleEvaluationsApi } from "@/api/sampleEvaluations";
import { trainedModelsApi } from "@/api/trainedModels";
import { trainingDatasetsApi } from "@/api/trainingDatasets";
import { useAuthStore } from "@/stores/authStore";
import { SampleEvaluationCreatePage } from "./SampleEvaluationCreate";

vi.mock("@/api/sampleEvaluations", () => ({
  sampleEvaluationsApi: { qualify: vi.fn(), create: vi.fn() },
}));
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
    model: { id: model, name: "候选", project_id: project },
    deployments: [{ id: deployment, registry_id: id(5), model_version_id: model, status: "ready" }],
  } as never);
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
async function choose() {
  await screen.findByRole("option", { name: "候选 · ultralytics" });
  fireEvent.change(await screen.findByRole("combobox", { name: /已注册模型/ }), {
    target: { value: model },
  });
  await waitFor(() => expect(screen.getByRole("option", { name: /后端/ })).toBeInTheDocument());
  fireEvent.change(screen.getByRole("combobox", { name: /已安装部署/ }), {
    target: { value: deployment },
  });
  await screen.findByRole("option", { name: "冻结数据" });
  fireEvent.change(screen.getByRole("combobox", { name: /就绪数据版本/ }), {
    target: { value: dataset },
  });
  fireEvent.change(screen.getByRole("textbox", { name: "评估名称" }), {
    target: { value: "路口误差" },
  });
  await screen.findByRole("checkbox", { name: /000000000100/ });
}
describe("explicit frozen evaluation creation", () => {
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
    fireEvent.click(screen.getByRole("checkbox", { name: /000000000100/ }));
    fireEvent.click(screen.getByRole("button", { name: "成员下一页" }));
    fireEvent.click(await screen.findByRole("checkbox", { name: /000000000112/ }));
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
    fireEvent.click(screen.getByRole("checkbox", { name: /000000000100/ }));
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

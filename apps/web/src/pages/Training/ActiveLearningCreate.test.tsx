import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { activeLearningApi } from "@/api/activeLearning";
import { trainedModelsApi } from "@/api/trainedModels";
import { useAuthStore } from "@/stores/authStore";
import { ActiveLearningCreatePage } from "./ActiveLearningCreate";

vi.mock("@/api/activeLearning", () => ({
  activeLearningApi: { pool: vi.fn(), preview: vi.fn(), create: vi.fn() },
}));
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
    deployments: [{ id: deployment, status: "ready" }],
  } as never);
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
  await screen.findByRole("option", { name: /就绪$/ });
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
    deployments: [{ id: deployment, status: "ready" }],
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

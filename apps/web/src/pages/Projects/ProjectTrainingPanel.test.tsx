import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { trainingRunsApi } from "@/api/trainingRuns";
import { ProjectTrainingPanel } from "./ProjectTrainingPanel";

vi.mock("@/api/trainingRuns", () => ({
  trainingRunsApi: {
    catalog: vi.fn(),
    list: vi.fn(),
    get: vi.fn(),
    events: vi.fn(),
    preflight: vi.fn(),
    create: vi.fn(),
    cancel: vi.fn(),
    resume: vi.fn(),
    remove: vi.fn(),
    download: vi.fn(),
  },
}));
const clients: QueryClient[] = [];
const dataset = { id: "dataset", name: "审核数据", status: "ready" };
const run = {
  id: "run",
  name: "车辆训练",
  status: "training",
  metrics: { epoch: 2 },
  config: { framework: "ultralytics", parameters: { epochs: 10 } },
  created_at: "2026-10-14T12:00:00Z",
  current_attempt_id: "attempt",
};
function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <ProjectTrainingPanel projectId="project" datasets={[dataset as never]} />
    </QueryClientProvider>,
  );
}
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(trainingRunsApi.catalog).mockResolvedValue({
    enabled: true,
    online: true,
    busy: false,
    resource_id: "gpu",
    items: [
      {
        framework: "ultralytics",
        version: "8.4.177",
        image_digest: "sha256:fixed",
        base_models: [{ id: "yolo11n", sha256: "base", filename: "yolo11n.pt" }],
        capabilities: {
          task_types: ["image-detection"],
          resume: true,
          artifact_download: true,
          model_registration: true,
        },
      },
    ],
  });
  vi.mocked(trainingRunsApi.list).mockResolvedValue({ items: [], total: 0 });
  vi.mocked(trainingRunsApi.preflight).mockResolvedValue({ config: {}, config_digest: "frozen" });
  vi.mocked(trainingRunsApi.create).mockResolvedValue(run as never);
  vi.mocked(trainingRunsApi.get).mockResolvedValue({ run: run as never, attempts: [] });
  vi.mocked(trainingRunsApi.events).mockResolvedValue({ attempt_id: "attempt", items: [] });
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
});
async function prepare() {
  const user = userEvent.setup();
  mount();
  await user.click(await screen.findByRole("button", { name: "新建实验" }));
  await screen.findByLabelText("实验名称");
  await user.type(screen.getByLabelText("实验名称"), "车辆训练");
  await user.selectOptions(screen.getByLabelText("训练数据"), "dataset");
  await user.click(screen.getByRole("button", { name: "检查训练配置" }));
  await screen.findByText(/训练检查通过/);
  return user;
}
describe("managed project training", () => {
  it("invalidates preflight when any frozen input changes", async () => {
    const user = await prepare();
    expect(screen.getByRole("button", { name: "开始训练" })).toBeEnabled();
    await user.clear(screen.getByLabelText("批大小"));
    await user.type(screen.getByLabelText("批大小"), "4");
    expect(screen.queryByText(/训练检查通过/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "开始训练" })).toBeDisabled();
  });
  it("reuses the same request after a lost create response", async () => {
    vi.mocked(trainingRunsApi.create).mockRejectedValueOnce(new Error("连接中断"));
    const user = await prepare();
    await user.click(screen.getByRole("button", { name: "开始训练" }));
    await screen.findByText("连接中断");
    const first = vi.mocked(trainingRunsApi.create).mock.calls[0][1];
    await user.click(screen.getByRole("button", { name: "开始训练" }));
    await waitFor(() => expect(trainingRunsApi.create).toHaveBeenCalledTimes(2));
    expect(vi.mocked(trainingRunsApi.create).mock.calls[1][1]).toEqual(first);
    expect(first.preflight_digest).toBe("frozen");
    expect(first.parameters?.batch).toBe(8);
  });
  it("requests cancellation without displaying terminal cancellation early", async () => {
    vi.mocked(trainingRunsApi.list).mockResolvedValue({ items: [run as never], total: 1 });
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("button", { name: "停止训练" }));
    await waitFor(() => expect(trainingRunsApi.cancel).toHaveBeenCalledWith("project", "run"));
    expect(screen.queryByText("已取消")).not.toBeInTheDocument();
  });
  it("offers resume only for the current attempt with an incomplete checkpoint", async () => {
    const failed = { ...run, status: "failed" };
    vi.mocked(trainingRunsApi.list).mockResolvedValue({ items: [failed as never], total: 1 });
    vi.mocked(trainingRunsApi.get).mockResolvedValue({
      run: failed as never,
      attempts: [
        {
          id: "attempt",
          ordinal: 1,
          status: "failed",
          artifacts: { "checkpoint-0002.pt": { role: "checkpoint", epoch: 2 } },
        } as never,
      ],
    });
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("button", { name: "车辆训练" }));
    await user.click(await screen.findByRole("button", { name: "从检查点续训" }));
    await waitFor(() => expect(trainingRunsApi.resume).toHaveBeenCalledWith("project", "run"));
  });
});

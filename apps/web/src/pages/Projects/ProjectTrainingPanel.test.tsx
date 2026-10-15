import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
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
const run = {
  id: "run",
  name: "车辆训练",
  status: "training",
  metrics: { epoch: 2 },
  config: { framework: "ultralytics", parameters: { epochs: 10 } },
  created_at: "2026-10-14T12:00:00Z",
  current_attempt_id: "attempt",
};
function Location() {
  return <p>{useLocation().pathname}</p>;
}
function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ProjectTrainingPanel projectId="project" />
        <Location />
      </MemoryRouter>
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
describe("managed project training", () => {
  it("opens the independent project creation page", async () => {
    const user = userEvent.setup();
    mount();
    await waitFor(() => expect(screen.getByRole("button", { name: "新建实验" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "新建实验" }));
    expect(screen.getByText("/projects/project/learning/new")).toBeVisible();
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

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { learningPoliciesApi } from "@/api/learningPolicies";
import { trainingRunsApi } from "@/api/trainingRuns";
import { trainingDatasetsApi } from "@/api/trainingDatasets";
import { ProjectLearningPolicyPanel } from "./ProjectLearningPolicyPanel";
vi.mock("@/api/learningPolicies", () => ({
  learningPoliciesApi: {
    get: vi.fn(),
    save: vi.fn(),
    check: vi.fn(),
    disable: vi.fn(),
    triggers: vi.fn(),
    remove: vi.fn(),
  },
}));
vi.mock("@/api/trainingRuns", () => ({ trainingRunsApi: { catalog: vi.fn() } }));
vi.mock("@/api/trainingDatasets", () => ({ trainingDatasetsApi: { list: vi.fn() } }));
vi.mock("@/components/ui/decisionDialog", () => ({
  confirmDialog: vi.fn().mockResolvedValue(true),
}));
const clients: QueryClient[] = [];
function mount() {
  const c = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  clients.push(c);
  return render(
    <QueryClientProvider client={c}>
      <ProjectLearningPolicyPanel projectId="project" />
    </QueryClientProvider>,
  );
}
const policy = {
  id: "policy",
  revision: 3,
  enabled: true,
  state: "active",
  message: "自动实验正在进行",
  baseline_dataset_id: null,
  config: {
    parent_id: "data",
    batch_id: null,
    template: { framework: "ultralytics", base_model: { id: "yolo11n" } },
    parameters: { epochs: 2, batch: 1, imgsz: 128, lr0: 0.01, seed: 42, patience: 2 },
    min_changed_samples: 10,
    check_interval_minutes: 5,
    cooldown_minutes: 60,
  },
};
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(learningPoliciesApi.get).mockResolvedValue(null);
  vi.mocked(learningPoliciesApi.triggers).mockResolvedValue({ total: 0, items: [] });
  vi.mocked(trainingDatasetsApi.list).mockResolvedValue({
    total: 1,
    items: [{ id: "data", name: "首轮训练数据", status: "ready" }],
  } as never);
  vi.mocked(trainingRunsApi.catalog).mockResolvedValue({
    enabled: true,
    items: [{ framework: "ultralytics", version: "8.4.177", base_models: [{ id: "yolo11n" }] }],
  } as never);
  vi.mocked(learningPoliciesApi.save).mockResolvedValue(policy as never);
  vi.mocked(learningPoliciesApi.disable).mockResolvedValue({ ...policy, enabled: false } as never);
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((c) => c.clear());
});
describe("ProjectLearningPolicyPanel", () => {
  it("requires an explicit threshold and keeps new policies disabled unless checked", async () => {
    const u = userEvent.setup();
    mount();
    await u.click(await screen.findByRole("button", { name: "配置自动策略" }));
    const save = screen.getByRole("button", { name: "保存自动策略" });
    expect(save).toBeDisabled();
    expect(screen.getByLabelText("启用自动训练")).not.toBeChecked();
    await u.selectOptions(screen.getByLabelText("初始划分数据"), "data");
    expect(save).toBeDisabled();
    await u.type(screen.getByLabelText("变化样本门槛"), "10");
    await u.click(save);
    await waitFor(() => expect(learningPoliciesApi.save).toHaveBeenCalled());
    expect(vi.mocked(learningPoliciesApi.save).mock.calls[0][1]).toMatchObject({
      enabled: false,
      expected_revision: 0,
      min_changed_samples: 10,
      parent_id: "data",
    });
  });
  it("preserves a draft and its original revision after a concurrent update", async () => {
    const u = userEvent.setup();
    vi.mocked(learningPoliciesApi.get).mockResolvedValue(policy as never);
    vi.mocked(learningPoliciesApi.save).mockRejectedValue(
      new Error("策略已被其他操作修改，请刷新"),
    );
    mount();
    await u.click(await screen.findByRole("button", { name: "编辑自动策略" }));
    await u.clear(screen.getByLabelText("变化样本门槛"));
    await u.type(screen.getByLabelText("变化样本门槛"), "25");
    await u.click(screen.getByRole("button", { name: "保存自动策略" }));
    await screen.findByText("策略已被其他操作修改，请刷新");
    expect(screen.getByLabelText("变化样本门槛")).toHaveValue(25);
    expect(vi.mocked(learningPoliciesApi.save).mock.calls[0][1].expected_revision).toBe(3);
  });
  it("can disable existing policies when the training catalog is unavailable", async () => {
    const u = userEvent.setup();
    vi.mocked(learningPoliciesApi.get).mockResolvedValue(policy as never);
    vi.mocked(trainingRunsApi.catalog).mockResolvedValue({ enabled: false, items: [] } as never);
    mount();
    await u.click(await screen.findByRole("button", { name: "关闭自动训练" }));
    await waitFor(() => expect(learningPoliciesApi.disable).toHaveBeenCalledWith("project", 3));
    expect(learningPoliciesApi.save).not.toHaveBeenCalled();
  });
});

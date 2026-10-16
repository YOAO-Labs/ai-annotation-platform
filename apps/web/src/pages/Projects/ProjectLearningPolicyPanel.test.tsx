import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { learningPoliciesApi } from "@/api/learningPolicies";
import { trainingRunsApi } from "@/api/trainingRuns";
import { trainingDatasetsApi } from "@/api/trainingDatasets";
import { trainingRecipesApi } from "@/api/trainingRecipes";
import { ProjectLearningPolicyPanel } from "./ProjectLearningPolicyPanel";
import { useAuthStore } from "@/stores/authStore";
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
vi.mock("@/api/trainingRecipes", async (original) => ({
  ...(await original<typeof import("@/api/trainingRecipes")>()),
  trainingRecipesApi: { list: vi.fn(), get: vi.fn(), revisions: vi.fn(), resolve: vi.fn() },
}));
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
  useAuthStore.getState().setAuth("policy-test", { id: "policy-owner" } as never);
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
  vi.mocked(trainingRecipesApi.list).mockResolvedValue({ total: 0, items: [] });
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((c) => c.clear());
});
describe("ProjectLearningPolicyPanel", () => {
  it("does not announce a missing policy or disabled executor while they are still loading", async () => {
    vi.mocked(learningPoliciesApi.get).mockReturnValue(new Promise(() => {}));
    vi.mocked(trainingRunsApi.catalog).mockReturnValue(new Promise(() => {}));
    mount();
    expect(screen.getByText("正在读取策略…")).toBeVisible();
    expect(screen.queryByText("尚未配置 · 默认关闭")).not.toBeInTheDocument();
    expect(screen.queryByText(/管理员配置受控训练执行器后/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "配置自动策略" })).toBeDisabled();
  });
  it("announces policy read failures without presenting the policy as unconfigured", async () => {
    vi.mocked(learningPoliciesApi.get).mockRejectedValue(new Error("策略读取失败"));
    mount();
    expect(await screen.findByText("策略状态读取失败")).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent("策略读取失败");
    expect(screen.queryByText("尚未配置 · 默认关闭")).not.toBeInTheDocument();
  });
  it("requires an explicit threshold and keeps new policies disabled unless checked", async () => {
    const u = userEvent.setup();
    mount();
    await u.click(await screen.findByRole("button", { name: "配置自动策略" }));
    const save = screen.getByRole("button", { name: "保存自动策略" });
    expect(save).toBeDisabled();
    expect(screen.getByLabelText("启用自动训练")).not.toBeChecked();
    await u.selectOptions(screen.getByLabelText("初始划分数据"), "data");
    await u.selectOptions(screen.getByLabelText("自动基础权重"), "yolo11n");
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
  it("preserves the full v2 configuration and pinned recipe when editing only thresholds", async () => {
    const configuration = {
      identity: { schema_id: "schema", schema_digest: "a".repeat(64) },
      user_values: { optimizer: "AdamW", mosaic: 0.25 },
      effective_values: { ...policy.config.parameters, optimizer: "AdamW", mosaic: 0.25 },
    };
    const origin = {
      kind: "recipe",
      recipe_id: "recipe",
      revision_id: "revision",
      digest: "b".repeat(64),
      label: "固定配方",
      ordinal: 1,
    };
    vi.mocked(learningPoliciesApi.get).mockResolvedValue({
      ...policy,
      config: { ...policy.config, configuration, origin },
    } as never);
    vi.mocked(trainingRecipesApi.get).mockResolvedValue({
      ...origin,
      head_revision: 2,
      archived: false,
      source_available: true,
      revision: { ordinal: 1, name: "固定配方" },
    } as never);
    const u = userEvent.setup();
    mount();
    await u.click(await screen.findByRole("button", { name: "编辑自动策略" }));
    expect(screen.getByLabelText("自动训练轮数")).toBeDisabled();
    await u.clear(screen.getByLabelText("变化样本门槛"));
    await u.type(screen.getByLabelText("变化样本门槛"), "25");
    await u.click(screen.getByRole("button", { name: "保存自动策略" }));
    await waitFor(() => expect(learningPoliciesApi.save).toHaveBeenCalled());
    const body = vi.mocked(learningPoliciesApi.save).mock.calls[0][1];
    expect(body.parameters).toBeUndefined();
    expect(body.configuration).toEqual({
      schema_id: "schema",
      schema_digest: "a".repeat(64),
      values: configuration.user_values,
    });
    expect(body.origin).toEqual({
      kind: "recipe",
      recipe_id: "recipe",
      revision_id: "revision",
      digest: "b".repeat(64),
    });
    expect(body.min_changed_samples).toBe(25);
  });
  it("changes one canonical field without resetting advanced values or creating other overrides", async () => {
    const configuration = {
      identity: { schema_id: "schema", schema_digest: "a".repeat(64) },
      user_values: { optimizer: "AdamW", mosaic: 0.25 },
      effective_values: { ...policy.config.parameters, optimizer: "AdamW", mosaic: 0.25 },
    };
    vi.mocked(learningPoliciesApi.get).mockResolvedValue({
      ...policy,
      config: { ...policy.config, configuration },
    } as never);
    const u = userEvent.setup();
    mount();
    await u.click(await screen.findByRole("button", { name: "编辑自动策略" }));
    await u.clear(screen.getByLabelText("自动训练轮数"));
    await u.type(screen.getByLabelText("自动训练轮数"), "4");
    await u.click(screen.getByRole("button", { name: "保存自动策略" }));
    await waitFor(() => expect(learningPoliciesApi.save).toHaveBeenCalled());
    expect(vi.mocked(learningPoliciesApi.save).mock.calls[0][1].configuration?.values).toEqual({
      optimizer: "AdamW",
      mosaic: 0.25,
      epochs: 4,
    });
  });
  it("does not reinterpret a blank numeric parameter as zero", async () => {
    vi.mocked(learningPoliciesApi.get).mockResolvedValue(policy as never);
    const u = userEvent.setup();
    mount();
    await u.click(await screen.findByRole("button", { name: "编辑自动策略" }));
    await u.clear(screen.getByLabelText("自动初始学习率"));
    await u.click(screen.getByRole("button", { name: "保存自动策略" }));
    await screen.findByText("lr0 必须为明确的有限数值");
    expect(learningPoliciesApi.save).not.toHaveBeenCalled();
  });
  it("keeps project weights when thresholds change or recipe origin is explicitly unlinked", async () => {
    const initialization = {
      kind: "trained_model",
      model_version_id: "11111111-1111-4111-8111-111111111111",
      source_digest: "c".repeat(64),
    };
    vi.mocked(learningPoliciesApi.get).mockResolvedValue({
      ...policy,
      config: {
        ...policy.config,
        configuration: {
          identity: { schema_id: "schema", schema_digest: "a".repeat(64) },
          user_values: {},
          effective_values: policy.config.parameters,
        },
        initialization,
        origin: {
          kind: "recipe",
          recipe_id: "recipe",
          revision_id: "revision",
          digest: "b".repeat(64),
        },
      },
    } as never);
    vi.mocked(trainingRecipesApi.get).mockResolvedValue({
      revision: { name: "旧配方", ordinal: 1 },
    } as never);
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("button", { name: "编辑自动策略" }));
    await user.click(screen.getByRole("button", { name: "解除策略配方" }));
    await user.clear(screen.getByLabelText("变化样本门槛"));
    await user.type(screen.getByLabelText("变化样本门槛"), "26");
    await user.click(screen.getByRole("button", { name: "保存自动策略" }));
    await waitFor(() => expect(learningPoliciesApi.save).toHaveBeenCalled());
    expect(vi.mocked(learningPoliciesApi.save).mock.calls[0][1]).toMatchObject({
      initialization,
      min_changed_samples: 26,
    });
    expect(vi.mocked(learningPoliciesApi.save).mock.calls[0][1].origin).toBeUndefined();
  });
});

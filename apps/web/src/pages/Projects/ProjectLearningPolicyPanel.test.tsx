import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
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
vi.mock("@/api/trainingRuns", () => ({
  trainingRunsApi: { catalog: vi.fn(), parameterSchema: vi.fn() },
}));
vi.mock("@/api/trainingDatasets", () => ({
  trainingDatasetsApi: { list: vi.fn(), getSummary: vi.fn() },
}));
vi.mock("@/api/trainingRecipes", async (original) => ({
  ...(await original<typeof import("@/api/trainingRecipes")>()),
  trainingRecipesApi: { list: vi.fn(), get: vi.fn(), revisions: vi.fn(), resolve: vi.fn() },
}));
vi.mock("@/components/ui/decisionDialog", () => ({
  confirmDialog: vi.fn().mockResolvedValue(true),
}));
const clients: QueryClient[] = [];
function mount(entry = "/", taskAvailability?: { detect: boolean; segment: boolean }) {
  const c = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  clients.push(c);
  return render(
    <QueryClientProvider client={c}>
      <MemoryRouter initialEntries={[entry]}>
        <ProjectLearningPolicyPanel projectId="project" taskAvailability={taskAvailability} />
      </MemoryRouter>
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
const segmentParent = {
  id: "mask-data",
  name: "原生实例掩码数据",
  status: "ready",
  task: "segment",
  contract_profile: {
    task: "segment",
    media_type: "image",
    source_media_type: "image",
    format: "aap-instance-rle",
    geometry: "instance-mask",
    bridge: "ultralytics-instance-rle-1",
  },
};
const polygonParent = {
  ...segmentParent,
  id: "polygon-data",
  name: "多边形分割数据",
  contract_profile: {
    ...segmentParent.contract_profile,
    format: "ultralytics-yolo",
    geometry: "normalized-simple-polygon",
    bridge: "ultralytics-simple-polygon-1",
  },
};
const segmentCatalog = {
  enabled: true,
  items: [
    {
      framework: "ultralytics",
      version: "8.4.177",
      base_models: [
        { id: "yolo11n", task: "detect" },
        {
          id: "yolo11n-seg",
          task: "segment",
          capabilities: { instance_rle: { bridge: "ultralytics-instance-rle-1" } },
        },
        { id: "yolo11s-seg", task: "segment" },
      ],
    },
    { framework: "libreyolo", version: "0.6", base_models: [] },
  ],
};
const segmentPolicy = {
  ...policy,
  id: "segment-policy",
  task: "segment",
  config: {
    ...policy.config,
    parent_id: "mask-data",
    bridge: "ultralytics-instance-rle-1",
    template: { framework: "ultralytics", base_model: { id: "yolo11n-seg" } },
  },
};
function prepareSegment() {
  vi.mocked(trainingRunsApi.catalog).mockResolvedValue(segmentCatalog as never);
  vi.mocked(trainingDatasetsApi.list).mockResolvedValue({
    total: 3,
    items: [segmentParent, polygonParent, { id: "detect-data", status: "ready", task: "detect" }],
  } as never);
  vi.mocked(trainingDatasetsApi.getSummary).mockImplementation(
    async (_project, id) => (id === polygonParent.id ? polygonParent : segmentParent) as never,
  );
}
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
  vi.mocked(trainingRunsApi.parameterSchema).mockResolvedValue({
    task: "segment",
    framework: "ultralytics",
    schema_id: "segment-schema",
    schema_digest: "a".repeat(64),
  } as never);
  vi.mocked(trainingDatasetsApi.getSummary).mockResolvedValue(segmentParent as never);
  vi.mocked(learningPoliciesApi.disable).mockResolvedValue({ ...policy, enabled: false } as never);
  vi.mocked(trainingRecipesApi.list).mockResolvedValue({ total: 0, items: [] });
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((c) => c.clear());
});
describe("ProjectLearningPolicyPanel", () => {
  it.each(["detect", "segment"] as const)(
    "checks %s recipe identity before adopting into a segmentation policy",
    async (recipeTask) => {
      prepareSegment();
      const initialization = {
        kind: "trained_model",
        model_version_id: "frozen-segment-model",
        model_name: "已训练分割模型",
        source_digest: "c".repeat(64),
        artifact: { sha256: "d".repeat(64) },
      };
      const recipe = {
        id: "recipe",
        head_revision: 1,
        archived: false,
        compatible: true,
        source_available: true,
        revision: {
          id: "revision",
          name: "分割配方",
          ordinal: 1,
          snapshot_digest: "b".repeat(64),
          snapshot: {
            framework: "ultralytics",
            framework_version: "8.4.177",
            base_model: {
              id: recipeTask === "segment" ? "yolo11n-seg" : "yolo11n",
              sha256: "e".repeat(64),
            },
            initialization,
            configuration: {
              identity: {
                task: recipeTask,
                schema_id: "segment-schema",
                schema_digest: "a".repeat(64),
              },
              user_values: { overlap_mask: false },
              effective_values: { ...policy.config.parameters, overlap_mask: false },
            },
          },
        },
      };
      vi.mocked(trainingRecipesApi.list).mockResolvedValue({ total: 1, items: [recipe] } as never);
      vi.mocked(trainingRecipesApi.get).mockResolvedValue(recipe as never);
      const user = userEvent.setup();
      mount("/?policy_task=segment");
      await user.click(await screen.findByRole("button", { name: "配置自动策略" }));
      await user.selectOptions(screen.getByLabelText("初始划分数据"), "mask-data");
      await user.type(screen.getByLabelText("变化样本门槛"), "14");
      await user.click(await screen.findByText("项目训练配方（1）"));
      await user.click(screen.getByRole("button", { name: "核对修订 1" }));
      if (recipeTask === "detect") {
        expect(await screen.findByText("配方任务与当前自动训练任务不同")).toBeVisible();
        expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
        expect(learningPoliciesApi.save).not.toHaveBeenCalled();
        return;
      }
      await user.click(await screen.findByRole("button", { name: "确认采用配置" }));
      expect(screen.getByLabelText("自动基础权重")).toHaveValue("yolo11n-seg");
      await user.click(screen.getByRole("button", { name: "保存自动策略" }));
      await waitFor(() => expect(learningPoliciesApi.save).toHaveBeenCalled());
      expect(vi.mocked(learningPoliciesApi.save).mock.calls[0][1]).toMatchObject({
        task: "segment",
        parent_id: "mask-data",
        min_changed_samples: 14,
        origin: {
          kind: "recipe",
          recipe_id: "recipe",
          revision_id: "revision",
          digest: "b".repeat(64),
        },
        initialization: {
          kind: "trained_model",
          model_version_id: initialization.model_version_id,
          source_digest: initialization.source_digest,
        },
        configuration: { schema_id: "segment-schema", values: { overlap_mask: false } },
      });
    },
  );
  it("saves a separate segmentation policy with a complete schema configuration", async () => {
    prepareSegment();
    const user = userEvent.setup();
    mount("/?policy_task=segment");
    await user.click(await screen.findByRole("button", { name: "配置自动策略" }));
    expect(learningPoliciesApi.get).toHaveBeenCalledWith("project", "segment");
    expect(learningPoliciesApi.triggers).toHaveBeenCalledWith("project", 0, "segment", 20);
    expect(trainingDatasetsApi.list).toHaveBeenCalledWith("project", 0, undefined, undefined, {
      task: "segment",
    });
    expect(within(screen.getByLabelText("自动训练器")).queryByText(/libreyolo/)).toBeNull();
    expect(within(screen.getByLabelText("自动基础权重")).queryByText("yolo11n")).toBeNull();
    expect(within(screen.getByLabelText("自动基础权重")).queryByText("yolo11s-seg")).toBeNull();
    await user.selectOptions(screen.getByLabelText("初始划分数据"), "mask-data");
    await user.selectOptions(screen.getByLabelText("自动基础权重"), "yolo11n-seg");
    await user.type(screen.getByLabelText("变化样本门槛"), "12");
    await user.click(screen.getByRole("button", { name: "保存自动策略" }));
    await waitFor(() => expect(learningPoliciesApi.save).toHaveBeenCalled());
    expect(vi.mocked(learningPoliciesApi.save).mock.calls[0][1]).toMatchObject({
      task: "segment",
      parent_id: "mask-data",
      base_model: "yolo11n-seg",
      enabled: false,
      expected_revision: 0,
      configuration: {
        schema_id: "segment-schema",
        schema_digest: "a".repeat(64),
        values: { epochs: 50, batch: 8, imgsz: 640, lr0: 0.01, seed: 42, patience: 50 },
      },
    });
    expect(vi.mocked(learningPoliciesApi.save).mock.calls[0][1].parameters).toBeUndefined();
  });
  it("rejects cross-bridge parents and preserves pinned segmentation configuration and weights", async () => {
    prepareSegment();
    const configuration = {
      identity: { task: "segment", schema_id: "segment-schema", schema_digest: "a".repeat(64) },
      user_values: { overlap_mask: false, mask_ratio: 2 },
      effective_values: { ...policy.config.parameters, overlap_mask: false, mask_ratio: 2 },
    };
    const initialization = {
      kind: "trained_model",
      model_version_id: "segment-model",
      source_digest: "c".repeat(64),
    };
    vi.mocked(learningPoliciesApi.get).mockResolvedValue({
      ...segmentPolicy,
      config: { ...segmentPolicy.config, configuration, initialization },
    } as never);
    const user = userEvent.setup();
    mount("/?policy_task=segment");
    await user.click(await screen.findByRole("button", { name: "编辑自动策略" }));
    expect(screen.getByRole("option", { name: /多边形分割数据.*训练桥接不同/ })).toBeDisabled();
    await user.clear(screen.getByLabelText("变化样本门槛"));
    await user.type(screen.getByLabelText("变化样本门槛"), "21");
    await user.click(screen.getByRole("button", { name: "保存自动策略" }));
    await waitFor(() => expect(learningPoliciesApi.save).toHaveBeenCalled());
    expect(vi.mocked(learningPoliciesApi.save).mock.calls[0][1]).toMatchObject({
      task: "segment",
      initialization,
      min_changed_samples: 21,
      configuration: {
        schema_id: "segment-schema",
        schema_digest: "a".repeat(64),
        values: configuration.user_values,
      },
    });
    expect(trainingRunsApi.parameterSchema).not.toHaveBeenCalled();
  });
  it("requires explicit native bridge qualification and a matching segmentation schema", async () => {
    prepareSegment();
    vi.mocked(trainingRunsApi.catalog).mockResolvedValue({
      ...segmentCatalog,
      items: [
        { ...segmentCatalog.items[0], base_models: [{ id: "yolo11n-seg", task: "segment" }] },
      ],
    } as never);
    vi.mocked(trainingRunsApi.parameterSchema).mockResolvedValue({
      task: "detect",
      framework: "ultralytics",
      schema_id: "wrong",
      schema_digest: "d".repeat(64),
    } as never);
    const user = userEvent.setup();
    mount("/?policy_task=segment");
    await user.click(await screen.findByRole("button", { name: "配置自动策略" }));
    await user.selectOptions(screen.getByLabelText("初始划分数据"), "mask-data");
    await user.selectOptions(screen.getByLabelText("自动基础权重"), "yolo11n-seg");
    await user.type(screen.getByLabelText("变化样本门槛"), "10");
    expect(await screen.findByText(/当前执行器未提供原生实例掩码桥接资格/)).toBeVisible();
    expect(await screen.findByText("训练配置与当前任务或训练器不一致")).toBeVisible();
    expect(screen.getByRole("button", { name: "保存自动策略" })).toBeDisabled();
    expect(learningPoliciesApi.save).not.toHaveBeenCalled();
  });
  it("uses the trigger history capacity for offsets and resets the page after changing it", async () => {
    vi.mocked(learningPoliciesApi.triggers).mockResolvedValue({ total: 65, items: [] });
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByText("自动触发记录（65）"));
    const size = screen.getByLabelText("每页自动触发记录数");
    await waitFor(() => expect(size).toBeEnabled());
    await user.selectOptions(size, "10");
    await waitFor(() =>
      expect(learningPoliciesApi.triggers).toHaveBeenLastCalledWith("project", 0, "detect", 10),
    );
    const next = screen.getByRole("button", { name: "下一页触发记录" });
    await waitFor(() => expect(next).toBeEnabled());
    await user.click(next);
    await waitFor(() =>
      expect(learningPoliciesApi.triggers).toHaveBeenLastCalledWith("project", 10, "detect", 10),
    );
    await waitFor(() => expect(size).toBeEnabled());
    await user.selectOptions(size, "50");
    await waitFor(() =>
      expect(learningPoliciesApi.triggers).toHaveBeenLastCalledWith("project", 0, "detect", 50),
    );
    expect(screen.getByRole("button", { name: "上一页触发记录" })).toBeDisabled();
  });
  it("resets drafts and paginated requests when changing task and ignores late trigger history", async () => {
    const late: { resolve?: (value: never) => void } = {};
    vi.mocked(learningPoliciesApi.get).mockResolvedValue(policy as never);
    vi.mocked(learningPoliciesApi.triggers).mockImplementation(async (_project, offset, task) => {
      if (offset === 20 && task !== "segment")
        return new Promise((resolve) => {
          late.resolve = resolve;
        });
      return { total: task === "segment" ? 0 : 21, items: [] };
    });
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("button", { name: "编辑自动策略" }));
    await user.clear(screen.getByLabelText("变化样本门槛"));
    await user.type(screen.getByLabelText("变化样本门槛"), "99");
    await user.click(screen.getByText("自动触发记录（21）"));
    await user.click(screen.getByRole("button", { name: "下一页触发记录" }));
    await waitFor(() =>
      expect(learningPoliciesApi.triggers).toHaveBeenCalledWith("project", 20, "detect", 20),
    );
    vi.mocked(learningPoliciesApi.get).mockResolvedValue(null);
    prepareSegment();
    await user.selectOptions(screen.getByLabelText("自动训练任务"), "segment");
    await waitFor(() =>
      expect(learningPoliciesApi.triggers).toHaveBeenCalledWith("project", 0, "segment", 20),
    );
    await user.click(await screen.findByRole("button", { name: "配置自动策略" }));
    expect(screen.getByLabelText("变化样本门槛")).toHaveValue(null);
    expect(screen.getByLabelText("初始划分数据")).toHaveValue("");
    await act(async () =>
      late.resolve?.({ total: 21, items: [{ id: "late", message: "过期检测记录" }] } as never),
    );
    expect(screen.queryByText("过期检测记录")).not.toBeInTheDocument();
    expect(screen.getByText("自动触发记录（0）")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "上一页触发记录" })).toBeDisabled();
  });
  it("ignores a detection policy response that arrives after the task changes", async () => {
    let resolve: ((value: never) => void) | undefined;
    vi.mocked(learningPoliciesApi.get).mockImplementation(async (_project, task) =>
      task === "segment"
        ? null
        : new Promise((done) => {
            resolve = done;
          }),
    );
    const user = userEvent.setup();
    mount();
    await user.selectOptions(screen.getByLabelText("自动训练任务"), "segment");
    await screen.findByText("尚未配置 · 默认关闭");
    await act(async () => resolve?.(policy as never));
    expect(screen.queryByText(policy.message)).not.toBeInTheDocument();
    expect(screen.getByText("尚未配置 · 默认关闭")).toBeVisible();
  });
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

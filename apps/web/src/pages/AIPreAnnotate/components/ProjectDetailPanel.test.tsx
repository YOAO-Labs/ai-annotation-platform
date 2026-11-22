import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HttpResponse, http } from "msw";
import type { MeResponse } from "@/api/auth";
import type { ModelCatalogResponse } from "@/api/modelCatalog";
import { server } from "@/mocks/server";
import type { asyncJobsApi } from "@/api/asyncJobs";
import type { PipelineStagePayload } from "@/hooks/usePreannotation";
import { useToastStore } from "@/components/ui/Toast";
import cropCases from "@/__fixtures__/crop-ocr-pipeline-cases.json";

const mockTriggerMutate = vi.fn();
const mockUseProject = vi.fn();
const mockUseBatches = vi.fn();
const mockUseMLBackends = vi.fn();
const mockUseTrigger = vi.fn();
const mockUseProjectPipelines = vi.fn();
const mockSummaryAPI = vi.fn();
const mockQueueAPI = vi.fn();
const mockAliasFreqAPI = vi.fn();
const mockSetupAPI = vi.fn();
const mockCapabilitiesAPI = vi.fn();
const mockUpdateProjectMutate = vi.fn();
const mockUpdateProjectMutateAsync = vi.fn();
const mockCreateProjectPipelineMutateAsync = vi.fn();
const mockApplyProjectPipelineMutate = vi.fn();
const mockApplyProjectPipelineMutateAsync = vi.fn();
const mockDeleteProjectPipelineMutate = vi.fn();
const mockUpdatePreferences = vi.fn();
const readModelCatalog = vi.fn<(pageSize: string | null) => ModelCatalogResponse["items"]>();
const mockAsyncJobGet = vi.hoisted(() => vi.fn<typeof asyncJobsApi.get>());

vi.mock("@/hooks/useProjects", () => ({
  useProject: (id: string) => mockUseProject(id),
  useProjects: () => ({ data: [], isLoading: false }),
  useUpdateProject: () => ({
    mutate: mockUpdateProjectMutate,
    mutateAsync: mockUpdateProjectMutateAsync,
    isPending: false,
  }),
}));
vi.mock("@/hooks/useBatchEventsSocket", () => ({
  useBatchEventsSocket: () => undefined,
}));
vi.mock("./PipelineGraphCanvas", () => ({
  default: ({
    onAddChild,
    onSelect,
  }: {
    onAddChild: (sid: string) => void;
    onSelect: (sid: string) => void;
  }) => (
    <div data-testid="pipeline-canvas">
      <button type="button" onClick={() => onAddChild("source")}>
        添加源模型下游
      </button>
      <button type="button" onClick={() => onSelect("source")}>
        查看源模型
      </button>
    </div>
  ),
}));
vi.mock("@/api/asyncJobs", async (importOriginal) => {
  const actual = await importOriginal<{ asyncJobsApi: typeof asyncJobsApi }>();
  return { ...actual, asyncJobsApi: { ...actual.asyncJobsApi, get: mockAsyncJobGet } };
});
vi.mock("@/hooks/useProjectPipelines", () => ({
  useProjectPipelines: (...args: unknown[]) => mockUseProjectPipelines(...args),
  useCreateProjectPipeline: () => ({
    mutateAsync: mockCreateProjectPipelineMutateAsync,
    isPending: false,
  }),
  useApplyProjectPipeline: () => ({
    mutate: mockApplyProjectPipelineMutate,
    mutateAsync: mockApplyProjectPipelineMutateAsync,
    isPending: false,
  }),
  useDeleteProjectPipeline: () => ({
    mutate: mockDeleteProjectPipelineMutate,
    isPending: false,
  }),
}));
vi.mock("@/hooks/useBatches", () => ({
  useBatches: (pid: string, status: string) => mockUseBatches(pid, status),
}));
vi.mock("@/hooks/useMLBackends", () => ({
  useMLBackends: (pid: string) => mockUseMLBackends(pid),
}));
vi.mock("@/hooks/usePreannotation", async () => {
  const actual = await vi.importActual<any>("@/hooks/usePreannotation");
  return {
    ...actual,
    useTriggerPreannotation: () => mockUseTrigger(),
    usePreannotationProgress: () => ({
      progress: null,
      connection: "idle",
      retries: 0,
    }),
  };
});
vi.mock("@/api/adminPreannotate", async () => {
  const actual = await vi.importActual<any>("@/api/adminPreannotate");
  return {
    ...actual,
    adminPreannotateApi: {
      ...actual.adminPreannotateApi,
      summary: () => mockSummaryAPI(),
      queue: (limit: number) => mockQueueAPI(limit),
      projectQueue: (projectId: string) => mockQueueAPI(projectId),
    },
  };
});
vi.mock("@/api/aliasFrequency", () => ({
  aliasFrequencyApi: {
    byProject: (pid: string) => mockAliasFreqAPI(pid),
  },
}));
vi.mock("@/api/ml-backends", () => ({
  mlBackendsApi: {
    setup: (projectId: string, backendId: string) => mockSetupAPI(projectId, backendId),
    capabilities: (projectId: string, backendId: string) =>
      mockCapabilitiesAPI(projectId, backendId),
  },
  mlBackendSetupQueryKey: (p: unknown, b: unknown) => ["ml-backends", p, b, "setup"],
}));
vi.mock("@/api/auth", () => ({
  authApi: {
    getPreferences: vi.fn().mockResolvedValue({ ai: { params_by_backend: {} } }),
    updatePreferences: (payload: unknown) => mockUpdatePreferences(payload),
  },
}));

import { ProjectDetailPanel } from "./ProjectDetailPanel";
import { useAuthStore } from "@/stores/authStore";

function setAuthRole(role: string) {
  useAuthStore.setState({
    user: {
      id: `u-${role}`,
      email: `${role}@example.test`,
      name: role,
      role,
      group_name: null,
      status: "online",
      created_at: "2026-01-01T00:00:00Z",
    } satisfies MeResponse,
  });
}

function renderUI(extras: Partial<{ summary: any }> = {}) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const view = render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <ProjectDetailPanel projectId="p1" onBack={() => {}} summary={extras.summary} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...view, qc };
}

describe("ProjectDetailPanel", () => {
  beforeEach(() => {
    readModelCatalog.mockReset().mockReturnValue([]);
    server.use(
      http.get("*/api/v1/projects/p1/model-catalog", ({ request }) => {
        const items = readModelCatalog(new URL(request.url).searchParams.get("page_size"));
        return HttpResponse.json<ModelCatalogResponse>({
          items,
          total: items.length,
          page: 1,
          page_size: 200,
        });
      }),
    );
    setAuthRole("super_admin");
    mockAsyncJobGet.mockReset();
    mockAsyncJobGet.mockImplementation(async (id) => ({
      id,
      status: "pending",
      kind: "batch_predict",
      project_id: "p1",
      user_id: "u-super_admin",
      project_display_id: null,
      project_name: null,
      progress_pct: 0,
      payload: {},
      result: {},
      error_message: null,
      celery_task_id: null,
      started_at: null,
      completed_at: null,
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
    }));
    sessionStorage.removeItem("ai-annotation:variant-hot-cache:v1");
    mockTriggerMutate.mockReset();
    mockSetupAPI.mockReset();
    mockCapabilitiesAPI.mockReset();
    mockUpdateProjectMutate.mockReset();
    mockUpdateProjectMutateAsync.mockReset();
    mockUpdateProjectMutateAsync.mockResolvedValue(undefined);
    mockCapabilitiesAPI.mockResolvedValue({ name: "grounded-sam2", models: [] });
    mockUpdatePreferences.mockReset();
    mockUseProject.mockReturnValue({
      data: { type_key: "image-det", ml_backend_id: "bk1" },
      isLoading: false,
    });
    mockUseProjectPipelines.mockReset();
    mockUseProjectPipelines.mockReturnValue({ data: [], isLoading: false });
    mockCreateProjectPipelineMutateAsync.mockReset();
    mockCreateProjectPipelineMutateAsync.mockResolvedValue({
      id: "pipe-created",
      name: "项目默认编排",
      scope: "private",
      project_id: "p1",
      organization_id: null,
      stages: [{ stage: 0, ml_backend_id: "bk1" }],
      is_default: true,
    });
    mockApplyProjectPipelineMutate.mockReset();
    mockApplyProjectPipelineMutateAsync.mockReset();
    mockApplyProjectPipelineMutateAsync.mockResolvedValue({
      id: "pipe-applied",
      name: "项目默认编排",
      scope: "private",
      project_id: "p1",
      organization_id: null,
      stages: [{ stage: 0, ml_backend_id: "bk1" }],
      is_default: true,
    });
    mockDeleteProjectPipelineMutate.mockReset();
    mockUseBatches.mockReturnValue({
      data: [
        {
          id: "b1",
          display_id: "B-1",
          name: "批次甲",
          status: "active",
          annotator_id: "u-anno",
          reviewer_id: null,
          total_tasks: 10,
        },
        {
          id: "b2",
          display_id: "B-2",
          name: "批次乙",
          status: "active",
          annotator_id: null,
          reviewer_id: null,
          total_tasks: 20,
        },
      ],
      isLoading: false,
    });
    mockUseMLBackends.mockReturnValue({
      data: [{ id: "bk1", name: "grounded-sam2" }],
      isLoading: false,
    });
    mockUseTrigger.mockReturnValue({
      mutateAsync: mockTriggerMutate.mockResolvedValue({
        job_id: "j1",
        total_tasks: 10,
      }),
      isPending: false,
    });
    mockSummaryAPI.mockResolvedValue({ items: [] });
    mockQueueAPI.mockResolvedValue({ items: [] });
    mockAliasFreqAPI.mockReset();
    mockAliasFreqAPI.mockResolvedValue({
      project_id: "p1",
      total_predictions: 0,
      frequency: {},
      last_computed_at: new Date().toISOString(),
    });
    mockSetupAPI.mockResolvedValue({
      name: "grounded-sam2",
      supported_prompts: ["text"],
      supported_variants: [
        {
          key: "sam_variant",
          title: "SAM 2",
          variants: [
            { value: "tiny", label: "Tiny", vram_gb: 1.5, tier: "fast" },
            { value: "large", label: "Large", vram_gb: 6, tier: "accurate" },
          ],
        },
        {
          key: "dino_variant",
          title: "DINO",
          variants: [
            { value: "T", label: "Tiny", vram_gb: 1.5, tier: "fast" },
            { value: "B", label: "Base", vram_gb: 3.5, tier: "accurate" },
          ],
        },
      ],
      params: {
        type: "object",
        properties: {
          box_threshold: { type: "number", default: 0.35, minimum: 0, maximum: 1 },
          sam_variant: {
            type: "string",
            enum: ["tiny", "large"],
            default: "tiny",
            title: "SAM 2 变体",
          },
          dino_variant: {
            type: "string",
            enum: ["T", "B"],
            default: "T",
            title: "DINO 变体",
          },
        },
      },
    });
    mockUpdatePreferences.mockResolvedValue(undefined);
  });

  afterEach(() => {
    sessionStorage.removeItem("ai-annotation:variant-hot-cache:v1");
    vi.restoreAllMocks();
  });

  function setupCropPipeline(cropCase: (typeof cropCases.cases)[number]) {
    const recognition = { ...cropCases.recognizer, ...cropCase.recognition };
    const downstream = [...(cropCase.sibling ? [cropCase.sibling] : []), recognition];
    readModelCatalog.mockReturnValue([
      {
        id: "builtin-ocr",
        source: "builtin",
        publication: "builtin",
        media_type: "image",
        task: "ocr",
        model_id: recognition.id,
        name: recognition.display_name,
        capabilities: { id: recognition.id, task: "ocr", supported_inputs: ["crop"] },
        class_metadata: { class_source: "builtin_manifest", verification_status: "uninspected" },
        operations: { pipeline: { allowed: true } },
      },
    ]);
    mockUseMLBackends.mockReturnValue({
      data: [
        { id: "bk1", name: "检测服务" },
        { id: "bk2", name: "识别服务" },
      ],
      isLoading: false,
    });
    mockCapabilitiesAPI.mockImplementation(async (_pid: string, backendId: string) => ({
      models:
        backendId === "bk1"
          ? [cropCases.source, ...(cropCase.sibling ? downstream : [])]
          : downstream,
    }));
    return renderUI();
  }

  it.each(cropCases.cases)("提交 OCR 裁剪编排：$name", async (cropCase) => {
    setupCropPipeline(cropCase);
    fireEvent.click(await screen.findByRole("button", { name: /plate.*\[4\]/ }));
    expect(screen.getByRole("button", { name: /✓.*plate.*\[4\]/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "添加源模型下游" }));
    if (cropCase.sibling) {
      fireEvent.change(screen.getByRole("combobox", { name: "模型服务" }), {
        target: { value: "bk1" },
      });
      fireEvent.change(await screen.findByRole("combobox", { name: "下游模型" }), {
        target: { value: "ocr-rec" },
      });
      expect(screen.queryByRole("option", { name: /ocr-e2e/ })).toBeNull();
    }
    await screen.findByText("识别");
    const form = within(screen.getByRole("group", { name: "推理参数" }));
    const axis = form.getByTestId(`ai-variant-${cropCase.edit_axis}`);
    const defaults: Record<string, string | undefined> = cropCase.recognition.default_variants;
    expect(axis).toHaveValue(defaults[cropCase.edit_axis]);
    const variants = within(form.getByTestId("ai-variant-selector")).getAllByRole("combobox");
    expect(variants.map((control) => control.dataset.testid)).toEqual(
      cropCase.recognition.supported_variants.map(({ key }) => `ai-variant-${key}`),
    );
    const options = cropCase.recognition.supported_variants.find(
      ({ key }) => key === cropCase.edit_axis,
    )?.variants;
    expect(
      within(axis)
        .getAllByRole("option")
        .map((option) => option.getAttribute("value")),
    ).toEqual(options?.map(({ value }) => value));
    expect(form.queryByTestId("schema-field-det_db_thresh")).toBeNull();
    expect(screen.queryByText(/不产类别属性/)).toBeNull();
    fireEvent.change(axis, { target: { value: cropCase.edited_value } });
    for (const [key, value] of Object.entries(cropCase.expected_child.model_variants)) {
      expect(form.getByTestId(`ai-variant-${key}`)).toHaveValue(value);
    }
    const param = within(form.getByTestId(`schema-field-${cropCase.params_key}`));
    fireEvent.change(param.getByRole("spinbutton"), { target: { value: "3" } });
    fireEvent.click(screen.getByRole("button", { name: "plate" }));
    fireEvent.click(screen.getByRole("button", { name: "识别文本" }));

    fireEvent.click(screen.getByRole("button", { name: "查看源模型" }));
    expect(screen.getByRole("button", { name: /✓.*plate.*\[4\]/ })).toBeInTheDocument();
    if (cropCase.sibling) {
      expect(screen.getByRole("combobox", { name: "模型任务" })).toHaveValue("plate-detect");
    }
    fireEvent.click(screen.getByRole("checkbox", { name: "选择 批次甲" }));
    fireEvent.click(screen.getByRole("button", { name: /跑预标.*1 批/ }));
    await waitFor(() => expect(mockTriggerMutate).toHaveBeenCalledTimes(1));
    const { pipeline_stages: stages } = mockTriggerMutate.mock.calls[0][0] as {
      pipeline_stages: PipelineStagePayload[];
    };
    expect(stages).toHaveLength(2);
    expect(stages[0]).toMatchObject({
      stage: 0,
      model_id: "plate-detect",
      model_variants: cropCases.source.default_variants,
      class_filter: [4],
    });
    expect(JSON.parse(JSON.stringify(stages[1]))).toEqual({
      ...cropCase.expected_child,
      stage: 1,
      parent_stage: 0,
      ml_backend_id: cropCase.sibling ? "bk1" : "bk2",
    });
    expect(mockUpdateProjectMutate).not.toHaveBeenCalled();
    expect(mockUpdatePreferences).not.toHaveBeenCalled();
  });

  it("切换源模型变体清除旧类别索引，切回时不复用", async () => {
    setupCropPipeline(cropCases.cases[0]);
    fireEvent.click(await screen.findByRole("button", { name: /plate.*\[4\]/ }));
    expect(screen.getByRole("button", { name: /✓.*plate.*\[4\]/ })).toBeInTheDocument();
    const form = within(screen.getByRole("group", { name: "推理参数" }));
    const size = form.getByTestId("ai-variant-size");
    fireEvent.change(size, { target: { value: "large" } });
    expect(size).toHaveValue("large");
    expect(screen.queryByRole("button", { name: /✓.*plate.*\[4\]/ })).toBeNull();
    fireEvent.change(size, { target: { value: "small" } });
    expect(size).toHaveValue("small");
    expect(screen.queryByRole("button", { name: /✓.*plate.*\[4\]/ })).toBeNull();
  });

  it.each([
    { name: "后台失败", status: "failed", result: {}, nextSize: "small", stale: false },
    {
      name: "全部跳过",
      status: "completed",
      result: { success_count: 0, failed_count: 0, done_count: 0, skipped_count: 10 },
      nextSize: "large",
      stale: false,
    },
    {
      name: "完成后已驱逐",
      status: "completed",
      result: { success_count: 10, failed_count: 0, done_count: 10, skipped_count: 0 },
      nextSize: "small",
      stale: true,
    },
  ])("派发不推定模型驻留：$name", async ({ status, result, nextSize, stale }) => {
    const legacyKey = "ai-annotation:variant-hot-cache:v1";
    const oldRecord = stale ? JSON.stringify({ hot: ["bk1|size=small"] }) : null;
    if (oldRecord) sessionStorage.setItem(legacyKey, oldRecord);
    const pushToast = vi.spyOn(useToastStore.getState(), "push");
    const { qc } = setupCropPipeline(cropCases.cases[0]);
    await screen.findByRole("button", { name: /plate.*\[4\]/ });
    fireEvent.click(screen.getByRole("button", { name: "添加源模型下游" }));
    await screen.findByText("识别");
    fireEvent.click(screen.getByRole("button", { name: "查看源模型" }));
    function submit() {
      type Receipt = { job_id: string; status: string };
      let resolve!: (receipt: Receipt) => void;
      const response = new Promise<Receipt>((done) => {
        resolve = done;
      });
      mockTriggerMutate.mockReturnValueOnce(response);
      fireEvent.click(screen.getByRole("checkbox", { name: "选择 批次甲" }));
      fireEvent.click(screen.getByRole("button", { name: /跑预标.*1 批/ }));
      expect(screen.getByRole("button", { name: "提交中…" })).toBeDisabled();
      expect(screen.queryByRole("button", { name: /加载模型|预热/ })).toBeNull();
      return resolve;
    }
    const first = submit();
    await act(async () => first({ job_id: "j1", status: "queued" }));
    await waitFor(() =>
      expect(qc.getQueryData(["async-job", "j1"])).toMatchObject({ status: "pending" }),
    );
    expect(screen.queryByText("运行中")).toBeNull();
    expect(pushToast).toHaveBeenCalledWith(
      expect.objectContaining({
        msg: "预标已提交",
        sub: "1 个批次已受理 · 0 个批次提交失败",
      }),
    );
    expect(sessionStorage.getItem(legacyKey)).toBe(oldRecord);
    act(() => {
      qc.setQueryData(["async-job", "j1"], { id: "j1", status, result });
    });
    if (nextSize === "large") {
      fireEvent.change(screen.getByTestId("ai-variant-size"), { target: { value: nextSize } });
    }
    const second = submit();
    expect(mockTriggerMutate).toHaveBeenNthCalledWith(
      2,
      expect.objectContaining({ model_variants: { size: nextSize } }),
    );
    await act(async () => second({ job_id: "j2", status: "queued" }));
    expect(sessionStorage.getItem(legacyKey)).toBe(oldRecord);
  });

  it("渲染 header 含项目名 + ml_backend chip", () => {
    renderUI({ summary: { project_name: "Demo", project_display_id: "P-9" } });
    expect(screen.getByText("Demo")).toBeInTheDocument();
    expect(screen.getByText(/P-9/)).toBeInTheDocument();
    expect(screen.getByText("grounded-sam2")).toBeInTheDocument();
  });

  it("项目预标注入口不承载全局 backend/model 池", () => {
    renderUI();
    expect(screen.queryByText("全局 backend/model 池")).toBeNull();
    expect(screen.queryByRole("button", { name: /保存公共编排|保存全局编排/ })).toBeNull();
  });

  it("空选中时配置区常驻 · prompt 表单仍显示但运行按钮禁用", () => {
    renderUI();
    expect(screen.getByPlaceholderText(/car, person/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /跑预标/ })).toBeDisabled();
  });

  it("选中 1 个 batch 后显示 prompt 表单 (无并发模式)", () => {
    renderUI();
    const batchChecks = screen.getAllByRole("checkbox", { name: /选择/ });
    fireEvent.click(batchChecks[0]);
    expect(screen.getByPlaceholderText(/car, person/)).toBeInTheDocument();
    expect(screen.queryByRole("radiogroup", { name: /并发模式/ })).toBeNull();
  });

  it("选中 ≥2 个 batch 时出现串/并行单选", () => {
    renderUI();
    fireEvent.click(screen.getByRole("checkbox", { name: /全选可预标批次/ }));
    expect(screen.getByRole("radiogroup", { name: /并发模式/ })).toBeInTheDocument();
  });

  it("批次查询限定 active,draft, 避免拉取全量批次历史", () => {
    renderUI();
    expect(mockUseBatches).toHaveBeenCalledWith("p1", "active,draft");
  });

  it("批次列表放行未分派 draft, 过滤已分派 draft 与人工流程批次", () => {
    mockUseBatches.mockReturnValue({
      data: [
        {
          id: "b-active",
          display_id: "B-A",
          name: "活动批次",
          status: "active",
          annotator_id: "u-anno",
          reviewer_id: null,
          total_tasks: 5,
        },
        {
          id: "b-draft-free",
          display_id: "B-D1",
          name: "未分派草稿",
          status: "draft",
          annotator_id: null,
          reviewer_id: null,
          total_tasks: 8,
        },
        {
          id: "b-draft-assigned",
          display_id: "B-D2",
          name: "已分派草稿",
          status: "draft",
          annotator_id: "u-anno",
          reviewer_id: null,
          total_tasks: 3,
        },
        {
          id: "b-annotating",
          display_id: "B-N",
          name: "标注中批次",
          status: "annotating",
          annotator_id: "u-anno",
          reviewer_id: null,
          total_tasks: 2,
        },
      ],
      isLoading: false,
    });
    renderUI();
    expect(screen.getByText("未分派草稿")).toBeInTheDocument();
    expect(screen.getByText("草稿·未分派")).toBeInTheDocument();
    expect(screen.queryByText("已分派草稿")).toBeNull();
    expect(screen.queryByText("标注中批次")).toBeNull();
    fireEvent.click(screen.getByRole("checkbox", { name: /全选可预标批次/ }));
    expect(screen.getByRole("checkbox", { name: /选择 活动批次/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /选择 未分派草稿/ })).toBeChecked();
  });

  it("串行模式: 多 batch 顺序触发 trigger.mutateAsync", async () => {
    renderUI();
    fireEvent.click(screen.getByRole("checkbox", { name: /全选可预标批次/ }));
    fireEvent.change(screen.getByPlaceholderText(/car, person/), {
      target: { value: "car" },
    });
    fireEvent.click(screen.getByRole("button", { name: /跑预标.*2 批/ }));

    await waitFor(() => {
      expect(mockTriggerMutate).toHaveBeenCalledTimes(2);
    });
    expect(mockTriggerMutate).toHaveBeenNthCalledWith(
      1,
      expect.objectContaining({ batch_id: "b1" }),
    );
    expect(mockTriggerMutate).toHaveBeenNthCalledWith(
      2,
      expect.objectContaining({ batch_id: "b2" }),
    );
  });

  it("并行模式: 同时触发 N 次 mutateAsync", async () => {
    renderUI();
    fireEvent.click(screen.getByRole("checkbox", { name: /全选可预标批次/ }));
    fireEvent.change(screen.getByPlaceholderText(/car, person/), {
      target: { value: "car, person" },
    });
    fireEvent.click(screen.getByLabelText(/并行/));
    fireEvent.click(screen.getByRole("button", { name: /跑预标.*2 批/ }));

    await waitFor(() => {
      expect(mockTriggerMutate).toHaveBeenCalledTimes(2);
    });
  });

  it("aliases 加载后默认填充 prompt (按预标频率降序)", async () => {
    mockUseProject.mockReturnValue({
      data: {
        type_key: "image-det",
        ml_backend_id: "bk1",
        classes_config: {
          car: { alias: "car" },
          person: { alias: "person" },
          truck: { alias: "truck" },
        },
      },
      isLoading: false,
    });
    mockAliasFreqAPI.mockResolvedValue({
      project_id: "p1",
      total_predictions: 100,
      frequency: { person: 50, car: 30, truck: 5 },
      last_computed_at: new Date().toISOString(),
    });
    renderUI();
    fireEvent.click(screen.getAllByRole("checkbox", { name: /选择/ })[0]);
    await waitFor(() => {
      const ta = screen.getByPlaceholderText(/car, person/) as HTMLTextAreaElement;
      expect(ta.value).toBe("person, car, truck");
    });
  });

  it("标注员不请求仅管理员可读的 alias 频率", async () => {
    setAuthRole("annotator");

    renderUI();

    await waitFor(() => expect(mockSetupAPI).toHaveBeenCalled());
    expect(mockAliasFreqAPI).not.toHaveBeenCalled();
  });

  it("用户已手填 prompt 时不被 alias 默认覆盖", async () => {
    mockUseProject.mockReturnValue({
      data: {
        type_key: "image-det",
        ml_backend_id: "bk1",
        classes_config: { car: { alias: "car" } },
      },
      isLoading: false,
    });
    mockAliasFreqAPI.mockResolvedValue({
      project_id: "p1",
      total_predictions: 0,
      frequency: {},
      last_computed_at: new Date().toISOString(),
    });
    renderUI();
    fireEvent.click(screen.getAllByRole("checkbox", { name: /选择/ })[0]);
    const ta = screen.getByPlaceholderText(/car, person/) as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "我手填的" } });
    await waitFor(() => expect(ta.value).toBe("我手填的"));
  });

  it("ai-pre 变体选择随 params 透传到预标请求", async () => {
    renderUI();
    fireEvent.click(screen.getAllByRole("checkbox", { name: /选择/ })[0]);
    fireEvent.change(screen.getByPlaceholderText(/car, person/), {
      target: { value: "car" },
    });

    await waitFor(() => {
      expect(screen.getByTestId("ai-variant-sam_variant")).toBeInTheDocument();
    });
    fireEvent.change(screen.getByTestId("ai-variant-sam_variant"), {
      target: { value: "large" },
    });
    fireEvent.change(screen.getByTestId("ai-variant-dino_variant"), {
      target: { value: "B" },
    });
    fireEvent.click(screen.getByRole("button", { name: /跑预标.*1 批/ }));

    await waitFor(() => {
      expect(mockTriggerMutate).toHaveBeenCalledWith(
        expect.objectContaining({
          model_variants: expect.objectContaining({
            sam_variant: "large",
            dino_variant: "B",
          }),
          params: expect.objectContaining({ box_threshold: 0.35 }),
        }),
      );
    });
  });

  it("保存当前配置时写入命名项目编排并设为默认", async () => {
    renderUI();
    fireEvent.change(screen.getByPlaceholderText(/car, person/), {
      target: { value: "car" },
    });
    fireEvent.change(screen.getByPlaceholderText(/例如 detect/), {
      target: { value: "车辆属性" },
    });
    fireEvent.click(screen.getByRole("button", { name: /保存为命名编排/ }));

    await waitFor(() => {
      expect(mockCreateProjectPipelineMutateAsync).toHaveBeenCalledWith(
        expect.objectContaining({
          name: "车辆属性",
          scope: "private",
          project_id: "p1",
          is_default: true,
          stages: [
            expect.objectContaining({
              stage: 0,
              ml_backend_id: "bk1",
            }),
          ],
        }),
      );
    });
    expect(mockApplyProjectPipelineMutateAsync).not.toHaveBeenCalled();
  });

  it("可从命名编排库套用为当前项目默认", async () => {
    mockUseProjectPipelines.mockReturnValue({
      data: [
        {
          id: "pipe-public",
          name: "detect-to-attr",
          scope: "public",
          project_id: null,
          organization_id: null,
          stages: [{ stage: 0, ml_backend_id: "bk1" }],
          is_default: false,
        },
      ],
      isLoading: false,
    });
    renderUI();

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /套用为默认/ })).toBeEnabled();
    });
    fireEvent.click(screen.getByRole("button", { name: /套用为默认/ }));
    expect(mockApplyProjectPipelineMutate).toHaveBeenCalledWith(
      { pipelineId: "pipe-public", setDefault: true },
      expect.any(Object),
    );
  });

  it("点返回按钮触发 onBack", () => {
    const onBack = vi.fn();
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ProjectDetailPanel projectId="p1" onBack={onBack} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: /返回项目列表/ }));
    expect(onBack).toHaveBeenCalled();
  });

  describe("多阶段下游 backend 门控", () => {
    it("没有兼容目录模型时提示启用或发布兼容模型", async () => {
      renderUI();
      await waitFor(() => expect(readModelCatalog.mock.calls).toEqual([["200"]]));
      expect(screen.getByText(/暂无兼容的下游模型/)).toBeInTheDocument();
    });

    it("双 backend 项目: 无单 backend 提示, 显示编排空态引导", () => {
      mockUseMLBackends.mockReturnValue({
        data: [
          { id: "bk1", name: "grounded-sam2" },
          { id: "bk2", name: "onnxtools" },
        ],
        isLoading: false,
      });
      renderUI();
      expect(screen.queryByText(/暂无兼容的下游模型/)).toBeNull();
      expect(screen.getByText(/加下游阶段/)).toBeInTheDocument();
    });
  });

  describe("OCR / 文档版面任务类型", () => {
    function withOcrCapabilities() {
      mockCapabilitiesAPI.mockResolvedValue({
        name: "doc-ai",
        models: [
          {
            id: "m-ocr",
            task: "ocr",
            display_name: "OCR",
            params: {
              type: "object",
              properties: { det_db_thresh: { type: "number", default: 0.3 } },
            },
          },
          { id: "m-layout", task: "doc_layout", display_name: "版面" },
        ],
      });
    }

    it("无 ocr / doc_layout 模型时,文本路径正常出 prompt(无 OCR 选项)", async () => {
      renderUI();
      fireEvent.click(screen.getAllByRole("checkbox", { name: /选择/ })[0]);
      await waitFor(() => {
        expect(screen.getByPlaceholderText(/car, person/)).toBeInTheDocument();
      });
      expect(screen.queryByRole("option", { name: /OCR/ })).toBeNull();
    });

    it("OCR backend: 统一「模型任务」下拉列出 OCR/版面,默认落 OCR 后隐藏 prompt + 显示静态提示", async () => {
      withOcrCapabilities();
      renderUI();
      fireEvent.click(screen.getAllByRole("checkbox", { name: /选择/ })[0]);
      await waitFor(() => {
        expect(screen.getByRole("option", { name: "OCR" })).toBeInTheDocument();
      });
      expect(screen.getByRole("option", { name: "版面" })).toBeInTheDocument();
      await waitFor(() => {
        expect(screen.queryByPlaceholderText(/car, person/)).toBeNull();
        expect(screen.getByText(/未配置 text 属性，文本不会入库/)).toBeInTheDocument();
      });
    });

    it("OCR 默认模型发起预标透传 model_id + task_type, 不带 prompt", async () => {
      withOcrCapabilities();
      renderUI();
      fireEvent.click(screen.getAllByRole("checkbox", { name: /选择/ })[0]);
      await waitFor(() => {
        expect(screen.getByRole("option", { name: "OCR" })).toBeInTheDocument();
      });
      fireEvent.click(screen.getByRole("button", { name: /跑预标.*1 批/ }));

      await waitFor(() => {
        expect(mockTriggerMutate).toHaveBeenCalledTimes(1);
      });
      const arg = mockTriggerMutate.mock.calls[0][0];
      expect(arg).toMatchObject({
        ml_backend_id: "bk1",
        model_id: "m-ocr",
        task: "ocr",
        batch_id: "b1",
      });
      expect(arg).not.toHaveProperty("prompt");
    });
  });
});

// v0.14.11 · CapabilityCatalogPanel 双层视图测试.
// 覆盖:
// - 0 backend + groupBy=task → 9 张协议卡 + onboarding 横幅;
// - 切换 groupBy=backend → 退回旧空态 (v0.14.10 行为, 不渲染空协议卡);
// - 搜索 "ocr" → 仅 OCR 协议卡可见。

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";

const mockOverview = vi.fn();
vi.mock("@/api/adminMlIntegrations", () => ({
  adminMlIntegrationsApi: { overview: () => mockOverview() },
}));

vi.mock("@/api/ml-backends", () => ({
  mlBackendsApi: {
    capabilities: vi.fn(),
    refreshCapabilities: vi.fn(),
  },
}));

const mockGetProtocol = vi.fn();
const mockGetInstances = vi.fn();
vi.mock("@/api/mlCapabilities", async () => {
  const { useQuery } = await import("@tanstack/react-query");
  return {
    useProtocolCapabilities: () =>
      useQuery({
        queryKey: ["ml-capabilities", "protocol"],
        queryFn: () => mockGetProtocol(),
      }),
    useCapabilityInstances: () =>
      useQuery({
        queryKey: ["ml-capabilities", "instances"],
        queryFn: () => mockGetInstances(),
      }),
    mlCapabilitiesApi: {
      getProtocol: () => mockGetProtocol(),
      getInstances: () => mockGetInstances(),
    },
  };
});

// 默认以 super_admin 渲染 (现有用例依赖 admin overview 可用); 降级用例按需切 project_admin。
const mockUsePermissions = vi.fn(() => ({ role: "super_admin" }));
vi.mock("@/hooks/usePermissions", () => ({
  usePermissions: () => mockUsePermissions(),
}));

const mockPushToast = vi.fn();
vi.mock("@/components/ui/Toast", async () => {
  const actual = await vi.importActual<any>("@/components/ui/Toast");
  return {
    ...actual,
    useToastStore: <T,>(sel: (s: any) => T) => sel({ push: mockPushToast }),
  };
});

import { CapabilityCatalogPanel } from "./CapabilityCatalogPanel";

function makeProtocol() {
  return {
    version: "v2",
    tasks: [
      {
        id: "detection",
        label: "目标检测",
        summary: "...",
        default_geometry: ["bbox"],
        default_modalities: ["image"],
        typical_models: ["YOLO"],
        protocol_notes: "...",
        suggested_backends: [],
      },
      {
        id: "obb",
        label: "旋转框",
        summary: "...",
        default_geometry: ["rotated_bbox"],
        default_modalities: ["image"],
        typical_models: ["YOLO-OBB"],
        protocol_notes: "...",
        suggested_backends: [],
      },
      {
        id: "segmentation",
        label: "实例分割",
        summary: "...",
        default_geometry: ["polygon"],
        default_modalities: ["image"],
        typical_models: ["Mask R-CNN"],
        protocol_notes: "...",
        suggested_backends: [],
      },
      {
        id: "keypoint",
        label: "关键点",
        summary: "...",
        default_geometry: ["keypoint"],
        default_modalities: ["image"],
        typical_models: ["YOLO-Pose"],
        protocol_notes: "...",
        suggested_backends: [],
      },
      {
        id: "classification",
        label: "图像分类",
        summary: "...",
        default_geometry: ["none"],
        default_modalities: ["image"],
        typical_models: ["ResNet"],
        protocol_notes: "...",
        suggested_backends: [],
      },
      {
        id: "ocr",
        label: "OCR",
        summary: "从图像提取文本",
        default_geometry: ["bbox"],
        default_modalities: ["image"],
        typical_models: ["PaddleOCR"],
        protocol_notes: "...",
        suggested_backends: [
          {
            name: "PaddleOCR",
            repo_url: "https://github.com/PaddlePaddle/PaddleOCR",
            summary: "...",
            research_link: null,
            infra: "paddle",
            builtin: false,
          },
        ],
      },
      {
        id: "doc_layout",
        label: "版面分析",
        summary: "...",
        default_geometry: ["bbox"],
        default_modalities: ["image"],
        typical_models: ["LayoutLMv3"],
        protocol_notes: "...",
        suggested_backends: [],
      },
      {
        id: "tracker",
        label: "视频追踪",
        summary: "...",
        default_geometry: [],
        default_modalities: ["video"],
        typical_models: ["ByteTrack"],
        protocol_notes: "...",
        suggested_backends: [],
      },
      {
        id: "interactive_seg",
        label: "交互分割",
        summary: "...",
        default_geometry: ["polygon"],
        default_modalities: ["image"],
        typical_models: ["SAM"],
        protocol_notes: "...",
        suggested_backends: [],
      },
    ],
    infras: [{ id: "pytorch", label: "PyTorch", summary: "..." }],
    modalities: [{ id: "image", label: "图像", summary: "..." }],
    geometries: [{ id: "bbox", label: "bbox", summary: "..." }],
  };
}

function renderUI() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <CapabilityCatalogPanel />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("CapabilityCatalogPanel · 协议双层视图", () => {
  it("目录搜索逐字输入保留空格", async () => {
    const user = userEvent.setup();
    renderUI();
    await screen.findByText(/支持 9 类 AI 标注能力/);
    const input = screen.getByRole("textbox");
    await user.type(input, "SAM ");
    expect(input).toHaveValue("SAM ");
    await user.type(input, "2");
    expect(input).toHaveValue("SAM 2");
  });

  it("按后端分组的模型卡保留任务徽标", async () => {
    mockGetInstances.mockResolvedValue({
      instances: [
        {
          backend_id: "gsam2-registry-id",
          state: "connected",
          source: "env",
          name: "gsam2",
          infra: "pytorch",
          models: [
            {
              id: "sam2-model",
              display_name: "SAM 2",
              task: "detection",
              infra: "pytorch",
              is_interactive: false,
              supported_prompts: ["text"],
              supported_geometric_outputs: ["bbox"],
              supported_trackers: [],
              modality: "image",
            },
          ],
        },
      ],
    });
    renderUI();
    await screen.findByText("SAM 2");
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "backend" } });
    expect(screen.getByText("SAM 2")).toBeInTheDocument();
    const card = screen.getByText("SAM 2").parentElement!.parentElement!;
    expect(within(card).getByText("检测")).toBeVisible();
  });
  beforeEach(() => {
    mockOverview.mockReset();
    mockGetProtocol.mockReset();
    mockGetInstances.mockReset();
    mockOverview.mockResolvedValue({
      projects: [],
      total_backends: 0,
      connected_backends: 0,
    });
    mockGetProtocol.mockResolvedValue(makeProtocol());
    mockGetInstances.mockResolvedValue({ instances: [] });
    mockUsePermissions.mockReturnValue({ role: "super_admin" });
  });

  it("0 backend + 默认 groupBy=task → 渲染 9 张协议卡 + onboarding 横幅", async () => {
    renderUI();
    await screen.findByText(/支持 9 类 AI 标注能力/);
    // 9 个 task label 都出现 (作为卡标题)
    for (const label of [
      "目标检测",
      "旋转框",
      "实例分割",
      "关键点",
      "图像分类",
      "OCR",
      "版面分析",
      "视频追踪",
      "交互分割",
    ]) {
      // task.label 在 title 和 badge 中都出现 (多次)
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    }
    // 0 backend 时所有协议卡都是「暂无接入」
    expect(screen.getAllByText("暂无接入").length).toBe(9);
  });

  it("非法 catalog_group 深链 → chip 提示且可移除，移除后非法参数从 URL 清掉", async () => {
    // 回归（PR #103 Codex P2）：issue chip 此前只读，共享链接里的非法
    // catalog_group 无法被用户清除。移除 = 回落默认枚举（codec 删除该键）。
    let currentSearch = "";
    function LocationProbe() {
      const location = useLocation();
      currentSearch = location.search;
      return null;
    }
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/?catalog_group=bogus"]}>
          <CapabilityCatalogPanel />
          <LocationProbe />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    // 渲染回落默认 task 分组，同时给出可见提示 chip。
    await screen.findByText(/支持 9 类 AI 标注能力/);
    expect(screen.getByText("无效的目录分组")).toBeInTheDocument();
    expect(currentSearch).toContain("catalog_group=bogus");

    fireEvent.click(screen.getByRole("button", { name: "移除无效的目录分组筛选" }));
    await waitFor(() => {
      expect(currentSearch).not.toContain("catalog_group");
    });
    expect(screen.queryByText("无效的目录分组")).not.toBeInTheDocument();
  });

  it("默认协议能力分组切到列表视图 → 渲染协议能力列表", async () => {
    renderUI();
    await screen.findByText(/支持 9 类 AI 标注能力/);
    fireEvent.click(screen.getByTitle("列表视图"));
    expect(screen.getByRole("columnheader", { name: "协议能力" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "接入状态" })).toBeInTheDocument();
    expect(screen.getAllByRole("row")).toHaveLength(10);
    expect(screen.getAllByText("暂无接入")).toHaveLength(9);
  });

  it("切换 groupBy=backend → 退回 v0.14.10 行为 (旧空态, 不渲染协议卡)", async () => {
    renderUI();
    await screen.findByText(/支持 9 类 AI 标注能力/);
    const select = screen.getByRole("combobox") as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "backend" } });
    await waitFor(() => {
      expect(screen.getByText("尚无项目注册 ML Backend")).toBeInTheDocument();
    });
    expect(screen.queryAllByText("暂无接入").length).toBe(0);
  });

  it("env-only 容器的 model 出现在对应协议卡 (即使 admin overview 为空)", async () => {
    mockGetInstances.mockResolvedValue({
      instances: [
        {
          backend_id: "gsam2-registry-id",
          state: "connected",
          // 后端 /instances 真实透传 ml_backend_registry.source = "env" | "manual"
          // (不是 "env_only"); 目录侧「平台内置 vs 已注册」由是否进 overview 决定。
          source: "env",
          name: "gsam2",
          infra: "pytorch",
          models: [
            {
              id: "grounded-sam2-detection",
              display_name: "Grounded-SAM 2 · 文本检测",
              task: "detection",
              infra: "pytorch",
              is_interactive: false,
              supported_prompts: ["text"],
              supported_geometric_outputs: ["bbox"],
              supported_trackers: [],
              modality: "image",
            },
            {
              id: "grounded-sam2-tracker",
              display_name: "Grounded-SAM 2 · 视频追踪",
              task: "tracker",
              infra: "pytorch",
              is_interactive: true,
              supported_prompts: ["bbox"],
              supported_geometric_outputs: ["bbox"],
              supported_trackers: ["sam2_video"],
              modality: "video",
            },
          ],
        },
      ],
    });
    renderUI();
    await screen.findByText("Grounded-SAM 2 · 文本检测");
    expect(screen.getByText("Grounded-SAM 2 · 视频追踪")).toBeInTheDocument();
    // env-only model 复用 ModelCard 渲染, 来源行展示「平台内置 · <backend名>」
    expect(screen.getAllByText(/平台内置/).length).toBeGreaterThan(0);
    // detection / tracker 协议卡都不再是「暂无接入」
    const undeployedCount = screen.queryAllByText("暂无接入").length;
    // 9 张卡里只剩 7 张暂无接入 (detection / tracker 各挂了 1 个 model)
    expect(undeployedCount).toBe(7);
    // 0 backend 横幅不应再出现
    expect(screen.queryByText(/支持 9 类 AI 标注能力/)).not.toBeInTheDocument();
  });

  it("overview 为空但有 env 实例时, groupBy=backend 也能看到平台内置 backend (回归: 非协议分组不再空白)", async () => {
    // BUG: overview 只含「项目已启用」的 backend; 项目未启用 backend 时非协议分组
    // (backend/infra/none) 曾因 env 兜底分支比错 source 字段而变死代码, 显示「暂无可用模型条目」。
    mockGetInstances.mockResolvedValue({
      instances: [
        {
          backend_id: "yolo-registry-id",
          state: "connected",
          source: "env",
          name: "yolo-backend",
          infra: "pytorch",
          models: [
            {
              id: "yolov8-det",
              display_name: "YOLOv8 · 目标检测",
              task: "detection",
              infra: "pytorch",
              is_interactive: false,
              supported_prompts: [],
              supported_geometric_outputs: ["bbox"],
              supported_trackers: [],
              // gsam2 / yolo 只报旧版扁平 output_attribute_types (schema 为空);
              // instances 路径须透传它, 否则「输出属性」行空成「—」。
              output_attribute_types: ["class"],
              output_attribute_schema: [],
              modality: "image",
            },
          ],
        },
      ],
    });
    renderUI();
    const select = (await screen.findByRole("combobox")) as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "backend" } });
    // 非协议分组下 env backend 的 model 应渲染, 而非退回空态。
    expect(await screen.findByText("YOLOv8 · 目标检测")).toBeInTheDocument();
    expect(screen.queryByText("暂无可用模型条目")).not.toBeInTheDocument();
    expect(screen.queryByText("尚无项目注册 ML Backend")).not.toBeInTheDocument();
    // 「输出属性」行透传 backend 自报的 output_attribute_types (而非只看空 schema)。
    // 阶段二后属性行在卡片详情展开区内，先展开再断言。
    fireEvent.click(screen.getByRole("button", { name: /详情/ }));
    expect(await screen.findByText("class")).toBeInTheDocument();
  });

  it("非超管 (project_admin) 不调 admin overview, 能力目录退到 /instances 视图而非硬报错", async () => {
    // 盲点4: overview (/admin/ml-integrations/overview) 是 super_admin only; project_admin
    // 也能进本页, 目录应退到 /instances 单端点 (登录用户可访问), 而不是整块「加载失败」。
    mockUsePermissions.mockReturnValue({ role: "project_admin" });
    mockGetInstances.mockResolvedValue({
      instances: [
        {
          backend_id: "yolo-registry-id",
          state: "connected",
          source: "env",
          name: "yolo-backend",
          infra: "pytorch",
          models: [
            {
              id: "yolov8-det",
              display_name: "YOLOv8 · 目标检测",
              task: "detection",
              infra: "pytorch",
              is_interactive: false,
              supported_prompts: [],
              supported_geometric_outputs: ["bbox"],
              supported_trackers: [],
              modality: "image",
            },
          ],
        },
      ],
    });
    renderUI();
    // 协议卡视图 (默认 task) 仍渲染, model 由 /instances 挂到 detection 卡。
    expect(await screen.findByText("YOLOv8 · 目标检测")).toBeInTheDocument();
    // admin overview 不应被调用 (super_admin only), 也不出现「加载失败」硬报错。
    expect(mockOverview).not.toHaveBeenCalled();
    expect(screen.queryByText(/加载失败/)).not.toBeInTheDocument();
  });

  it("同 URL 跨多项目注册时, groupBy=backend 只渲染一组 + 注册状态列聚合项目名", async () => {
    // v0.14.12 · backendRefs 按 URL 去重 (避免重复 fetch + 重复显示).
    const sharedBackend = (projectId: string) => ({
      id: `${projectId}-bk`,
      project_id: projectId,
      name: "shared-yolo",
      url: "http://172.17.0.1:9999/",
      state: "connected" as const,
      auth_method: "none",
      is_interactive: false,
      extra_params: {},
      created_at: "2026-06-01T00:00:00Z",
      updated_at: "2026-06-01T00:00:00Z",
      last_checked_at: null,
      error_message: null,
      health_meta: null,
    });
    mockOverview.mockResolvedValue({
      projects: [
        { project_id: "p1", project_name: "项目甲", backends: [sharedBackend("p1")] },
        { project_id: "p2", project_name: "项目乙", backends: [sharedBackend("p2")] },
        { project_id: "p3", project_name: "项目丙", backends: [sharedBackend("p3")] },
      ],
      total_backends: 3,
      connected_backends: 3,
    });
    const { mlBackendsApi } = await import("@/api/ml-backends");
    (mlBackendsApi.capabilities as ReturnType<typeof vi.fn>).mockResolvedValue({
      name: "shared-yolo",
      infra: "pytorch",
      is_interactive: false,
      supported_prompts: ["none"],
      supported_geometric_outputs: ["bbox"],
      models: [
        {
          id: "detect",
          display_name: "YOLO 目标检测",
          task: "detection",
          infra: "pytorch",
          supported_geometric_outputs: ["bbox"],
          supported_prompts: ["none"],
        },
      ],
    });
    renderUI();
    // 切到 backend 分组 + 列表视图 (一旦 overview 解析完, 这俩会触发 capability fetch + rerender).
    const select = (await screen.findByRole("combobox")) as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "backend" } });
    fireEvent.click(screen.getByTitle("列表视图"));
    // 注册状态列聚合显示: "项目甲 +2"
    await screen.findByText("项目甲 +2");
    // capabilities API 只调用一次 (按 URL 去重) — 而非 3 次.
    expect(mlBackendsApi.capabilities).toHaveBeenCalledTimes(1);
    // 同 backendName 的 group 只出现一次 (按 URL 合并)
    expect(screen.getAllByText("shared-yolo").length).toBe(2); // group header + 来源列
  });

  it("backend 上报越界字段 → capabilities.warnings → ModelCard 显示 ⚠ 徽标", async () => {
    // v0.18.29 · 协议契约校验: backend 上报越界 prompt「boxes」, 平台 /capabilities 带 warnings,
    // 模型市场该 model 卡显示 ⚠ + 可读原因 (按 model_id 关联)。
    mockOverview.mockResolvedValue({
      projects: [
        {
          project_id: "p1",
          project_name: "项目甲",
          backends: [
            {
              id: "bk1",
              project_id: "p1",
              name: "bad-yolo",
              url: "http://172.17.0.1:9001/",
              state: "connected" as const,
              auth_method: "none",
              is_interactive: false,
              extra_params: {},
              created_at: "2026-06-01T00:00:00Z",
              updated_at: "2026-06-01T00:00:00Z",
              last_checked_at: null,
              error_message: null,
              health_meta: null,
            },
          ],
        },
      ],
      total_backends: 1,
      connected_backends: 1,
    });
    const { mlBackendsApi } = await import("@/api/ml-backends");
    (mlBackendsApi.capabilities as ReturnType<typeof vi.fn>).mockResolvedValue({
      name: "bad-yolo",
      infra: "pytorch",
      is_interactive: false,
      supported_prompts: ["boxes"],
      supported_geometric_outputs: ["bbox"],
      models: [
        {
          id: "detect",
          display_name: "YOLO 目标检测",
          task: "detection",
          infra: "pytorch",
          supported_geometric_outputs: ["bbox"],
          supported_prompts: ["boxes"],
        },
      ],
      warnings: [
        {
          level: "warning",
          model_id: "detect",
          field: "supported_prompts",
          value: "boxes",
          message: "未知 prompt「boxes」; 前端工具门控不识别, 该提示将静默失效。",
        },
      ],
    });
    renderUI();
    const select = (await screen.findByRole("combobox")) as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "backend" } });
    await screen.findByText("⚠ 协议 1");
  });

  it("模型族筛选同时作用于协议卡视图", async () => {
    mockGetInstances.mockResolvedValue({
      instances: [
        {
          backend_id: "sam-registry-id",
          state: "connected",
          source: "env",
          name: "sam-backend",
          infra: "pytorch",
          models: [
            {
              id: "sam-detect",
              display_name: "SAM 检测",
              task: "detection",
              model_family: "sam",
              infra: "pytorch",
              is_interactive: false,
              supported_prompts: [],
              supported_geometric_outputs: ["bbox"],
              supported_trackers: [],
              modality: "image",
            },
            {
              id: "yolo-detect",
              display_name: "YOLO 检测",
              task: "detection",
              model_family: "yolo",
              infra: "pytorch",
              is_interactive: false,
              supported_prompts: [],
              supported_geometric_outputs: ["bbox"],
              supported_trackers: [],
              modality: "image",
            },
          ],
        },
      ],
    });
    renderUI();
    expect(await screen.findByText("SAM 检测")).toBeInTheDocument();
    expect(screen.getByText("YOLO 检测")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "更多筛选" }));
    fireEvent.click(screen.getByRole("button", { name: "sam" }));
    expect(screen.getByRole("button", { name: "sam" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("SAM 检测")).toBeInTheDocument();
    expect(screen.queryByText("YOLO 检测")).not.toBeInTheDocument();
  });

  it("搜索 'ocr' → 仅 OCR 协议卡可见", async () => {
    renderUI();
    await screen.findByText(/支持 9 类 AI 标注能力/);
    const searchBox = screen.getByPlaceholderText(
      "搜索模型、ID、模型族、任务或来源",
    ) as HTMLInputElement;
    fireEvent.change(searchBox, { target: { value: "ocr" } });
    await waitFor(() => {
      // OCR 卡仍在
      expect(screen.getAllByText("OCR").length).toBeGreaterThan(0);
      // 其他 task 卡不再出现 (用「目标检测」作为代表)
      expect(screen.queryAllByText("目标检测").length).toBe(0);
    });
  });
});

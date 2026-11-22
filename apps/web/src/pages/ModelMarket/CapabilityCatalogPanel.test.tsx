import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

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

vi.mock("@/hooks/usePermissions", () => ({
  usePermissions: () => ({ role: "super_admin" }),
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
  });

  it("env-only 容器的 model 出现在对应协议卡 (即使 admin overview 为空)", async () => {
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
    expect(screen.getAllByText(/平台内置/).length).toBeGreaterThan(0);
    const undeployedCount = screen.queryAllByText("暂无接入").length;
    expect(undeployedCount).toBe(7);
    expect(screen.queryByText(/支持 9 类 AI 标注能力/)).not.toBeInTheDocument();
  });

  it("overview 为空但有 env 实例时, groupBy=backend 也能看到平台内置 backend (回归: 非协议分组不再空白)", async () => {
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
    expect(await screen.findByText("YOLOv8 · 目标检测")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /详情/ }));
    expect(await screen.findByText("class")).toBeInTheDocument();
  });

  it("同 URL 跨多项目注册时, groupBy=backend 只渲染一组 + 注册状态列聚合项目名", async () => {
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
    const select = (await screen.findByRole("combobox")) as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "backend" } });
    fireEvent.click(screen.getByTitle("列表视图"));
    await screen.findByText("项目甲 +2");
    expect(mlBackendsApi.capabilities).toHaveBeenCalledTimes(1);
    expect(screen.getAllByText("shared-yolo").length).toBe(2);
  });

  it("backend 上报越界字段 → capabilities.warnings → ModelCard 显示 ⚠ 徽标", async () => {
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
});

import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const mockUseCapabilityInstances = vi.fn();
const mockCreateProjectPipelineMutateAsync = vi.fn();

vi.mock("@/api/mlCapabilities", () => ({
  useCapabilityInstances: () => mockUseCapabilityInstances(),
}));

const mockUseProjectPipelines = vi.fn();
const mockDeleteProjectPipelineMutate = vi.fn();
vi.mock("@/hooks/useProjectPipelines", () => ({
  useCreateProjectPipeline: () => ({
    mutateAsync: mockCreateProjectPipelineMutateAsync,
    isPending: false,
  }),
  useProjectPipelines: () => mockUseProjectPipelines(),
  useDeleteProjectPipeline: () => ({
    mutate: mockDeleteProjectPipelineMutate,
    isPending: false,
  }),
}));

// v0.21.0 · 用可交互的 canvas mock 触发 addStage/onSelect, 让测试不依赖 react-flow 真实 DOM.
type CanvasProps = {
  onAddChild?: (sid: string) => void;
  onSelect?: (sid: string) => void;
};
vi.mock("./components/PipelineGraphCanvas", () => ({
  default: (props: CanvasProps) => (
    <div data-testid="pipeline-canvas">
      <button data-testid="add-child-source" onClick={() => props.onAddChild?.("source")}>
        +child
      </button>
      <button data-testid="select-stage-1" onClick={() => props.onSelect?.("stage-1")}>
        select stage-1
      </button>
    </div>
  ),
}));

import GlobalPipelineLibraryPage from "./GlobalPipelineLibraryPage";

function renderUI() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <GlobalPipelineLibraryPage />
    </QueryClientProvider>,
  );
}

describe("GlobalPipelineLibraryPage", () => {
  beforeEach(() => {
    mockCreateProjectPipelineMutateAsync.mockReset();
    mockCreateProjectPipelineMutateAsync.mockResolvedValue({ id: "pipe-public" });
    mockUseCapabilityInstances.mockReset();
    mockUseCapabilityInstances.mockReturnValue({
      data: { instances: [] },
      isLoading: false,
    });
    mockUseProjectPipelines.mockReset();
    mockUseProjectPipelines.mockReturnValue({ data: [], isLoading: false });
    mockDeleteProjectPipelineMutate.mockReset();
  });

  it("Inspector 里 state=error 的 backend 展示但禁用选择", async () => {
    mockUseCapabilityInstances.mockReturnValue({
      data: {
        instances: [
          {
            backend_id: "bk-global-ok",
            state: "connected",
            name: "global-det",
            models: [
              {
                id: "det",
                display_name: "Global Det",
                task: "detect",
                supported_inputs: ["full_image"],
                supported_geometric_outputs: ["bbox"],
                output_attribute_types: [],
                output_attribute_schema: [],
              },
            ],
          },
          {
            backend_id: "bk-global-error",
            state: "error",
            name: "bad-det",
            models: [
              {
                id: "bad",
                display_name: "Bad Det",
                task: "detect",
                supported_inputs: ["full_image"],
                supported_geometric_outputs: ["bbox"],
                output_attribute_types: [],
                output_attribute_schema: [],
              },
            ],
          },
        ],
      },
      isLoading: false,
    });

    renderUI();
    await screen.findByTestId("pipeline-canvas");

    const source = screen.getByLabelText("源阶段模型") as HTMLSelectElement;
    const errorOption = Array.from(source.options).find((o) => o.textContent?.includes("探测失败"));
    expect(errorOption).toBeTruthy();
    expect(errorOption?.disabled).toBe(true);
  });

  it("多层 DAG 保存: 源 backend + 下游按 sid 组装 stages, 用 instances.backend_id", async () => {
    mockUseCapabilityInstances.mockReturnValue({
      data: {
        instances: [
          {
            backend_id: "bk-global-det",
            state: "connected",
            name: "global-det",
            models: [
              {
                id: "det",
                display_name: "Global Det",
                task: "detect",
                supported_inputs: ["full_image"],
                supported_geometric_outputs: ["bbox"],
                output_attribute_types: [],
                output_attribute_schema: [],
                // v0.21.0 · 变体轴: 源检测 model 声明 size 轴, default=m; 保存 payload 应带 model_variants.
                supported_variants: [
                  {
                    key: "size",
                    title: "尺寸",
                    variants: [
                      { value: "n", label: "nano" },
                      { value: "m", label: "medium" },
                      { value: "x", label: "xlarge" },
                    ],
                  },
                ],
                default_variants: { size: "m" },
              },
            ],
          },
          {
            backend_id: "bk-attr",
            state: "connected",
            name: "attr-backend",
            models: [
              {
                id: "attr",
                display_name: "Vehicle Attr",
                task: "classify",
                supported_inputs: ["crop"],
                supported_geometric_outputs: [],
                output_attribute_types: ["class"],
                output_attribute_schema: [{ key: "color", label: "Color", type: "select" }],
              },
            ],
          },
        ],
      },
      isLoading: false,
    });
    renderUI();
    await screen.findByTestId("pipeline-canvas");

    fireEvent.change(screen.getByPlaceholderText(/例如 detect/), {
      target: { value: "global detect attr" },
    });
    // 源阶段选 det.
    fireEvent.change(screen.getByLabelText("源阶段模型"), {
      target: { value: "bk-global-det::det" },
    });
    // v0.21.6 · 从画布触发 addStage(source) 给源模型 stage 挂下游子.
    fireEvent.click(screen.getByTestId("add-child-source"));
    // 切到 stage-1 Inspector.
    fireEvent.click(screen.getByTestId("select-stage-1"));
    fireEvent.change(screen.getByLabelText("阶段 2 模型"), {
      target: { value: "bk-attr::attr" },
    });
    fireEvent.click(screen.getByRole("button", { name: /保存公共编排/ }));

    await waitFor(() => {
      expect(mockCreateProjectPipelineMutateAsync).toHaveBeenCalledWith(
        expect.objectContaining({
          name: "global detect attr",
          scope: "public",
          project_id: null,
          organization_id: null,
          is_default: false,
          stages: [
            expect.objectContaining({
              stage: 0,
              ml_backend_id: "bk-global-det",
              model_id: "det",
              // 源未手改变体 → 取 model default_variants (size=m).
              model_variants: { size: "m" },
            }),
            expect.objectContaining({
              stage: 1,
              parent_stage: 0,
              ml_backend_id: "bk-attr",
              model_id: "attr",
              write: expect.objectContaining({ target: "attributes" }),
            }),
          ],
        }),
      );
    });
  });

  it("命名编排库: 列出 public/organization, 支持删除", async () => {
    mockUseProjectPipelines.mockReturnValue({
      data: [
        {
          id: "pipe-1",
          name: "公共编排 A",
          scope: "public",
          project_id: null,
          organization_id: null,
          stages: [{ stage: 0, ml_backend_id: "bk", model_id: "det" }],
          is_default: false,
          usage_count: 3,
          created_by: "u1",
          created_at: "2026-07-01T00:00:00Z",
          updated_at: "2026-07-01T00:00:00Z",
        },
        {
          id: "pipe-2",
          name: "组织编排 B",
          scope: "organization",
          project_id: null,
          organization_id: "org1",
          stages: [{ stage: 0, ml_backend_id: "bk", model_id: "det" }],
          is_default: false,
          usage_count: 1,
          created_by: "u2",
          created_at: "2026-07-01T00:00:00Z",
          updated_at: "2026-07-01T00:00:00Z",
        },
        {
          id: "pipe-3",
          name: "私有编排 C (不应展示)",
          scope: "private",
          project_id: "proj",
          organization_id: null,
          stages: [{ stage: 0, ml_backend_id: "bk", model_id: "det" }],
          is_default: true,
          usage_count: 0,
          created_by: "u3",
          created_at: "2026-07-01T00:00:00Z",
          updated_at: "2026-07-01T00:00:00Z",
        },
      ],
      isLoading: false,
    });
    renderUI();
    await screen.findByTestId("pipeline-canvas");

    expect(screen.getByText("公共编排 A")).toBeTruthy();
    expect(screen.getByText("组织编排 B")).toBeTruthy();
    expect(screen.queryByText(/私有编排 C/)).toBeNull();
    // usage_count 展示.
    expect(screen.getByText(/已套用 3 次/)).toBeTruthy();

    // 删除第一条.
    const [firstDeleteBtn] = screen.getAllByRole("button", { name: /删除/ });
    fireEvent.click(firstDeleteBtn);
    expect(mockDeleteProjectPipelineMutate).toHaveBeenCalledWith("pipe-1", expect.any(Object));
  });
});

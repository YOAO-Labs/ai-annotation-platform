import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

const mockCfg: any = {
  backendId: "bk2",
  paramsValue: {},
  selectedClassIdx: new Set<number>(),
  configReady: true,
  buildArgs: () => ({ ml_backend_id: "bk2", params: {} }),
  capabilitiesQ: { isLoading: false, data: { models: [] } },
  selectableModels: [],
  selectedModelId: null,
  selectTaskModel: vi.fn(),
};

vi.mock("./usePreannotateConfig", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  usePreannotateConfig: () => mockCfg,
}));
vi.mock("./PreannotateConfigForm", () => ({
  PreannotateConfigForm: () => <div data-testid="cfg-form" />,
}));

import { StageCard } from "./StageCard";

function renderCard(props: Partial<Record<string, unknown>> = {}) {
  return render(
    <StageCard
      id="s1"
      displayIndex={2}
      projectId="p1"
      backends={[
        { id: "bk1", name: "det" },
        { id: "bk2", name: "cls" },
      ]}
      projectMlBackendId="bk1"
      sourceBackendId="bk1"
      projectClasses={["car", "person"]}
      projectAttributeKeys={["color", "vehicle_type"]}
      onChange={() => {}}
      onRemove={() => {}}
      {...(props as any)}
    />,
  );
}

describe("StageCard 阶段配置与运行态", () => {
  beforeEach(() => {
    mockCfg.capabilitiesQ = { isLoading: false, data: { models: [] } };
    mockCfg.configReady = true;
    mockCfg.currentVariantSlice = {};
    mockCfg.selectableModels = [];
    mockCfg.selectedModelId = null;
    mockCfg.selectTaskModel = vi.fn();
    mockCfg.buildArgs = () => ({ ml_backend_id: "bk2", params: {} });
  });

  it("父框类别 chip 来自项目类别, 可点选", () => {
    renderCard();
    expect(screen.getByRole("button", { name: "car" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "person" })).toBeInTheDocument();
  });

  it("backend 自报属性 schema 时, 写回属性键 chip 取 schema key", () => {
    mockCfg.capabilitiesQ = {
      isLoading: false,
      data: {
        models: [
          {
            output_attribute_schema: [{ key: "vehicle_color", label: "车辆颜色", type: "select" }],
          },
        ],
      },
    };
    renderCard();
    expect(screen.getByRole("button", { name: "车辆颜色" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "color" })).toBeNull();
  });

  it("模型未声明输出属性时，写回键回落项目属性", () => {
    renderCard();
    expect(screen.getByRole("button", { name: "color" })).toBeInTheDocument();
  });

  it("运行态徽标 + 计数: running 显「运行中」+ 成功/失败/几何跳过", () => {
    renderCard({
      runState: "running",
      stat: { stage: 1, targeted: 5, ok: 3, failed: 1, skipped_geometry: 1 },
    });
    expect(screen.getByText("运行中")).toBeInTheDocument();
    expect(screen.getByText("目标")).toBeInTheDocument();
    expect(screen.getByText("5")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("几何跳过")).toBeInTheDocument();
  });

  it("未跑时徽标为「待运行」, 无计数行", () => {
    renderCard();
    expect(screen.getByText("待运行")).toBeInTheDocument();
    expect(screen.queryByText(/目标/)).toBeNull();
  });

  it("backend 有纯分类 model 时, 下游 payload 用分类 model_id + 显示提示", () => {
    mockCfg.buildArgs = () => ({
      ml_backend_id: "bk2",
      params: {},
      class_filter: [2],
      model_variants: { series: "n" },
    });
    mockCfg.currentVariantSlice = { conf_size: "L", series: "n" };
    mockCfg.capabilitiesQ = {
      isLoading: false,
      data: {
        models: [
          { id: "vehicle-attr", task: "detect", display_name: "检测+分类" },
          {
            id: "vehicle-attr-classify",
            task: "classify",
            display_name: "纯分类·吃 ROI",
            supported_variants: [{ key: "conf_size", variants: [{ value: "L" }] }],
            output_attribute_schema: [{ key: "color", label: "颜色", type: "select" }],
          },
        ],
      },
    };
    const onChange = vi.fn();
    renderCard({ onChange });
    expect(screen.getByText(/纯分类，跳过检测/)).toBeInTheDocument();
    const calls = onChange.mock.calls;
    const lastPayload = calls[calls.length - 1]?.[1];
    expect(lastPayload?.model_id).toBe("vehicle-attr-classify");
    expect(lastPayload?.task).toBe("classify");
    expect(lastPayload?.class_filter).toBeUndefined();
    expect(lastPayload?.model_variants).toEqual({ conf_size: "L" });
  });

  it("backend 暴露 box-seg 时, 走 geometry 下游: payload 直构 (无需 prompt) + 隐藏属性字段", () => {
    mockCfg.configReady = false;
    mockCfg.currentVariantSlice = { sam_variant: "tiny", dino_variant: "T" };
    mockCfg.capabilitiesQ = {
      isLoading: false,
      data: {
        models: [
          {
            id: "grounded-sam2-box-seg",
            task: "segment",
            display_name: "框→分割",
            is_interactive: false,
            supported_prompts: ["bbox"],
            supported_variants: [{ key: "sam_variant", variants: [{ value: "tiny" }] }],
          },
        ],
      },
    };
    const onChange = vi.fn();
    renderCard({ onChange });
    expect(screen.getByText(/框→分割：消费上游检测框/)).toBeInTheDocument();
    expect(screen.queryByText(/写回属性键/)).toBeNull();
    expect(screen.queryByText(/ROI 扩展 pad/)).toBeNull();
    const calls = onChange.mock.calls;
    const lastPayload = calls[calls.length - 1]?.[1];
    expect(lastPayload?.model_id).toBe("grounded-sam2-box-seg");
    expect(lastPayload?.task).toBe("segment");
    expect(lastPayload?.roi?.mode).toBe("geometry");
    expect(lastPayload?.write?.target).toBe("geometry");
    expect(lastPayload?.model_variants).toEqual({ sam_variant: "tiny" });
  });

  it("分类阶段有「子物体命名」输入, 填了 → payload 带 label 前缀", () => {
    const onChange = vi.fn();
    renderCard({ onChange });
    const labelInput = screen.getByPlaceholderText("留空=不加前缀");
    fireEvent.change(labelInput, { target: { value: "hat" } });
    const calls = onChange.mock.calls;
    const lastPayload = calls[calls.length - 1]?.[1];
    expect(lastPayload?.label).toBe("hat");
    expect(lastPayload?.write?.target).toBe("attributes");
  });

  it("选 detection 下游 → crop-detect 几何 payload (crop 投递 + 回映)", () => {
    mockCfg.currentVariantSlice = { size: "s" };
    mockCfg.capabilitiesQ = {
      isLoading: false,
      data: {
        models: [
          {
            id: "hat-detector",
            task: "detect",
            display_name: "帽子检测",
            is_interactive: false,
            supported_variants: [{ key: "size", variants: [{ value: "s" }] }],
          },
        ],
      },
    };
    const onChange = vi.fn();
    renderCard({ onChange });
    expect(screen.getByText(/在父框 crop 上检测子物体/)).toBeInTheDocument();
    const calls = onChange.mock.calls;
    const lastPayload = calls[calls.length - 1]?.[1];
    expect(lastPayload?.model_id).toBe("hat-detector");
    expect(lastPayload?.task).toBe("detect");
    expect(lastPayload?.roi?.mode).toBe("crop");
    expect(lastPayload?.input?.mode).toBe("crop");
    expect(lastPayload?.write?.target).toBe("geometry");
    expect(lastPayload?.model_variants).toEqual({ size: "s" });
  });

  it("仅裁剪 OCR 无需整图表单 ready 或切换模型", () => {
    mockCfg.configReady = false;
    mockCfg.buildArgs = vi.fn();
    mockCfg.capabilitiesQ = {
      isLoading: false,
      data: {
        models: [
          {
            id: "ocr-rec",
            task: "ocr",
            composition: "atom",
            is_interactive: false,
            display_name: "文本识别（原子）",
            supported_inputs: ["crop"],
            supported_variants: [
              { key: "version", variants: [{ value: "v5" }] },
              { key: "size", variants: [{ value: "mobile" }] },
              { key: "lang", variants: [{ value: "universal" }] },
            ],
            output_attribute_types: ["text", "orientation", "language"],
            output_attribute_schema: [{ key: "text", label: "识别文本", type: "text" }],
          },
        ],
      },
    };
    const onChange = vi.fn();
    renderCard({ onChange });
    expect(screen.getByText("识别")).toBeInTheDocument();
    expect(screen.getByText(/在父框 crop 上识别文本/)).toBeInTheDocument();
    expect(screen.queryByText(/不产类别属性/)).toBeNull();
    expect(mockCfg.selectTaskModel).not.toHaveBeenCalled();
    expect(mockCfg.buildArgs).not.toHaveBeenCalled();
    const calls = onChange.mock.calls;
    const lastPayload = calls[calls.length - 1]?.[1];
    expect(lastPayload?.model_id).toBe("ocr-rec");
    expect(lastPayload?.task).toBe("ocr");
    expect(lastPayload?.roi?.mode).toBe("crop");
    expect(lastPayload?.write?.target).toBe("attributes");
    expect(lastPayload?.model_variants).toEqual({
      version: "v5",
      size: "mobile",
      lang: "universal",
    });
  });

  it("仅接受整图输入的 OCR 不能作为裁剪下游", () => {
    mockCfg.capabilitiesQ = {
      isLoading: false,
      data: { models: [{ id: "ocr-e2e", task: "ocr", supported_inputs: ["full_image"] }] },
    };
    mockCfg.buildArgs = () => ({
      ml_backend_id: "bk2",
      model_id: "ocr-e2e",
      task: "ocr",
      params: {},
    });
    const onChange = vi.fn();
    renderCard({ onChange });
    expect(screen.queryByText("识别")).toBeNull();
    expect(onChange).toHaveBeenLastCalledWith("s1", null);
  });

  it("父框类别优先用 parentClassOptions(上游筛完类别), 自由文本可加任意名", () => {
    const onChange = vi.fn();
    renderCard({ onChange, parentClassOptions: ["person", "car"] });
    expect(screen.getByRole("button", { name: "person" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "car" })).toBeInTheDocument();
    const input = screen.getByPlaceholderText("输入类名添加（匹配检测框类名）");
    fireEvent.change(input, { target: { value: "forklift" } });
    fireEvent.keyDown(input, { key: "Enter" });
    const calls = onChange.mock.calls;
    const lastPayload = calls[calls.length - 1]?.[1];
    expect(lastPayload?.parent_class_filter).toContain("forklift");
  });

  it("点类别 chip 选中后再点清空", () => {
    renderCard();
    const carChip = screen.getByRole("button", { name: "car" });
    fireEvent.click(carChip);
    expect(screen.getByRole("button", { name: /✓.*car/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "清空" }));
    expect(screen.queryByRole("button", { name: /✓/ })).toBeNull();
  });
});

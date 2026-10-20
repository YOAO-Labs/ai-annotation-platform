/**
 * v0.18.5 / v0.18.6 · StageCard 单测: 选择器化字段 + 运行态徽标。
 *
 * 重依赖 (usePreannotateConfig 拉 setup/capabilities + PreannotateConfigForm) 全 mock,
 * 聚焦 StageCard 自身: 类别/属性键 chip 多选、能力门控 ⚠、运行态徽标 + 计数。
 */
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

vi.mock("./usePreannotateConfig", () => ({
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

describe("StageCard v0.18.5/6", () => {
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
    // 不产属性的回落项 (项目 key) 不应出现, 因 backend 自报优先
    expect(screen.queryByRole("button", { name: "color" })).toBeNull();
  });

  it("backend 未自报属性时, 给 ⚠ 警示 + 写回键回落项目属性 key", () => {
    renderCard();
    expect(screen.getByText(/该后端未自报输出属性/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "color" })).toBeInTheDocument();
  });

  it("运行态徽标 + 计数: running 显「运行中」+ 成功/失败/几何跳过", () => {
    renderCard({
      runState: "running",
      stat: { stage: 1, targeted: 5, ok: 3, failed: 1, skipped_geometry: 1 },
    });
    expect(screen.getByText("运行中")).toBeInTheDocument();
    // 计数块: 标签 + 值分列 (StatCard 风格)
    expect(screen.getByText("目标")).toBeInTheDocument();
    expect(screen.getByText("5")).toBeInTheDocument(); // 目标值
    expect(screen.getByText("3")).toBeInTheDocument(); // 成功值
    expect(screen.getByText("几何跳过")).toBeInTheDocument();
  });

  it("未跑时徽标为「待运行」, 无计数行", () => {
    renderCard();
    expect(screen.getByText("待运行")).toBeInTheDocument();
    expect(screen.queryByText(/目标/)).toBeNull();
  });

  it("backend 有纯分类 model 时, 下游 payload 用分类 model_id + 显示提示", () => {
    // 「模型任务」整图模型的 class_filter / 变体不应流进下游分类阶段。
    mockCfg.buildArgs = () => ({
      ml_backend_id: "bk2",
      params: {},
      class_filter: [2], // 源整图检测模型的 index 白名单 — 必须被丢弃
      model_variants: { series: "n" }, // 源模型的轴 — 必须被下游轴过滤掉
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
    // 可见提示: 走纯分类 model
    expect(screen.getByText(/纯分类，跳过检测/)).toBeInTheDocument();
    // 上抛的 payload 用分类 model 而非 buildArgs 默认 (检测 model)
    const calls = onChange.mock.calls;
    const lastPayload = calls[calls.length - 1]?.[1];
    expect(lastPayload?.model_id).toBe("vehicle-attr-classify");
    expect(lastPayload?.task).toBe("classify");
    // 既有小毛病修复: 源 class_filter 不透传; 变体只保留下游 model 自报的轴 (series 被滤掉)。
    expect(lastPayload?.class_filter).toBeUndefined();
    expect(lastPayload?.model_variants).toEqual({ conf_size: "L" });
  });

  it("backend 暴露 box-seg 时, 走 geometry 下游: payload 直构 (无需 prompt) + 隐藏属性字段", () => {
    mockCfg.configReady = false; // box-seg 不依赖 prompt; 仍应产 payload
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
    // 可见提示: 框→分割
    expect(screen.getByText(/框→分割：消费上游检测框/)).toBeInTheDocument();
    // 属性写回字段不渲染 (geometry 产 polygon, 不写属性)
    expect(screen.queryByText(/写回属性键/)).toBeNull();
    expect(screen.queryByText(/ROI 扩展 pad/)).toBeNull();
    // 上抛 geometry payload: model_id + segmentation + roi.mode=geometry + 仅 sam 变体轴
    const calls = onChange.mock.calls;
    const lastPayload = calls[calls.length - 1]?.[1];
    expect(lastPayload?.model_id).toBe("grounded-sam2-box-seg");
    expect(lastPayload?.task).toBe("segment");
    expect(lastPayload?.roi?.mode).toBe("geometry");
    expect(lastPayload?.write?.target).toBe("geometry");
    expect(lastPayload?.model_variants).toEqual({ sam_variant: "tiny" }); // dino 被过滤
  });

  it("v0.18.15 · 分类阶段有「子物体命名」输入, 填了 → payload 带 label 前缀", () => {
    const onChange = vi.fn();
    renderCard({ onChange });
    const labelInput = screen.getByPlaceholderText("留空=不加前缀");
    fireEvent.change(labelInput, { target: { value: "hat" } });
    const calls = onChange.mock.calls;
    const lastPayload = calls[calls.length - 1]?.[1];
    expect(lastPayload?.label).toBe("hat");
    expect(lastPayload?.write?.target).toBe("attributes");
  });

  it("v0.18.15 · 选 detection 下游 → crop-detect 几何 payload (crop 投递 + 回映)", () => {
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
    // 提示: 在父框 crop 上检测子物体
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

  it("v0.20.x · 选 ocr-rec 下游 → 识别阶段: attributes payload (crop 投递) + 无 no-class 误报", () => {
    // 跨 backend 编排: 上游 det 出框 → 下游 rapidocr rec 认字。rec 产 text/orientation/language。
    mockCfg.currentVariantSlice = { version: "v5", size: "mobile", lang: "universal" };
    // 整图 OCR 同胞 (e2e) 进 selectableModels, 供 StageCard 对齐变体轴 (effect 调 selectTaskModel)。
    mockCfg.selectableModels = [{ id: "ocr-e2e", task: "ocr" }];
    mockCfg.selectedModelId = null; // 未对齐 → effect 应调 selectTaskModel 切到 e2e 同胞
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
            // rec 自报 version/size/lang 轴 (与 e2e 同套), payload 据此保留这三轴。
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
    // 角色徽标 = 识别; 描述 = 在父框 crop 上识别文本
    expect(screen.getByText("识别")).toBeInTheDocument();
    expect(screen.getByText(/在父框 crop 上识别文本/)).toBeInTheDocument();
    // 「不产类别属性」误报不应出现 (rec 产 text 是有效产出)
    expect(screen.queryByText(/不产类别属性/)).toBeNull();
    // 已选 rec 下游 → 对齐整图 OCR 同胞 (e2e), 使 configReady + version/size/lang 轴就位
    expect(mockCfg.selectTaskModel).toHaveBeenCalledWith("ocr-e2e");
    // 上抛 attributes payload: model_id=ocr-rec + task=ocr + crop 投递 + 写属性
    const calls = onChange.mock.calls;
    const lastPayload = calls[calls.length - 1]?.[1];
    expect(lastPayload?.model_id).toBe("ocr-rec");
    expect(lastPayload?.task).toBe("ocr");
    expect(lastPayload?.roi?.mode).toBe("crop");
    expect(lastPayload?.write?.target).toBe("attributes");
    // 变体按 rec 自报轴保留 version/size/lang (与 e2e 同胞共用同套轴)。
    expect(lastPayload?.model_variants).toEqual({
      version: "v5",
      size: "mobile",
      lang: "universal",
    });
  });

  it("v0.20.x · 父框类别优先用 parentClassOptions(上游筛完类别), 自由文本可加任意名", () => {
    const onChange = vi.fn();
    renderCard({ onChange, parentClassOptions: ["person", "car"] });
    // 选项来自上游筛完类别, 而非项目类别 (car/person 在, 项目的 banana 不在)
    expect(screen.getByRole("button", { name: "person" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "car" })).toBeInTheDocument();
    // 自由文本: 输入任意名 + 回车 → 进 parent_class_filter (即便不在选项里)
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
    // 选中后出现 ✓ 前缀
    expect(screen.getByRole("button", { name: /✓.*car/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "清空" }));
    expect(screen.queryByRole("button", { name: /✓/ })).toBeNull();
  });
});

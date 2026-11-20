import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { PreannotateConfigForm } from "./PreannotateConfigForm";
import type { PreannotateConfig } from "./usePreannotateConfig";

// OCR 路径走统一「模型任务」下拉的基础 cfg:可注入 selectableModels / variantGroups。
function docCfg(over: Partial<PreannotateConfig> = {}): PreannotateConfig {
  const e2e = {
    id: "ocr-e2e",
    task: "ocr",
    display_name: "RapidOCR · 端到端 OCR",
    composition: "composite" as const,
    supported_inputs: ["full_image"],
  };
  return {
    backendId: "b1",
    isGeometricBackend: false,
    isTextPath: false,
    isDocMode: true,
    taskType: "ocr",
    setTaskType: vi.fn(),
    selectableModels: [e2e],
    selectedModelId: "ocr-e2e",
    selectTaskModel: vi.fn(),
    sourceBatchableWarning: null,
    hasAnyParams: false,
    hasNonVariantParams: false,
    variantGroups: [
      {
        key: "version",
        title: "PP-OCR 版本",
        variants: [
          { value: "v5", label: "V5" },
          { value: "v6", label: "V6" },
        ],
      },
    ],
    variantCombinations: undefined,
    variantDefaults: { version: "v5" },
    paramsSchema: { type: "object", properties: {} },
    paramsValue: { version: "v5" },
    onVariantOrParamsChange: vi.fn(),
    onParamsChange: vi.fn(),
    panelShape: { showOutputMode: false, forcedOutputMode: null, promptKind: "none" },
    presets: [],
    applyPreset: vi.fn(),
    savePreset: vi.fn(),
    removePreset: vi.fn(),
    ...over,
  } as unknown as PreannotateConfig;
}

describe("PreannotateConfigForm", () => {
  // OCR 路径 model-first 统一化回归。
  it("OCR 路径(单 model)展示当前模型,端到端 OCR 不再隐身", () => {
    render(<PreannotateConfigForm cfg={docCfg()} />);
    expect(screen.getByText(/端到端 OCR/)).toBeInTheDocument();
  });

  it("OCR 多 model 走统一「模型任务」下拉(det / e2e 都可选)", () => {
    const det = {
      id: "ocr-det",
      task: "ocr",
      display_name: "RapidOCR · 文本检测",
      composition: "atom" as const,
      supported_inputs: ["full_image"],
    };
    const e2e = {
      id: "ocr-e2e",
      task: "ocr",
      display_name: "RapidOCR · 端到端 OCR",
      composition: "composite" as const,
      supported_inputs: ["full_image"],
    };
    render(
      <PreannotateConfigForm
        cfg={docCfg({ selectableModels: [det, e2e], selectedModelId: "ocr-e2e" })}
      />,
    );
    expect(screen.getByRole("option", { name: "RapidOCR · 文本检测" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "RapidOCR · 端到端 OCR" })).toBeInTheDocument();
  });

  it("OCR 有 supported_variants 时渲染变体选择器,不再误显「无可调参数」", () => {
    render(<PreannotateConfigForm cfg={docCfg()} />);
    expect(screen.queryByText(/无可调参数/)).toBeNull();
    expect(screen.getByText(/PP-OCR 版本/)).toBeInTheDocument();
  });

  it("OCR 既无 params 又无变体轴 → 显「无可调参数」", () => {
    render(<PreannotateConfigForm cfg={docCfg({ variantGroups: [] })} />);
    expect(screen.getByText(/无可调参数/)).toBeInTheDocument();
  });
});

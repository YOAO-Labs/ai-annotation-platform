import { describe, expect, it } from "vitest";
import type { MLModelCapability, MLBackendSupportedVariantGroup } from "@/api/ml-backends";
import { derivePanelShape, deriveTextPanelShape, deriveVariantSource } from "./panelShape";

function model(overrides: Partial<MLModelCapability>): MLModelCapability {
  return { id: "m1", ...overrides };
}

describe("derivePanelShape", () => {
  it("YOLO detection (闭集, 单一 bbox): 隐藏输出形态 + 强制 box + 类别筛选", () => {
    const shape = derivePanelShape(
      model({
        task: "detect",
        supported_prompts: ["none"],
        supported_geometric_outputs: ["bbox"],
      }),
      false,
    );
    expect(shape.showOutputMode).toBe(false);
    expect(shape.forcedOutputMode).toBe("box");
    expect(shape.promptKind).toBe("class_filter");
  });

  it("YOLO segmentation (闭集, 单一 polygon): 隐藏输出形态 + 强制 mask", () => {
    const shape = derivePanelShape(
      model({
        task: "segment",
        supported_prompts: ["none"],
        supported_geometric_outputs: ["polygon"],
      }),
      false,
    );
    expect(shape.showOutputMode).toBe(false);
    expect(shape.forcedOutputMode).toBe("mask");
    expect(shape.promptKind).toBe("class_filter");
  });

  it("gsam2 (开集 text, 框+掩膜): 显示输出形态三选 + Prompt", () => {
    const shape = derivePanelShape(
      model({
        task: "detect",
        supported_prompts: ["text", "bbox"],
        supported_geometric_outputs: ["bbox", "polygon"],
      }),
      false,
    );
    expect(shape.showOutputMode).toBe(true);
    expect(shape.forcedOutputMode).toBe(null);
    expect(shape.promptKind).toBe("prompt");
  });

  it("keypoint (无框/掩膜概念): 隐藏输出形态, 不强制", () => {
    const shape = derivePanelShape(
      model({
        task: "pose",
        supported_prompts: ["none"],
        supported_geometric_outputs: ["keypoint"],
      }),
      false,
    );
    expect(shape.showOutputMode).toBe(false);
    expect(shape.forcedOutputMode).toBe(null);
  });

  it("isDocMode: 隐藏输出形态 + prompt none", () => {
    const shape = derivePanelShape(model({ task: "ocr" }), true);
    expect(shape.showOutputMode).toBe(false);
    expect(shape.promptKind).toBe("none");
  });

  it("能力声明不全 (无字段): 安全兜底为显示输出形态 + 文本 prompt, 不藏可用字段", () => {
    const shape = derivePanelShape(model({ task: "detect" }), false);
    expect(shape.showOutputMode).toBe(true);
    expect(shape.forcedOutputMode).toBe(null);
    expect(shape.promptKind).toBe("prompt");
  });

  it("model 为空 (能力未就位): 兜底显示 + 文本 prompt", () => {
    const shape = derivePanelShape(undefined, false);
    expect(shape.showOutputMode).toBe(true);
    expect(shape.promptKind).toBe("prompt");
  });
});

describe("deriveTextPanelShape (文本批量, 顶层 supported_text_outputs)", () => {
  it("gsam2 [box,mask,both]: 三选可见, 不强制 (修 #3 回归)", () => {
    const shape = deriveTextPanelShape(["box", "mask", "both"]);
    expect(shape.showOutputMode).toBe(true);
    expect(shape.forcedOutputMode).toBe(null);
    expect(shape.promptKind).toBe("prompt");
  });

  it("仅 box: 隐藏三选 + 强制 box", () => {
    const shape = deriveTextPanelShape(["box"]);
    expect(shape.showOutputMode).toBe(false);
    expect(shape.forcedOutputMode).toBe("box");
  });

  it("仅 mask: 隐藏三选 + 强制 mask", () => {
    const shape = deriveTextPanelShape(["mask"]);
    expect(shape.showOutputMode).toBe(false);
    expect(shape.forcedOutputMode).toBe("mask");
  });

  it("both 单项也视为同时支持框+掩膜 → 三选可见", () => {
    const shape = deriveTextPanelShape(["both"]);
    expect(shape.showOutputMode).toBe(true);
    expect(shape.forcedOutputMode).toBe(null);
  });

  it("声明不全 (空/缺): 安全兜底为三选可见", () => {
    expect(deriveTextPanelShape(undefined).showOutputMode).toBe(true);
    expect(deriveTextPanelShape([]).showOutputMode).toBe(true);
  });
});

describe("deriveVariantSource — 文本走顶层 / 几何走选中 model (#3 回归)", () => {
  const SAM_GROUP: MLBackendSupportedVariantGroup = {
    key: "sam_variant",
    title: "SAM2",
    variants: [{ value: "tiny" }, { value: "large" }],
  };
  const DINO_GROUP: MLBackendSupportedVariantGroup = {
    key: "dino_variant",
    title: "DINO",
    variants: [{ value: "swint" }],
  };
  const SIZE_GROUP: MLBackendSupportedVariantGroup = {
    key: "size",
    title: "尺寸",
    variants: [{ value: "n" }, { value: "x" }],
  };

  it("文本路径 (v0.18.12 model-first): 走选中文本 model 的逐 model 变体 (检测=仅 dino)", () => {
    const src = deriveVariantSource({
      isDocMode: false,
      isGeometricBackend: false,
      activeDocModel: undefined,
      geometricModel: undefined,
      // 选中「检测」文本 model → 只表达 dino 一组 (选检测时不再白显 SAM2 组)。
      textModel: model({
        task: "detect",
        supported_variants: [DINO_GROUP],
        default_variants: { dino_variant: "swint" },
      }),
      topSupportedVariants: [SAM_GROUP, DINO_GROUP], // 顶层在场也不该被用 (textModel 优先)
    });
    expect(src.groups).toEqual([DINO_GROUP]);
    expect(src.defaults).toEqual({ dino_variant: "swint" });
  });

  it("文本路径 textModel 缺位 → 回落顶层 union (能力未就位兜底)", () => {
    const src = deriveVariantSource({
      isDocMode: false,
      isGeometricBackend: false,
      activeDocModel: undefined,
      geometricModel: undefined,
      textModel: undefined,
      topSupportedVariants: [SAM_GROUP, DINO_GROUP],
    });
    expect(src.groups).toEqual([SAM_GROUP, DINO_GROUP]);
    expect(src.combinations).toBeUndefined();
    expect(src.defaults).toBeUndefined();
  });

  it("几何路径: 走选中 task model 的逐 model 变体 + 组合 + 默认", () => {
    const src = deriveVariantSource({
      isDocMode: false,
      isGeometricBackend: true,
      activeDocModel: undefined,
      geometricModel: model({
        task: "detect",
        supported_variants: [SIZE_GROUP],
        variant_combinations: [["n"], ["x"]],
        default_variants: { size: "n" },
      }),
      topSupportedVariants: [SAM_GROUP, DINO_GROUP], // 顶层在场也不该被用
    });
    expect(src.groups).toEqual([SIZE_GROUP]);
    expect(src.combinations).toEqual([["n"], ["x"]]);
    expect(src.defaults).toEqual({ size: "n" });
  });

  it("doc 路径: 走选中文档 model 的逐 model 变体", () => {
    const src = deriveVariantSource({
      isDocMode: true,
      isGeometricBackend: false,
      activeDocModel: model({
        task: "doc_layout",
        supported_variants: [SIZE_GROUP],
        default_variants: { size: "x" },
      }),
      geometricModel: undefined,
      topSupportedVariants: [SAM_GROUP],
    });
    expect(src.groups).toEqual([SIZE_GROUP]);
    expect(src.defaults).toEqual({ size: "x" });
  });

  it("文本路径顶层缺失 → groups undefined (不抛, 上层兜底)", () => {
    const src = deriveVariantSource({
      isDocMode: false,
      isGeometricBackend: false,
      activeDocModel: undefined,
      geometricModel: undefined,
      topSupportedVariants: undefined,
    });
    expect(src.groups).toBeUndefined();
  });
});

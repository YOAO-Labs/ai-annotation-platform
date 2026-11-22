import type { MLModelCapability, MLBackendSupportedVariantGroup } from "@/api/ml-backends";
import type { TextOutputMode } from "@/hooks/usePreannotation";

export type PromptKind = "prompt" | "class_filter" | "none";

export interface PanelShape {
  showOutputMode: boolean;
  forcedOutputMode: TextOutputMode | null;
  promptKind: PromptKind;
}

function geoToMode(geo: string): TextOutputMode | null {
  if (geo === "bbox" || geo === "rotated_bbox") return "box";
  if (geo === "polygon" || geo === "mask") return "mask";
  return null;
}

/**
 * 由当前选中 model 的 capability 派生面板形态.
 * @param model    当前任务对应的 model capability (文本任务取 primaryModel; 为空表示能力未就位)
 * @param isDocMode OCR / 文档版面模式 (走 model_id + task, 无文本 prompt / 无几何选择)
 */
export function derivePanelShape(
  model: MLModelCapability | undefined,
  isDocMode: boolean,
): PanelShape {
  if (isDocMode) {
    return { showOutputMode: false, forcedOutputMode: null, promptKind: "none" };
  }

  const geos = model?.supported_geometric_outputs;
  let showOutputMode = true;
  let forcedOutputMode: TextOutputMode | null = null;
  if (geos && geos.length > 0) {
    const modes = Array.from(
      new Set(geos.map(geoToMode).filter((m): m is TextOutputMode => m !== null)),
    );
    showOutputMode = modes.length > 1;
    forcedOutputMode = modes.length === 1 ? modes[0] : null;
  }

  const prompts = model?.supported_prompts;
  let promptKind: PromptKind = "prompt";
  if (prompts && prompts.length > 0 && !prompts.includes("text")) {
    promptKind = "class_filter";
  }

  return { showOutputMode, forcedOutputMode, promptKind };
}

export function deriveTextPanelShape(supportedTextOutputs: string[] | undefined): PanelShape {
  if (!supportedTextOutputs || supportedTextOutputs.length === 0) {
    return { showOutputMode: true, forcedOutputMode: null, promptKind: "prompt" };
  }
  const set = new Set(supportedTextOutputs);
  const canBox = set.has("box") || set.has("both");
  const canMask = set.has("mask") || set.has("both");
  const showOutputMode = canBox && canMask;
  const forcedOutputMode: TextOutputMode | null = showOutputMode
    ? null
    : canBox
      ? "box"
      : canMask
        ? "mask"
        : null;
  return { showOutputMode, forcedOutputMode, promptKind: "prompt" };
}

export interface VariantSource {
  groups: MLBackendSupportedVariantGroup[] | undefined;
  combinations: string[][] | undefined;
  defaults: Record<string, string> | undefined;
}

export function deriveVariantSource(input: {
  isDocMode: boolean;
  isGeometricBackend: boolean;
  activeDocModel: MLModelCapability | undefined;
  geometricModel: MLModelCapability | undefined;
  textModel?: MLModelCapability | undefined;
  topSupportedVariants: MLBackendSupportedVariantGroup[] | undefined;
}): VariantSource {
  const {
    isDocMode,
    isGeometricBackend,
    activeDocModel,
    geometricModel,
    textModel,
    topSupportedVariants,
  } = input;
  if (isDocMode) {
    return {
      groups: activeDocModel?.supported_variants,
      combinations: activeDocModel?.variant_combinations,
      defaults: activeDocModel?.default_variants,
    };
  }
  if (isGeometricBackend) {
    return {
      groups: geometricModel?.supported_variants,
      combinations: geometricModel?.variant_combinations,
      defaults: geometricModel?.default_variants,
    };
  }
  if (textModel) {
    return {
      groups: textModel.supported_variants,
      combinations: textModel.variant_combinations,
      defaults: textModel.default_variants,
    };
  }
  return { groups: topSupportedVariants, combinations: undefined, defaults: undefined };
}

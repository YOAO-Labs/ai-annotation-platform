/**
 * v0.14.16 · 批跑预标面板的 capability 驱动变形 (纯函数, 便于单测).
 *
 * 不同 backend 的 model 字段集天然不同: YOLO 闭集检测器输出形态固定 (只 bbox)、无 prompt
 * (supported_prompts=['none'], 文本框实为类别白名单); gsam2 支持框/掩膜两种几何 + text prompt;
 * OCR/版面无几何选择概念. 本函数把"当前选中 model 的 capability"映射成"面板该显示哪些字段",
 * 集中判定逻辑, 避免在 JSX 里散落条件 + 杜绝 `if backend.name==='yolo'` 之类按 backend 名硬分.
 *
 * 闭/开集判定一律逐 model (以 supported_prompts / supported_geometric_outputs 为准): 未来同一
 * yolo-backend 可能并存闭集 (detect/seg/obb) 与开集 (YOLO-World/YOLOE) model, 不可按 backend 名分.
 */
import type { MLModelCapability, MLBackendSupportedVariantGroup } from "@/api/ml-backends";
import type { TextOutputMode } from "@/hooks/usePreannotation";

/** prompt 区的三种形态: 文本 prompt / 类别筛选 (闭集白名单) / 隐藏. */
export type PromptKind = "prompt" | "class_filter" | "none";

export interface PanelShape {
  /** 是否显示"输出形态"框/掩膜/全部三选 (model 同时支持框与掩膜时才有意义). */
  showOutputMode: boolean;
  /** 当 model 只支持单一几何输出时, 强制使用的输出形态 (隐藏三选时下发它); 多形态/未知时为 null. */
  forcedOutputMode: TextOutputMode | null;
  /** prompt 区形态. */
  promptKind: PromptKind;
}

/** 几何输出名 → 框/掩膜; keypoint 等无框/掩膜概念的返回 null. */
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

  // ── 输出形态 ──
  // capability 声明不全 (无 supported_geometric_outputs) 时安全兜底: 维持显示, 不强行隐藏.
  const geos = model?.supported_geometric_outputs;
  let showOutputMode = true;
  let forcedOutputMode: TextOutputMode | null = null;
  if (geos && geos.length > 0) {
    const modes = Array.from(
      new Set(geos.map(geoToMode).filter((m): m is TextOutputMode => m !== null)),
    );
    // 同时有框与掩膜 → 三选有意义; 仅一种 → 强制并隐藏; 一种都不映射 (如 keypoint) → 隐藏.
    showOutputMode = modes.length > 1;
    forcedOutputMode = modes.length === 1 ? modes[0] : null;
  }

  // ── prompt 区 ──
  // 声明不全 (无 supported_prompts) 时安全兜底为文本 prompt, 不藏掉可用字段.
  const prompts = model?.supported_prompts;
  let promptKind: PromptKind = "prompt";
  if (prompts && prompts.length > 0 && !prompts.includes("text")) {
    // 无文本 prompt 的闭集检测器 (YOLO supported_prompts=['none']): 文本框降级为类别白名单.
    promptKind = "class_filter";
  }

  return { showOutputMode, forcedOutputMode, promptKind };
}

/**
 * v0.14.18 · 文本 prompt 批量路径 (gsam2 「找全图」) 的面板形态.
 *
 * 文本批量是**后端级**能力 (detection 出框 + segmentation 出掩膜, 由 output_mode 在后端内部编排),
 * 任何单 model 都表达不全 (#3 回归: primaryModel=detection → 输出锁 box / 变体仅 dino)。
 * 故输出形态改由顶层 `supported_text_outputs` (box/mask/both) 派生; prompt 区恒为文本框。
 */
export function deriveTextPanelShape(supportedTextOutputs: string[] | undefined): PanelShape {
  // 声明不全时安全兜底: 维持三选可见, 不强行锁定。
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

/** variant 选择器的来源: 哪一组 variant 轴 + 组合 + 默认值由当前路径决定。 */
export interface VariantSource {
  groups: MLBackendSupportedVariantGroup[] | undefined;
  combinations: string[][] | undefined;
  defaults: Record<string, string> | undefined;
}

/**
 * v0.14.18 · 当前路径的 variant 来源 (修 #3 回归).
 *
 * - doc (OCR/版面) → 选中文档 model 的逐 model 变体;
 * - 几何闭集 (yolo 等) → 选中 task model 的逐 model 变体;
 * - 文本 prompt 批量 (gsam2 / sam3) → v0.18.12 起 model-first: **选中文本 task model 的逐 model 变体**
 *   (检测 model 只 dino; 分割 model sam+dino)。取代旧的「顶层 union」兜底 (选检测时不再白显 SAM)。
 *   textModel 缺位时回落顶层 topSupportedVariants (能力未就位的安全兜底)。
 */
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
  // 文本路径: 优先选中文本 model 的逐 model 变体; 缺位回落顶层 union。
  if (textModel) {
    return {
      groups: textModel.supported_variants,
      combinations: textModel.variant_combinations,
      defaults: textModel.default_variants,
    };
  }
  return { groups: topSupportedVariants, combinations: undefined, defaults: undefined };
}

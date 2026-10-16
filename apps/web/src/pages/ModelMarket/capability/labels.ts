import { normalizeTask } from "@/api/generated/capabilityVocab.gen";
// 能力目录的标签映射与配色(从 CapabilityCatalogPanel.tsx 拆出,行为零变化)。
// 受控 task / infra / modality 的中文短标签 + task → Badge variant 配色 + 列表行 task 后缀。

import { inputLabel as generatedInputLabel } from "@/api/capabilityInputs";

// 受控 task → 中文短标签 (协议 v2 边界枚举).
const TASK_LABELS: Record<string, string> = {
  detect: "检测",
  obb: "旋转框",
  segment: "分割",
  pose: "关键点",
  classify: "分类",
  ocr: "OCR",
  doc_layout: "版面分析",
  tracker: "追踪",
  interactive_seg: "交互分割",
};

// 受控 infra → 中文短标签.
const INFRA_LABELS: Record<string, string> = {
  pytorch: "PyTorch",
  onnx: "ONNX",
  paddle: "Paddle",
  tensorrt: "TensorRT",
  openvino: "OpenVINO",
  other: "其它",
  unknown: "未知",
};

const MODALITY_LABELS: Record<string, string> = {
  image: "图像",
  video: "视频",
  text: "文本",
  point_cloud: "点云",
};

const TASK_SUFFIX: Record<string, string> = {
  detect: "Det",
  obb: "OBB",
  segment: "Seg",
  pose: "Pose",
  interactive_seg: "ISeg",
  tracker: "Track",
  classify: "Cls",
  ocr: "OCR",
  doc_layout: "Layout",
};

export function taskLabel(task: string) {
  return TASK_LABELS[normalizeTask(task)] ?? task;
}
export function infraLabel(infra: string) {
  return INFRA_LABELS[infra] ?? infra;
}
export function modalityLabel(m: string) {
  return MODALITY_LABELS[m] ?? m;
}

// task → badge 配色 (复用既有 Badge variant, 不引入新 token).
export function taskVariant(task: string): "accent" | "ai" | "success" | "warning" | "outline" {
  task = normalizeTask(task);
  if (task === "detect" || task === "obb") return "accent";
  if (task === "segment" || task === "interactive_seg") return "ai";
  if (task === "pose" || task === "classify") return "success";
  if (task === "ocr" || task === "doc_layout") return "warning";
  return "outline";
}

export function taskSuffix(task: string | undefined): string {
  if (!task) return "";
  return TASK_SUFFIX[normalizeTask(task)] ?? task;
}

// composition → badge (协议 v2.2 · 原子 vs 内部编排)。atom=单次推理原子; composite=一个 model
// 内部串多原子的内置流程。老 backend 缺字段 → 不渲染。ModelCard / ModelListTable 共用。
export const COMPOSITION_BADGE: Record<
  string,
  { variant: "outline" | "ai"; label: string; title: string }
> = {
  atom: { variant: "outline", label: "原子", title: "单次推理原子，可作编排单元" },
  composite: { variant: "ai", label: "内置流程", title: "一个 model 内部串联多个原子（内置编排）" },
};

export function inputLabel(i: string) {
  return generatedInputLabel(i);
}

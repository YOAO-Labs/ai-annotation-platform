import { normalizeTask } from "@/api/generated/capabilityVocab.gen";

import { inputLabel as generatedInputLabel } from "@/api/capabilityInputs";

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

import type { TrainingEpochEvent, TrainingRunOut } from "@/api/trainingRuns";

import { normalizeTask } from "@/api/generated/capabilityVocab.gen";

export const trainingModelFamilyLabels: Record<string, string> = {
  yolov8: "YOLOv8",
  yolo11: "YOLO11",
  yolo26: "YOLO26",
  yolo9: "YOLO9",
};

export function trainingModelFamily(base: { id: string; model_family?: string | null }) {
  if (base.model_family) return base.model_family;
  const id = base.id.toLowerCase();
  return id.startsWith("yolov8")
    ? "yolov8"
    : id.startsWith("libreyolo9")
      ? "yolo9"
      : id.startsWith("yolo26")
        ? "yolo26"
        : id.startsWith("yolo11")
          ? "yolo11"
          : base.id;
}

export const segmentationTask = "segment";
export const trainingTaskLabel = (task?: unknown) => {
  const value = normalizeTask(typeof task === "string" ? task : "detect");
  return value === "segment" ? "图片实例分割" : value === "detect" ? "图片目标检测" : value;
};

export function trainingTaskOf(config?: Record<string, unknown>) {
  const configuration = config?.configuration as { identity?: { task?: string } } | undefined;
  return normalizeTask(String(config?.task ?? configuration?.identity?.task ?? "detect"));
}

export const activeTrainingStates = new Set([
  "queued",
  "preparing",
  "training",
  "evaluating",
  "uploading",
  "cancelling",
  "lost",
]);

export const trainingStatusLabels: Record<string, string> = {
  queued: "排队中",
  preparing: "准备数据",
  training: "训练中",
  evaluating: "评估中",
  uploading: "保存产物",
  cancelling: "正在停止",
  lost: "执行器失联 · 等待恢复",
  succeeded: "训练完成",
  failed: "训练失败",
  cancelled: "已取消",
};

export const artifactLabels: Record<string, string> = {
  weights: "推理权重",
  checkpoint: "续训检查点",
  metrics: "评估指标",
  config: "训练配置",
  log: "脱敏训练日志",
  telemetry: "资源与阶段观测摘要",
  manifest: "产物清单",
};

export function trainingStatusVariant(status: string) {
  if (status === "succeeded") return "success";
  if (status === "failed" || status === "lost") return "danger";
  if (activeTrainingStates.has(status)) return "warning";
  return "default";
}

export function trainingProgress(run: TrainingRunOut) {
  const parameters = run.config.parameters as Record<string, unknown> | undefined;
  const epoch = Number(run.metrics.epoch ?? 0);
  const total = Number(parameters?.epochs ?? 0);
  return {
    epoch,
    total,
    percent: total > 0 ? Math.min(100, Math.max(0, (epoch / total) * 100)) : 0,
  };
}

export function metricLabel(key: string) {
  const normalized = key.replace(/^train\/(?=metrics\/)/, "").replace(/\([BM]\)$/, "");
  const labels: Record<string, string> = {
    "metrics/mAP50-95": "mAP50–95",
    "metrics/mAP50": "mAP50",
    "metrics/mAP75": "mAP75",
    "metrics/precision": "Precision",
    "metrics/recall": "Recall",
    "metrics/AR100": "AR100",
    "train/loss": "训练损失",
    "train/box_loss": "训练框回归损失",
    "train/cls_loss": "训练分类损失",
    "train/dfl_loss": "训练 DFL 损失",
    "train/l1_loss": "训练 L1 损失",
    "train/seg_loss": "掩码训练损失",
    "train/sem_loss": "语义训练损失",
    "val/box_loss": "验证框回归损失",
    "val/cls_loss": "验证分类损失",
    "val/dfl_loss": "验证 DFL 损失",
    "val/l1_loss": "验证 L1 损失",
    "val/seg_loss": "掩码验证损失",
    "val/sem_loss": "语义验证损失",
    fitness: "原生最佳权重选择分数",
  };
  const label = labels[normalized] ?? key;
  const scoped = key.endsWith("(M)")
    ? `掩码 ${label}`
    : key.endsWith("(B)")
      ? `框 ${label}`
      : label;
  return key.startsWith("train/metrics/") ? `训练${scoped}` : scoped;
}

export function epochPoints(
  events: TrainingEpochEvent[],
  framework: string,
): Record<string, number>[] {
  return events
    .filter((event) => event.kind === "epoch" && event.epoch !== null)
    .map((event) => ({
      epoch: event.epoch!,
      ...Object.fromEntries(
        Object.entries(event.metrics).filter(
          ([key, value]) =>
            Number.isFinite(value) &&
            !(framework === "libreyolo" && /precision|recall/i.test(key)) &&
            !(key.endsWith("(B)") && event.metrics[key.slice(0, -3)] != null) &&
            !(/^(train\/)?metrics\/(mAP|AR|precision|recall)/.test(key) && value < 0),
        ),
      ),
    }));
}

export function formatDuration(start?: string | null, end?: string | null, now = Date.now()) {
  if (!start) return "尚未开始";
  const seconds = Math.max(
    0,
    Math.floor(((end ? Date.parse(end) : now) - Date.parse(start)) / 1000),
  );
  if (!Number.isFinite(seconds)) return "—";
  if (seconds < 60) return `${seconds} 秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
  return `${Math.floor(seconds / 3600)} 小时 ${Math.floor((seconds % 3600) / 60)} 分`;
}

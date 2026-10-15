import type { AsyncJob, AsyncJobStatus } from "@/api/asyncJobs";

export const JOB_KIND_LABEL: Record<string, string> = {
  batch_predict: "批量预标",
  video_tracker: "视频追踪",
  video_correction: "视频 Mask 纠错",
  audit_archive: "审计归档",
  predictions_import: "预测导入",
  prediction_retry: "失败预测重试",
  dataset_import: "数据集导入",
  create_tasks: "建任务",
  export: "数据导出",
  training_dataset: "训练数据构建",
  training_run: "模型训练",
  training_preview: "原生增强预览",
  mask_qc: "Mask 质检",
  mask_repair: "Mask 批量修复",
  mask_repair_rollback: "Mask 修复回滚",
  mask_format_import: "Mask 格式导入",
  point_cloud_cross_frame: "3D 跨帧传播",
  point_cloud_quality: "点云质量检查",
};

export const JOB_STATUS_LABEL: Record<AsyncJobStatus, string> = {
  pending: "等待中",
  running: "进行中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

export function jobString(record: Record<string, unknown>, key: string): string | null {
  const value = record[key];
  return typeof value === "string" && value ? value : null;
}

export function jobNumber(record: Record<string, unknown>, key: string): number | null {
  const value = record[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function jobResultSummary(job: Pick<AsyncJob, "kind" | "result">): string | null {
  const result = job.result ?? {};
  const fields =
    job.kind === "dataset_import" || job.kind === "mask_format_import"
      ? { imported: "导入", skipped: "跳过", error_count: "错误" }
      : job.kind === "create_tasks"
        ? { created_tasks: "已建任务", skipped: "跳过" }
        : job.kind === "export"
          ? { file_count: "文件" }
          : { success_count: "成功", failed_count: "失败", skipped_count: "跳过" };
  const parts = Object.entries(fields).flatMap(([key, label]) => {
    const value = jobNumber(result, key);
    return value === null ? [] : [`${label} ${value}`];
  });
  return parts.length ? parts.join(" / ") : null;
}

export function jobStage(job: AsyncJob): string {
  if (["completed", "failed", "cancelled"].includes(job.status))
    return JOB_STATUS_LABEL[job.status];
  const phase = jobString(job.result, "stage") ?? jobString(job.payload, "stage");
  const labels: Record<string, string> = {
    queued: "等待执行",
    collecting: "枚举文件",
    enumerating: "枚举文件",
    importing: "导入文件",
    creating: "创建任务",
    processing: "处理中",
    exporting: "生成导出文件",
    uploading: "上传结果",
    completed: "已完成",
  };
  if (phase && labels[phase]) return labels[phase];
  if (job.status === "running" && job.kind === "create_tasks") return "创建任务";
  return JOB_STATUS_LABEL[job.status];
}

/** Missing or malformed expiry is not evidence that a signed link is still usable. */
export function exportDownloadState(result: Record<string, unknown>, now = Date.now()) {
  const raw = jobString(result, "download_url");
  const expiresAt = jobString(result, "expires_at");
  const expires = expiresAt ? Date.parse(expiresAt) : NaN;
  if (!raw) return { url: null, reason: "导出文件暂不可用，请重新导出。" };
  if (!Number.isFinite(expires) || expires <= now) {
    return { url: null, reason: "下载链接已过期或有效期未知，请重新导出。" };
  }
  try {
    const url = new URL(raw, window.location.origin);
    if (url.protocol !== "https:" && url.protocol !== "http:") throw new Error("scheme");
    if (url.username || url.password) throw new Error("credentials");
    return { url: url.href, reason: null };
  } catch {
    return { url: null, reason: "下载链接不可用，请重新导出。" };
  }
}

import type { AsyncJob, AsyncJobStatus } from "@/api/asyncJobs";

export const VIDEO_ASSET_LABEL: Record<string, string> = {
  probe: "视频元数据",
  poster: "视频海报",
  frame_timetable: "帧时间表",
  chunk: "视频分块",
  frame: "帧缓存",
};

export const JOB_KIND_LABEL: Record<string, string> = {
  batch_predict: "批量预标",
  video_tracker: "视频追踪",
  video_correction: "视频 Mask 纠错",
  audit_archive: "审计归档",
  predictions_import: "预测导入",
  prediction_retry: "失败预测重试",
  video_asset_retry: "视频资产重试",
  dataset_import: "数据集导入",
  dataset_scan: "目录观测扫描",
  storage_usage: "存储对象盘点",
  managed_original_verification: "托管原件核验",
  dataset_quality_review: "质量批量复核",
  dataset_quality: "数据集图片质检",
  dataset_registration: "目录预检与登记",
  create_tasks: "建任务",
  export: "数据导出",
  training_dataset: "训练数据构建",
  training_run: "模型训练",
  sample_evaluation: "逐样本评估",
  active_learning_round: "主动学习选样",
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

export function jobNotificationVerb(type: string, kind: unknown): string | null {
  const suffix =
    type === "job.completed"
      ? "完成"
      : type === "job.failed"
        ? "失败"
        : type === "job.cancelled"
          ? "已取消"
          : null;
  if (!suffix) return null;
  const label = (typeof kind === "string" && JOB_KIND_LABEL[kind]) || "后台任务";
  return `${label}${suffix}`;
}

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
  if (job.kind === "managed_original_verification") {
    const labels: Record<string, string> = {
      matched: "本次内容与历史摘要相符",
      missing: "本次未找到对象",
      changed: "本次对象或内容已变化",
      unverifiable: "本次无法完成核验",
      failed: "本次核验失败",
      cancelled: "本次核验已取消",
    };
    return labels[jobString(result, "outcome") ?? ""] ?? null;
  }
  const fields =
    job.kind === "dataset_quality_review"
      ? { applied_count: "已复核", conflict_count: "冲突", blocked_count: "已阻止" }
      : job.kind === "dataset_quality"
        ? {
            checked_count: "已检测",
            skipped_count: "未检测",
            issue_count: "解码或尺寸异常",
            duplicate_group_count: "重复组",
          }
        : job.kind === "dataset_registration"
          ? {
              ready_count: "已核验",
              registered_count: "新增",
              unchanged_count: "已存在",
              error_count: "异常",
            }
          : job.kind === "dataset_scan"
            ? { processed_count: "已观测", file_count: "匹配文件", skipped_count: "跳过" }
            : job.kind === "dataset_import" || job.kind === "mask_format_import"
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
    dispatch_pending: "派发尚未确认",
    preflight: "核验图片与接入影响",
    registering: "登记业务样本",
    collecting: "枚举文件",
    reading: "读取并核验原件",
    enumerating: "枚举文件",
    resume_validation: "复核扫描检查点",
    validating: "复核目录清单",
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

export function jobProgressText(
  job: Pick<AsyncJob, "kind" | "status" | "progress_pct" | "result">,
): string {
  if (job.kind === "video_asset_retry" && ["pending", "running"].includes(job.status)) {
    return job.result?.stage === "dispatch_pending"
      ? "派发尚未确认，可回到视频资产失败列表重试"
      : "处理完成后显示本次资产的实际结果";
  }
  if (job.kind === "managed_original_verification" && ["pending", "running"].includes(job.status)) {
    return job.result?.cancel_requested === true
      ? "已请求取消，等待底层读取收尾"
      : "核验单条原件 · 完整读取结束后给出结果";
  }
  if (job.kind === "storage_usage" && ["pending", "running"].includes(job.status)) {
    const done =
      (jobNumber(job.result, "success_count") ?? 0) + (jobNumber(job.result, "failed_count") ?? 0);
    return `已处理 ${done} 个桶 · 当前桶对象总量未知`;
  }
  if (job.kind === "dataset_scan") {
    const count = jobNumber(job.result, "processed_count") ?? 0;
    const state = ["pending", "running"].includes(job.status)
      ? "总量未知"
      : job.result.complete === true
        ? "枚举完成"
        : "未完整扫描";
    return `已扫描 ${count} 项 · ${state}`;
  }
  return `${Math.max(0, Math.min(100, job.progress_pct))}%`;
}

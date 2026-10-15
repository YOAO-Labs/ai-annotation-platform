import type { TelemetryOut, TrainingFailure } from "@/api/trainingObservability";

export const resourceLabels: Record<string, string> = {
  cpu_cores: "进程组 CPU 核数",
  rss_bytes: "进程组 RSS 总和",
  cuda_allocated_bytes: "PyTorch 已分配显存",
  cuda_reserved_bytes: "PyTorch 保留显存",
  gpu_utilization: "整卡 GPU 利用率",
  gpu_used_mib: "整卡已用显存",
  gpu_total_mib: "整卡总显存",
  process_gpu_utilization: "进程 GPU 利用率",
  images_per_second: "原生批次吞吐",
};
export const unavailableLabels: Record<string, string> = {
  baseline_pending: "等待第二次采样",
  process_unavailable: "训练进程尚不可用",
  permission_denied: "采样权限不足",
  pid_reused: "进程身份已变化",
  device_unavailable: "设备采样不可用",
  invalid_value: "未取得有效数值",
  pid_namespace_unresolved: "尚无法确认进程与设备 PID 对应",
  cuda_uninitialized: "CUDA 尚未初始化",
  callback_unavailable: "原生回调未提供",
  callback_stale: "原生回调数据已过期",
  recovery_no_process: "恢复上传时无训练进程",
};
export function resourceValue(key: string, value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return "不可用";
  if (key.endsWith("_bytes")) return `${(value / 1024 / 1024).toFixed(1)} MiB`;
  if (key.endsWith("_mib")) return `${value.toFixed(0)} MiB`;
  if (key.includes("utilization")) return `${value.toFixed(0)}%`;
  if (key === "cpu_cores") return `${value.toFixed(2)} 核`;
  return `${value.toFixed(1)} 张/秒`;
}
export function secondsLabel(value: number) {
  if (value < 60) return `${Math.max(0, Math.round(value))} 秒`;
  if (value < 3600) return `${Math.round(value / 60)} 分钟`;
  return `${(value / 3600).toFixed(1)} 小时`;
}
export function estimateLabel(
  latest?: Pick<TelemetryOut, "phase" | "progress"> | null,
  status = "training",
  stale = false,
) {
  if (["succeeded", "failed", "cancelled"].includes(status)) return "执行已结束，不再估算";
  if (["lost", "cancelling"].includes(status)) return "等待执行确认，暂不估算";
  if (stale) return "采样或原生回调已过期，暂不估算";
  const eta = latest?.progress?.eta;
  if (latest && latest.phase !== "training") return "当前阶段剩余时间未知";
  if (!eta?.available || eta.minimum_seconds == null || eta.maximum_seconds == null)
    return eta?.reason === "unobserved_augmentation_regime"
      ? "剩余轮次包含尚未观测的增强阶段，暂不估算"
      : eta?.reason === "training_complete"
        ? "训练轮次已完成，评估与保存耗时未知"
        : "等待至少 3 个可比较的完整训练周期";
  return `${secondsLabel(eta.minimum_seconds)}–${secondsLabel(eta.maximum_seconds)} · 基于 ${eta.sample_count} 个周期；不含评估与保存`;
}
export function failureGuidance(failure?: TrainingFailure | null) {
  const labels: Record<string, string> = {
    cuda_oom:
      "训练进程报告显存不足。可用原配置初始化权重新建草稿，检查批大小（batch）与图片尺寸（imgsz）；参数不会自动修改。",
    input_mismatch: "冻结输入校验失败。检查数据包或检查点是否完整；更换数据需要新建实验。",
    config_mismatch: "实际训练配置与冻结配置不一致，请核对配置后新建实验。",
    runtime_mismatch: "执行环境与冻结训练器不兼容，请核对执行器环境。",
    execution_lost: "执行器失联或重启。资源须经退出确认后才能释放；观测缺失不会释放资源。",
    authority_lost: "执行权限已失效，平台正在确认进程退出与资源归还。",
    native_signal: "训练进程被信号终止，现有证据无法确认具体原因。请查看脱敏日志。",
    native_exception: "原生训练器报告异常，请查看诊断与脱敏日志。",
    preparation_failed: "训练准备失败，请检查诊断与冻结输入。",
    upload_failed: "产物尚未完成保存。执行器会按原回执重试上传，无需重新训练。",
    budget_exhausted:
      "本次试验的原生计算额度已用尽，执行器已请求停止。停止宽限按实际时间计入；只有搜索仍有余额时才能从已提交检查点续训。",
  };
  return failure
    ? (labels[failure.code] ?? "失败原因尚未确认，请查看脱敏诊断。")
    : "此历史执行未记录结构化失败原因，请查看脱敏日志。";
}
export const recoveryReasons: Record<string, string> = {
  study_budget_unavailable: "参数搜索预算不足、计时未知，或另一个试验仍在执行",
  attempt_not_stopped: "当前执行尚未确认停止",
  lease_not_released: "当前执行仍占用资源",
  unfinished_checkpoint_missing: "没有可继续训练的已提交检查点",
  runtime_unknown: "执行器环境尚未确认",
  runtime_incompatible: "当前执行器与冻结配置不兼容",
  authority_unavailable: "当前权限不允许续训",
  checkpoint_unavailable: "检查点存储校验未通过",
};

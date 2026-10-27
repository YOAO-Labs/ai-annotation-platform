import { CopyableError } from "@/components/ui/CopyableError";
import type { TelemetryPage } from "@/api/trainingObservability";
import { TrainingMetricChart } from "./TrainingMetricChart";
import type { ObservationWindow } from "./useTrainingObservability";
import {
  estimateLabel,
  resourceLabels,
  resourceValue,
  secondsLabel,
  unavailableLabels,
} from "./trainingObservability";
import { trainingStatusLabels } from "./trainingPresentation";

export function TrainingResourcePanel({
  data,
  error,
  pending,
  live,
  now,
  status,
}: {
  data?: ObservationWindow<TelemetryPage>;
  error?: string;
  pending: boolean;
  live: boolean;
  now: number;
  status: string;
}) {
  const latest = data?.summary?.latest ?? data?.items[data.items.length - 1];
  const stale = !!latest && live && now - Date.parse(latest.captured_at) > 15000;
  const keys = [
    "gpu_utilization",
    "gpu_used_mib",
    "gpu_total_mib",
    "rss_bytes",
    "cuda_allocated_bytes",
    "cuda_reserved_bytes",
    "cpu_cores",
    "images_per_second",
    "process_gpu_utilization",
  ];
  const points = (data?.items ?? []).flatMap((item, index, items) => [
    ...(index && item.sequence > items[index - 1].sequence + 1
      ? [{ sample: items[index - 1].sequence + 1, gpu: null }]
      : []),
    { sample: item.sequence, gpu: item.metrics.gpu_utilization?.value ?? null },
  ]);
  return (
    <section aria-label="所选执行资源与耗时" className="mb-5 space-y-3">
      {error && (
        <p role="alert" className="text-xs text-status-caution">
          观测连接中断：
          <CopyableError message={error} />
          。训练状态仍以执行器回执为准。
        </p>
      )}
      {pending && !latest && (
        <p role="status" className="text-xs text-muted-foreground">
          正在加载资源采样…
        </p>
      )}
      <div className="rounded-lg bg-muted p-3 text-xs">
        <p className="font-medium">
          观测阶段：{latest ? (trainingStatusLabels[latest.phase] ?? latest.phase) : "暂无采样"}
          {stale ? " · 采样已过期" : ""}
        </p>
        {latest && (
          <p className="mt-1 text-muted-foreground">
            本阶段已观测 {secondsLabel(latest.phase_elapsed_seconds)} · 采集于{" "}
            {new Date(latest.captured_at).toLocaleTimeString("zh-CN")}
          </p>
        )}
        <p className="mt-2 text-muted-foreground">
          训练剩余时间：
          {estimateLabel(latest, status, stale || (latest?.callback_age_seconds ?? 0) > 10)}
        </p>
      </div>
      <dl className="grid grid-cols-2 gap-2 xl:grid-cols-3">
        {keys.map((key) => {
          const metric = latest?.metrics[key];
          return (
            <div key={key} className="rounded-md border border-border p-3">
              <dt className="text-2xs text-muted-foreground">{resourceLabels[key]}</dt>
              <dd className="mt-1 font-mono text-sm tabular-nums">
                {resourceValue(key, metric?.value)}
              </dd>
              {data?.summary?.peaks?.[key] != null && (
                <dd className="mt-1 text-2xs text-muted-foreground">
                  采样峰值 {resourceValue(key, data.summary.peaks?.[key])}
                </dd>
              )}
              {metric?.value == null && (
                <dd className="mt-1 text-2xs text-muted-foreground">
                  {unavailableLabels[metric?.reason ?? ""] ?? "尚未收到采样"}
                </dd>
              )}
            </div>
          );
        })}
      </dl>
      <p className="text-2xs leading-relaxed text-muted-foreground">
        整卡数值包含同一 GPU 上的其他进程。RSS 为训练进程组求和；PyTorch
        数值来自子进程分配器。无法确认归属的进程 GPU 利用率不可用。
      </p>
      <details className="text-xs">
        <summary className="cursor-pointer font-medium">
          近期资源趋势 · {points.length} 条采样
        </summary>
        <div className="mt-3">
          <TrainingMetricChart
            title="整卡 GPU 利用率"
            points={points}
            keys={points.length ? ["gpu"] : []}
            labels={{ gpu: "整卡利用率（%）" }}
            domain={[0, 100]}
            empty="尚无资源采样"
            xDataKey="sample"
            xLabel="采样序号"
            pointUnit="条采样"
          />
        </div>
        {!!data?.gaps.length && (
          <p className="mt-2 text-muted-foreground">
            部分采样已因保留窗口或本地容量限制移除，图表不补造缺失数值。
          </p>
        )}
      </details>
    </section>
  );
}

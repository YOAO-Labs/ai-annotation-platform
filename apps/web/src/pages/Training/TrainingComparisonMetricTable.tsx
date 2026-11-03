import { listStyles } from "@/components/ui/listStyles";
import { cn } from "@/lib/utils";
import { metricLabel } from "./trainingPresentation";
import {
  comparisonMetricRanking,
  comparisonMetricValues,
  comparisonLatestPoint,
  type ComparisonExperiment,
  type ComparisonMetricSource,
} from "./trainingComparison";

const metricOrder = ["", "(B)", "(M)"].flatMap((suffix) =>
  ["precision", "recall", "mAP50", "mAP50-95", "mAP75", "AR100"].map(
    (key) => `metrics/${key}${suffix}`,
  ),
);
const metricPosition = (key: string) => {
  const position = metricOrder.indexOf(key);
  return position < 0 ? metricOrder.length : position;
};

const gapLabel = (delta: number, grouped: boolean) => {
  const points = Math.abs(delta * 100);
  const value = points >= 0.1 ? points.toFixed(1) : Number(points.toPrecision(1)).toString();
  return `较${grouped ? "组内" : ""}最优 -${value} 个百分点`;
};

export function TrainingComparisonMetricTable({
  experiments,
  source,
}: {
  experiments: ComparisonExperiment[];
  source: ComparisonMetricSource;
}) {
  const label = source === "best" ? "Best" : "Last";
  const values = experiments.map((e) => comparisonMetricValues(e, source));
  const metrics = [...new Set(values.flatMap(Object.keys))].sort(
    (a, b) => metricPosition(a) - metricPosition(b) || a.localeCompare(b),
  );
  return (
    <section aria-labelledby={`${source}-metrics-title`}>
      <h2 id={`${source}-metrics-title`} className="text-md font-semibold">
        {source === "best" ? "最佳权重最终评估" : "最近上报轮次指标"} · {label}
      </h2>
      <p className="mb-3 mt-1 text-xs leading-relaxed text-muted-foreground">
        {source === "best"
          ? "读取所选成功执行保存的最佳权重最终评估；续训可能沿用历史最佳权重。"
          : "读取所选执行最近上报的轮次指标，不代表最佳权重的最终评估。"}
        同口径下每行最优值高亮，并列最优同时标记；绿色差值表示落后最优值的百分点。
      </p>
      <div
        role="region"
        aria-label={`${label} 指标表`}
        tabIndex={0}
        className="overflow-x-auto rounded-lg border border-border bg-card focus-visible:ring-2 focus-visible:ring-ring"
      >
        <table className={cn(listStyles.table, "min-w-xl")}>
          <caption className="sr-only">{label} 指标与同行最优差距</caption>
          <thead className="bg-muted">
            <tr>
              <th scope="col">{label} · 指标</th>
              {experiments.map((e) => (
                <th scope="col" key={e.selection.runId}>
                  <span className="block">
                    {e.run?.name ?? "不可用实验"} · 执行 #{e.attempt?.ordinal ?? "?"}
                  </span>
                  <span className="mt-1 block text-2xs font-normal text-muted-foreground">
                    {source === "best"
                      ? e.report?.descriptor?.candidate.best_epoch != null
                        ? `最佳轮次 · 第 ${e.report.descriptor.candidate.best_epoch} 轮`
                        : e.report
                          ? "最佳轮次未记录"
                          : "最终评估不可用"
                      : comparisonLatestPoint(e)
                        ? `上报轮次 · 第 ${comparisonLatestPoint(e)!.epoch} 轮`
                        : e.eventsError || e.error
                          ? "轮次不可用"
                          : "暂无轮次事件"}
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {metrics.map((metric) => {
              const ranking = comparisonMetricRanking(experiments, source, metric);
              return (
                <tr key={metric}>
                  <th scope="row">{metricLabel(metric)}</th>
                  {experiments.map((e, index) => {
                    const value = values[index][metric];
                    const rank = ranking[index];
                    return (
                      <td
                        key={e.selection.runId}
                        className={cn("tabular-nums", rank?.best && "bg-primary/10")}
                      >
                        <span className={cn(rank?.best && "font-semibold text-primary")}>
                          {value == null ? "—" : value.toFixed(4)}
                        </span>
                        {rank && (
                          <span
                            className={cn(
                              "mt-1 block text-2xs",
                              rank.best ? "font-medium text-primary" : "text-status-positive",
                            )}
                          >
                            {rank.best
                              ? `${rank.grouped ? "组内" : ""}${rank.tied ? "并列最优" : "最优"}`
                              : gapLabel(rank.delta, rank.grouped)}
                          </span>
                        )}
                      </td>
                    );
                  })}
                </tr>
              );
            })}
            {!metrics.length && (
              <tr>
                <td colSpan={experiments.length + 1} className="text-muted-foreground">
                  {source === "best"
                    ? "所选执行暂无可用的最终评估指标。"
                    : "所选执行暂无可用的轮次指标。"}
                  缺失结果显示为 —，不参与排名。
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

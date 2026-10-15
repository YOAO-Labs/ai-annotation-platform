import { useState } from "react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/shadcn/ui/tabs";
import { Button } from "@/components/ui/Button";
import { trainingInputClass } from "./TrainingParameterField";
import { TrainingMetricChart } from "./TrainingMetricChart";
import { formatDuration, metricLabel } from "./trainingPresentation";
import {
  comparisonPoints,
  curveData,
  curveIncompatibility,
  incompatibility,
  inputDifferences,
  parseComparison,
  type ComparisonExperiment,
} from "./trainingComparison";

const valueLabel = (value: number | null | undefined) => (value == null ? "—" : value.toFixed(4));
const name = (e: ComparisonExperiment) =>
  `${e.run?.name ?? "不可用实验"} · 执行 #${e.attempt?.ordinal ?? "?"}`;
const tableClass =
  "w-full text-left text-xs [&_th]:px-3 [&_th]:py-3 [&_th]:font-medium [&_td]:px-3 [&_td]:py-3 [&_td]:align-top [&_tr]:border-b [&_tr]:border-border";

export function TrainingComparisonResults({
  experiments,
  state,
  onChange,
}: {
  experiments: ComparisonExperiment[];
  state: ReturnType<typeof parseComparison>;
  onChange: (key: string, value: string) => void;
}) {
  const columns =
    experiments.length === 2
      ? "md:grid-cols-2"
      : experiments.length === 3
        ? "md:grid-cols-2 xl:grid-cols-3"
        : "md:grid-cols-2 xl:grid-cols-4";
  const reference = experiments.find((e) => e.selection.runId === state.reference)!;
  const metrics = [...new Set(experiments.flatMap((e) => Object.keys(e.report?.summary ?? {})))];
  const rows = inputDifferences(experiments, state.showAll !== true);
  const maxClasses = Math.max(...experiments.map((e) => e.report?.per_class_total ?? 0));
  return (
    <Tabs value={state.view} onValueChange={(view) => onChange("view", view)}>
      <TabsList aria-label="比较视图">
        <TabsTrigger className="text-foreground" value="summary">
          最终结果
        </TabsTrigger>
        <TabsTrigger className="text-foreground" value="inputs">
          输入差异
        </TabsTrigger>
        <TabsTrigger className="text-foreground" value="curves">
          训练曲线
        </TabsTrigger>
      </TabsList>
      <TabsContent value="summary" className="mt-4 space-y-5">
        <div>
          <h2 className="text-md font-semibold">最佳权重的最终评估</h2>
          <p className="mt-1 text-xs text-muted-foreground">
            使用冻结的原生类别空间；注册模型的项目类别绑定仍在「模型与发布」中核对。差值以百分点表示。
          </p>
        </div>
        <div className={`grid items-start gap-3 ${columns}`}>
          {experiments.map((e) => {
            const reasons = incompatibility(reference.report, e.report);
            return (
              <section
                key={e.selection.runId}
                aria-label={`${name(e)}评估证据`}
                className="min-w-0 rounded-lg border border-border bg-card p-4"
              >
                <h3 className="break-words text-sm font-medium">{name(e)}</h3>
                {e.loading && (
                  <p role="status" className="mt-2 text-xs text-muted-foreground">
                    正在读取执行证据…
                  </p>
                )}
                {(e.error || e.reportError) && (
                  <p role="alert" className="mt-2 text-xs text-status-danger">
                    {e.error || e.reportError}
                  </p>
                )}
                {!e.report && !e.loading && !e.reportError && !e.error && (
                  <p className="mt-2 text-xs text-muted-foreground">
                    本次执行尚无成功发布的最终报告。
                  </p>
                )}
                {e.report && (
                  <>
                    <p
                      className={`mt-3 text-xs ${reasons.length ? "text-status-caution" : "text-status-positive"}`}
                    >
                      {reasons.length
                        ? reasons.join("；")
                        : e === reference
                          ? "已核验评估身份 · 参照实验"
                          : "评估口径兼容 · 可计算差值"}
                    </p>
                    <dl className="mt-3 space-y-2 text-xs">
                      <div>
                        <dt className="text-muted-foreground">最佳权重 SHA</dt>
                        <dd className="mt-1 break-all font-mono">
                          {String(e.report.provenance.weights_sha256)}
                        </dd>
                      </div>
                      <div>
                        <dt className="text-muted-foreground">最佳轮次</dt>
                        <dd>{e.report.descriptor?.candidate.best_epoch ?? "未记录"}</dd>
                      </div>
                    </dl>
                    <details className="mt-3 text-xs">
                      <summary className="cursor-pointer text-muted-foreground">
                        报告与评估身份
                      </summary>
                      <pre
                        tabIndex={0}
                        aria-label={`${name(e)}报告身份原文`}
                        className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-all rounded-md bg-muted p-2"
                      >
                        {JSON.stringify(
                          { descriptor: e.report.descriptor, provenance: e.report.provenance },
                          null,
                          2,
                        )}
                      </pre>
                    </details>
                  </>
                )}
              </section>
            );
          })}
        </div>
        <div
          role="region"
          aria-label="最终评估指标表"
          tabIndex={0}
          className="overflow-x-auto rounded-lg border border-border bg-card focus-visible:ring-2 focus-visible:ring-ring"
        >
          <table className={`${tableClass} min-w-xl`}>
            <caption className="sr-only">最佳权重最终评估与参照差值</caption>
            <thead className="sticky top-0 z-10 bg-muted">
              <tr>
                <th scope="col">指标</th>
                {experiments.map((e) => (
                  <th scope="col" key={e.selection.runId}>
                    {name(e)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {metrics.map((metric) => (
                <tr key={metric}>
                  <th scope="row">{metricLabel(metric)}</th>
                  {experiments.map((e) => {
                    const value = e.report?.summary[metric],
                      baseline = reference.report?.summary[metric];
                    const allowed =
                      e !== reference &&
                      value != null &&
                      baseline != null &&
                      incompatibility(reference.report, e.report).length === 0;
                    return (
                      <td key={e.selection.runId} className="tabular-nums">
                        <span>{valueLabel(value)}</span>
                        {allowed && (
                          <span className="mt-1 block text-2xs text-muted-foreground">
                            较参照 {value! - baseline! >= 0 ? "+" : ""}
                            {((value! - baseline!) * 100).toFixed(2)} 个百分点
                          </span>
                        )}
                      </td>
                    );
                  })}
                </tr>
              ))}
              {!metrics.length && (
                <tr>
                  <td colSpan={experiments.length + 1} className="text-muted-foreground">
                    暂无最终指标；缺失结果不会显示为零。
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        <section aria-label="本次执行进度与耗时">
          <h2 className="mb-3 text-md font-semibold">本次执行进度与耗时</h2>
          <div className={`grid gap-3 ${columns}`}>
            {experiments.map((e) => {
              const points = comparisonPoints(e),
                latest = points[points.length - 1];
              return (
                <article
                  key={e.selection.runId}
                  className="min-w-0 rounded-lg border border-border bg-card p-4"
                >
                  <h3 className="text-sm font-medium">{name(e)}</h3>
                  <dl className="mt-3 space-y-2 text-xs">
                    <div>
                      <dt className="text-muted-foreground">最新上报轮次</dt>
                      <dd>{latest ? `第 ${latest.epoch} 轮` : "暂无轮次事件"}</dd>
                    </div>
                    {latest &&
                      Object.entries(latest)
                        .filter(([key]) => key.startsWith("metrics/"))
                        .map(([key, value]) => (
                          <div key={key} className="flex justify-between gap-2">
                            <dt>{metricLabel(key)}</dt>
                            <dd className="tabular-nums">{valueLabel(value)}</dd>
                          </div>
                        ))}
                    <div>
                      <dt className="text-muted-foreground">执行墙钟耗时</dt>
                      <dd>
                        {e.attempt
                          ? formatDuration(e.attempt.started_at, e.attempt.completed_at)
                          : "—"}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-muted-foreground">停止原因</dt>
                      <dd>未记录结构化原因</dd>
                    </div>
                  </dl>
                  {e.eventsError && (
                    <p role="alert" className="mt-2 text-xs text-status-danger">
                      {e.eventsError}
                    </p>
                  )}
                </article>
              );
            })}
          </div>
          <p className="mt-2 text-xs text-muted-foreground">
            最新轮次不代表最佳权重。耗时包含准备、评估和上传，不是 GPU
            计算时间；提前完成不能据此认定为耐心早停。
          </p>
        </section>
        <section aria-label="分类别最终评估">
          <h2 className="mb-3 text-md font-semibold">分类别最终评估</h2>
          <div className="grid items-start gap-3 lg:grid-cols-2">
            {experiments.map((e) => (
              <article
                key={e.selection.runId}
                className="min-w-0 rounded-lg border border-border bg-card p-4"
              >
                <h3 className="mb-3 text-sm font-medium">{name(e)}</h3>
                {!e.report ? (
                  <p className="text-xs text-muted-foreground">最终报告不可用。</p>
                ) : !e.report.per_class_available ? (
                  <p className="text-xs text-muted-foreground">该评估器未提供分类别指标。</p>
                ) : !e.report.per_class_total ? (
                  <p className="text-xs text-muted-foreground">没有实际参与评估的类别。</p>
                ) : (
                  <div
                    role="region"
                    aria-label={`${name(e)}分类别指标表`}
                    tabIndex={0}
                    className="overflow-x-auto focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <table className={`${tableClass} min-w-sm`}>
                      <caption className="sr-only">{name(e)}分类别指标</caption>
                      <thead>
                        <tr>
                          <th scope="col">原生类别</th>
                          <th scope="col">Precision</th>
                          <th scope="col">Recall</th>
                          <th scope="col">mAP50</th>
                          <th scope="col">mAP50–95</th>
                        </tr>
                      </thead>
                      <tbody>
                        {e.report.per_class.map((row) => (
                          <tr key={row.index}>
                            <th scope="row">
                              {row.name}
                              <span className="ml-1 text-muted-foreground">#{row.index}</span>
                            </th>
                            {["precision", "recall", "mAP50", "mAP50-95"].map((key) => (
                              <td key={key} className="tabular-nums">
                                {valueLabel(row.metrics[key])}
                              </td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </article>
            ))}
          </div>
          {maxClasses > 50 && (
            <div className="mt-3 flex items-center justify-between gap-2 text-xs text-muted-foreground">
              <Button
                size="sm"
                disabled={!state.classPage}
                onClick={() => onChange("class_page", String(state.classPage - 1))}
              >
                上一页类别
              </Button>
              <span>第 {state.classPage + 1} 页 · 每页 50 类</span>
              <Button
                size="sm"
                disabled={(state.classPage + 1) * 50 >= maxClasses}
                onClick={() => onChange("class_page", String(state.classPage + 1))}
              >
                下一页类别
              </Button>
            </div>
          )}
        </section>
      </TabsContent>
      <TabsContent value="inputs" className="mt-4 space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 className="text-md font-semibold">冻结输入差异</h2>
          <label className="flex items-center gap-2 text-xs">
            <input
              type="checkbox"
              checked={state.showAll !== true}
              onChange={(event) => onChange("inputs", event.target.checked ? "changed" : "all")}
            />
            仅显示变化项
          </label>
        </div>
        <p className="text-xs text-muted-foreground">
          读取创建实验时冻结的生效值和来源，历史配置不会按当前参数定义重新计算。
        </p>
        <div
          role="region"
          aria-label="冻结输入差异表"
          tabIndex={0}
          className="max-h-[65vh] overflow-auto rounded-lg border border-border bg-card focus-visible:ring-2 focus-visible:ring-ring"
        >
          <table className={`${tableClass} min-w-xl`}>
            <caption className="sr-only">实验冻结输入差异</caption>
            <thead className="sticky top-0 z-10 bg-muted">
              <tr>
                <th scope="col">输入</th>
                {experiments.map((e) => (
                  <th scope="col" key={e.selection.runId}>
                    {name(e)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.key}>
                  <th scope="row" className="min-w-36">
                    {row.key}
                  </th>
                  {row.values.map((value, index) => (
                    <td key={experiments[index].selection.runId} className="min-w-48 max-w-sm">
                      <p
                        tabIndex={value.length > 160 ? 0 : undefined}
                        className="max-h-36 overflow-auto whitespace-pre-wrap break-all font-mono"
                      >
                        {value}
                      </p>
                      {row.origins[index] && (
                        <p className="mt-1 text-2xs text-muted-foreground">
                          来源：{row.origins[index]}
                        </p>
                      )}
                    </td>
                  ))}
                </tr>
              ))}
              {!rows.length && (
                <tr>
                  <td colSpan={experiments.length + 1} className="text-muted-foreground">
                    当前可读取的冻结输入没有变化项。
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </TabsContent>
      <TabsContent value="curves" className="mt-4">
        <ComparisonCurves
          experiments={experiments}
          metric={state.metric}
          smoothed={state.smoothing === "ema"}
          onChange={onChange}
        />
      </TabsContent>
    </Tabs>
  );
}

function ComparisonCurves({
  experiments,
  metric,
  smoothed,
  onChange,
}: {
  experiments: ComparisonExperiment[];
  metric: string;
  smoothed: boolean;
  onChange: (key: string, value: string) => void;
}) {
  const [page, setPage] = useState(0);
  const available = [
    ...new Set(
      experiments.flatMap((e) =>
        comparisonPoints(e).flatMap((point) => Object.keys(point).filter((key) => key !== "epoch")),
      ),
    ),
  ];
  const groups: ComparisonExperiment[][] = [];
  for (const e of experiments) {
    const group = groups.find((g) => curveIncompatibility(g[0], e, metric).length === 0);
    if (group) group.push(e);
    else groups.push([e]);
  }
  const raw = experiments.map((e) => new Map(comparisonPoints(e).map((p) => [p.epoch, p[metric]])));
  const epochs = [...new Set(raw.flatMap((map) => [...map.keys()]))].sort((a, b) => a - b);
  const currentPage = Math.min(page, Math.max(0, Math.ceil(epochs.length / 50) - 1));
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <label className="text-xs text-muted-foreground">
          曲线指标
          <select
            className={`${trainingInputClass} mt-1 max-w-sm`}
            value={metric}
            onChange={(event) => {
              setPage(0);
              onChange("metric", event.target.value);
            }}
          >
            {!available.includes(metric) && (
              <option value={metric}>{metricLabel(metric)} · 暂无数据</option>
            )}
            {available.map((key) => (
              <option key={key} value={key}>
                {metricLabel(key)}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-2 text-xs">
          <input
            type="checkbox"
            checked={smoothed}
            onChange={(event) => onChange("smoothing", event.target.checked ? "ema" : "raw")}
          />
          平滑曲线
        </label>
      </div>
      <p className="text-xs text-muted-foreground">
        {smoothed
          ? "指数平滑（当前点权重 0.4），遇到缺失或轮次间断时重新开始。"
          : "绘制原始上报值。"}
        平滑只影响绘图；下表与最终结果保持原值，缺失位置断线。
      </p>
      {groups.length > 1 && (
        <p className="rounded-lg bg-muted p-3 text-xs text-foreground">
          部分实验的评估身份或轮次参数不兼容，分别绘制。
          {[
            ...new Set(
              experiments.slice(1).flatMap((e) => curveIncompatibility(experiments[0], e, metric)),
            ),
          ].join("；")}
          。
        </p>
      )}
      <div className={`grid gap-3 ${groups.length > 1 ? "lg:grid-cols-2" : ""}`}>
        {groups.map((group) => {
          const points = curveData(group, metric, smoothed),
            keys = group
              .map((_, index) => `series${index}`)
              .filter((key) => points.some((p) => p[key] != null));
          return (
            <TrainingMetricChart
              headingLevel={2}
              key={group.map((e) => e.selection.runId).join(":")}
              title={`${metricLabel(metric)} · ${group.map(name).join(" / ")}`}
              points={points}
              keys={keys}
              labels={Object.fromEntries(group.map((e, index) => [`series${index}`, name(e)]))}
              domain={metric.startsWith("metrics/") ? [0, 1] : [0, "auto"]}
              empty="所选执行暂无该指标"
            />
          );
        })}
      </div>
      {experiments.some((e) => e.eventsError) && (
        <p role="alert" className="text-xs text-status-danger">
          {experiments
            .filter((e) => e.eventsError)
            .map((e) => `${name(e)}：${e.eventsError}`)
            .join("；")}
        </p>
      )}
      <div
        role="region"
        aria-label="原始轮次指标表"
        tabIndex={0}
        className="max-h-[65vh] overflow-auto rounded-lg border border-border bg-card focus-visible:ring-2 focus-visible:ring-ring"
      >
        <table className={`${tableClass} min-w-xl`}>
          <caption className="px-3 py-3 text-left text-sm font-medium">
            原始轮次指标 · {metricLabel(metric)}
          </caption>
          <thead className="sticky top-0 z-10 bg-muted">
            <tr>
              <th scope="col">轮次</th>
              {experiments.map((e) => (
                <th scope="col" key={e.selection.runId}>
                  {name(e)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {epochs.slice(currentPage * 50, (currentPage + 1) * 50).map((epoch) => (
              <tr key={epoch}>
                <th scope="row">{epoch}</th>
                {raw.map((map, index) => (
                  <td key={experiments[index].selection.runId} className="tabular-nums">
                    {valueLabel(map.get(epoch))}
                  </td>
                ))}
              </tr>
            ))}
            {!epochs.length && (
              <tr>
                <td colSpan={experiments.length + 1}>暂无轮次事件。</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {epochs.length > 50 && (
        <div className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
          <Button size="sm" disabled={!currentPage} onClick={() => setPage(currentPage - 1)}>
            上一页轮次
          </Button>
          <span>
            {currentPage + 1} / {Math.ceil(epochs.length / 50)}
          </span>
          <Button
            size="sm"
            disabled={(currentPage + 1) * 50 >= epochs.length}
            onClick={() => setPage(currentPage + 1)}
          >
            下一页轮次
          </Button>
        </div>
      )}
    </div>
  );
}

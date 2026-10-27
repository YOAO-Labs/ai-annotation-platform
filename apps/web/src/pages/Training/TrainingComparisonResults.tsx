import { ListPagination } from "@/components/ui/ListPagination";
import { listStyles } from "@/components/ui/listStyles";
import { CopyableError } from "@/components/ui/CopyableError";
import { useState } from "react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/shadcn/ui/tabs";
import { CopyValueButton } from "@/components/ui/CopyValueButton";
import { trainingInputClass } from "./TrainingParameterField";
import { TrainingMetricChart } from "./TrainingMetricChart";
import { TrainingComparisonMetricTable } from "./TrainingComparisonMetricTable";
import {
  formatDuration,
  metricLabel,
  segmentationTask,
  trainingTaskOf,
} from "./trainingPresentation";
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
const tableClass = listStyles.table;
const classMetrics = (e: ComparisonExperiment) => {
  const keys = ["precision", "recall", "mAP50", "mAP50-95"];
  return e.report?.descriptor?.evaluator.task === "instance-segmentation" ||
    trainingTaskOf(e.run?.config) === segmentationTask
    ? ["B", "M"].flatMap((kind) => keys.map((key) => `${key}(${kind})`))
    : keys;
};

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
  const rows = inputDifferences(experiments, state.showAll !== true);
  const maxClasses = Math.max(...experiments.map((e) => e.report?.per_class_total ?? 0));
  return (
    <Tabs value={state.view} onValueChange={(view) => onChange("view", view)}>
      <TabsList aria-label="比较视图">
        <TabsTrigger className="text-foreground" value="summary">
          指标结果
        </TabsTrigger>
        <TabsTrigger className="text-foreground" value="inputs">
          输入差异
        </TabsTrigger>
        <TabsTrigger className="text-foreground" value="curves">
          训练曲线
        </TabsTrigger>
      </TabsList>
      <TabsContent value="summary" className="mt-4 space-y-5">
        <TrainingComparisonMetricTable experiments={experiments} source="best" />
        <TrainingComparisonMetricTable experiments={experiments} source="last" />
        <p className="text-xs text-muted-foreground">
          仅对评估身份和对应指标口径兼容的实验计算最优值与差距，不兼容时按组比较；缺失或不可用指标不参与排名。
          使用冻结的原生类别空间，模型的项目类别绑定仍在「模型与发布」中核对。
        </p>
        <div className={`grid items-start gap-3 ${columns}`}>
          {experiments.map((e) => {
            const reasons = incompatibility(e.report, e.report);
            const hasPeer = experiments.some(
              (other) => other !== e && incompatibility(e.report, other.report).length === 0,
            );
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
                    <CopyableError message={e.error || e.reportError} />
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
                        : hasPeer
                          ? "评估口径兼容 · 可计算最优差距"
                          : "评估身份已核验 · 暂无同口径比较项"}
                    </p>
                    <dl className="mt-3 space-y-2 text-xs">
                      <div className="flex items-center gap-1">
                        <dt className="text-muted-foreground">最佳权重 SHA</dt>
                        <dd>
                          <CopyValueButton
                            value={String(e.report.provenance.weights_sha256 ?? "")}
                            label="复制最佳权重哈希"
                          />
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
                      <CopyableError message={e.eventsError} />
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
                          {classMetrics(e).map((key) => (
                            <th scope="col" key={key}>
                              {metricLabel(`metrics/${key}`)}
                            </th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {e.report.per_class.map((row) => (
                          <tr key={row.index}>
                            <th scope="row">
                              {row.name}
                              <span className="ml-1 text-muted-foreground">#{row.index}</span>
                            </th>
                            {classMetrics(e).map((key) => (
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
          <ListPagination
            label="类别"
            page={state.classPage}
            pageSize={state.classPageSize}
            total={maxClasses}
            onPageChange={(page) => onChange("class_page", String(page))}
            onPageSizeChange={(size) => onChange("class_page_size", String(size))}
            previousLabel="上一页类别"
            nextLabel="下一页类别"
          />
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
          metric={
            !state.metricExplicit &&
            experiments.length > 0 &&
            experiments.every((e) => trainingTaskOf(e.run?.config) === segmentationTask)
              ? "metrics/mAP50-95(M)"
              : state.metric
          }
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
  const [pageSize, setPageSize] = useState(20);
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
  const currentPage = Math.min(page, Math.max(0, Math.ceil(epochs.length / pageSize) - 1));
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
          <CopyableError
            message={experiments
              .filter((e) => e.eventsError)
              .map((e) => `${name(e)}：${e.eventsError}`)
              .join("；")}
          />
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
            {epochs.slice(currentPage * pageSize, (currentPage + 1) * pageSize).map((epoch) => (
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
      <ListPagination
        label="轮次"
        page={currentPage}
        pageSize={pageSize}
        total={epochs.length}
        onPageChange={setPage}
        onPageSizeChange={(size) => {
          setPageSize(size);
          setPage(0);
        }}
        previousLabel="上一页轮次"
        nextLabel="下一页轮次"
      />
    </div>
  );
}

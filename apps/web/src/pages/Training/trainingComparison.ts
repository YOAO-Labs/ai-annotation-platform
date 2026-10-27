import { listPageSize } from "@/components/ui/ListPagination";
import type {
  TrainingAttemptOut,
  TrainingEpochEvent,
  TrainingEvaluationOut,
  TrainingRunOut,
} from "@/api/trainingRuns";
import { record } from "@/api/trainedModels";
import {
  epochPoints,
  trainingTaskOf,
  trainingTaskLabel,
  segmentationTask,
} from "./trainingPresentation";

export type ComparisonSelection = { runId: string; attemptId: string };
export type ComparisonExperiment = {
  selection: ComparisonSelection;
  run?: TrainingRunOut;
  attempt?: TrainingAttemptOut;
  attempts: TrainingAttemptOut[];
  events: TrainingEpochEvent[];
  report?: TrainingEvaluationOut;
  error?: string;
  eventsError?: string;
  reportError?: string;
  loading: boolean;
};
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export const selectionValue = (item: ComparisonSelection) => `${item.runId}:${item.attemptId}`;

export function parseComparison(params: URLSearchParams) {
  const raw = params.getAll("runs");
  const selections: ComparisonSelection[] = [];
  let error = "";
  if (raw.length > 1 || (raw[0]?.length ?? 0) > 300) error = "比较链接的实验选择无效";
  for (const pair of raw[0]?.split(",").filter(Boolean) ?? []) {
    const [runId, attemptId, extra] = pair.toLowerCase().split(":");
    if (
      !uuid.test(runId) ||
      !uuid.test(attemptId ?? "") ||
      extra !== undefined ||
      selections.some((s) => s.runId === runId)
    ) {
      error = "比较链接须包含唯一的实验及其具体执行标识";
      break;
    }
    selections.push({ runId, attemptId });
  }
  if (selections.length > 4) error = "一次最多比较 4 个实验";
  const reference = params.get("reference") ?? selections[0]?.runId ?? "";
  if (reference && !selections.some((s) => s.runId === reference)) error = "参照实验不在当前选择中";
  const view = params.get("view") ?? "summary";
  if (!["summary", "inputs", "curves"].includes(view)) error = "比较链接的视图无效";
  const metric = params.get("metric") ?? "metrics/mAP50-95";
  if (!/^[a-zA-Z0-9_/().-]{1,100}$/.test(metric)) error = "比较链接的指标无效";
  const smoothing = params.get("smoothing") ?? "raw";
  if (!["raw", "ema"].includes(smoothing)) error = "比较链接的平滑选项无效";
  const classPage = params.get("class_page") ?? "0";
  if (!/^\d{1,3}$/.test(classPage) || Number(classPage) > 200) error = "分类别结果页码无效";
  const inputs = params.get("inputs") ?? "changed";
  if (!["changed", "all"].includes(inputs)) error = "输入差异选项无效";
  return {
    selections: error ? [] : selections,
    reference,
    view,
    metric,
    metricExplicit: params.has("metric"),
    smoothing,
    classPage: Number(classPage),
    classPageSize: listPageSize(params.get("class_page_size")),
    showAll: inputs === "all",
    error,
  };
}

export function stable(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`;
  if (value && typeof value === "object")
    return `{${Object.entries(value)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([k, v]) => `${JSON.stringify(k)}:${stable(v)}`)
      .join(",")}}`;
  return value === undefined ? "未知" : JSON.stringify(value);
}

export function frozenInputs(experiment: ComparisonExperiment) {
  const { run, attempt, attempts } = experiment;
  if (!run || !attempt) return {};
  const config = run.config;
  const configuration = record(config.configuration);
  const values = config.configuration
    ? record(configuration.effective_values)
    : { ...record(config.fixed_options), ...record(config.parameters) };
  const base = record(config.base_model);
  const initialization = record(config.initialization);
  const initializationArtifact = record(initialization.artifact);
  const parent = attempts.find((a) => a.id === attempt.resume_from_id);
  const checkpoint = Object.values(parent?.artifacts ?? {})
    .map(record)
    .find((a) => a.role === "checkpoint");
  return {
    数据版本: run.dataset_id,
    训练任务: trainingTaskLabel(trainingTaskOf(config)),
    数据桥接: record(config.data_contract).bridge ?? "矩形框检测",
    训练包摘要: config.dataset_digest,
    冻结验证摘要: config.validation_digest,
    原生类别顺序: config.classes,
    框架: config.framework,
    框架版本: config.framework_version,
    适配器修订: config.adapter_revision,
    基础权重: base.id,
    "基础权重 SHA": base.sha256,
    本次初始化: attempt.resume_from_id
      ? "完整检查点续训"
      : initialization.kind === "trained_model"
        ? "项目模型权重 · 全新训练"
        : "预训练基础权重",
    原始初始化模型: initialization.model_version_id ?? "目录权重",
    原始初始化来源摘要: initialization.source_digest ?? "目录权重",
    "原始初始化权重 SHA": initializationArtifact.sha256 ?? base.sha256,
    续训来源执行: attempt.resume_from_id ?? "无",
    "续训检查点 SHA": attempt.resume_from_id ? checkpoint?.sha256 : "无",
    续训检查点轮次: attempt.resume_from_id ? checkpoint?.epoch : "无",
    运行环境: attempt.runtime,
    参数定义: configuration.identity ?? "旧配置：仅已保存的六项参数及固定选项，其他默认值未知",
    ...Object.fromEntries(Object.entries(values).map(([key, value]) => [`参数 · ${key}`, value])),
    ...Object.fromEntries(
      Object.entries(record(configuration.inactive_fields)).map(([key, value]) => [
        `未生效 · ${key}`,
        value,
      ]),
    ),
  };
}

export function inputDifferences(experiments: ComparisonExperiment[], changedOnly: boolean) {
  const inputs = experiments.map(frozenInputs);
  const keys = [...new Set(inputs.flatMap((input) => Object.keys(input)))];
  return keys
    .map((key) => ({
      key,
      values: inputs.map((input) => stable((input as Record<string, unknown>)[key])),
      origins: experiments.map((e) =>
        key.startsWith("参数 · ")
          ? String(
              record(record(e.run?.config.configuration).origins)[key.slice(5)] ??
                "旧配置：来源未知",
            )
          : "",
      ),
    }))
    .filter((row) => !changedOnly || new Set(row.values).size > 1);
}

export function incompatibility(a?: TrainingEvaluationOut, b?: TrainingEvaluationOut): string[] {
  if (!a || !b) return ["最终评估尚不可用"];
  if (!a.descriptor || !b.descriptor || !a.compatibility_digest || !b.compatibility_digest)
    return [...new Set([...a.reasons, ...b.reasons, "缺少完整评估身份"])];
  if (a.compatibility_digest === b.compatibility_digest) return [];
  const reasons = [];
  if (stable(a.descriptor.benchmark) !== stable(b.descriptor.benchmark))
    reasons.push("冻结验证基准或原生类别顺序不同");
  if (stable(a.descriptor.evaluator) !== stable(b.descriptor.evaluator))
    reasons.push("评估器、版本、源码或实际选项不同");
  if (stable(a.descriptor.semantics) !== stable(b.descriptor.semantics))
    reasons.push("指标语义不同");
  if (!reasons.length) reasons.push("实际参与评估的类别覆盖不同");
  return reasons;
}

export function comparisonPoints(experiment: ComparisonExperiment) {
  const points = epochPoints(experiment.events, String(experiment.run?.config.framework));
  const byEpoch = new Map<number, Record<string, number>>();
  for (const point of points) {
    if (!Number.isInteger(point.epoch) || point.epoch < 1 || point.epoch > 1000) continue;
    const normalized = Object.fromEntries(
      Object.entries(point).map(([key, value]) => [
        trainingTaskOf(experiment.run?.config) === segmentationTask
          ? key
          : key.replace(/\(B\)$/, ""),
        value,
      ]),
    );
    byEpoch.set(point.epoch, normalized);
  }
  return [...byEpoch.values()].sort((a, b) => a.epoch - b.epoch);
}

export function comparisonLatestPoint(experiment: ComparisonExperiment) {
  const points = comparisonPoints(experiment);
  return points[points.length - 1];
}

export function curveIncompatibility(
  a: ComparisonExperiment,
  b: ComparisonExperiment,
  metric: string,
) {
  const reasons = incompatibility(a.report, b.report);
  const inputs = [a, b].map((e) => record(record(e.run?.config.configuration).effective_values));
  if (inputs.some((v) => !Object.keys(v).length)) reasons.push("旧配置缺少完整的轮次评估参数");
  const keys = new Set(
    inputs.flatMap((v) =>
      Object.keys(v).filter(
        (k) =>
          /^(imgsz|batch|amp|conf|iou|max_det|rect|single_cls|classes|agnostic_nms|fraction|augment|eval_interval|faster_coco_eval|quantize|nms|channels_last|overlap_mask|mask_ratio)$/.test(
            k,
          ) ||
          (!metric.startsWith("metrics/") && /loss|box|cls|dfl|iou|bbox/.test(k)),
      ),
    ),
  );
  if ([...keys].some((k) => stable(inputs[0][k]) !== stable(inputs[1][k])))
    reasons.push("轮次评估或损失参数不同");
  return [...new Set(reasons)];
}

export type ComparisonMetricSource = "best" | "last";

export function comparisonMetricValues(
  experiment: ComparisonExperiment,
  source: ComparisonMetricSource,
): Record<string, number | null> {
  if (experiment.error || (source === "best" ? experiment.reportError : experiment.eventsError))
    return {};
  const values = source === "best" ? experiment.report?.summary : comparisonLatestPoint(experiment);
  return Object.fromEntries(
    Object.entries(values ?? {})
      .filter(([key]) => key.startsWith("metrics/"))
      .map(([key, value]) => [
        key,
        typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null,
      ]),
  );
}

/** Rank each metric within its own compatible evidence group, independently of URL reference. */
export function comparisonMetricRanking(
  experiments: ComparisonExperiment[],
  source: ComparisonMetricSource,
  metric: string,
) {
  const values = experiments.map((e) => comparisonMetricValues(e, source)[metric]);
  const compatible = (a: ComparisonExperiment, b: ComparisonExperiment) =>
    (source === "best" ? incompatibility(a.report, b.report) : curveIncompatibility(a, b, metric))
      .length === 0;
  const groups: number[][] = [];
  experiments.forEach((experiment, index) => {
    if (values[index] == null || !compatible(experiment, experiment)) return;
    const group = groups.find((items) =>
      items.every((other) => compatible(experiments[other], experiment)),
    );
    if (group) group.push(index);
    else groups.push([index]);
  });
  const ranking: ({ best: boolean; tied: boolean; grouped: boolean; delta: number } | undefined)[] =
    experiments.map(() => undefined);
  for (const group of groups) {
    if (group.length < 2) continue;
    const best = Math.max(...group.map((index) => values[index]!));
    const winners = group.filter((index) => Math.abs(values[index]! - best) <= 1e-12);
    group.forEach((index) => {
      ranking[index] = {
        best: winners.includes(index),
        tied: winners.length > 1,
        grouped: groups.length > 1,
        delta: winners.includes(index) ? 0 : values[index]! - best,
      };
    });
  }
  return ranking;
}

/** Null entries mark absent observations, never interpolated metric values. */
export function curveData(
  experiments: ComparisonExperiment[],
  metric: string,
  smoothed: boolean,
): Record<string, number | null>[] {
  const rows = experiments.map(comparisonPoints);
  const epochs = new Set(rows.flatMap((points) => points.map((p) => p.epoch)));
  for (const points of rows)
    for (let i = 1; i < points.length; i++)
      if (points[i].epoch - points[i - 1].epoch > 1) epochs.add(points[i - 1].epoch + 1);
  const ordered = [...epochs].sort((a, b) => a - b);
  const maps = rows.map((points) => new Map(points.map((p) => [p.epoch, p[metric]])));
  const previous: (number | null)[] = maps.map(() => null);
  return ordered.map((epoch) => ({
    epoch,
    ...Object.fromEntries(
      maps.map((map, index) => {
        const raw = map.get(epoch);
        const value =
          raw === undefined || !Number.isFinite(raw)
            ? null
            : smoothed && previous[index] !== null
              ? previous[index]! * 0.6 + raw * 0.4
              : raw;
        previous[index] = value;
        return [`series${index}`, value];
      }),
    ),
  }));
}

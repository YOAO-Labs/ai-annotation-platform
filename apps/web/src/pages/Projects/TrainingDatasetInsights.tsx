import { ListPagination, listPageSize } from "@/components/ui/ListPagination";
import { listStyles } from "@/components/ui/listStyles";
import { randomId } from "@/utils/id";
import { CopyableError } from "@/components/ui/CopyableError";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Image as ImageIcon } from "lucide-react";
import {
  trainingDatasetsApi,
  type DatasetSampleChange,
  type DatasetSampleFilters,
  type TrainingDatasetSummary,
  type TrainingDatasetAnalyzedSampleOut,
} from "@/api/trainingDatasets";
import { useAuthStore } from "@/stores/authStore";
import { Button } from "@/components/ui/Button";
import { CopyValueButton } from "@/components/ui/CopyValueButton";
import { TrainingDatasetSamplePreview } from "./TrainingDatasetSamplePreview";
import { segmentationTask, trainingTaskLabel } from "../Training/trainingPresentation";
import { TrainingDatasetClassChart, TrainingDatasetSplitChart } from "./TrainingDatasetCharts";
import { TrainingDatasetDetectionCharts } from "./TrainingDatasetDetectionCharts";
import { TrainingDatasetSampleComparison } from "./TrainingDatasetSampleComparison";
import { datasetComparisonReason, datasetParentReason } from "./datasetCompatibility";
import { instanceMaskBridge } from "../Training/trainingDatasetBridges";
import { useTrainingSessionRevision } from "../Training/trainingViewOwner";

const inputClass =
  "w-full min-w-0 rounded-md border border-border bg-background px-3 py-2 text-xs text-foreground focus-visible:ring-2 focus-visible:ring-ring";
const changes: Record<DatasetSampleChange, string> = {
  all: "全部包内样本",
  added: "新增内容",
  removed: "移除内容",
  labels_changed: "同内容标注修正",
  split_changed: "分区变化",
  multiplicity_changed: "重复次数变化",
  task_ids_changed: "任务 ID 变化",
  reintroduced: "祖先内容重新纳入",
  unchanged: "训练语义未变化",
  unknown: "训练语义变化无法确认",
  source_relation_changed: "来源关系变化",
  source_evidence_updated: "来源证据更新",
  source_unknown: "来源证据不足",
};
const exclusionReasons: Record<string, string> = {
  unsupported_media: "不是图片任务",
  unusable_sample: "当时标记为不可用",
  not_reviewed: "当时尚未审核通过",
  unconfirmed_negative: "当时未明确确认无目标",
  package_deduplicated: "入包时合并重复内容",
  not_in_package: "未出现在保留的训练包中",
};

function listPage(raw: string | null) {
  const value = Number(raw);
  return Number.isSafeInteger(value) && value >= 0 && Number.isSafeInteger(value * 100) ? value : 0;
}

function Pages({
  page,
  total,
  size,
  loading = false,
  label,
  onChange,
  onSizeChange,
}: {
  page: number;
  total: number;
  size: number;
  loading?: boolean;
  label: string;
  onChange: (page: number) => void;
  onSizeChange: (size: number) => void;
}) {
  return (
    <ListPagination
      label={label}
      page={page}
      pageSize={size}
      total={total}
      loading={loading}
      previousLabel={`上一页${label}`}
      nextLabel={`下一页${label}`}
      onPageChange={onChange}
      onPageSizeChange={onSizeChange}
    />
  );
}

function Distribution({
  title,
  rows,
}: {
  title: string;
  rows: { key: string; label: string; train: number; val: number }[];
}) {
  return (
    <section className="rounded-lg border border-border p-4">
      <h3 className="mb-2 text-xs font-semibold">{title}</h3>
      <table className={listStyles.table}>
        <thead className="text-muted-foreground">
          <tr>
            <th className="py-2 font-medium">分布范围</th>
            <th className="py-2 text-right font-medium">训练</th>
            <th className="py-2 text-right font-medium">验证</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.key} className="border-t border-border">
              <td className="py-2">{row.label}</td>
              <td className="py-2 text-right tabular-nums">{row.train}</td>
              <td className="py-2 text-right tabular-nums">{row.val}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

export function TrainingDatasetInsights(props: {
  projectId: string;
  version: TrainingDatasetSummary;
  onClose: () => void;
}) {
  const userId = useAuthStore((state) => state.user?.id ?? "");
  const token = useAuthStore((state) => state.token ?? "");
  const revision = useTrainingSessionRevision();
  const owner = useMemo(
    () => ({
      userId,
      token,
      revision,
      projectId: props.projectId,
      versionId: props.version.id,
      key: randomId(),
    }),
    [userId, token, revision, props.projectId, props.version.id],
  );
  return <InsightsContent key={owner.key} {...props} owner={owner} />;
}

function InsightsContent({
  projectId,
  version,
  onClose,
  owner,
}: {
  projectId: string;
  version: TrainingDatasetSummary;
  onClose: () => void;
  owner: { userId: string; token: string; key: string };
}) {
  const [params, setParams] = useSearchParams();
  const client = useQueryClient();
  const rawView = params.get("data_view");
  const view = rawView === "samples" || rawView === "compare" ? rawView : "overview";
  const baselineId = params.get("data_baseline") ?? "";
  const change = (params.get("data_change") ?? "all") as DatasetSampleChange;
  const rawClass = params.get("data_class") ?? "";
  const classIndex = /^\d+$/.test(rawClass) ? Number(rawClass) : undefined;
  const rawPage = params.get("data_page") ?? "0";
  const page = /^\d+$/.test(rawPage) ? Number(rawPage) : 0;
  const pageSize = listPageSize(params.get("data_page_size"));
  const classPageSize = listPageSize(params.get("data_class_page_size"));
  const filterPageSize = listPageSize(params.get("data_filter_page_size"));
  const baselinePageSize = listPageSize(params.get("data_baseline_page_size"));
  const invalidFilters =
    (rawClass !== "" && classIndex === undefined) ||
    !/^\d+$/.test(rawPage) ||
    !Number.isSafeInteger(page) ||
    !Number.isSafeInteger(page * pageSize) ||
    (classIndex !== undefined && !Number.isSafeInteger(classIndex));
  const kind = (params.get("data_kind") ?? "all") as DatasetSampleFilters["kind"];
  const split = (params.get("data_split") || undefined) as DatasetSampleFilters["split"];
  const [classSearch, setClassSearch] = useState("");
  const classPage = listPage(params.get("data_class_page"));
  const setClassPage = (value: number) => update({ data_class_page: String(value) });
  const [filterSearch, setFilterSearch] = useState("");
  const filterPage = listPage(params.get("data_filter_page"));
  const setFilterPage = (value: number) => update({ data_filter_page: String(value) });
  const baselinePage = listPage(params.get("data_baseline_page"));
  const setBaselinePage = (value: number) => update({ data_baseline_page: String(value) });
  const [preview, setPreview] = useState<{
    sample: TrainingDatasetAnalyzedSampleOut;
    scope: string;
    opener: HTMLButtonElement;
  } | null>(null);
  const [rawOpen, setRawOpen] = useState(false);
  const [comparisonPreview, setComparisonPreview] = useState<{
    sha: string;
    scope: string;
    opener: HTMLButtonElement;
  } | null>(null);
  const ready = version.status === "ready";
  const key = ["training-data-insights", owner.key, projectId, version.id];
  const update = (values: Record<string, string>) =>
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      Object.entries(values).forEach(([name, value]) =>
        value ? next.set(name, value) : next.delete(name),
      );
      return next;
    });
  const overview = useQuery({
    queryKey: [...key, "overview"],
    queryFn: ({ signal }) => trainingDatasetsApi.insights(projectId, version.id, { signal }),
    enabled: ready,
    retry: false,
  });
  const classes = useQuery({
    queryKey: [...key, "classes", classSearch, classPage, classPageSize],
    queryFn: ({ signal }) =>
      trainingDatasetsApi.classes(
        projectId,
        version.id,
        { search: classSearch, offset: classPage * classPageSize, limit: classPageSize },
        { signal },
      ),
    enabled: ready && view === "overview",
  });
  const baseline = useQuery({
    queryKey: [...key, "baseline", baselineId],
    queryFn: async ({ signal }) => {
      const value = await trainingDatasetsApi.getSummary(projectId, baselineId, { signal });
      const reason = datasetComparisonReason(version, value);
      if (reason) throw new Error(`比较基线不可用：${reason}`);
      return value;
    },
    enabled: ready && !!baselineId,
    retry: false,
  });
  const baselineChoices = useQuery({
    queryKey: [
      ...key,
      "baseline-choices",
      version.task ?? "detect",
      baselinePage,
      baselinePageSize,
    ],
    queryFn: ({ signal }) =>
      trainingDatasetsApi.list(
        projectId,
        baselinePage * baselinePageSize,
        "ready",
        { signal },
        { task: version.task ?? "detect" },
        baselinePageSize,
      ),
    enabled: ready && view === "compare",
  });
  const baselineOverview = useQuery({
    queryKey: [...key, "baseline-overview", baselineId],
    queryFn: ({ signal }) => trainingDatasetsApi.insights(projectId, baselineId, { signal }),
    enabled:
      ready &&
      !!baselineId &&
      baseline.isSuccess &&
      !baseline.isError &&
      (view === "compare" || view === "samples"),
    retry: false,
  });
  const compared = useQuery({
    queryKey: [...key, "compare", baselineId],
    queryFn: ({ signal }) =>
      trainingDatasetsApi.compare(projectId, version.id, baselineId, { signal }),
    enabled:
      ready &&
      !!baselineId &&
      baseline.isSuccess &&
      !baseline.isError &&
      (view === "compare" || (view === "samples" && change !== "all")),
    retry: false,
  });
  const sampleVersionId = change === "removed" ? baselineId : version.id;
  const baselineCompatible = !baselineId || (baseline.isSuccess && !baseline.isError);
  const filterClasses = useQuery({
    queryKey: [...key, "filter-classes", sampleVersionId, filterSearch, filterPage, filterPageSize],
    queryFn: ({ signal }) =>
      trainingDatasetsApi.classes(
        projectId,
        sampleVersionId,
        { search: filterSearch, offset: filterPage * filterPageSize, limit: filterPageSize },
        { signal },
      ),
    enabled: ready && view === "samples" && !!sampleVersionId && baselineCompatible,
  });
  const selectedClass = useQuery({
    queryKey: [...key, "selected-class", sampleVersionId, classIndex],
    queryFn: ({ signal }) =>
      trainingDatasetsApi.classes(
        projectId,
        sampleVersionId,
        { offset: classIndex, limit: 1 },
        { signal },
      ),
    enabled:
      ready &&
      view === "samples" &&
      !invalidFilters &&
      classIndex !== undefined &&
      !!sampleVersionId &&
      baselineCompatible,
  });
  const filters: DatasetSampleFilters = {
    limit: pageSize,
    offset: page * pageSize,
    kind,
    split,
    class_index: classIndex,
    baseline_id: baselineId || undefined,
    change,
  };
  const sampleScope = JSON.stringify([view, filters]);
  useEffect(() => {
    setPreview(null);
    setComparisonPreview(null);
  }, [sampleScope]);
  const samples = useQuery({
    queryKey: [...key, "samples", filters],
    queryFn: ({ signal }) =>
      trainingDatasetsApi.samples(projectId, version.id, filters, { signal }),
    enabled: ready && view === "samples" && !invalidFilters && baselineCompatible,
    retry: false,
  });
  useEffect(() => {
    const pages = [
      ["data_class_page", classPage, classPageSize, classes.data?.total],
      ["data_filter_page", filterPage, filterPageSize, filterClasses.data?.total],
      ["data_baseline_page", baselinePage, baselinePageSize, baselineChoices.data?.total],
      ["data_page", page, pageSize, samples.data?.total],
    ] as const;
    const changed = pages.filter(
      ([, current, size, total]) =>
        total != null && current > Math.max(0, Math.ceil(total / size) - 1),
    );
    if (changed.length)
      setParams(
        (previous) => {
          const next = new URLSearchParams(previous);
          for (const [name, , size, total] of changed)
            next.set(name, String(Math.max(0, Math.ceil(total! / size) - 1)));
          return next;
        },
        { replace: true },
      );
  }, [
    classPage,
    classPageSize,
    classes.data?.total,
    filterPage,
    filterPageSize,
    filterClasses.data?.total,
    baselinePage,
    baselinePageSize,
    baselineChoices.data?.total,
    page,
    pageSize,
    samples.data?.total,
    setParams,
  ]);
  const raw = useQuery({
    queryKey: [...key, "raw-manifest"],
    queryFn: ({ signal }) => trainingDatasetsApi.get(projectId, version.id, { signal }),
    enabled: rawOpen,
    retry: false,
  });
  const observed = useCallback(() => {
    void client.invalidateQueries({
      queryKey: ["training-data-insights", owner.key, projectId, version.id, "overview"],
    });
  }, [client, owner.key, projectId, version.id]);
  const showSamples = (values: Record<string, string>) =>
    update({
      data_view: "samples",
      data_page: "0",
      data_class: "",
      data_split: "",
      data_kind: "all",
      ...values,
    });
  const data = overview.data;
  const baselineUnknownContents =
    baselineOverview.data?.observations.unknown_contents ??
    (baselineOverview.data?.status === "complete" ? 0 : null);
  const targetUnknownContents =
    data?.observations.unknown_contents ?? (data?.status === "complete" ? 0 : null);
  const error =
    view === "overview"
      ? classes.error
      : view === "compare"
        ? baselineChoices.error || baseline.error || compared.error
        : baseline.error || samples.error || filterClasses.error || selectedClass.error;
  return (
    <section
      className="min-w-0 rounded-xl border border-border bg-card p-4 sm:p-5"
      aria-label="数据版本详情"
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <h2 className="break-words text-lg font-semibold tracking-tight">{version.name}</h2>
          <p className="mt-1 text-2xs text-muted-foreground">
            {version.version_ordinal != null
              ? `任务版本 #${version.version_ordinal}`
              : "历史版本 · 待关联任务序号"}
            {" · "}
            {version.status === "ready" ? "已就绪" : version.status}
          </p>
          <p className="mt-1 text-xs text-muted-foreground">
            {trainingTaskLabel(version.task)} · 冻结时刻的数据、标注与划分 ·
            后续任务编辑不改变这里的统计
          </p>
          <p className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-2xs text-muted-foreground">
            <time dateTime={version.created_at}>
              {new Date(version.created_at).toLocaleString("zh-CN")}
            </time>
            <span>{version.format ?? "ultralytics-yolo"}</span>
            {version.contract_profile && (
              <span>
                {version.contract_profile.source_media_type ?? "未知来源"} →{" "}
                {version.contract_profile.media_type ?? "未知输出"} ·{" "}
                {version.contract_profile.geometry} ·{" "}
                {version.contract_profile.bridge === instanceMaskBridge
                  ? "原生实例掩码训练"
                  : "原生训练桥接"}
              </span>
            )}
            {version.completed_at && (
              <time dateTime={version.completed_at}>
                完成于 {new Date(version.completed_at).toLocaleString("zh-CN")}
              </time>
            )}
            {version.size_bytes != null && (
              <span>训练包 {(version.size_bytes / 1024 / 1024).toFixed(2)} MiB</span>
            )}
            {version.parent_id && (
              <span className="inline-flex items-center gap-1">
                继承自
                <CopyValueButton value={version.parent_id} kind="id" label="复制父数据版本 ID" />
              </span>
            )}
          </p>
        </div>
        <Button size="sm" variant="ghost" onClick={onClose}>
          收起数据详情
        </Button>
      </div>
      {data && (
        <dl aria-label="冻结版本摘要" className="mt-4 grid grid-cols-2 gap-3 lg:grid-cols-5">
          {[
            ["训练包图片", data.counts.images],
            [data.status === "complete" ? "标注实例" : "已知标注实例", data.counts.instances],
            ["定义类别", data.classes_total],
            [
              data.status === "complete" ? "观察到的类别" : "已知观察类别",
              data.counts.observed_classes ?? "未知",
            ],
            [
              (data.observations.unknown_contents ?? 0) > 0 ? "已知唯一图片内容" : "唯一图片内容",
              data.counts.unique_contents,
            ],
          ].map(([label, value]) => (
            <div key={label} className="rounded-lg border border-border bg-muted/30 p-3">
              <dt className="text-xs text-muted-foreground">{label}</dt>
              <dd className="mt-2 text-2xl font-semibold tracking-tight tabular-nums">{value}</dd>
            </div>
          ))}
        </dl>
      )}
      {ready && (
        <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
          <p className="flex items-center gap-1 text-2xs text-muted-foreground">
            冻结版本
            <CopyValueButton value={version.id} kind="id" label="复制冻结数据版本 ID" />·
            后续项目编辑不改变此版本
          </p>
          {!datasetParentReason(version, version.task ?? "detect") ? (
            <Link
              className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-xs font-medium text-foreground transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              to={`/projects/${projectId}/learning/new?${new URLSearchParams({ dataset: version.id, tab: "data", return_tab: "data", return_dataset: version.id, data_task: version.task ?? "detect" })}`}
            >
              使用此版本新建实验
            </Link>
          ) : (
            <span className="text-2xs text-muted-foreground">
              训练入口不可用：{datasetParentReason(version, version.task ?? "detect")}
            </span>
          )}
        </div>
      )}
      <nav className="mt-4 flex gap-2 border-b border-border pb-3" aria-label="数据洞察视图">
        {(
          [
            ["overview", "概览与类别"],
            ["samples", "冻结样本"],
            ["compare", "版本比较"],
          ] as const
        ).map(([value, label]) => (
          <Button
            key={value}
            size="sm"
            variant="ghost"
            className={
              view === value
                ? "bg-chart-1/10 text-chart-1 hover:bg-chart-1/15"
                : "text-muted-foreground"
            }
            aria-pressed={view === value}
            onClick={() => update({ data_view: value })}
          >
            {label}
          </Button>
        ))}
      </nav>
      {!ready && (
        <p className="mt-4 text-sm text-muted-foreground">
          版本构建完成后可查看冻结数据洞察。
          <CopyableError message={version.error_message} />
        </p>
      )}
      {ready && overview.isLoading && (
        <p role="status" className="py-8 text-sm text-muted-foreground">
          正在分析冻结数据…
        </p>
      )}
      {overview.error && (
        <div
          role="alert"
          className="mt-4 rounded-md bg-status-caution-soft p-3 text-sm text-status-caution"
        >
          <p>
            数据洞察暂不可用：
            <CopyableError message={overview.error.message} />
            。训练包下载入口仍独立可用。
          </p>
          <Button size="sm" className="mt-2" onClick={() => void overview.refetch()}>
            重试数据洞察
          </Button>
        </div>
      )}
      {data && data.status !== "complete" && (
        <p
          role="status"
          className="mt-4 rounded-md bg-status-caution-soft p-3 text-xs text-status-caution"
        >
          {data.status === "partial" ? "部分冻结元数据未保留" : "冻结标注不可用"}：标注未知{" "}
          {data.observations.unknown_labels ?? 0} 张，尺寸未知 {data.dimensions.unknown ?? 0}{" "}
          张。以下只统计可确认的数据。
        </p>
      )}
      {error && (
        <p role="alert" className="mt-4 text-sm text-status-danger">
          <CopyableError message={error.message} />
        </p>
      )}
      {ready && view === "overview" && data && (
        <div className="mt-5 space-y-5">
          <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,0.85fr)_minmax(0,1.4fr)]">
            <TrainingDatasetSplitChart
              splits={data.splits}
              totalMembers={data.counts.images}
              unknownMembers={data.observations.unknown_split ?? 0}
              onSelect={(split) => showSamples({ data_split: split, data_change: "all" })}
            />
            <section
              aria-label="冻结类别分布"
              className="min-w-0 rounded-xl border border-border bg-card p-4"
            >
              <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                <h3 className="text-sm font-semibold">类别分布</h3>
                <label className="text-xs text-muted-foreground">
                  搜索类别
                  <input
                    className={`${inputClass} mt-1`}
                    value={classSearch}
                    onChange={(event) => {
                      setClassSearch(event.target.value);
                      setClassPage(0);
                    }}
                    placeholder="按冻结类别名称查找"
                  />
                </label>
              </div>
              <p className="mb-2 text-xs text-muted-foreground">
                “图片”表示出现该类的图片项数；一张图片可能包含多个实例。
                {data.status !== "complete"
                  ? "以下未出现类别仅依据已知标注，不能断言整个版本缺少该类别。"
                  : ""}
                验证集中已知标注未出现{" "}
                {data.observations.classes_with_no_known_validation_instances ??
                  data.observations.classes_absent_from_validation ??
                  "未知"}{" "}
                个类别，训练集中未出现{" "}
                {data.observations.classes_with_no_known_train_instances ??
                  data.observations.classes_absent_from_train ??
                  "未知"}{" "}
                个类别。
              </p>
              <TrainingDatasetClassChart
                rows={classes.data?.items ?? []}
                partial={data.status !== "complete"}
                onSelect={(index, split) =>
                  showSamples({
                    data_class: String(index),
                    data_split: split ?? "",
                    data_change: "all",
                  })
                }
              />
              {classes.isLoading && (
                <p role="status" className="py-4 text-xs text-muted-foreground">
                  正在加载类别分布…
                </p>
              )}
              <details className="mt-3 text-xs">
                <summary className="cursor-pointer text-muted-foreground">完整类别明细</summary>
                <div className={listStyles.surface}>
                  <div className={listStyles.scroll}>
                    <table className={listStyles.table}>
                      <thead className="text-muted-foreground">
                        <tr>
                          <th className="py-2 font-medium">冻结类别</th>
                          <th className="py-2 text-right font-medium">训练图片</th>
                          <th className="py-2 text-right font-medium">验证图片</th>
                          <th className="py-2 text-right font-medium">训练实例</th>
                          <th className="py-2 text-right font-medium">验证实例</th>
                        </tr>
                      </thead>
                      <tbody>
                        {classes.data?.items.map((row) => (
                          <tr key={row.index} className="border-t border-border">
                            <td className="py-3 pr-3">
                              <button
                                className="cursor-pointer text-left hover:underline"
                                onClick={() =>
                                  showSamples({ data_class: String(row.index), data_change: "all" })
                                }
                              >
                                {row.name}
                              </button>
                            </td>
                            <td className="py-3 text-right tabular-nums">
                              <button
                                className="cursor-pointer hover:underline disabled:cursor-default"
                                aria-label={`查看 ${row.name} 训练图片`}
                                disabled={!row.train_images}
                                onClick={() =>
                                  showSamples({
                                    data_class: String(row.index),
                                    data_split: "train",
                                    data_change: "all",
                                  })
                                }
                              >
                                {row.train_images}
                              </button>
                            </td>
                            <td className="py-3 text-right tabular-nums">
                              <button
                                className="cursor-pointer hover:underline disabled:cursor-default"
                                aria-label={`查看 ${row.name} 验证图片`}
                                disabled={!row.val_images}
                                onClick={() =>
                                  showSamples({
                                    data_class: String(row.index),
                                    data_split: "val",
                                    data_change: "all",
                                  })
                                }
                              >
                                {row.val_images}
                              </button>
                            </td>
                            <td className="py-3 text-right tabular-nums">{row.train_instances}</td>
                            <td className="py-3 text-right tabular-nums">{row.val_instances}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              </details>
              {classes.data?.items.length === 0 && (
                <p className="py-4 text-xs text-muted-foreground">没有匹配的冻结类别。</p>
              )}
              <Pages
                label="类别"
                size={classPageSize}
                onSizeChange={(value) =>
                  update({ data_class_page_size: String(value), data_class_page: "0" })
                }
                page={classPage}
                total={classes.data?.total ?? 0}
                loading={classes.isFetching}
                onChange={setClassPage}
              />
            </section>
          </div>
          {data.task_statistics?.task === "detect" && (
            <TrainingDatasetDetectionCharts data={data.task_statistics} />
          )}
          <details className="rounded-xl border border-border p-4 text-xs">
            <summary className="cursor-pointer font-medium">统计口径与冻结证据</summary>
            <div className="mt-3 space-y-3 leading-relaxed text-muted-foreground">
              <p>
                图片数与实例数按实际训练包计数，包含重复图片项。明确负样本{" "}
                {data.counts.explicit_negatives} 张；确认历史未知的空标注{" "}
                {data.counts.negative_confirmation_unknown} 张。 可确认标注{" "}
                {data.counts.known_label_images} / {data.counts.images} 张。 冻结定义{" "}
                {data.classes_total} 个类别；已观察类别与未出现类别以可确认标注为依据。 内容身份未知{" "}
                {data.observations.unknown_contents ?? 0} 张，划分未知{" "}
                {data.observations.unknown_split ?? 0} 张。
              </p>
              <div className="rounded-lg bg-muted p-3 text-xs text-foreground">
                {data.preparation.recorded ? (
                  <p>
                    冻结准备记录：选中 {data.preparation.summary?.selected ?? 0} · 合格{" "}
                    {data.preparation.summary?.eligible ?? 0} · 当时排除{" "}
                    {data.preparation.summary?.excluded ?? 0}。
                    {data.counts.frozen_unpacked_images > 0 &&
                      `另有 ${data.counts.frozen_unpacked_images} 个冻结任务未作为独立成员入包。`}
                    <button
                      className="ml-2 cursor-pointer text-foreground underline"
                      onClick={() => showSamples({ data_kind: "excluded", data_change: "all" })}
                    >
                      查看冻结排除记录
                    </button>
                  </p>
                ) : (
                  <p>历史排除记录未保留，不能用当前任务状态补算当时的排除原因。</p>
                )}
              </div>
              <p>
                最近媒体读取记录：字节已校验 {data.media.verified} · 不可用 {data.media.unavailable}{" "}
                · 尚未读取 {data.media.unchecked}。
                {data.media.last_checked_at
                  ? `最近检查 ${new Date(data.media.last_checked_at).toLocaleString("zh-CN")}。`
                  : "尚未检查图片存储。"}
                记录只反映读取时刻，每次预览会重新检查。
              </p>
            </div>
          </details>
          <details className="rounded-xl border border-border p-4 text-xs">
            <summary className="cursor-pointer font-medium">尺寸与面积分布明细</summary>
            <div className="mt-4 grid gap-3 lg:grid-cols-2">
              <Distribution title="图片逻辑尺寸分布（张）" rows={data.dimension_bins} />
              <Distribution
                title={
                  version.task === segmentationTask
                    ? version.contract_profile?.bridge === instanceMaskBridge
                      ? "归一化实例掩码面积分布（实例）"
                      : "归一化多边形面积分布（实例）"
                    : "归一化矩形框面积分布（实例）"
                }
                rows={data.box_area_bins}
              />
            </div>
            <p className="mt-3 text-2xs text-muted-foreground">
              尺寸和面积仅用于描述分布，不构成通用质量评分。已知尺寸{" "}
              {data.dimensions.min_width ?? "未知"}–{data.dimensions.max_width ?? "未知"} px 宽、
              {data.dimensions.min_height ?? "未知"}–{data.dimensions.max_height ?? "未知"} px 高。
            </p>
          </details>
        </div>
      )}
      {ready && view === "compare" && (
        <div className="mt-4 space-y-4">
          <div className="rounded-lg bg-muted p-4">
            <div className="flex items-end gap-2">
              <label className="grid min-w-0 flex-1 gap-2 text-xs font-medium">
                选择比较基线
                <select
                  className={inputClass}
                  value={baselineId}
                  onChange={(event) =>
                    update({
                      data_baseline: event.target.value,
                      data_class: "",
                      data_page: "0",
                      data_change: "all",
                    })
                  }
                >
                  <option value="" disabled hidden>
                    选择已就绪版本
                  </option>
                  {baselineId &&
                    !baselineChoices.data?.items.some(
                      (item) =>
                        item.id === baselineId &&
                        item.id !== version.id &&
                        (item.task ?? "detect") === (version.task ?? "detect"),
                    ) && (
                      <option
                        value={baselineId}
                        disabled={baselineId === version.id}
                        hidden={baselineId === version.id}
                      >
                        {baselineId === version.id
                          ? `${version.name}（不能作为自身基线）`
                          : (baseline.data?.name ?? "当前基线")}
                      </option>
                    )}
                  {baselineChoices.data?.items
                    .filter(
                      (item) =>
                        item.id !== version.id &&
                        (item.task ?? "detect") === (version.task ?? "detect"),
                    )
                    .map((item) => (
                      <option
                        key={item.id}
                        value={item.id}
                        disabled={!!datasetComparisonReason(version, item)}
                      >
                        {item.name}
                        {datasetComparisonReason(version, item)
                          ? ` · ${datasetComparisonReason(version, item)}`
                          : ""}
                      </option>
                    ))}
                </select>
              </label>
              {baselineId && (
                <Button
                  type="button"
                  size="xs"
                  variant="ghost"
                  onClick={() =>
                    update({
                      data_baseline: "",
                      data_class: "",
                      data_page: "0",
                      data_change: "all",
                    })
                  }
                >
                  清除比较基线
                </Button>
              )}
              {baselineId && (
                <CopyValueButton value={baselineId} kind="id" label="复制比较基线数据版本 ID" />
              )}
            </div>
            <Pages
              label="基线"
              size={baselinePageSize}
              onSizeChange={(value) =>
                update({ data_baseline_page_size: String(value), data_baseline_page: "0" })
              }
              page={baselinePage}
              total={baselineChoices.data?.total ?? 0}
              loading={baselineChoices.isFetching}
              onChange={setBaselinePage}
            />
          </div>
          {!baselineId && (
            <p className="py-5 text-sm text-muted-foreground">
              选择基线，查看它到当前版本的内容、标注与验证基准变化。
            </p>
          )}
          {baselineId && (
            <p className="flex flex-wrap items-center gap-2 text-sm font-medium">
              <span>{baseline.data?.name ?? "基线版本"}</span>
              <ArrowRight className="size-4 text-muted-foreground" />
              <span>{version.name}</span>
            </p>
          )}
          {compared.isFetching && (
            <p role="status" className="text-xs text-muted-foreground">
              正在比较冻结版本…
            </p>
          )}
          {compared.data && (
            <>
              <p className="text-xs text-muted-foreground">
                按已知 SHA 的唯一图片内容集合比较，训练语义和冻结来源分别列出；变化类别可重叠。
                基线实际成员 {compared.data.baseline_members} 项 → 当前{" "}
                {compared.data.target_members} 项，下面各类变化不能相加。
                {compared.data.status !== "complete" &&
                  "部分冻结证据未保留，无法确认的训练语义已单列。"}
              </p>
              {(baselineUnknownContents !== 0 || targetUnknownContents !== 0) && (
                <p className="rounded-md bg-status-caution-soft p-3 text-xs text-status-caution">
                  基线内容身份未知 {baselineUnknownContents ?? "待确认"} 张，当前未知{" "}
                  {targetUnknownContents ?? "待确认"}{" "}
                  张。新增与移除仅表示已知身份集合的差异，不能排除未知成员含有同一内容。
                </p>
              )}
              <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
                {Object.entries(changes)
                  .filter(([key]) => key !== "all" && !key.startsWith("source_"))
                  .map(([key, label]) => (
                    <button
                      key={key}
                      className="cursor-pointer rounded-lg border border-border p-3 text-left hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring"
                      onClick={() => showSamples({ data_change: key })}
                    >
                      <span className="text-xs text-muted-foreground">
                        {key === "added" && baselineUnknownContents !== 0
                          ? "已知内容新增（待核实）"
                          : key === "removed" && targetUnknownContents !== 0
                            ? "已知内容移除（待核实）"
                            : label}
                      </span>
                      <strong className="mt-1 block text-stat font-semibold tabular-nums">
                        {compared.data!.counts[key] ?? 0}
                      </strong>
                    </button>
                  ))}
              </div>
              <section aria-label="冻结来源比较" className="rounded-lg border border-border p-4">
                <h3 className="text-md font-semibold">冻结来源</h3>
                <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
                  {compared.data.source_comparison_status === "complete"
                    ? "共同内容的来源证据完整。"
                    : compared.data.source_comparison_status === "partial"
                      ? "部分共同内容缺少完整来源证据，无法确认其来源关系。"
                      : "没有可比较的共同内容来源证据。"}
                  来源变化可以与训练语义未变化同时成立；记录来自各版本已保存的冻结事实。来源身份变化不能单独证明文件发生了物理搬迁，也不表示当前来源在线。
                </p>
                <div className="mt-3 grid gap-2 sm:grid-cols-3">
                  {Object.entries(changes)
                    .filter(([key]) => key.startsWith("source_"))
                    .map(([key, label]) => (
                      <Button key={key} size="sm" onClick={() => showSamples({ data_change: key })}>
                        {label} · {compared.data!.counts[key] ?? 0}
                      </Button>
                    ))}
                </div>
              </section>
              <p className="text-xs text-muted-foreground">
                类别名称新增 {compared.data.classes_added} · 移除 {compared.data.classes_removed} ·{" "}
                {compared.data.class_order_changed
                  ? "类别顺序已变化"
                  : compared.data.class_encoding_changed
                    ? "类别编码映射已变化"
                    : "类别映射相同"}
                。名称变化不能单凭快照推断为重命名。
              </p>
              <div className="rounded-lg border border-border p-4">
                <h3 className="text-sm font-semibold">验证基准</h3>
                <p className="mt-2 text-sm">
                  包含重复次数的评估基准：
                  {compared.data.evaluation_benchmark_changed === null
                    ? "无法确认"
                    : compared.data.evaluation_benchmark_changed
                      ? "已变化"
                      : "相同"}
                </p>
                <p className="mt-2 text-xs text-muted-foreground">
                  去重后的验证内容与标签：
                  {compared.data.semantic_validation_changed === null
                    ? "无法确认"
                    : compared.data.semantic_validation_changed
                      ? "已变化"
                      : "相同"}
                  。重复次数会改变评估权重，基准相同也不代表模型分数可以省略评估条件直接排名。
                </p>
              </div>
            </>
          )}
        </div>
      )}
      {ready && view === "samples" && (
        <div className="mt-4 space-y-4">
          {change === "removed" && (
            <p className="rounded-md bg-muted p-3 text-xs text-foreground">
              正在查看基线“{baseline.data?.name ?? baselineId}”中被移除的内容，预览也读取基线版本。
            </p>
          )}
          {kind === "excluded" && !data?.preparation.recorded && (
            <p className="rounded-md bg-status-caution-soft p-3 text-xs text-status-caution">
              历史排除记录未保留；这里只能列出仍保留的冻结任务入包记录。
            </p>
          )}
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <label className="grid gap-1.5 text-xs">
              分区
              <select
                className={inputClass}
                value={split ?? ""}
                onChange={(event) => update({ data_split: event.target.value, data_page: "0" })}
              >
                <option value="">训练与验证</option>
                <option value="train">训练集</option>
                <option value="val">验证集</option>
              </select>
            </label>
            <label className="grid gap-1.5 text-xs">
              样本类型
              <select
                className={inputClass}
                value={kind}
                onChange={(event) => update({ data_kind: event.target.value, data_page: "0" })}
              >
                <option value="all">全部包内样本</option>
                <option value="positive">有目标</option>
                <option value="negative">空标注样本</option>
                <option value="excluded">冻结排除记录</option>
                <option value="unavailable">标注元数据不可用</option>
              </select>
            </label>
            <label className="grid gap-1.5 text-xs">
              变化类别
              <select
                className={inputClass}
                value={change}
                onChange={(event) =>
                  update({ data_change: event.target.value, data_class: "", data_page: "0" })
                }
              >
                {Object.entries(changes).map(([value, label]) => (
                  <option key={value} value={value} disabled={value !== "all" && !baselineId}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label className="grid gap-1.5 text-xs">
              冻结类别
              <select
                className={inputClass}
                value={rawClass}
                onChange={(event) => update({ data_class: event.target.value, data_page: "0" })}
              >
                <option value="">全部冻结类别</option>
                {classIndex !== undefined &&
                  !filterClasses.data?.items.some((row) => row.index === classIndex) && (
                    <option value={rawClass}>
                      {selectedClass.data?.items[0]?.name ?? `类别 ${rawClass}`}
                    </option>
                  )}
                {filterClasses.data?.items.map((row) => (
                  <option key={row.index} value={row.index}>
                    {row.name}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <details className="text-xs text-muted-foreground">
            <summary className="cursor-pointer">查找更多冻结类别</summary>
            <label className="mt-2 block">
              搜索冻结类别
              <input
                className={`${inputClass} mt-1 max-w-sm`}
                value={filterSearch}
                onChange={(event) => {
                  setFilterSearch(event.target.value);
                  setFilterPage(0);
                }}
              />
            </label>
            <Pages
              label="筛选类别"
              size={filterPageSize}
              onSizeChange={(value) =>
                update({ data_filter_page_size: String(value), data_filter_page: "0" })
              }
              page={filterPage}
              total={filterClasses.data?.total ?? 0}
              loading={filterClasses.isFetching}
              onChange={setFilterPage}
            />
          </details>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-xs text-muted-foreground">
              筛选在服务端分页前执行。变化计数按唯一内容，列表展示实际图片项。图片只在打开预览时读取。
            </p>
            <Button
              size="sm"
              variant="ghost"
              onClick={() =>
                update({
                  data_class: "",
                  data_split: "",
                  data_kind: "all",
                  data_change: "all",
                  data_page: "0",
                })
              }
            >
              清除样本筛选
            </Button>
          </div>
          {invalidFilters && (
            <p role="alert" className="text-xs text-status-danger">
              URL 中的类别或页码无效，请清除样本筛选。
            </p>
          )}
          {samples.isFetching && (
            <p role="status" className="py-3 text-xs text-muted-foreground">
              正在加载冻结样本…
            </p>
          )}
          <div className={listStyles.surface}>
            <div className={listStyles.scroll}>
              <table className={listStyles.table}>
                <thead className="text-muted-foreground">
                  <tr>
                    <th className="py-2 font-medium">冻结样本</th>
                    <th className="py-2 font-medium">分区与尺寸</th>
                    <th className="py-2 font-medium">标注</th>
                    <th className="py-2 text-right font-medium">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {samples.data?.items.map((item, index) => (
                    <tr
                      key={`${item.version_id}:${item.task_id}`}
                      className="border-t border-border"
                    >
                      <td className="py-3 pr-3">
                        <span>样本 {page * pageSize + index + 1}</span>
                        <CopyValueButton
                          value={item.task_id}
                          kind="id"
                          label="复制冻结样本任务 ID"
                        />
                      </td>
                      <td className="py-3 pr-3 tabular-nums">
                        {item.split === "val"
                          ? "验证"
                          : item.split === "train"
                            ? "训练"
                            : item.included
                              ? "划分未知"
                              : "未入包"}
                        <span className="mt-1 block text-muted-foreground">
                          {item.width ?? "未知"} × {item.height ?? "未知"}
                        </span>
                      </td>
                      <td className="py-3 pr-3">
                        {!item.included
                          ? (exclusionReasons[item.exclusion_reason ?? ""] ?? "冻结排除")
                          : item.issue
                            ? "冻结标注不可用"
                            : item.instances === 0
                              ? item.explicit_negative
                                ? "明确无目标"
                                : "空标注，确认历史未知"
                              : `${item.instances} 个实例`}
                        {item.media_state === "unavailable" && (
                          <span className="mt-1 block text-status-caution">最近读取图片不可用</span>
                        )}
                      </td>
                      <td className="py-3 text-right">
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={!item.included}
                          onClick={(event) =>
                            setPreview({
                              sample: item,
                              scope: sampleScope,
                              opener: event.currentTarget,
                            })
                          }
                        >
                          <ImageIcon className="size-3.5" />
                          预览冻结样本
                        </Button>
                        {baselineId && baselineCompatible && item.included && item.sha256 && (
                          <Button
                            size="sm"
                            variant="ghost"
                            onClick={(event) =>
                              setComparisonPreview({
                                sha: item.sha256!,
                                scope: sampleScope,
                                opener: event.currentTarget,
                              })
                            }
                          >
                            前后对照
                          </Button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
          {samples.data?.items.length === 0 && (
            <p className="py-6 text-center text-sm text-muted-foreground">
              没有符合当前条件的冻结样本。
            </p>
          )}
          <Pages
            label="样本"
            size={pageSize}
            onSizeChange={(value) => update({ data_page_size: String(value), data_page: "0" })}
            page={page}
            total={samples.data?.total ?? 0}
            loading={samples.isFetching}
            onChange={(value) => update({ data_page: String(value) })}
          />
        </div>
      )}
      <details
        className="mt-5 border-t border-border pt-4 text-xs text-muted-foreground"
        onToggle={(event) => setRawOpen(event.currentTarget.open)}
      >
        <summary className="cursor-pointer">查看原始训练与验证清单</summary>
        <p className="mt-3 flex items-center gap-1">
          快照摘要：
          <CopyValueButton value={version.snapshot_digest} label="复制数据快照摘要哈希" />
        </p>
        {version.size_bytes != null && (
          <p className="mt-2">训练包大小：{(version.size_bytes / 1024 / 1024).toFixed(2)} MiB</p>
        )}
        {raw.isLoading && <p className="mt-2">正在加载原始清单…</p>}
        {raw.error && (
          <p role="alert" className="mt-2 text-status-danger">
            <CopyableError message={raw.error.message} />
          </p>
        )}
        {raw.data && (
          <pre className="mt-3 max-h-80 overflow-auto whitespace-pre-wrap break-all font-mono text-xs">
            {JSON.stringify(raw.data.manifest, null, 2)}
          </pre>
        )}
      </details>
      {preview && preview.scope === sampleScope && view === "samples" && (
        <TrainingDatasetSamplePreview
          projectId={projectId}
          sample={preview.sample}
          onClose={() => {
            const opener = preview.opener;
            setPreview(null);
            requestAnimationFrame(() => {
              if (opener.isConnected) opener.focus();
            });
          }}
          onObserved={observed}
        />
      )}
      {comparisonPreview &&
        comparisonPreview.scope === sampleScope &&
        view === "samples" &&
        baselineCompatible && (
          <TrainingDatasetSampleComparison
            projectId={projectId}
            baselineVersionId={baselineId}
            targetVersionId={version.id}
            contentSha256={comparisonPreview.sha}
            baselineUnknownContents={baselineUnknownContents}
            targetUnknownContents={targetUnknownContents}
            onObserved={observed}
            onClose={() => {
              const opener = comparisonPreview.opener;
              setComparisonPreview(null);
              requestAnimationFrame(() => {
                if (opener.isConnected) opener.focus();
              });
            }}
          />
        )}
    </section>
  );
}

import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
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
import { TrainingDatasetSamplePreview } from "./TrainingDatasetSamplePreview";

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
  unchanged: "内容与标注未变",
  unknown: "标注变化无法确认",
};
const exclusionReasons: Record<string, string> = {
  unsupported_media: "不是图片任务",
  unusable_sample: "当时标记为不可用",
  not_reviewed: "当时尚未审核通过",
  unconfirmed_negative: "当时未明确确认无目标",
  package_deduplicated: "入包时合并重复内容",
  not_in_package: "未出现在保留的训练包中",
};

function Pages({
  page,
  total,
  size = 25,
  loading = false,
  label,
  onChange,
}: {
  page: number;
  total: number;
  size?: number;
  loading?: boolean;
  label: string;
  onChange: (page: number) => void;
}) {
  return (
    <div className="mt-3 flex flex-wrap items-center justify-end gap-2 text-xs">
      <span className="mr-auto text-foreground">
        共 {total} 项 · 第 {page + 1} 页
      </span>
      <Button size="sm" disabled={!page || loading} onClick={() => onChange(page - 1)}>
        上一页{label}
      </Button>
      <Button
        size="sm"
        disabled={(page + 1) * size >= total || loading}
        onClick={() => onChange(page + 1)}
      >
        下一页{label}
      </Button>
    </div>
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
      <table className="w-full text-left text-xs">
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
  const owner = useMemo(
    () => ({
      userId,
      token,
      projectId: props.projectId,
      versionId: props.version.id,
      key: crypto.randomUUID(),
    }),
    [userId, token, props.projectId, props.version.id],
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
  const invalidFilters =
    (rawClass !== "" && classIndex === undefined) ||
    !/^\d+$/.test(rawPage) ||
    !Number.isSafeInteger(page) ||
    !Number.isSafeInteger(page * 25) ||
    (classIndex !== undefined && !Number.isSafeInteger(classIndex));
  const kind = (params.get("data_kind") ?? "all") as DatasetSampleFilters["kind"];
  const split = (params.get("data_split") || undefined) as DatasetSampleFilters["split"];
  const [classSearch, setClassSearch] = useState("");
  const [classPage, setClassPage] = useState(0);
  const [filterSearch, setFilterSearch] = useState("");
  const [filterPage, setFilterPage] = useState(0);
  const [baselinePage, setBaselinePage] = useState(0);
  const [preview, setPreview] = useState<{
    sample: TrainingDatasetAnalyzedSampleOut;
    scope: string;
    opener: HTMLButtonElement;
  } | null>(null);
  const [rawOpen, setRawOpen] = useState(false);
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
    queryKey: [...key, "classes", classSearch, classPage],
    queryFn: ({ signal }) =>
      trainingDatasetsApi.classes(
        projectId,
        version.id,
        { search: classSearch, offset: classPage * 25 },
        { signal },
      ),
    enabled: ready && view === "overview",
  });
  const baseline = useQuery({
    queryKey: [...key, "baseline", baselineId],
    queryFn: ({ signal }) => trainingDatasetsApi.getSummary(projectId, baselineId, { signal }),
    enabled: ready && !!baselineId,
    retry: false,
  });
  const baselineChoices = useQuery({
    queryKey: [...key, "baseline-choices", baselinePage],
    queryFn: ({ signal }) =>
      trainingDatasetsApi.list(projectId, baselinePage * 20, "ready", { signal }),
    enabled: ready && view === "compare",
  });
  const compared = useQuery({
    queryKey: [...key, "compare", baselineId],
    queryFn: ({ signal }) =>
      trainingDatasetsApi.compare(projectId, version.id, baselineId, { signal }),
    enabled:
      ready && !!baselineId && (view === "compare" || (view === "samples" && change !== "all")),
    retry: false,
  });
  const sampleVersionId = change === "removed" ? baselineId : version.id;
  const filterClasses = useQuery({
    queryKey: [...key, "filter-classes", sampleVersionId, filterSearch, filterPage],
    queryFn: ({ signal }) =>
      trainingDatasetsApi.classes(
        projectId,
        sampleVersionId,
        { search: filterSearch, offset: filterPage * 25 },
        { signal },
      ),
    enabled: ready && view === "samples" && !!sampleVersionId,
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
      !!sampleVersionId,
  });
  const filters: DatasetSampleFilters = {
    limit: 25,
    offset: page * 25,
    kind,
    split,
    class_index: classIndex,
    baseline_id: baselineId || undefined,
    change,
  };
  const sampleScope = JSON.stringify([view, filters]);
  useEffect(() => {
    setPreview(null);
  }, [sampleScope]);
  const samples = useQuery({
    queryKey: [...key, "samples", filters],
    queryFn: ({ signal }) =>
      trainingDatasetsApi.samples(projectId, version.id, filters, { signal }),
    enabled: ready && view === "samples" && !invalidFilters,
    retry: false,
  });
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
  const error =
    view === "overview"
      ? classes.error
      : view === "compare"
        ? baselineChoices.error || baseline.error || compared.error
        : samples.error || filterClasses.error || selectedClass.error;
  return (
    <section
      className="mt-6 min-w-0 rounded-xl border border-border bg-card p-4 sm:p-5"
      aria-label="数据版本详情"
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <h2 className="break-words text-sm font-semibold">{version.name}</h2>
          <p className="mt-1 text-xs text-muted-foreground">
            冻结时刻的数据、标注与划分 · 后续任务编辑不改变这里的统计
          </p>
        </div>
        <Button size="sm" variant="ghost" onClick={onClose}>
          收起数据详情
        </Button>
      </div>
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
            variant={view === value ? "primary" : "ghost"}
            aria-pressed={view === value}
            onClick={() => update({ data_view: value })}
          >
            {label}
          </Button>
        ))}
      </nav>
      {!ready && (
        <p className="mt-4 text-sm text-muted-foreground">
          版本构建完成后可查看冻结数据洞察。{version.error_message}
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
          <p>数据洞察暂不可用：{overview.error.message}。训练包下载入口仍独立可用。</p>
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
          {error.message}
        </p>
      )}
      {ready && view === "overview" && data && (
        <div className="mt-4 space-y-5">
          <dl className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            {[
              ["训练包图片", data.counts.images],
              ["标注实例", data.counts.instances],
              ["唯一图片内容", data.counts.unique_contents],
              ["重复图片项", data.counts.duplicate_images],
            ].map(([label, value]) => (
              <div key={label} className="rounded-lg bg-muted p-3">
                <dt className="text-xs text-foreground">{label}</dt>
                <dd className="mt-1 text-stat font-semibold tabular-nums">{value}</dd>
              </div>
            ))}
          </dl>
          <p className="text-xs text-muted-foreground">
            图片数与实例数按实际训练包计数，包含重复图片项。明确负样本{" "}
            {data.counts.explicit_negatives} 张；确认历史未知的空标注{" "}
            {data.counts.negative_confirmation_unknown} 张。可确认标注{" "}
            {data.counts.known_label_images} / {data.counts.images} 张。
          </p>
          <div className="grid gap-3 md:grid-cols-2">
            {(
              [
                ["train", "训练集"],
                ["val", "验证集"],
              ] as const
            ).map(([split, label]) => (
              <section key={split} className="rounded-lg border border-border p-4">
                <h3 className="text-xs font-semibold">{label}</h3>
                <p className="mt-2 text-sm tabular-nums">
                  {data.splits[split].images} 张图片 · {data.splits[split].instances} 个实例
                </p>
                <Button
                  size="sm"
                  variant="ghost"
                  className="mt-2"
                  onClick={() => showSamples({ data_split: split, data_change: "all" })}
                >
                  查看{label}样本
                </Button>
              </section>
            ))}
          </div>
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
          <section aria-label="冻结类别分布">
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
              “图片”表示出现该类的图片项数；一张图片可能包含多个实例。验证集中未出现{" "}
              {data.observations.classes_absent_from_validation ?? 0} 个类别，训练集中未出现{" "}
              {data.observations.classes_absent_from_train ?? 0} 个类别。
            </p>
            {classes.isLoading && (
              <p role="status" className="py-4 text-xs text-muted-foreground">
                正在加载类别分布…
              </p>
            )}
            <div className="overflow-x-auto">
              <table className="w-full min-w-md text-left text-xs">
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
            {classes.data?.items.length === 0 && (
              <p className="py-4 text-xs text-muted-foreground">没有匹配的冻结类别。</p>
            )}
            <Pages
              label="类别"
              page={classPage}
              total={classes.data?.total ?? 0}
              loading={classes.isFetching}
              onChange={setClassPage}
            />
          </section>
          <div className="grid gap-3 lg:grid-cols-2">
            <Distribution title="图片逻辑尺寸分布（张）" rows={data.dimension_bins} />
            <Distribution title="归一化矩形框面积分布（实例）" rows={data.box_area_bins} />
          </div>
          <p className="text-xs text-muted-foreground">
            尺寸和面积仅用于描述分布，不构成通用质量评分。已知尺寸{" "}
            {data.dimensions.min_width ?? "未知"}–{data.dimensions.max_width ?? "未知"} px 宽、
            {data.dimensions.min_height ?? "未知"}–{data.dimensions.max_height ?? "未知"} px 高。
          </p>
          <p className="rounded-md border border-border p-3 text-xs text-muted-foreground">
            最近媒体读取记录：字节已校验 {data.media.verified} · 不可用 {data.media.unavailable} ·
            尚未读取 {data.media.unchecked}。
            {data.media.last_checked_at
              ? `最近检查 ${new Date(data.media.last_checked_at).toLocaleString("zh-CN")}。`
              : "尚未检查图片存储。"}
            记录只反映读取时刻，每次预览会重新检查。
          </p>
        </div>
      )}
      {ready && view === "compare" && (
        <div className="mt-4 space-y-4">
          <div className="rounded-lg bg-muted p-4">
            <label className="grid gap-2 text-xs font-medium">
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
                <option value="">选择已就绪版本</option>
                {baselineId &&
                  !baselineChoices.data?.items.some((item) => item.id === baselineId) && (
                    <option value={baselineId}>
                      {baseline.data?.name ?? `当前基线 ${baselineId.slice(0, 8)}`}
                    </option>
                  )}
                {baselineChoices.data?.items
                  .filter((item) => item.id !== version.id)
                  .map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.name}
                    </option>
                  ))}
              </select>
            </label>
            <Pages
              label="基线"
              page={baselinePage}
              size={20}
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
                按唯一图片内容比较，忽略路径及任务 ID 噪声；变化类别可重叠。
                {compared.data.status !== "complete" && "部分冻结证据未保留，未知标注变化已单列。"}
              </p>
              <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
                {Object.entries(changes)
                  .filter(([key]) => key !== "all")
                  .map(([key, label]) => (
                    <button
                      key={key}
                      className="cursor-pointer rounded-lg border border-border p-3 text-left hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring"
                      onClick={() => showSamples({ data_change: key })}
                    >
                      <span className="text-xs text-muted-foreground">{label}</span>
                      <strong className="mt-1 block text-stat font-semibold tabular-nums">
                        {compared.data!.counts[key] ?? 0}
                      </strong>
                    </button>
                  ))}
              </div>
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
              page={filterPage}
              total={filterClasses.data?.total ?? 0}
              loading={filterClasses.isFetching}
              onChange={setFilterPage}
            />
          </details>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-xs text-muted-foreground">
              筛选在服务端分页前执行。变化计数按唯一内容，列表展示实际图片项。每页最多 25
              项；图片只在打开预览时读取。
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
          <div className="overflow-x-auto">
            <table className="w-full min-w-md text-left text-xs">
              <thead className="text-muted-foreground">
                <tr>
                  <th className="py-2 font-medium">冻结样本</th>
                  <th className="py-2 font-medium">分区与尺寸</th>
                  <th className="py-2 font-medium">标注</th>
                  <th className="py-2 text-right font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                {samples.data?.items.map((item) => (
                  <tr key={`${item.version_id}:${item.task_id}`} className="border-t border-border">
                    <td className="py-3 pr-3 font-mono">{item.task_id.slice(0, 8)}</td>
                    <td className="py-3 pr-3 tabular-nums">
                      {item.split === "val" ? "验证" : item.split === "train" ? "训练" : "未入包"}
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
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {samples.data?.items.length === 0 && (
            <p className="py-6 text-center text-sm text-muted-foreground">
              没有符合当前条件的冻结样本。
            </p>
          )}
          <Pages
            label="样本"
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
        <p className="mt-3 break-all font-mono">快照摘要：{version.snapshot_digest}</p>
        {version.size_bytes != null && (
          <p className="mt-2">训练包大小：{(version.size_bytes / 1024 / 1024).toFixed(2)} MiB</p>
        )}
        {raw.isLoading && <p className="mt-2">正在加载原始清单…</p>}
        {raw.error && (
          <p role="alert" className="mt-2 text-status-danger">
            {raw.error.message}
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
    </section>
  );
}

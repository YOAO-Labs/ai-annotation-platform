import type { CSSProperties, ReactNode } from "react";

import type { DataManagerFilterField, DataManagerSummary } from "@/api/taskViews";
import { Skeleton } from "@/components/shadcn/ui/skeleton";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { useDataManagerSummary } from "@/hooks/useTaskViews";
import { DataManagerCharts } from "./DataManagerCharts";

interface DataManagerOverviewProps {
  summary: DataManagerSummary | undefined;
  isLoading: boolean;
  fields?: DataManagerFilterField[];
  onSelect?: (field: string, value: string) => void;
}

function metric(value: number | undefined) {
  return value === undefined ? "—" : value.toLocaleString();
}

const KIND_METRIC_LABELS: Record<string, string> = {
  images_with_dimensions: "已有尺寸",
  distinct_resolutions: "分辨率种类",
  duration_ms: "总时长（毫秒）",
  frame_count: "总帧数",
  keyframes: "关键帧",
  outside_ranges: "不可见区间",
  box_3d: "3D 框",
  point_mask_3d: "点云 Mask",
  camera_links: "相机路数合计",
  calibration_issues: "标定异常",
  scenes: "Scene",
  interpolated_annotations: "插值标注",
};

function Distribution({
  title,
  values,
  labels = {},
}: {
  title: string;
  values: Record<string, number | null>;
  labels?: Record<string, string>;
}) {
  const items = Object.entries(values).filter(
    (entry): entry is [string, number] => typeof entry[1] === "number" && entry[1] > 0,
  );
  return (
    <div className="min-w-0">
      <div className="mb-2 text-xs font-semibold text-muted-foreground">{title}</div>
      <div className="flex flex-wrap gap-1.5">
        {items.length ? (
          items.map(([key, value]) => (
            <Badge key={key} variant="outline">
              {labels[key] ?? key} · {value.toLocaleString()}
            </Badge>
          ))
        ) : (
          <span className="text-xs text-muted-foreground">当前范围无数据</span>
        )}
      </div>
    </div>
  );
}

type OverviewItem = {
  label: string;
  value: number | undefined;
  detail: string;
  // 有值时该 KPI 可点，下钻为一条 `field op value` 筛选；无 → 纯展示。
  drill?: { field: string; op: string; value: string };
};

function overviewItems(summary: DataManagerSummary | undefined): OverviewItem[] {
  return [
    {
      label: "当前匹配",
      value: summary?.scope.matched_task_total,
      detail: `可见 ${metric(summary?.scope.visible_task_total)}`,
    },
    {
      label: "标注对象",
      value: summary?.annotations.total,
      detail: `人工 ${metric(summary?.annotations.by_source.manual)} · AI ${metric(summary?.annotations.by_source.prediction_based)}`,
    },
    {
      label: "AI 待审",
      value: (summary?.ai_review.prediction_shapes ?? 0) + (summary?.ai_review.tracker_jobs ?? 0),
      detail: `低置信 ${metric(summary?.ai_review.low_confidence_prediction_shapes)}`,
      drill: { field: "ai.pending_prediction_shape_count", op: "gt", value: "0" },
    },
    {
      label: "逻辑轨迹",
      value: summary?.annotations.distinct_tracks,
      detail: `轨迹标注 ${metric(summary?.annotations.tracked)}`,
    },
    {
      label: "未解决问题",
      value: summary?.unresolved_feedback,
      detail: summary?.unresolved_feedback ? "需要处理" : "当前无问题",
      drill: { field: "feedback.unresolved_count", op: "gt", value: "0" },
    },
  ];
}

export function DataManagerSummaryStrip({
  summary,
  isLoading,
  onDrill,
}: DataManagerOverviewProps & {
  onDrill?: (rule: { field: string; op: string; value: string }) => void;
}) {
  const items = overviewItems(summary);
  return (
    <section
      aria-label="数据概览"
      className="grid shrink-0 grid-cols-5 gap-px overflow-hidden rounded-md border border-border bg-border max-md:flex max-md:overflow-x-auto"
    >
      {items.map((item) => {
        const drillable = Boolean(item.drill && onDrill);
        const inner = (
          <>
            <div className="flex items-baseline justify-between gap-2">
              <span className="truncate text-xs text-muted-foreground">{item.label}</span>
              {isLoading ? (
                <Skeleton className="h-5 w-10" />
              ) : (
                <strong className="font-mono text-base font-semibold tabular-nums text-foreground">
                  {metric(item.value)}
                </strong>
              )}
            </div>
            <div className="mt-0.5 truncate text-2xs text-muted-foreground">
              {isLoading ? <Skeleton className="h-3 w-full" /> : item.detail}
            </div>
          </>
        );
        return drillable ? (
          <button
            key={item.label}
            type="button"
            onClick={() => item.drill && onDrill?.(item.drill)}
            className="min-w-0 bg-card px-3 py-2 text-left transition-colors hover:bg-muted max-md:min-w-36"
          >
            {inner}
          </button>
        ) : (
          <div key={item.label} className="min-w-0 bg-card px-3 py-2 max-md:min-w-36">
            {inner}
          </div>
        );
      })}
    </section>
  );
}

export function DataManagerAnalyticsContent({
  summary,
  isLoading,
  fields,
  onSelect,
}: DataManagerOverviewProps) {
  return (
    <section aria-label="详细统计" className="flex flex-col gap-4">
      <div>
        <DataManagerCharts
          scope="tasks"
          summary={summary}
          fields={fields}
          isLoading={isLoading}
          onSelect={onSelect}
        />
      </div>
      <details className="rounded-lg border border-border bg-card" open>
        <summary className="cursor-pointer px-3 py-2 text-sm font-medium text-foreground marker:text-muted-foreground">
          属性完整度与当前模态
        </summary>
        <div className="grid gap-5 border-t border-border p-3 md:grid-cols-2">
          {isLoading || !summary ? (
            Array.from({ length: 2 }, (_, index) => (
              <Skeleton key={index} className="h-16 w-full" />
            ))
          ) : (
            <>
              <div className="min-w-0">
                <div className="mb-2 text-xs font-semibold text-muted-foreground">属性完整度</div>
                <div className="grid gap-2 sm:grid-cols-2">
                  {summary.attributes.length ? (
                    summary.attributes.map((attribute) => (
                      <div
                        key={`${attribute.tool_unit_id}.${attribute.key}`}
                        className="rounded-md border border-border bg-background p-2"
                      >
                        <div className="flex items-center justify-between gap-2 text-xs">
                          <span className="truncate font-medium">{attribute.label}</span>
                          <span className="font-mono text-muted-foreground">
                            {attribute.present}/{attribute.eligible}
                          </span>
                        </div>
                        <progress
                          className="mt-1.5 h-1.5 w-full accent-primary"
                          value={attribute.present}
                          max={Math.max(attribute.eligible, 1)}
                          aria-label={`${attribute.label}完整度`}
                        />
                        <div className="mt-1.5 text-xs text-muted-foreground">
                          缺失 {attribute.missing}
                        </div>
                      </div>
                    ))
                  ) : (
                    <span className="text-xs text-muted-foreground">项目未配置属性字段</span>
                  )}
                </div>
              </div>
              <Distribution
                title="当前模态"
                values={summary.kind_metrics}
                labels={KIND_METRIC_LABELS}
              />
            </>
          )}
        </div>
      </details>
    </section>
  );
}

export function DataManagerOverview(props: DataManagerOverviewProps) {
  return (
    <div className="flex flex-col gap-4">
      <DataManagerSummaryStrip {...props} />
      <DataManagerAnalyticsContent {...props} />
    </div>
  );
}

const STATUS_LABELS: Record<string, string> = {
  pending: "待标注",
  in_progress: "标注中",
  review: "待审核",
  completed: "已完成",
  rejected: "已退回",
  uploading: "上传中",
};

const STATUS_COLORS: Record<string, string> = {
  pending: "bg-status-caution",
  in_progress: "bg-status-info",
  review: "bg-status-caution",
  completed: "bg-status-positive",
  rejected: "bg-status-danger",
  uploading: "bg-status-info-alt",
};

function OverviewMetric({
  label,
  value,
  detail,
  onClick,
}: {
  label: string;
  value: string;
  detail: string;
  onClick?: () => void;
}) {
  const content = (
    <>
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-1 font-mono text-2xl font-semibold tabular-nums text-foreground">
        {value}
      </div>
      <div className="mt-1 text-xs text-muted-foreground">{detail}</div>
    </>
  );
  return onClick ? (
    <button
      type="button"
      className="rounded-lg border border-border bg-card p-4 text-left transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      onClick={onClick}
    >
      {content}
    </button>
  ) : (
    <div className="rounded-lg border border-border bg-card p-4">{content}</div>
  );
}

export function DataManagerProjectOverview({
  projectId,
  summaryFilter = {},
  onDrill,
  children,
}: {
  projectId: string;
  summaryFilter?: Record<string, unknown>;
  onDrill?: (rule: { field: string; op: "eq" | "gt"; value: string }) => void;
  children?: ReactNode;
}) {
  const summaryQ = useDataManagerSummary(projectId, summaryFilter);
  const summary = summaryQ.data;
  const visible = summary?.scope.visible_task_total ?? 0;
  const completed = summary?.task_status.completed ?? 0;
  const review = summary?.task_status.review ?? 0;
  const feedback = summary?.unresolved_feedback ?? 0;
  const statusTotal = Object.values(summary?.task_status ?? {}).reduce(
    (total, value) => total + value,
    0,
  );

  if (summaryQ.isError) {
    return (
      <section
        role="alert"
        className="rounded-lg border border-status-danger/30 bg-status-danger-soft p-6"
      >
        <div className="flex items-start gap-3">
          <Icon name="warning" size={18} className="mt-0.5 shrink-0 text-status-danger" />
          <div>
            <h2 className="text-sm font-semibold text-status-danger">项目概览暂时不可用</h2>
            <p className="mt-1 text-xs text-status-danger/80">无法读取当前项目的任务汇总。</p>
            <Button size="sm" variant="ghost" className="mt-3" onClick={() => summaryQ.refetch()}>
              <Icon name="refresh" size={12} />
              重试
            </Button>
          </div>
        </div>
      </section>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col gap-4 overflow-y-auto pb-4">
      <section aria-labelledby="data-manager-overview-title">
        <div className="mb-3 flex items-end justify-between gap-3">
          <div>
            <h2 id="data-manager-overview-title" className="text-base font-semibold">
              项目概览
            </h2>
            <p className="mt-1 text-xs text-muted-foreground">
              当前项目可见任务快照；点击指标可进入对应数据集合。
            </p>
          </div>
          <Badge variant="outline">当前快照</Badge>
        </div>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {summaryQ.isLoading ? (
            Array.from({ length: 4 }, (_, index) => (
              <Skeleton key={index} className="h-28 rounded-lg" />
            ))
          ) : (
            <>
              <OverviewMetric
                label="可见任务"
                value={visible.toLocaleString()}
                detail="项目权限范围内"
              />
              <OverviewMetric
                label="已完成"
                value={completed.toLocaleString()}
                detail={`任务状态 · ${completed}/${Math.max(statusTotal, visible)} 个`}
                onClick={
                  completed > 0
                    ? () => onDrill?.({ field: "task.status", op: "eq", value: "completed" })
                    : undefined
                }
              />
              <OverviewMetric
                label="待审核"
                value={review.toLocaleString()}
                detail="当前待审核任务"
                onClick={
                  review > 0
                    ? () => onDrill?.({ field: "task.status", op: "eq", value: "review" })
                    : undefined
                }
              />
              <OverviewMetric
                label="未解决问题"
                value={feedback.toLocaleString()}
                detail={feedback ? "需要处理" : "当前无反馈"}
                onClick={
                  feedback > 0
                    ? () => onDrill?.({ field: "feedback.unresolved_count", op: "gt", value: "0" })
                    : undefined
                }
              />
            </>
          )}
        </div>
      </section>

      {!summaryQ.isLoading && !visible && (
        <section className="rounded-lg border border-dashed border-border bg-card p-8 text-center">
          <Icon name="inbox" size={24} className="mx-auto text-muted-foreground" />
          <h2 className="mt-3 text-sm font-semibold">项目中还没有可见任务</h2>
          <p className="mt-1 text-xs text-muted-foreground">
            任务导入后，交付和质量状态会显示在这里。
          </p>
        </section>
      )}

      {!summaryQ.isLoading && summary && visible > 0 && (
        <div className="grid gap-4 xl:grid-cols-2">
          <section
            className="rounded-lg border border-border bg-card p-4"
            aria-labelledby="delivery-title"
          >
            <div className="flex items-center justify-between gap-3">
              <div>
                <h2 id="delivery-title" className="text-sm font-semibold">
                  交付状态
                </h2>
                <p className="mt-1 text-xs text-muted-foreground">按当前可见任务计数</p>
              </div>
              <span className="font-mono text-xs text-muted-foreground">
                {statusTotal.toLocaleString()} 条状态记录
              </span>
            </div>
            <div className="mt-4 flex flex-col gap-3">
              {Object.entries(summary.task_status).map(([status, count]) => {
                const percentage = statusTotal ? Math.round((count / statusTotal) * 100) : 0;
                return (
                  <div key={status}>
                    <div className="flex items-center justify-between gap-3 text-xs">
                      <span>{STATUS_LABELS[status] ?? status}</span>
                      <span className="font-mono tabular-nums text-muted-foreground">
                        {count.toLocaleString()} · {percentage}%
                      </span>
                    </div>
                    <div
                      className="mt-1.5 h-2 overflow-hidden rounded-full bg-muted"
                      aria-hidden="true"
                    >
                      <div
                        className={`h-full rounded-full ${STATUS_COLORS[status] ?? "bg-status-info"}`}
                        // eslint-disable-next-line no-restricted-syntax -- status bar width is data-driven.
                        style={{ width: `${percentage}%` } as CSSProperties}
                      />
                    </div>
                  </div>
                );
              })}
            </div>
          </section>

          <section
            className="rounded-lg border border-border bg-card p-4"
            aria-labelledby="quality-title"
          >
            <div>
              <h2 id="quality-title" className="text-sm font-semibold">
                质量关注
              </h2>
              <p className="mt-1 text-xs text-muted-foreground">需要负责人优先查看的当前信号</p>
            </div>
            <div className="mt-4 grid gap-3 sm:grid-cols-2">
              <button
                type="button"
                className="rounded-md border border-border p-3 text-left hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                onClick={() =>
                  onDrill?.({
                    field: "ai.low_confidence_prediction_shape_count",
                    op: "gt",
                    value: "0",
                  })
                }
              >
                <div className="text-xs text-muted-foreground">低置信 AI 候选</div>
                <div className="mt-1 font-mono text-xl tabular-nums">
                  {(summary.ai_review.low_confidence_prediction_shapes ?? 0).toLocaleString()}
                </div>
                <div className="mt-1 text-2xs text-muted-foreground">点击查看对应任务</div>
              </button>
              <button
                type="button"
                className="rounded-md border border-border p-3 text-left hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                onClick={() =>
                  onDrill?.({ field: "feedback.unresolved_count", op: "gt", value: "0" })
                }
              >
                <div className="text-xs text-muted-foreground">问题积压</div>
                <div className="mt-1 font-mono text-xl tabular-nums">
                  {feedback.toLocaleString()}
                </div>
                <div className="mt-1 text-2xs text-muted-foreground">按任务查看未解决问题</div>
              </button>
            </div>
          </section>
        </div>
      )}
      {children}
    </div>
  );
}

import type { ReactNode } from "react";
import { CopyableError } from "@/components/ui/CopyableError";
import type { UseQueryResult } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  ArrowRight,
  CheckCircle2,
  CircleAlert,
  Clock3,
  ListStart,
  RefreshCw,
  Server,
} from "lucide-react";
import type { QueueState, TrainingQueuePage } from "@/api/trainingHub";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { ProgressBar } from "@/components/ui/ProgressBar";
import { cn } from "@/lib/utils";
import {
  trainingProgress,
  trainingStatusLabels,
  trainingStatusVariant,
  trainingTaskLabel,
  trainingTaskOf,
} from "./trainingPresentation";
import { TrainingError, TrainingLoading } from "./TrainingFeedback";

const columns = "lg:grid-cols-[minmax(0,1fr)_minmax(0,0.6fr)_minmax(0,0.65fr)_8rem_1rem]";
const waitingLabels: Record<string, string> = {
  queued: "等待执行器调度",
  preparing: "正在准备训练数据",
  evaluating: "正在评估模型",
  uploading: "正在保存训练产物",
  cancelling: "等待安全停止",
  lost: "等待执行器恢复",
  succeeded: "训练已完成",
  failed: "训练已中断",
  cancelled: "实验已取消",
};

function createdLabel(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "—"
    : date.toLocaleString("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      });
}

export function TrainingQueuePanel({
  query,
  state,
  framework,
  onFilter,
  onStart,
  pagination,
}: {
  query: UseQueryResult<TrainingQueuePage, Error>;
  state: QueueState;
  framework: string;
  onFilter: (updates: { state?: QueueState; framework?: string }) => void;
  onStart: () => void;
  pagination?: ReactNode;
}) {
  const hasFilter = state !== "all" || Boolean(framework);
  return (
    <div className="space-y-5">
      <dl
        aria-label="训练概况"
        className="grid grid-cols-2 overflow-hidden rounded-xl border border-border bg-card lg:grid-cols-4"
      >
        {(
          [
            ["执行与恢复中", query.data?.counts.running, Server, "text-status-caution"],
            ["等待执行", query.data?.counts.queued, Clock3, "text-muted-foreground"],
            ["训练完成", query.data?.counts.succeeded, CheckCircle2, "text-status-positive"],
            ["训练失败", query.data?.counts.failed, CircleAlert, "text-status-danger"],
          ] as const
        ).map(([label, count, Icon, color], index) => (
          <div
            key={label}
            className={cn(
              "px-4 py-3 sm:px-5 sm:py-4",
              index % 2 === 1 && "border-l border-border",
              index > 1 && "border-t border-border lg:border-t-0",
              index === 2 && "lg:border-l",
            )}
          >
            <dt className="flex items-center gap-2 text-xs text-muted-foreground">
              <Icon aria-hidden="true" className={cn("size-3.5 shrink-0", color)} />
              {label}
            </dt>
            <dd className="mt-2 font-mono text-stat font-medium tabular-nums tracking-tight">
              {count ?? "—"}
            </dd>
          </div>
        ))}
      </dl>

      <section
        aria-label="训练实验"
        className="overflow-hidden rounded-xl border border-border bg-card"
      >
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-4 py-3 sm:px-5">
          <div className="flex items-baseline gap-2.5">
            <h2 className="text-sm font-semibold">实验记录</h2>
            <span className="text-xs tabular-nums text-muted-foreground">
              {query.data ? `${query.data.total} 个结果` : "—"}
            </span>
          </div>
          <div className="flex items-center gap-2">
            <span className="hidden text-2xs text-muted-foreground sm:block">每 3 秒自动更新</span>
            <Button
              size="xs"
              variant="ghost"
              aria-label="刷新训练队列"
              disabled={query.isFetching}
              onClick={() => void query.refetch()}
            >
              <RefreshCw aria-hidden="true" className="size-3" />
              刷新
            </Button>
          </div>
        </div>
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-4 py-3 sm:px-5">
          <div
            className="flex flex-wrap gap-0.5 rounded-md bg-muted/60 p-1"
            role="group"
            aria-label="筛选训练状态"
          >
            {(
              [
                ["active", "进行中"],
                ["finished", "已结束"],
                ["failed", "异常"],
                ["all", "全部实验"],
              ] as const
            ).map(([key, label]) => (
              <Button
                key={key}
                size="sm"
                variant="ghost"
                aria-pressed={state === key}
                className={cn(
                  "h-7 px-2.5 text-xs hover:translate-y-0",
                  state === key && "bg-background text-foreground shadow-sm hover:bg-background",
                )}
                onClick={() => onFilter({ state: key })}
              >
                {label}
              </Button>
            ))}
          </div>
          <select
            aria-label="筛选训练器"
            value={framework}
            onChange={(event) => onFilter({ framework: event.target.value })}
            className="min-h-8 max-w-full rounded-md border border-border bg-background px-2.5 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <option value="">全部训练器</option>
            <option value="ultralytics">Ultralytics</option>
            <option value="libreyolo">LibreYOLO</option>
          </select>
        </div>
        {query.error && (
          <div className="p-4">
            <TrainingError message={query.error.message} onRetry={() => void query.refetch()} />
          </div>
        )}
        {query.isLoading && <TrainingLoading label="正在加载训练队列…" />}
        {Boolean(query.data?.items.length) && (
          <>
            <div
              aria-hidden="true"
              className={cn(
                "hidden items-center gap-5 border-b border-border bg-muted/30 px-5 py-2.5 text-2xs text-muted-foreground lg:grid",
                columns,
              )}
            >
              <span>实验 / 项目</span>
              <span>状态</span>
              <span>训练进度</span>
              <span>创建时间</span>
              <span />
            </div>
            <ul className="divide-y divide-border">
              {query.data?.items.map(({ run, project_name, project_display_id }) => {
                const progress = trainingProgress(run);
                const showProgress = run.status !== "queued" && progress.total > 0;
                const runFramework =
                  run.config.framework === "libreyolo"
                    ? "LibreYOLO"
                    : run.config.framework === "ultralytics"
                      ? "Ultralytics"
                      : String(run.config.framework ?? "未指定训练器");
                return (
                  <li key={run.id}>
                    <Link
                      to={`/projects/${run.project_id}/learning?tab=training&run=${run.id}`}
                      className={cn(
                        "group grid grid-cols-2 items-center gap-x-5 gap-y-3 px-4 py-4 transition-colors hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring sm:px-5 lg:py-5",
                        columns,
                      )}
                    >
                      <div className="order-1 col-span-2 min-w-0 lg:order-none lg:col-span-1">
                        <h3
                          title={run.name}
                          className="line-clamp-2 break-words text-sm font-semibold group-hover:text-primary"
                        >
                          {run.name}
                        </h3>
                        <p
                          className="mt-1 truncate text-xs text-muted-foreground"
                          title={project_name}
                        >
                          {project_display_id && `${project_display_id} · `}
                          {project_name}
                        </p>
                        <p className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-2xs text-muted-foreground">
                          <span className="font-medium text-foreground/80">{runFramework}</span>
                          <span aria-hidden="true">/</span>
                          <span>{trainingTaskLabel(run.task ?? trainingTaskOf(run.config))}</span>
                          {run.auto_trigger_id && <span>· 自动训练</span>}
                        </p>
                      </div>
                      <div className="order-2 min-w-0 lg:order-none">
                        <Badge
                          dot
                          variant={trainingStatusVariant(run.status)}
                          className="max-w-full whitespace-normal leading-relaxed"
                        >
                          {run.status === "lost"
                            ? "执行器失联"
                            : (trainingStatusLabels[run.status] ?? run.status)}
                        </Badge>
                        {run.status === "lost" && (
                          <p className="mt-1 text-2xs text-muted-foreground">等待恢复</p>
                        )}
                      </div>
                      <div className="order-4 col-span-2 min-w-0 sm:order-3 sm:col-span-1 lg:order-none">
                        {showProgress ? (
                          <div
                            role="progressbar"
                            aria-label={`${run.name} 训练轮次`}
                            aria-valuemin={0}
                            aria-valuemax={progress.total}
                            aria-valuenow={Math.min(progress.total, Math.max(0, progress.epoch))}
                            aria-valuetext={`${progress.epoch} / ${progress.total} 轮`}
                          >
                            <div className="mb-2 flex items-center justify-between gap-2 text-2xs">
                              <span className="font-mono tabular-nums">
                                {progress.epoch}{" "}
                                <span className="text-muted-foreground">/ {progress.total} 轮</span>
                              </span>
                              <span className="font-mono tabular-nums text-muted-foreground">
                                {Math.round(progress.percent)}%
                              </span>
                            </div>
                            <ProgressBar
                              value={progress.percent}
                              color={
                                run.status === "succeeded"
                                  ? "var(--sc-status-positive)"
                                  : run.status === "failed"
                                    ? "var(--sc-status-danger)"
                                    : "var(--sc-brand)"
                              }
                            />
                          </div>
                        ) : (
                          <p className="text-xs text-muted-foreground">
                            {waitingLabels[run.status] ?? "等待轮次数据"}
                          </p>
                        )}
                      </div>
                      <time
                        dateTime={run.created_at}
                        title={
                          run.created_at
                            ? new Date(run.created_at).toLocaleString("zh-CN")
                            : undefined
                        }
                        className="order-3 justify-self-end text-2xs tabular-nums text-muted-foreground sm:order-4 sm:justify-self-start lg:order-none"
                      >
                        <span className="mr-1 lg:hidden">创建于</span>
                        {createdLabel(run.created_at)}
                      </time>
                      <ArrowRight
                        aria-hidden="true"
                        className="hidden size-4 text-muted-foreground transition-transform group-hover:translate-x-0.5 motion-reduce:transition-none lg:block"
                      />
                    </Link>
                    {run.error_message && (
                      <p className="px-4 pb-4 text-xs text-status-danger sm:px-5">
                        <CopyableError message={run.error_message} truncate className="w-full" />
                      </p>
                    )}
                  </li>
                );
              })}
            </ul>
          </>
        )}
        {query.data?.items.length === 0 && !query.error && (
          <div className="px-5 py-12 text-center sm:py-16">
            <div className="mx-auto mb-4 flex size-11 items-center justify-center rounded-xl bg-muted text-muted-foreground">
              <ListStart aria-hidden="true" className="size-5" />
            </div>
            <h3 className="text-sm font-semibold">
              {state === "active" ? "当前没有进行中的训练" : "此筛选下暂无实验"}
            </h3>
            <p className="mx-auto mt-2 max-w-sm text-xs leading-relaxed text-muted-foreground">
              {state === "active"
                ? "查看已结束的实验，或进入项目准备下一次训练。"
                : "试试其他状态或训练器，也可以进入项目创建实验。"}
            </p>
            <div className="mt-5 flex flex-wrap justify-center gap-2">
              <Button
                size="sm"
                onClick={() => (state === "active" ? onFilter({ state: "finished" }) : onStart())}
              >
                {state === "active" ? "查看训练历史" : "开始训练"}
                <ArrowRight aria-hidden="true" className="size-3.5" />
              </Button>
              {hasFilter && (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => onFilter({ state: "all", framework: "" })}
                >
                  重置筛选
                </Button>
              )}
            </div>
          </div>
        )}
        {pagination}
      </section>
      <p className="flex items-start gap-1.5 text-2xs leading-relaxed text-muted-foreground">
        <Server aria-hidden="true" className="mt-0.5 size-3 shrink-0" />
        统计覆盖当前管理范围，并随训练器筛选更新；失联与停止中的实验仍可能占用资源。
      </p>
    </div>
  );
}

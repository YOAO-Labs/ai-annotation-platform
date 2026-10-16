import { Database, Plus } from "lucide-react";
import type { TrainingDatasetSummary } from "@/api/trainingDatasets";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { ProgressBar } from "@/components/ui/ProgressBar";
import { TrainingError, TrainingLoading } from "../Training/TrainingFeedback";
import { trainingTaskLabel } from "../Training/trainingPresentation";

type DatasetAction = "retry" | "download" | "delete" | "cancel";
const statuses: Record<string, string> = {
  pending: "等待构建",
  running: "构建中",
  ready: "已就绪",
  failed: "构建失败",
  cancelled: "已取消",
};

export function ProjectTrainingDataVersions({
  data,
  loading,
  error,
  pending,
  page,
  onPage,
  onRetry,
  onSelect,
  onAction,
  onPrepare,
}: {
  data?: { items: TrainingDatasetSummary[]; total: number };
  loading: boolean;
  error?: string;
  pending: boolean;
  page: number;
  onPage: (page: number) => void;
  onRetry: () => void;
  onSelect: (id: string) => void;
  onAction: (kind: DatasetAction, version: TrainingDatasetSummary) => void;
  onPrepare?: () => void;
}) {
  return (
    <section
      aria-labelledby="dataset-versions-title"
      className="min-w-0 overflow-hidden rounded-xl border border-border bg-card"
    >
      <header className="flex flex-wrap items-start justify-between gap-3 border-b border-border p-4 sm:p-5">
        <div>
          <h2 id="dataset-versions-title" className="text-sm font-semibold">
            数据版本{" "}
            <span className="ml-1 text-xs font-normal tabular-nums text-muted-foreground">
              {data?.total ?? "—"}
            </span>
          </h2>
          <p className="mt-1 text-xs text-muted-foreground">
            查看冻结数据，选择版本检查样本与划分。
          </p>
        </div>
        {onPrepare && (
          <Button size="sm" onClick={onPrepare}>
            <Plus aria-hidden="true" className="size-3.5" />
            准备新版本
          </Button>
        )}
      </header>
      {loading && <TrainingLoading label="正在加载数据版本…" />}
      {error && (
        <div className="p-4">
          <TrainingError message={error} onRetry={onRetry} />
        </div>
      )}
      {!!data?.items.length && (
        <ul className="divide-y divide-border">
          {data.items.map((version) => {
            const building = ["pending", "running"].includes(version.status);
            return (
              <li key={version.id} className="p-4 sm:p-5">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <button
                      title={version.name}
                      className="rounded-sm break-words text-left text-sm font-semibold transition-colors hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      onClick={() => onSelect(version.id)}
                    >
                      {version.name}
                    </button>
                    <p className="mt-1 text-xs text-muted-foreground">
                      {trainingTaskLabel(version.task)} ·{" "}
                      {version.source_media_type === "video" ? "视频来源 → 图片" : "图片来源"}
                    </p>
                  </div>
                  <Badge
                    dot
                    variant={
                      version.status === "ready"
                        ? "success"
                        : version.status === "failed"
                          ? "danger"
                          : building
                            ? "warning"
                            : "default"
                    }
                  >
                    {statuses[version.status] ?? version.status}
                  </Badge>
                </div>
                <dl className="mt-4 grid grid-cols-3 gap-3 text-xs sm:grid-cols-5">
                  {[
                    ["类别", version.class_count ?? 0],
                    ["样本", version.summary?.samples ?? version.summary?.eligible ?? "—"],
                    ["对象", version.summary?.objects ?? "—"],
                    ...(version.status === "ready"
                      ? [
                          ["训练", version.summary?.train ?? "—"],
                          ["验证", version.summary?.val ?? "—"],
                        ]
                      : []),
                  ].map(([label, value]) => (
                    <div key={String(label)}>
                      <dt className="text-2xs text-muted-foreground">{String(label)}</dt>
                      <dd className="mt-1 font-mono text-sm tabular-nums">{String(value)}</dd>
                    </div>
                  ))}
                </dl>
                {building && (
                  <div
                    className="mt-3"
                    role="progressbar"
                    aria-label={`${version.name} 构建进度`}
                    aria-valuenow={version.progress_pct}
                    aria-valuemin={0}
                    aria-valuemax={100}
                  >
                    <ProgressBar value={version.progress_pct} />
                  </div>
                )}
                {version.error_message && (
                  <p className="mt-3 break-words text-xs text-status-danger">
                    {version.error_message}
                  </p>
                )}
                <div className="mt-4 flex flex-wrap items-center justify-between gap-2">
                  <time
                    dateTime={version.created_at}
                    className="text-2xs tabular-nums text-muted-foreground"
                  >
                    {new Date(version.created_at).toLocaleString("zh-CN")}
                  </time>
                  <div className="flex flex-wrap gap-1">
                    {building && version.async_job_id && (
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={pending}
                        onClick={() => onAction("cancel", version)}
                      >
                        取消构建
                      </Button>
                    )}
                    {version.status === "ready" && (
                      <Button
                        size="sm"
                        disabled={pending}
                        onClick={() => onAction("download", version)}
                      >
                        下载训练包
                      </Button>
                    )}
                    {["failed", "cancelled"].includes(version.status) && (
                      <Button
                        size="sm"
                        disabled={pending}
                        onClick={() => onAction("retry", version)}
                      >
                        重试
                      </Button>
                    )}
                    {!building && (
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={pending}
                        onClick={() => onAction("delete", version)}
                      >
                        删除
                      </Button>
                    )}
                  </div>
                </div>
              </li>
            );
          })}
        </ul>
      )}
      {data?.items.length === 0 && !error && (
        <div className="px-5 py-10 text-center">
          <Database aria-hidden="true" className="mx-auto mb-3 size-6 text-muted-foreground" />
          <h3 className="text-sm font-medium">尚无训练数据版本</h3>
          <p className="mx-auto mt-2 max-w-sm text-xs leading-relaxed text-muted-foreground">
            选择标注范围并检查数据，创建第一个固定的训练与验证版本。
          </p>
        </div>
      )}
      {((data?.total ?? 0) > 20 || page > 0) && (
        <nav
          aria-label="数据版本分页"
          className="flex flex-wrap items-center justify-end gap-2 border-t border-border p-3"
        >
          <span className="mr-1 text-xs tabular-nums text-muted-foreground">
            第 {page + 1} / {Math.max(1, Math.ceil((data?.total ?? 0) / 20))} 页
          </span>
          <Button size="sm" disabled={!page} onClick={() => onPage(page - 1)}>
            上一页
          </Button>
          <Button
            size="sm"
            disabled={(page + 1) * 20 >= (data?.total ?? 0)}
            onClick={() => onPage(page + 1)}
          >
            下一页
          </Button>
        </nav>
      )}
    </section>
  );
}

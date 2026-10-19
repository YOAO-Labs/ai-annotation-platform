import { CopyableError } from "@/components/ui/CopyableError";
import { useId, useState } from "react";
import { ArrowRight, ChevronDown, Database, Plus } from "lucide-react";
import type { TrainingDatasetSummary } from "@/api/trainingDatasets";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { ProgressBar } from "@/components/ui/ProgressBar";
import { TrainingError, TrainingLoading } from "../Training/TrainingFeedback";
import { cn } from "@/lib/utils";

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
  selectedId,
  selectedVersion,
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
  selectedId?: string;
  selectedVersion?: TrainingDatasetSummary;
}) {
  const id = useId();
  const [expanded, setExpanded] = useState(false);
  const selected =
    data?.items.find((item) => item.id === selectedId) ??
    (selectedVersion?.id === selectedId ? selectedVersion : undefined);
  const actions = (version: TrainingDatasetSummary) => {
    const building = ["pending", "running"].includes(version.status);
    return (
      <>
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
          <Button size="sm" disabled={pending} onClick={() => onAction("download", version)}>
            下载训练包
          </Button>
        )}
        {["failed", "cancelled"].includes(version.status) && (
          <Button size="sm" disabled={pending} onClick={() => onAction("retry", version)}>
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
      </>
    );
  };
  return (
    <section
      aria-label="数据版本"
      className="min-w-0 overflow-hidden rounded-xl border border-border bg-card"
    >
      <div className="flex flex-wrap items-center gap-3 p-3">
        <label className="flex min-w-0 flex-1 items-center gap-3 text-xs font-medium">
          <span className="shrink-0">数据版本</span>
          <select
            aria-label="选择数据版本"
            value={selectedId ?? ""}
            disabled={loading && !data}
            className="min-w-0 flex-1 rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring sm:max-w-md"
            onChange={(event) => onSelect(event.target.value)}
          >
            {!selectedId && <option value="">选择数据版本</option>}
            {selectedId && !data?.items.some((item) => item.id === selectedId) && (
              <option value={selectedId}>
                {selected?.name ?? `${selectedId.slice(0, 8)} · 正在读取`}
              </option>
            )}
            {data?.items.map((version) => (
              <option key={version.id} value={version.id}>
                {version.version_ordinal != null ? `V${version.version_ordinal} · ` : ""}
                {version.name} · {statuses[version.status] ?? version.status}
              </option>
            ))}
          </select>
        </label>
        <Button
          size="sm"
          variant="ghost"
          aria-expanded={expanded}
          aria-controls={`${id}-list`}
          onClick={() => setExpanded((value) => !value)}
        >
          浏览版本 · {data?.total ?? "—"}
          <ChevronDown
            aria-hidden="true"
            className={cn("size-3.5 transition-transform", expanded && "rotate-180")}
          />
        </Button>
        <div className="flex flex-wrap items-center gap-2">
          {selected && (
            <>
              {!expanded && (
                <Button size="sm" variant="ghost" onClick={() => onSelect(selected.id)}>
                  查看详情
                  <ArrowRight aria-hidden="true" />
                </Button>
              )}
              {actions(selected)}
            </>
          )}
          {onPrepare && (
            <Button size="sm" onClick={onPrepare}>
              <Plus aria-hidden="true" />
              准备新版本
            </Button>
          )}
        </div>
      </div>
      {loading && <TrainingLoading label="正在加载数据版本…" />}
      {error && (
        <div className="p-3">
          <TrainingError message={error} onRetry={onRetry} />
        </div>
      )}
      {expanded && (
        <div id={`${id}-list`} className="border-t border-border">
          <p className="px-4 pt-3 text-2xs text-muted-foreground">
            按创建时间浏览当前任务的冻结版本，选择后在下方查看详情。
          </p>
          <ul className="max-h-72 divide-y divide-border overflow-y-auto">
            {data?.items.map((version) => {
              const building = ["pending", "running"].includes(version.status);
              return (
                <li
                  key={version.id}
                  className={cn(
                    "flex flex-wrap items-center gap-3 px-4 py-3 transition-colors",
                    selectedId === version.id && "bg-chart-1/5",
                  )}
                >
                  <div className="min-w-0 flex-1">
                    <p className="break-words text-sm font-medium">
                      {version.version_ordinal != null && (
                        <span className="mr-2 text-chart-1">V{version.version_ordinal}</span>
                      )}
                      {version.name}
                    </p>
                    <p className="mt-1 text-2xs text-muted-foreground">
                      <time dateTime={version.created_at}>
                        {new Date(version.created_at).toLocaleString("zh-CN")}
                      </time>{" "}
                      · {String(version.summary?.samples ?? version.summary?.eligible ?? "未知")}{" "}
                      张图片 · {version.class_count ?? "未知"} 类
                    </p>
                    {building && <ProgressBar value={version.progress_pct} />}
                    {version.error_message && (
                      <p className="mt-1 break-words text-xs text-status-danger">
                        <CopyableError message={version.error_message} />
                      </p>
                    )}
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
                  <Button
                    size="sm"
                    variant="ghost"
                    aria-label={`查看详情：${version.name}`}
                    aria-current={selectedId === version.id ? "true" : undefined}
                    onClick={() => {
                      onSelect(version.id);
                      setExpanded(false);
                    }}
                  >
                    查看详情
                    <ArrowRight aria-hidden="true" />
                  </Button>
                </li>
              );
            })}
          </ul>
          {((data?.total ?? 0) > 20 || page > 0) && (
            <nav
              aria-label="数据版本分页"
              className="flex flex-wrap items-center justify-end gap-2 border-t border-border p-3"
            >
              <span className="mr-auto text-2xs tabular-nums text-muted-foreground">
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
        </div>
      )}
      {data?.items.length === 0 && !error && (
        <div className="flex items-center gap-3 border-t border-border p-4 text-xs">
          <Database aria-hidden="true" className="size-5 shrink-0 text-muted-foreground" />
          <div>
            <h3 className="font-medium">尚无训练数据版本</h3>
            <p className="mt-1 text-muted-foreground">准备新版本，冻结当前项目的训练与验证数据。</p>
          </div>
        </div>
      )}
    </section>
  );
}

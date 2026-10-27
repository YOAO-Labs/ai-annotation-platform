import { listStyles } from "@/components/ui/listStyles";
import { CopyableError } from "@/components/ui/CopyableError";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { Database } from "lucide-react";
import type { TrainingDatasetSummary } from "@/api/trainingDatasets";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { ProgressBar } from "@/components/ui/ProgressBar";
import { TASKS } from "@/api/generated/capabilityVocab.gen";
import { TrainingError, TrainingLoading } from "../Training/TrainingFeedback";
import {
  TrainingListControls,
  TrainingListPagination,
  type TrainingListFilters,
} from "../Training/TrainingListControls";

type DatasetAction = "retry" | "download" | "delete" | "cancel";
const statuses: Record<string, string> = {
  pending: "等待构建",
  running: "构建中",
  ready: "已就绪",
  failed: "构建失败",
  cancelled: "已取消",
};

export function ProjectTrainingDataActions({
  version,
  pending,
  onAction,
}: {
  version: TrainingDatasetSummary;
  pending: boolean;
  onAction: (kind: DatasetAction, version: TrainingDatasetSummary) => void;
}) {
  const building = ["pending", "running"].includes(version.status);
  return (
    <div className="flex items-center gap-1">
      {building && version.async_job_id && (
        <Button
          size="xs"
          variant="ghost"
          disabled={pending}
          onClick={() => onAction("cancel", version)}
        >
          取消构建
        </Button>
      )}
      {version.status === "ready" && (
        <Button size="xs" disabled={pending} onClick={() => onAction("download", version)}>
          下载训练包
        </Button>
      )}
      {["failed", "cancelled"].includes(version.status) && (
        <Button size="xs" disabled={pending} onClick={() => onAction("retry", version)}>
          重试
        </Button>
      )}
      {!building && (
        <Button
          size="xs"
          variant="ghost"
          disabled={pending}
          onClick={() => onAction("delete", version)}
        >
          删除
        </Button>
      )}
    </div>
  );
}

export function ProjectTrainingDataVersions({
  data,
  loading,
  error,
  pending,
  page,
  onPage,
  pageSize,
  onPageSize,
  onRetry,
  href,
  onAction,
  selectedId,
  filters,
  onFilter,
  onReset,
  taskFilter,
  hasExtraFilters = false,
  showTask = false,
}: {
  data?: { items: TrainingDatasetSummary[]; total: number };
  loading: boolean;
  error?: string;
  pending: boolean;
  page: number;
  onPage: (page: number) => void;
  pageSize: number;
  onPageSize: (size: number) => void;
  onRetry: () => void;
  href: (id: string) => string;
  onAction: (kind: DatasetAction, version: TrainingDatasetSummary) => void;
  selectedId?: string;
  filters: TrainingListFilters;
  onFilter: (values: Partial<TrainingListFilters>) => void;
  onReset: () => void;
  taskFilter?: ReactNode;
  hasExtraFilters?: boolean;
  showTask?: boolean;
}) {
  return (
    <section
      aria-label="数据版本"
      className="min-w-0 overflow-hidden rounded-xl border border-border bg-card"
    >
      <TrainingListControls
        kind="数据版本"
        filters={filters}
        onChange={onFilter}
        onReset={onReset}
        statuses={statuses}
        hasExtraFilters={hasExtraFilters}
      >
        {taskFilter}
      </TrainingListControls>
      {loading && <TrainingLoading label="正在加载数据版本…" />}
      {error && (
        <div className="p-3">
          <TrainingError message={error} onRetry={onRetry} />
        </div>
      )}
      <div className="overflow-x-auto border-t border-border">
        <table aria-label="训练数据版本列表" className={listStyles.table}>
          <thead className="bg-muted/40 text-2xs text-muted-foreground">
            <tr>
              {[
                "版本名称",
                ...(showTask ? ["数据类型"] : []),
                "状态",
                "样本数",
                "类别数",
                "训练包",
                "创建时间",
                "操作",
              ].map((label) => (
                <th key={label} scope="col" className="px-4 py-3 font-medium">
                  {label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {data?.items.map((version) => {
              const building = ["pending", "running"].includes(version.status);
              const samples = version.summary?.samples ?? version.summary?.eligible;
              return (
                <tr
                  key={version.id}
                  aria-selected={selectedId === version.id}
                  className="hover:bg-muted/30 aria-selected:bg-primary/5"
                >
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-2">
                      {version.version_ordinal != null && (
                        <span className="text-2xs tabular-nums text-muted-foreground">
                          V{version.version_ordinal}
                        </span>
                      )}
                      <Link
                        aria-label={`查看详情：${version.name}`}
                        title={version.name}
                        className="max-w-64 truncate rounded-sm text-left font-medium text-primary underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        to={href(version.id)}
                      >
                        {version.name}
                      </Link>
                    </div>
                    {version.error_message && (
                      <p className="mt-1 max-w-64 text-2xs text-status-danger">
                        <CopyableError message={version.error_message} truncate />
                      </p>
                    )}
                  </td>
                  {showTask && (
                    <td className="px-4 py-3 text-muted-foreground">
                      {version.task ? (TASKS[version.task]?.label ?? version.task) : "未识别"}
                    </td>
                  )}
                  <td className="px-4 py-3">
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
                    {building && <ProgressBar value={version.progress_pct} />}
                  </td>
                  <td className="px-4 py-3 tabular-nums">
                    {typeof samples === "number" ? samples : "—"}
                  </td>
                  <td className="px-4 py-3 tabular-nums">{version.class_count ?? "—"}</td>
                  <td className="px-4 py-3 tabular-nums text-muted-foreground">
                    {version.size_bytes == null
                      ? "—"
                      : `${(version.size_bytes / 1024 / 1024).toFixed(1)} MB`}
                  </td>
                  <td className="px-4 py-3 tabular-nums text-muted-foreground">
                    <time dateTime={version.created_at}>
                      {new Date(version.created_at).toLocaleString("zh-CN")}
                    </time>
                  </td>
                  <td className="px-4 py-3">
                    <ProjectTrainingDataActions
                      version={version}
                      pending={pending}
                      onAction={onAction}
                    />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {data?.items.length === 0 && !loading && !error && (
        <div className="flex items-center gap-3 border-t border-border p-6 text-xs">
          <Database aria-hidden="true" className="size-5 shrink-0 text-muted-foreground" />
          <div>
            <h3 className="font-medium">
              {Object.values(filters).some(Boolean) ? "没有符合条件的数据版本" : "尚无训练数据版本"}
            </h3>
            <p className="mt-1 text-muted-foreground">
              调整筛选，或准备新版本以冻结当前项目的训练与验证数据。
            </p>
          </div>
        </div>
      )}
      <TrainingListPagination
        kind="数据版本"
        page={page}
        total={data?.total ?? 0}
        pending={loading}
        onPage={onPage}
        pageSize={pageSize}
        onPageSize={onPageSize}
      />
    </section>
  );
}

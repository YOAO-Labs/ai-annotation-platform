import { Link } from "react-router-dom";
import type { TrainingRunOut } from "@/api/trainingRuns";
import { Badge } from "@/components/ui/Badge";
import { TrainingLoading } from "../Training/TrainingFeedback";
import {
  trainingProgress,
  trainingStatusLabels,
  trainingStatusVariant,
  trainingTaskLabel,
  trainingTaskOf,
} from "../Training/trainingPresentation";
import { TrainingListPagination } from "../Training/TrainingListControls";
import { listStyles } from "@/components/ui/listStyles";

export function TrainingRunTable({
  data,
  loading,
  failed,
  page,
  onPage,
  pageSize,
  onPageSize,
  selectedId,
  href,
}: {
  data?: { items: TrainingRunOut[]; total: number };
  loading: boolean;
  failed: boolean;
  page: number;
  onPage: (page: number) => void;
  pageSize: number;
  onPageSize: (size: number) => void;
  selectedId: string;
  href: (id: string) => string;
}) {
  return (
    <>
      {loading && <TrainingLoading label="正在加载训练实验…" rows={2} />}
      <div className="overflow-x-auto border-t border-border">
        <table aria-label="训练实验列表" className={listStyles.table}>
          <thead className="bg-muted/40 text-2xs text-muted-foreground">
            <tr>
              {["实验名称", "状态", "任务", "训练框架", "基础权重", "训练进度", "创建时间"].map(
                (label) => (
                  <th key={label} scope="col" className="px-4 py-3 font-medium">
                    {label}
                  </th>
                ),
              )}
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {data?.items.map((run) => {
              const progress = trainingProgress(run);
              return (
                <tr
                  key={run.id}
                  aria-selected={selectedId === run.id}
                  className="hover:bg-muted/30 aria-selected:bg-primary/5"
                >
                  <td className="px-4 py-3">
                    <Link
                      className="inline-block max-w-72 truncate rounded-sm align-middle text-left font-medium text-primary underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      title={run.name}
                      to={href(run.id)}
                    >
                      {run.name}
                    </Link>
                  </td>
                  <td className="px-4 py-3">
                    <Badge dot variant={trainingStatusVariant(run.status)}>
                      {trainingStatusLabels[run.status] ?? run.status}
                    </Badge>
                  </td>
                  <td className="px-4 py-3">
                    {trainingTaskLabel(run.task ?? trainingTaskOf(run.config))}
                  </td>
                  <td className="px-4 py-3">
                    {run.config.framework === "ultralytics"
                      ? "Ultralytics"
                      : run.config.framework === "libreyolo"
                        ? "LibreYOLO"
                        : String(run.config.framework ?? "—")}{" "}
                    <span className="text-muted-foreground">
                      {String(run.config.framework_version ?? "")}
                    </span>
                  </td>
                  <td className="px-4 py-3 font-mono text-2xs">
                    {typeof run.config.base_model === "object" && run.config.base_model !== null
                      ? String((run.config.base_model as { id?: string }).id ?? "—")
                      : String(run.config.base_model ?? "—")}
                  </td>
                  <td className="px-4 py-3 tabular-nums">
                    {run.status === "queued"
                      ? "等待调度"
                      : `${progress.epoch} / ${progress.total} 轮`}
                  </td>
                  <td className="px-4 py-3 tabular-nums text-muted-foreground">
                    <time dateTime={run.created_at}>
                      {new Date(run.created_at).toLocaleString("zh-CN")}
                    </time>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {!loading && !failed && !data?.items.length && (
          <p className="p-8 text-center text-xs text-muted-foreground">
            没有符合条件的实验，请调整筛选或新建实验。
          </p>
        )}
      </div>
      <TrainingListPagination
        kind="实验"
        page={page}
        total={data?.total ?? 0}
        pending={loading}
        onPage={onPage}
        pageSize={pageSize}
        onPageSize={onPageSize}
      />
    </>
  );
}

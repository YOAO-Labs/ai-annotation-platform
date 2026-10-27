import { useId, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import type { DatasetWorkbenchProject } from "@/api/datasets";
import { taskViewsApi, type DataManagerSummary } from "@/api/taskViews";
import { useDatasetOwner } from "@/hooks/useDatasets";
import { Button } from "@/components/ui/Button";
import { CopyableError } from "@/components/ui/CopyableError";
import { ListPagination } from "@/components/ui/ListPagination";
import { listStyles } from "@/components/ui/listStyles";
import { datasetDataManagerPath, datasetTaskFilter } from "../Projects/data-manager/datasetScope";

export function DatasetProjectStatistics({
  datasetId,
  projects,
  selectedProjectId,
  onProjectChange,
}: {
  datasetId: string;
  projects: DatasetWorkbenchProject[];
  selectedProjectId: string | undefined;
  onProjectChange: (id: string) => void;
}) {
  const owner = useDatasetOwner();
  const selectId = useId();
  const hintId = useId();
  const project =
    selectedProjectId === undefined
      ? projects[0]
      : projects.find((value) => value.id === selectedProjectId);
  const invalidSelection = selectedProjectId !== undefined && !project;
  const scope = JSON.stringify([datasetId, project?.id, ...owner.key]);
  const currentScope = useRef(scope);
  currentScope.current = scope;
  const summary = useQuery({
    queryKey: ["dataset-project-statistics", datasetId, project?.id, ...owner.key],
    queryFn: async ({ signal }) => {
      const currentOwner = owner.capture();
      const assertCurrent = () => {
        owner.assert(signal);
        if (!currentOwner() || currentScope.current !== scope)
          throw new DOMException("项目统计请求已失效", "AbortError");
      };
      assertCurrent();
      const value = await taskViewsApi.summary(project!.id, datasetTaskFilter(datasetId), {
        signal,
      });
      assertCurrent();
      return value;
    },
    enabled: owner.enabled && !!project,
    staleTime: 0,
    retry: false,
  });

  return (
    <section aria-label="项目标签统计" className="rounded-lg border border-border bg-card p-4">
      <h2 className="text-md font-medium">项目标签统计</h2>
      {(projects.length > 0 || invalidSelection) && (
        <div className="mt-3 grid max-w-md gap-2">
          <label htmlFor={selectId} className="text-sm">
            统计项目
          </label>
          <select
            id={selectId}
            aria-describedby={hintId}
            className="w-full rounded-md border border-input bg-background px-2.5 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            value={project?.id ?? selectedProjectId ?? ""}
            onChange={(event) => onProjectChange(event.target.value)}
          >
            {invalidSelection && (
              <option value={selectedProjectId} disabled>
                不可用项目
              </option>
            )}
            {projects.map((value) => (
              <option key={value.id} value={value.id}>
                {value.name}
              </option>
            ))}
          </select>
          <p id={hintId} className="text-xs leading-relaxed text-muted-foreground">
            仅统计本数据集在所选项目中你可见的任务；切换项目后重新统计。
          </p>
        </div>
      )}
      {invalidSelection ? (
        <p role="alert" className="mt-3 text-sm text-status-caution">
          所选项目不可用，可能已取消关联或没有访问权限。请选择其他关联项目。
        </p>
      ) : !project ? (
        <p className="mt-3 text-sm text-muted-foreground">暂无可统计的关联项目。</p>
      ) : summary.isError ? (
        <div
          role="alert"
          className="mt-3 flex flex-wrap items-center gap-2 rounded-md bg-status-danger-soft p-3 text-xs text-status-danger"
        >
          <CopyableError
            message={summary.error instanceof Error ? summary.error.message : "标签统计加载失败"}
          />
          <Button size="sm" onClick={() => void summary.refetch()}>
            重试标签统计
          </Button>
        </div>
      ) : summary.isFetching || !summary.data ? (
        <p role="status" className="mt-3 text-sm text-muted-foreground">
          正在加载标签统计…
        </p>
      ) : (
        <StatisticsResult
          key={scope}
          datasetId={datasetId}
          projectId={project.id}
          summary={summary.data}
        />
      )}
    </section>
  );
}

function StatisticsResult({
  datasetId,
  projectId,
  summary,
}: {
  datasetId: string;
  projectId: string;
  summary: DataManagerSummary;
}) {
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(20);
  const labels = Object.entries(summary.annotations.by_class).sort(
    ([a, left], [b, right]) => right - left || a.localeCompare(b),
  );
  const currentPage = Math.min(page, Math.max(0, Math.ceil(labels.length / pageSize) - 1));
  return (
    <div className="mt-4 space-y-3">
      <dl className="flex flex-wrap gap-x-6 gap-y-3 text-sm">
        <div>
          <dt className="text-xs text-muted-foreground">本数据集可见任务</dt>
          <dd className="mt-1 tabular-nums">{summary.scope.matched_task_total}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">已保存标注对象</dt>
          <dd className="mt-1 tabular-nums">{summary.annotations.total}</dd>
        </div>
      </dl>
      <p className="text-xs leading-relaxed text-muted-foreground">
        标签数量按已保存、活跃且未取消的标注对象计数，包含相机标注；不代表样本数、视频帧数或预测数量。
      </p>
      {labels.length === 0 ? (
        <p className="rounded-md bg-muted p-4 text-sm text-muted-foreground">
          当前范围暂无已保存的标签标注。
        </p>
      ) : (
        <div className={listStyles.surface}>
          <div className={listStyles.scroll}>
            <table className={listStyles.table} aria-label="项目标签数量">
              <thead>
                <tr>
                  <th scope="col">标签</th>
                  <th scope="col">标注对象数</th>
                  <th scope="col">任务入口</th>
                </tr>
              </thead>
              <tbody>
                {labels
                  .slice(currentPage * pageSize, (currentPage + 1) * pageSize)
                  .map(([name, count]) => (
                    <tr key={name}>
                      <td className="max-w-xs break-words">{name || "（空标签）"}</td>
                      <td className="tabular-nums">{count}</td>
                      <td>
                        <Link
                          className="text-xs text-primary hover:underline"
                          to={datasetDataManagerPath(projectId, datasetId, {
                            field: "annotation.class_name",
                            op: "eq",
                            value: name,
                          })}
                        >
                          查看含该标签的任务
                        </Link>
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
          {labels.length > 20 && (
            <ListPagination
              label="项目标签"
              page={currentPage}
              pageSize={pageSize}
              total={labels.length}
              onPageChange={setPage}
              onPageSizeChange={(size) => {
                setPageSize(size);
                setPage(0);
              }}
            />
          )}
        </div>
      )}
    </div>
  );
}

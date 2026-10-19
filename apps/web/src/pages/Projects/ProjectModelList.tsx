import { Link } from "react-router-dom";
import { Layers } from "lucide-react";
import { modelMetrics, type TrainedModelOut } from "@/api/trainedModels";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { TrainingError, TrainingLoading } from "../Training/TrainingFeedback";
import {
  TrainingListControls,
  TrainingListPagination,
  trainingRecordPath,
  type TrainingListFilters,
} from "../Training/TrainingListControls";

export function ProjectModelList({
  projectId,
  filters,
  onChange,
  onReset,
  archive,
  onArchive,
  models,
  loading,
  error,
  onRetry,
  page,
  onPage,
  params,
  lineageHref,
  onRegister,
}: {
  projectId: string;
  filters: TrainingListFilters;
  onChange: (values: Partial<TrainingListFilters>) => void;
  onReset: () => void;
  archive: "exclude" | "include" | "only";
  onArchive: (value: string) => void;
  models?: { items: TrainedModelOut[]; total: number };
  loading: boolean;
  error?: string;
  onRetry: () => void;
  page: number;
  onPage: (page: number) => void;
  params: URLSearchParams;
  lineageHref: (id: string) => string;
  onRegister: () => void;
}) {
  const filtered = archive !== "exclude" || Object.values(filters).some(Boolean);
  return (
    <section
      aria-label="候选模型"
      className="mt-5 min-w-0 overflow-hidden rounded-xl border border-border bg-card"
    >
      <TrainingListControls
        kind="模型"
        filters={filters}
        onChange={onChange}
        onReset={onReset}
        hasExtraFilters={archive !== "exclude"}
      >
        <select
          aria-label="候选显示范围"
          className="min-w-0 rounded-md border border-border bg-background px-2.5 py-2 text-xs text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          value={archive}
          onChange={(event) => onArchive(event.target.value)}
        >
          <option value="exclude">默认候选（保留当前发布）</option>
          <option value="include">全部候选，包含归档</option>
          <option value="only">仅已归档候选</option>
        </select>
      </TrainingListControls>
      {error ? (
        <TrainingError message={error} onRetry={onRetry} />
      ) : loading ? (
        <TrainingLoading label="正在加载候选模型…" />
      ) : models && models.items.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="w-full whitespace-nowrap text-left text-xs">
            <thead className="bg-muted/40 text-2xs text-muted-foreground">
              <tr>
                {["模型名称", "状态", "框架", "类别", "mAP50-95", "创建时间", "操作"].map(
                  (title) => (
                    <th key={title} scope="col" className="px-4 py-3 font-medium">
                      {title}
                    </th>
                  ),
                )}
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {models.items.map((model) => {
                const metrics = modelMetrics(model);
                const score = metrics["metrics/mAP50-95(B)"] ?? metrics["metrics/mAP50-95"];
                return (
                  <tr key={model.id} className="hover:bg-muted/20">
                    <td className="px-4 py-3">
                      <Link
                        className="block max-w-64 truncate font-medium text-primary hover:underline"
                        title={model.name}
                        to={trainingRecordPath(projectId, "models", model.id, params)}
                      >
                        {model.name}
                      </Link>
                    </td>
                    <td className="px-4 py-3">
                      <span className="flex gap-1">
                        {model.is_current_release && <Badge variant="success">当前发布</Badge>}
                        {model.archived ? (
                          <Badge>已归档</Badge>
                        ) : (
                          !model.is_current_release && <Badge>候选</Badge>
                        )}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      {model.framework === "libreyolo" ? "LibreYOLO" : "Ultralytics"}{" "}
                      <span className="text-muted-foreground">{model.framework_version}</span>
                    </td>
                    <td className="px-4 py-3 tabular-nums">{model.classes.length}</td>
                    <td className="px-4 py-3 font-mono tabular-nums">
                      {score === undefined ? "—" : score.toFixed(4)}
                    </td>
                    <td className="px-4 py-3 text-muted-foreground">
                      {new Date(model.created_at).toLocaleString("zh-CN", { hour12: false })}
                    </td>
                    <td className="px-4 py-3">
                      <Link className="text-primary hover:underline" to={lineageHref(model.id)}>
                        来源与使用记录
                      </Link>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        models && (
          <div className="px-5 py-10 text-center">
            <Layers aria-hidden="true" className="mx-auto mb-3 size-6 text-muted-foreground" />
            <h3 className="text-sm font-medium">
              {filtered
                ? "没有符合筛选条件的模型"
                : page > 0
                  ? "当前页没有候选模型"
                  : "尚未注册模型版本"}
            </h3>
            <p className="mt-2 text-xs text-muted-foreground">
              {filtered
                ? "调整筛选条件，或重置后查看全部候选。"
                : "成功训练后注册权重，完成评估与加载试用，再由负责人发布。"}
            </p>
            <Button
              size="sm"
              variant="ghost"
              className="mt-3"
              onClick={filtered ? onReset : onRegister}
            >
              {filtered ? "重置筛选" : "填写候选信息"}
            </Button>
          </div>
        )
      )}
      <TrainingListPagination
        kind="模型"
        page={page}
        total={models?.total ?? 0}
        pending={loading || !!error}
        onPage={onPage}
      />
    </section>
  );
}

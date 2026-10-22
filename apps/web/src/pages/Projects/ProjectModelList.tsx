import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { Layers } from "lucide-react";
import { modelMetrics, record, type ModelTask, type TrainedModelOut } from "@/api/trainedModels";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { listStyles } from "@/components/ui/listStyles";
import { TrainingError, TrainingLoading } from "../Training/TrainingFeedback";
import { trainingTaskOf } from "../Training/trainingPresentation";
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
  pageSize,
  onPageSize,
  params,
  onRegister,
  onSetDefault,
  task,
  taskFilter,
  defaultPreannotation,
  children,
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
  pageSize: number;
  onPageSize: (size: number) => void;
  params: URLSearchParams;
  onRegister: () => void;
  onSetDefault: (modelId: string, trigger: HTMLButtonElement) => void;
  task: ModelTask | "all";
  taskFilter: ReactNode;
  defaultPreannotation?: {
    modelId: string | null | undefined;
    loading: boolean;
    pending: boolean;
    onDisable: (modelId: string) => void;
  };
  children?: ReactNode;
}) {
  const taskFiltered = !!filters.task && task !== "all";
  const filtered =
    archive !== "exclude" ||
    taskFiltered ||
    Object.entries(filters).some(([key, value]) => key !== "task" && !!value);
  const modelParams = new URLSearchParams(params);
  modelParams.set("model_task", task);
  return (
    <section
      aria-label="模型列表"
      className="mt-5 min-w-0 overflow-hidden rounded-xl border border-border bg-card"
    >
      <TrainingListControls
        kind="模型"
        filters={{ ...filters, task: "" }}
        onChange={onChange}
        onReset={onReset}
        hasExtraFilters={archive !== "exclude" || taskFiltered}
      >
        {taskFilter}
        <select
          aria-label="模型显示范围"
          className="min-w-0 rounded-md border border-border bg-background px-2.5 py-2 text-xs text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          value={archive}
          onChange={(event) => onArchive(event.target.value)}
        >
          <option value="exclude">未归档及默认模型</option>
          <option value="include">全部模型，包含归档</option>
          <option value="only">仅已归档模型</option>
        </select>
      </TrainingListControls>
      {defaultPreannotation?.loading && (
        <p role="status" className="px-3 pb-3 text-xs text-muted-foreground">
          正在读取默认预标设置…
        </p>
      )}
      {error ? (
        <TrainingError message={error} onRetry={onRetry} />
      ) : loading ? (
        <TrainingLoading label="正在加载模型列表…" />
      ) : models && models.items.length > 0 ? (
        <div className="overflow-x-auto border-t border-border">
          <table className={listStyles.table}>
            <thead className="bg-muted/40 text-2xs text-muted-foreground">
              <tr>
                {[
                  "模型名称",
                  "型号",
                  "类型",
                  "状态",
                  "框架",
                  "类别",
                  task === "segment" ? "掩码 mAP50-95" : "mAP50-95",
                  "创建时间",
                ].map((title) => (
                  <th key={title} scope="col" className="px-4 py-3 font-medium">
                    {title}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {models.items.map((model) => {
                const modelTask = trainingTaskOf({ task: model.task ?? model.provenance.task });
                const baseModel = model.provenance.base_model;
                const modelSpec = typeof baseModel === "string" ? baseModel : record(baseModel).id;
                const isDefault =
                  !defaultPreannotation || defaultPreannotation.loading
                    ? !!model.is_current_release
                    : model.id === defaultPreannotation.modelId;
                const metrics = modelMetrics(model);
                const score =
                  modelTask === "segment"
                    ? metrics["metrics/mAP50-95(M)"]
                    : (metrics["metrics/mAP50-95(B)"] ?? metrics["metrics/mAP50-95"]);
                return (
                  <tr key={model.id} className="hover:bg-muted/20">
                    <td className="px-4 py-3">
                      <Link
                        className="block max-w-64 truncate font-medium text-primary hover:underline"
                        title={model.name}
                        to={trainingRecordPath(projectId, "models", model.id, modelParams)}
                      >
                        {model.name}
                      </Link>
                    </td>
                    <td className="px-4 py-3 font-mono">
                      {typeof modelSpec === "string" && modelSpec.trim() ? modelSpec : "—"}
                    </td>
                    <td className="px-4 py-3">
                      {modelTask === "segment" ? "实例分割" : "目标检测"}
                    </td>
                    <td className="px-4 py-3">
                      <span className="flex flex-wrap items-center gap-2">
                        {isDefault && <Badge variant="success">默认预标</Badge>}
                        {model.archived ? (
                          <Badge>已归档</Badge>
                        ) : (
                          !isDefault && <Badge>已注册</Badge>
                        )}
                        {isDefault && defaultPreannotation && (
                          <Button
                            size="xs"
                            variant="ghost"
                            className="text-muted-foreground hover:text-foreground"
                            disabled={defaultPreannotation.pending}
                            onClick={() => defaultPreannotation.onDisable(model.id)}
                          >
                            停用默认预标
                          </Button>
                        )}
                        {!isDefault && (
                          <Button
                            size="xs"
                            variant="ghost"
                            className="text-muted-foreground hover:text-foreground"
                            onClick={(event) => onSetDefault(model.id, event.currentTarget)}
                          >
                            设为默认预标
                          </Button>
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
                  ? "当前页没有模型"
                  : "尚未注册模型版本"}
            </h3>
            <p className="mt-2 text-xs text-muted-foreground">
              {filtered
                ? "调整筛选条件，或重置后查看全部模型。"
                : "成功训练后注册权重，完成评估与加载试用后，可设置对应类型的默认预标模型。"}
            </p>
            <Button
              size="sm"
              variant="ghost"
              className="mt-3"
              onClick={filtered ? onReset : onRegister}
            >
              {filtered ? "重置筛选" : "选择训练结果"}
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
        pageSize={pageSize}
        onPageSize={onPageSize}
      />
      {children}
    </section>
  );
}

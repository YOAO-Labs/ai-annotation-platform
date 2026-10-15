import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { trainingRunsApi, type TrainingRunCreate, type TrainingRunOut } from "@/api/trainingRuns";
import type { TrainingDatasetSummary } from "@/api/trainingDatasets";
import { Plus, FlaskConical } from "lucide-react";
import { Modal } from "@/components/ui/Modal";
import { TrainingRunMonitor } from "../Training/TrainingRunMonitor";
import { TrainingParameterInventory } from "../Training/TrainingParameterInventory";
import { useTrainingEvents } from "../Training/useTrainingEvents";
import {
  activeTrainingStates as active,
  trainingStatusLabels as labels,
  trainingProgress,
  trainingStatusVariant,
} from "../Training/trainingPresentation";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { ProgressBar } from "@/components/ui/ProgressBar";
import { confirmDialog } from "@/components/ui/decisionDialog";

const inputClass =
  "w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground focus-visible:ring-2 focus-visible:ring-ring";
export function ProjectTrainingPanel({
  projectId,
  datasets,
  initialRunId = "",
  initialFramework = "",
  initialCreate = false,
  visible = true,
  onRunSelect,
  onCreateClose,
  datasetTotal,
  hasMoreDatasets = false,
  loadingMoreDatasets = false,
  onLoadMoreDatasets,
}: {
  projectId: string;
  datasets: TrainingDatasetSummary[];
  initialRunId?: string;
  initialFramework?: string;
  initialCreate?: boolean;
  visible?: boolean;
  onRunSelect?: (id: string) => void;
  onCreateClose?: () => void;
  datasetTotal?: number;
  hasMoreDatasets?: boolean;
  loadingMoreDatasets?: boolean;
  onLoadMoreDatasets?: () => void;
}) {
  const client = useQueryClient();
  const [name, setName] = useState("");
  const [datasetId, setDatasetId] = useState("");
  const [chosenFramework, setFramework] = useState(initialFramework);
  const [createOpen, setCreateOpen] = useState(initialCreate);
  const [inventoryOpen, setInventoryOpen] = useState(false);
  const [attemptId, setAttemptId] = useState("");
  const [baseModel, setBaseModel] = useState("");
  const [parameters, setParameters] = useState({
    epochs: "50",
    batch: "8",
    imgsz: "640",
    seed: "42",
    lr0: "0.01",
    patience: "50",
  });
  const [report, setReport] = useState<{
    config_digest: string;
    config: Record<string, unknown>;
  } | null>(null);
  const [error, setError] = useState("");
  const [page, setPage] = useState(0);
  const [selectedId, setDetailId] = useState(initialRunId);
  useEffect(() => {
    setDetailId(initialRunId);
    setAttemptId("");
  }, [initialRunId]);
  // Retain the request identity after a lost response; changing inputs retires it.
  const requestId = useRef(crypto.randomUUID());
  const checkedDigest = useRef<string | null>(null);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const catalog = useQuery({
    queryKey: ["training-catalog", projectId],
    queryFn: () => trainingRunsApi.catalog(projectId),
    enabled: visible,
    refetchInterval: visible ? 5000 : false,
  });
  const runs = useQuery({
    queryKey: ["training-runs", projectId, page],
    queryFn: () => trainingRunsApi.list(projectId, page * 20),
    enabled: visible,
    refetchInterval: visible ? 3000 : false,
  });
  const detailId = selectedId || runs.data?.items[0]?.id || "";
  const selectRun = (id: string) => {
    setDetailId(id);
    setAttemptId("");
    onRunSelect?.(id);
  };
  const detail = useQuery({
    queryKey: ["training-run", projectId, detailId],
    queryFn: () => trainingRunsApi.get(projectId, detailId),
    enabled: visible && !!detailId,
    refetchInterval: (query) =>
      visible && query.state.data?.run && active.has(query.state.data.run.status) ? 3000 : false,
  });
  const selectedAttemptId = attemptId || detail.data?.run.current_attempt_id || "";
  const events = useTrainingEvents(
    projectId,
    detailId,
    selectedAttemptId,
    visible,
    !!detail.data?.run &&
      active.has(detail.data.run.status) &&
      selectedAttemptId === detail.data.run.current_attempt_id,
  );
  const framework = chosenFramework || catalog.data?.items[0]?.framework || "ultralytics";
  const template = catalog.data?.items.find((item) => item.framework === framework);
  const modelId = baseModel || template?.base_models[0]?.id || "";
  const body = useMemo(
    (): TrainingRunCreate => ({
      name: name.trim(),
      dataset_id: datasetId,
      framework: framework as TrainingRunCreate["framework"],
      base_model: modelId,
      request_id: requestId.current,
      parameters: Object.fromEntries(
        Object.entries(parameters).map(([key, value]) => [key, Number(value)]),
      ) as TrainingRunCreate["parameters"],
    }),
    [name, datasetId, framework, modelId, parameters],
  );
  const refresh = () => {
    void client.invalidateQueries({ queryKey: ["training-runs", projectId] });
    void client.invalidateQueries({ queryKey: ["training-run", projectId] });
    void client.invalidateQueries({ queryKey: ["training-events", projectId] });
    void client.invalidateQueries({ queryKey: ["training-catalog", projectId] });
    void client.invalidateQueries({ queryKey: ["training-hub"] });
  };
  const preflight = useMutation({
    mutationFn: () =>
      trainingRunsApi.preflight(projectId, { ...body, request_id: requestId.current }),
    onSuccess: (value) => {
      if (checkedDigest.current && checkedDigest.current !== value.config_digest)
        requestId.current = crypto.randomUUID();
      checkedDigest.current = value.config_digest;
      setReport(value);
    },
    onError: (reason: Error) => {
      setError(reason.message);
      setReport(null);
    },
  });
  const create = useMutation({
    mutationFn: () =>
      trainingRunsApi.create(projectId, {
        ...body,
        request_id: requestId.current,
        preflight_digest: report?.config_digest,
      }),
    onSuccess: (run) => {
      refresh();
      if (!mounted.current) return;
      selectRun(run.id);
      setCreateOpen(false);
      setReport(null);
      setName("");
      requestId.current = crypto.randomUUID();
      checkedDigest.current = null;
    },
    onError: (reason: Error) => setError(reason.message),
  });
  const action = useMutation({
    mutationFn: async ({
      kind,
      run,
      attemptId,
      filename,
    }: {
      kind: "cancel" | "resume" | "delete" | "download";
      run: TrainingRunOut;
      attemptId?: string;
      filename?: string;
    }) => {
      if (kind === "download" && attemptId && filename) {
        const result = await trainingRunsApi.download(projectId, run.id, attemptId, filename);
        const link = document.createElement("a");
        link.href = result.url;
        link.download = filename;
        link.rel = "noopener";
        link.click();
      } else if (kind === "cancel") await trainingRunsApi.cancel(projectId, run.id);
      else if (kind === "resume") await trainingRunsApi.resume(projectId, run.id);
      else if (
        kind === "delete" &&
        (await confirmDialog({
          title: `删除实验“${run.name}”`,
          description: "将删除此实验的全部权重、检查点和日志。训练数据版本会保留。",
          tone: "danger",
          confirmLabel: "删除实验",
        }))
      ) {
        await trainingRunsApi.remove(projectId, run.id);
        if (detailId === run.id) setDetailId("");
      }
    },
    onSuccess: refresh,
    onError: (reason: Error) => setError(reason.message),
  });
  const update = (callback: () => void) => {
    callback();
    setReport(null);
    setError("");
    requestId.current = crypto.randomUUID();
    checkedDigest.current = null;
  };
  const busy = preflight.isPending || create.isPending;
  const ready = datasets.filter((item) => item.status === "ready");
  const valid =
    !!name.trim() &&
    ready.some((item) => item.id === datasetId) &&
    !!modelId &&
    catalog.data?.enabled &&
    Object.values(parameters).every((value) => value.trim() && Number.isFinite(Number(value)));
  const current = detail.data?.run;
  return (
    <section aria-labelledby="training-title">
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 id="training-title" className="text-md font-semibold">
            训练实验
          </h2>
          <p className="mt-1 text-xs text-muted-foreground">
            选择实验查看实时进度、指标曲线与产物。每次执行和续训独立记录。
          </p>
        </div>
        <Button
          size="sm"
          variant="primary"
          disabled={!catalog.data?.enabled}
          onClick={() => {
            setError("");
            setCreateOpen(true);
          }}
        >
          <Plus className="size-3.5" />
          新建实验
        </Button>
      </div>
      <div className="mb-4 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <Badge
          dot
          variant={catalog.data?.online ? (catalog.data.busy ? "warning" : "success") : "default"}
        >
          {catalog.data?.enabled
            ? catalog.data.online
              ? catalog.data.busy
                ? "GPU 正忙 · 新实验将排队"
                : "执行器在线 · 可开始训练"
              : "执行器离线 · 实验可排队"
            : "训练执行器尚未启用"}
        </Badge>
        <span>
          {datasetTotal ?? ready.length} 个就绪数据版本 · 共 {runs.data?.total ?? 0} 个实验
        </span>
      </div>
      {(error || catalog.error || runs.error || detail.error) && !createOpen && (
        <p
          role="alert"
          className="mb-4 rounded-md bg-status-danger-soft p-3 text-sm text-status-danger"
        >
          {error || catalog.error?.message || runs.error?.message || detail.error?.message}
        </p>
      )}
      {runs.isLoading && (
        <p role="status" className="py-8 text-sm text-muted-foreground">
          正在加载训练实验…
        </p>
      )}
      <div className="grid items-start gap-4 lg:grid-cols-[minmax(240px,0.7fr)_minmax(0,1.8fr)]">
        <aside className="min-w-0" aria-label="实验列表">
          <div className="space-y-2">
            {runs.data?.items.map((run) => {
              const progress = trainingProgress(run);
              return (
                <button
                  key={run.id}
                  aria-label={run.name}
                  aria-pressed={detailId === run.id}
                  onClick={() => selectRun(run.id)}
                  className={`w-full rounded-lg border p-4 text-left transition-colors focus-visible:ring-2 focus-visible:ring-ring ${detailId === run.id ? "border-primary bg-primary/5" : "border-border bg-card hover:bg-muted"}`}
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="break-words text-sm font-medium">{run.name}</span>
                    <Badge dot variant={trainingStatusVariant(run.status)}>
                      {labels[run.status] ?? run.status}
                    </Badge>
                  </div>
                  <p className="mt-2 text-xs text-muted-foreground">
                    {String(run.config.framework)} · {run.auto_trigger_id ? "自动" : "手动"}
                  </p>
                  <div className="mt-3 flex justify-between text-xs tabular-nums text-muted-foreground">
                    <span>{new Date(run.created_at).toLocaleDateString("zh-CN")}</span>
                    <span>
                      {progress.epoch} / {progress.total} 轮
                    </span>
                  </div>
                  <div className="mt-2">
                    <ProgressBar value={progress.percent} />
                  </div>
                </button>
              );
            })}
          </div>
          {!!runs.data?.total && (
            <div className="mt-3 flex items-center justify-between gap-2">
              <Button size="sm" disabled={!page} onClick={() => setPage((value) => value - 1)}>
                上一页实验
              </Button>
              <span className="text-xs text-muted-foreground">
                {page + 1} / {Math.ceil(runs.data.total / 20)}
              </span>
              <Button
                size="sm"
                disabled={(page + 1) * 20 >= runs.data.total}
                onClick={() => setPage((value) => value + 1)}
              >
                下一页实验
              </Button>
            </div>
          )}
        </aside>
        {current ? (
          <TrainingRunMonitor
            key={current.id}
            run={current}
            attempts={detail.data?.attempts ?? []}
            events={events.data ?? []}
            selectedAttemptId={selectedAttemptId}
            onAttemptChange={setAttemptId}
            eventsPending={events.isLoading}
            eventsError={events.error?.message}
            pending={action.isPending}
            onAction={(value) => action.mutate(value)}
          />
        ) : (
          <div className="flex min-h-80 flex-col items-center justify-center rounded-xl border border-dashed border-border p-8 text-center">
            <FlaskConical className="mb-4 size-8 text-muted-foreground" />
            <h3 className="text-sm font-medium">
              {detail.isLoading
                ? "正在加载实验详情…"
                : runs.data?.items.length
                  ? "选择一个训练实验"
                  : "创建你的第一个训练实验"}
            </h3>
            <p className="mt-2 max-w-sm text-xs leading-relaxed text-muted-foreground">
              使用已就绪的数据版本创建实验，训练开始后将在这里显示进度和验证表现。
            </p>
            {!ready.length && (
              <p className="mt-3 text-xs text-status-caution">
                请先在「训练数据」中创建并构建数据版本。
              </p>
            )}
          </div>
        )}
      </div>
      <Modal
        open={createOpen}
        onClose={() => {
          if (!busy) {
            setCreateOpen(false);
            onCreateClose?.();
          }
        }}
        title="新建训练实验"
        width={760}
      >
        <p className="mb-4 text-xs leading-relaxed text-muted-foreground">
          选择冻结数据与训练仓库，检查配置后加入训练队列。
        </p>
        {error && (
          <p
            role="alert"
            className="mb-4 rounded-md bg-status-danger-soft p-3 text-sm text-status-danger"
          >
            {error}
          </p>
        )}
        {!ready.length && (
          <p className="mb-4 rounded-md bg-status-caution-soft p-3 text-xs text-status-caution">
            暂无就绪数据版本，请先在「训练数据」中构建训练包。
          </p>
        )}
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault();
            if (report && valid) create.mutate();
          }}
        >
          <fieldset disabled={busy} className="grid gap-4 sm:grid-cols-2">
            <label className="text-xs text-muted-foreground">
              实验名称
              <input
                aria-label="实验名称"
                className={`${inputClass} mt-1`}
                value={name}
                maxLength={120}
                onChange={(event) => update(() => setName(event.target.value))}
              />
            </label>
            <label className="text-xs text-muted-foreground">
              训练数据
              <select
                aria-label="训练数据"
                className={`${inputClass} mt-1`}
                value={datasetId}
                onChange={(event) => update(() => setDatasetId(event.target.value))}
              >
                <option value="">选择已就绪版本</option>
                {ready.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name}
                  </option>
                ))}
              </select>
              {hasMoreDatasets && (
                <Button
                  type="button"
                  size="sm"
                  className="mt-2"
                  disabled={loadingMoreDatasets}
                  onClick={onLoadMoreDatasets}
                >
                  {loadingMoreDatasets ? "加载中…" : "加载更早的数据版本"}
                </Button>
              )}
            </label>
            <label className="text-xs text-muted-foreground">
              训练器
              <select
                aria-label="训练器"
                className={`${inputClass} mt-1`}
                value={framework}
                onChange={(event) =>
                  update(() => {
                    setFramework(event.target.value);
                    setBaseModel("");
                  })
                }
              >
                {catalog.data?.items.map((item) => (
                  <option key={item.framework} value={item.framework}>
                    {item.framework} {item.version}
                  </option>
                ))}
              </select>
            </label>
            <label className="text-xs text-muted-foreground">
              基础权重
              <select
                aria-label="基础权重"
                className={`${inputClass} mt-1`}
                value={modelId}
                onChange={(event) => update(() => setBaseModel(event.target.value))}
              >
                {template?.base_models.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.id}
                  </option>
                ))}
              </select>
            </label>
            {(
              [
                ["epochs", "训练轮数", 1, 1000, 1],
                ["batch", "批大小", 1, 64, 1],
                ["imgsz", "图片尺寸", 128, 1280, 32],
              ] as const
            ).map(([key, label, min, max, step]) => (
              <label key={key} className="text-xs text-muted-foreground">
                {label}
                <input
                  aria-label={label}
                  type="number"
                  min={min}
                  max={max}
                  step={step}
                  className={`${inputClass} mt-1`}
                  value={parameters[key]}
                  onChange={(event) =>
                    update(() =>
                      setParameters((previous) => ({ ...previous, [key]: event.target.value })),
                    )
                  }
                />
              </label>
            ))}
          </fieldset>
          <details className="rounded-lg border border-border p-3 text-xs">
            <summary className="cursor-pointer font-medium">
              高级参数 · 学习率、随机种子与早停
            </summary>
            <fieldset disabled={busy} className="mt-3 grid gap-3 sm:grid-cols-3">
              {(
                [
                  ["lr0", "初始学习率", 0.000001, 0.1, "any"],
                  ["seed", "训练随机种子", 0, 2147483647, 1],
                  ["patience", "早停耐心轮数", 0, 1000, 1],
                ] as const
              ).map(([key, label, min, max, step]) => (
                <label key={key} className="text-muted-foreground">
                  {label}
                  <input
                    aria-label={label}
                    type="number"
                    min={min}
                    max={max}
                    step={step}
                    className={`${inputClass} mt-1`}
                    value={parameters[key]}
                    onChange={(event) =>
                      update(() =>
                        setParameters((previous) => ({ ...previous, [key]: event.target.value })),
                      )
                    }
                  />
                </label>
              ))}
            </fieldset>
          </details>
          <details
            className="rounded-lg border border-border p-3 text-xs"
            onToggle={(event) => setInventoryOpen(event.currentTarget.open)}
          >
            <summary className="cursor-pointer font-medium">完整参数目录 · 分组与支持边界</summary>
            {inventoryOpen && (
              <div className="mt-3">
                <TrainingParameterInventory
                  projectId={projectId}
                  framework={framework}
                  baseModel={modelId}
                />
              </div>
            )}
          </details>
          <p className="mt-3 text-xs text-muted-foreground">
            使用固定数据与验证集。显存不足时会报告失败；取消后可从已保存的完整轮次续训。
          </p>
          {template?.capabilities && !template.capabilities.model_registration && (
            <p className="mt-2 text-xs text-muted-foreground">
              此训练器提供原生权重、指标和续训检查点下载；当前尚未开放模型回流。指标按该框架的含义展示。
            </p>
          )}
          {report && (
            <p role="status" className="mt-3 text-xs text-status-positive">
              训练检查通过 · 数据、基础权重和参数已冻结
            </p>
          )}
          <div className="mt-3 flex flex-wrap gap-2">
            <Button
              type="button"
              size="sm"
              disabled={!valid || busy}
              onClick={() => {
                setError("");
                preflight.mutate();
              }}
            >
              {preflight.isPending ? "检查中…" : "检查训练配置"}
            </Button>
            <Button type="submit" size="sm" disabled={!valid || !report || busy}>
              {create.isPending ? "提交中…" : "开始训练"}
            </Button>
          </div>
        </form>
      </Modal>
    </section>
  );
}

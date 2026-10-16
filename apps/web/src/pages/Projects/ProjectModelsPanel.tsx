import { useMemo, useRef, useState } from "react";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/api/client";
import { asyncJobsApi } from "@/api/asyncJobs";
import {
  dataManagerTaskActionsApi,
  type DataManagerTaskPreannotationPayload,
} from "@/api/dataManagerActions";
import { mlBackendsApi } from "@/api/ml-backends";
import { tasksApi } from "@/api/tasks";
import { trainingRunsApi } from "@/api/trainingRuns";
import {
  comparisonIdentity,
  modelMetrics,
  record,
  trainedModelsApi,
  type ModelReleaseCreate,
  type ModelReleaseOut,
  type TrainedModelOut,
} from "@/api/trainedModels";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { ProgressBar } from "@/components/ui/ProgressBar";
import { useTrainingSessionRevision, useTrainingViewOwner } from "../Training/trainingViewOwner";
import { confirmDialog } from "@/components/ui/decisionDialog";

const inputClass =
  "w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground focus-visible:ring-2 focus-visible:ring-ring";
const metricNames: Record<string, string> = {
  "metrics/mAP50-95": "mAP50-95",
  "metrics/mAP50": "mAP50",
  "metrics/mAP75": "mAP75",
  "metrics/AR100": "AR100",
  "metrics/mAP50-95(B)": "mAP50-95",
  "metrics/mAP50(B)": "mAP50",
  "metrics/precision(B)": "Precision",
  "metrics/recall(B)": "Recall",
};
const states: Record<string, string> = {
  checking: "检查中",
  ready: "加载与试用通过",
  failed: "检查未通过",
  pending: "排队中",
  running: "预标注中",
  completed: "预标注完成",
  cancelled: "已取消",
};
const numeric = (value: unknown) =>
  typeof value === "number" && Number.isFinite(value) ? value.toFixed(4) : "—";

function Evaluation({ model }: { model: TrainedModelOut }) {
  const metrics = modelMetrics(model);
  const perClass = Array.isArray(model.evaluation.per_class)
    ? model.evaluation.per_class.map(record)
    : [];
  return (
    <div className="mt-3 space-y-3 text-xs">
      <p className="text-muted-foreground">
        评估使用固定验证集，未包含独立测试集；分数不代表实际项目已获得改善。
      </p>
      <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {Object.entries(metrics).map(([key, value]) => (
          <div key={key} className="rounded-md bg-background p-2">
            <dt className="text-muted-foreground">{metricNames[key] ?? key}</dt>
            <dd className="mt-1 font-mono">{numeric(value)}</dd>
          </div>
        ))}
      </dl>
      {model.evaluation.per_class_available === false && (
        <p className="text-muted-foreground">
          原生报告未提供分类别指标；缺失项保持不可用，不作零值展示。
        </p>
      )}
      {perClass.length > 0 && (
        <details>
          <summary className="cursor-pointer">分类别结果（仅列出验证集中出现的类别）</summary>
          <div className="mt-2 overflow-x-auto">
            <table className="w-full text-left">
              <thead>
                <tr>
                  <th className="p-2">类别</th>
                  <th className="p-2">Precision</th>
                  <th className="p-2">Recall</th>
                  <th className="p-2">mAP50</th>
                  <th className="p-2">mAP50-95</th>
                </tr>
              </thead>
              <tbody>
                {perClass.map((item, index) => (
                  <tr key={index} className="border-t border-border">
                    <td className="p-2">{String(item.name ?? item.index)}</td>
                    {["precision", "recall", "mAP50", "mAP50-95"].map((key) => (
                      <td key={key} className="p-2 font-mono">
                        {numeric(item[key])}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}
      <details>
        <summary className="cursor-pointer">来源与评估范围</summary>
        <dl className="mt-2 space-y-1 break-all text-muted-foreground">
          <div>实验：{model.run_id}</div>
          <div>执行尝试：{model.attempt_id}</div>
          <div>数据版本：{String(model.provenance.dataset_id ?? "—")}</div>
          <div>验证基准：{String(model.provenance.validation_digest ?? "—")}</div>
          <div>权重 SHA-256：{String(model.artifact.sha256 ?? "—")}</div>
          <div>
            类别绑定：
            {model.classes.map((name, i) => `${i}: ${name} → ${model.bindings[i]}`).join("；")}
          </div>
          <div>评估配置：{JSON.stringify(model.evaluation.evaluation ?? {})}</div>
        </dl>
      </details>
    </div>
  );
}

function TrialImage({ trial }: { trial: Record<string, unknown> }) {
  const boxes = Array.isArray(trial.result) ? trial.result.map(record) : [];
  return (
    <div className="mt-3">
      {typeof trial.sample_url === "string" && (
        <div className="relative max-w-lg overflow-hidden rounded-md border border-border bg-background">
          <img
            src={trial.sample_url}
            alt="固定验证样本与候选模型检测框"
            className="block h-auto w-full"
          />
          <svg
            viewBox="0 0 100 100"
            preserveAspectRatio="none"
            className="pointer-events-none absolute inset-0 h-full w-full"
            aria-hidden="true"
          >
            {boxes.map((box, index) => {
              const value = record(box.value);
              return (
                <rect
                  key={index}
                  x={Number(value.x)}
                  y={Number(value.y)}
                  width={Number(value.width)}
                  height={Number(value.height)}
                  className="fill-transparent stroke-primary"
                  strokeWidth="2"
                  vectorEffect="non-scaling-stroke"
                />
              );
            })}
          </svg>
        </div>
      )}
      <p className="mt-2 text-xs text-muted-foreground">
        固定验证样本检出 {boxes.length} 个框 · 推理 {String(trial.inference_time_ms ?? "—")}{" "}
        ms。单张样本耗时受加载状态影响。
      </p>
      {boxes.length > 0 && (
        <p className="mt-1 text-xs">
          {boxes
            .map((box) => {
              const labels = record(box.value).rectanglelabels;
              return Array.isArray(labels) ? labels.join("、") : "";
            })
            .join("、")}
        </p>
      )}
    </div>
  );
}

export function ProjectModelsPanel(props: { projectId: string; classes: string[] }) {
  const revision = useTrainingSessionRevision();
  const scope = useMemo(
    () => ({ key: crypto.randomUUID(), projectId: props.projectId, revision }),
    [props.projectId, revision],
  );
  return <ModelsContent key={scope.key} {...props} scope={scope.key} />;
}

function ModelsContent({
  projectId,
  classes,
  scope,
}: {
  projectId: string;
  classes: string[];
  scope: string;
}) {
  const client = useQueryClient();
  const location = useLocation();
  const session = useTrainingViewOwner(projectId, scope);
  const viewIdentity = useMemo(
    () => ({ key: crypto.randomUUID(), location: location.key }),
    [location.key],
  );
  const viewKey = viewIdentity.key;
  const currentView = useRef(viewKey);
  currentView.current = viewKey;
  type View = { owner: typeof session.owner; key: string };
  const captureView = (): View => ({ owner: session.owner, key: currentView.current });
  const ownsView = (view: View) =>
    session.owns() && view.owner === session.owner && view.key === currentView.current;
  const assertView = (view: View) => {
    session.assert();
    if (!ownsView(view)) throw new DOMException("查看位置已变化", "AbortError");
  };
  const read = async <T,>(signal: AbortSignal, fn: () => Promise<T>): Promise<T> => {
    session.assert(signal);
    const result = await fn();
    session.assert(signal);
    return result;
  };
  const [params, setParams] = useSearchParams();
  const archive =
    params.get("archive") === "only"
      ? "only"
      : params.get("archive") === "include"
        ? "include"
        : "exclude";
  const page = Math.min(5000, Math.max(0, Math.floor(Number(params.get("model_page"))) || 0));
  const detailId = params.get("model") ?? "";
  const updateModelParams = (values: Record<string, string | undefined>) =>
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      next.set("tab", "models");
      Object.entries(values).forEach(([key, value]) =>
        value === undefined ? next.delete(key) : next.set(key, value),
      );
      return next;
    });
  const setDetailId = (id: string) => updateModelParams({ model: id || undefined });
  const setPage = (change: (page: number) => number) =>
    updateModelParams({ model_page: String(change(page)) });
  const lineageHref = (id: string) =>
    `/projects/${projectId}/learning/lineage/model/${id}?returnTo=${encodeURIComponent(location.pathname + location.search)}`;
  const [name, setName] = useState("");
  const [runId, setRunId] = useState("");
  const [attemptId, setAttemptId] = useState("");
  const [bindings, setBindings] = useState<Record<number, string>>({});
  const [sourcePage, setSourcePage] = useState(0);
  const [historyPage, setHistoryPage] = useState(0);
  const [registryId, setRegistryId] = useState("");
  const [confirmed, setConfirmed] = useState<Record<string, boolean>>({});
  const [compareId, setCompareId] = useState("");
  const [error, setError] = useState("");
  const [taskPage, setTaskPage] = useState(0);
  const [selectedTasks, setSelectedTasks] = useState<string[]>([]);
  const [predictMode, setPredictMode] = useState<"skip_predicted" | "overwrite">("skip_predicted");
  const [confidence, setConfidence] = useState("0.25");
  const [jobId, setJobId] = useState("");
  const registrationId = useRef(crypto.randomUUID());
  const publishRequest = useRef<{ intent: string; body: ModelReleaseCreate } | null>(null);
  const predictRequest = useRef<{ id: string; payload?: DataManagerTaskPreannotationPayload }>({
    id: crypto.randomUUID(),
  });
  const runs = useQuery({
    queryKey: ["training-runs", projectId, scope, sourcePage],
    queryFn: ({ signal }) => read(signal, () => trainingRunsApi.list(projectId, sourcePage * 20)),
    refetchInterval: 5000,
  });
  const source = useQuery({
    queryKey: ["training-run", projectId, scope, runId],
    queryFn: ({ signal }) => read(signal, () => trainingRunsApi.get(projectId, runId)),
    enabled: !!runId,
  });
  const models = useQuery({
    queryKey: ["trained-models", projectId, scope, archive, page],
    queryFn: ({ signal }) =>
      read(signal, () => trainedModelsApi.list(projectId, page * 20, undefined, archive)),
    refetchInterval: 5000,
  });
  const detail = useQuery({
    queryKey: ["trained-model", projectId, scope, detailId],
    queryFn: ({ signal }) => read(signal, () => trainedModelsApi.get(projectId, detailId)),
    enabled: !!detailId,
    refetchInterval: 5000,
  });
  const backends = useQuery({
    queryKey: ["learning-backends", projectId, scope],
    queryFn: ({ signal }) => read(signal, () => mlBackendsApi.listAvailable(projectId)),
    refetchInterval: 10000,
  });
  const releases = useQuery({
    queryKey: ["model-releases", projectId, scope, historyPage],
    queryFn: ({ signal }) =>
      read(signal, () => trainedModelsApi.releases(projectId, historyPage * 20)),
    refetchInterval: 5000,
  });
  const pendingTasks = useQuery({
    queryKey: ["learning-pending-tasks", projectId, scope, taskPage],
    queryFn: ({ signal }) =>
      read(signal, () =>
        tasksApi.listByProject(projectId, { status: "pending", limit: 25, offset: taskPage * 25 }),
      ),
  });
  const job = useQuery({
    queryKey: ["learning-prediction-job", projectId, scope, jobId],
    queryFn: ({ signal }) => read(signal, () => asyncJobsApi.get(jobId)),
    enabled: !!jobId,
    refetchInterval: (query) =>
      ["pending", "running"].includes(query.state.data?.status ?? "") ? 1500 : false,
  });
  const nativeClasses = useMemo(() => {
    const raw = source.data?.run.config.classes;
    return Array.isArray(raw) && raw.every((v) => typeof v === "string") ? (raw as string[]) : [];
  }, [source.data]);
  const selectedAttempt =
    attemptId || source.data?.attempts.find((item) => item.status === "succeeded")?.id || "";
  const current = releases.data?.current;
  const currentModel = useQuery({
    queryKey: ["trained-model", projectId, scope, current?.model_version_id],
    queryFn: ({ signal }) =>
      read(signal, () => trainedModelsApi.get(projectId, current!.model_version_id!)),
    enabled: !!current?.model_version_id,
  });
  const activeModel = detail.data?.model;
  const comparison = models.data?.items.find((item) => item.id === compareId);
  const comparable =
    activeModel &&
    comparison &&
    comparisonIdentity(activeModel) !== null &&
    comparisonIdentity(activeModel) === comparisonIdentity(comparison);
  const enabledBackends =
    backends.data?.items.filter((item) => item.enabled && item.backend.auth_method === "token") ??
    [];
  const refresh = () => {
    void client.invalidateQueries({ queryKey: ["trained-models", projectId] });
    void client.invalidateQueries({ queryKey: ["trained-model", projectId] });
    void client.invalidateQueries({ queryKey: ["model-releases", projectId] });
  };
  const changeRegistration = (change: () => void) => {
    change();
    registrationId.current = crypto.randomUUID();
    setError("");
  };
  const showDetail = (id: string) => {
    setDetailId(id);
    setConfirmed({});
    setRegistryId("");
    setCompareId("");
    setError("");
  };
  const register = useMutation({
    mutationFn: (view: View) => {
      assertView(view);
      return trainedModelsApi.register(projectId, {
        request_id: registrationId.current,
        name: name.trim(),
        run_id: runId,
        attempt_id: selectedAttempt,
        bindings: nativeClasses.map((_, index) => bindings[index] ?? ""),
      });
    },
    onSuccess: (model, view) => {
      if (!ownsView(view)) return;
      showDetail(model.id);
      setName("");
      registrationId.current = crypto.randomUUID();
      refresh();
    },
    onError: (reason: Error, view) => {
      if (ownsView(view)) setError(reason.message);
    },
  });
  const deploy = useMutation({
    mutationFn: (view: View) => {
      assertView(view);
      return trainedModelsApi.deploy(projectId, detailId, registryId);
    },
    onSuccess: (_, view) => {
      if (!ownsView(view)) return;
      setConfirmed({});
      refresh();
    },
    onError: (reason: Error, view) => {
      if (!ownsView(view)) return;
      setError(reason.message);
      refresh();
    },
  });
  const publish = useMutation({
    mutationFn: async (target: {
      modelId: string | null;
      deploymentId: string | null;
      rollbackFromId?: string;
      view: View;
    }) => {
      assertView(target.view);
      const allowed = await confirmDialog({
        title: target.rollbackFromId
          ? "回退项目模型"
          : target.modelId
            ? "发布项目模型"
            : "停用项目模型发布",
        description: "操作只影响之后新建的预标注任务。已排入的任务继续使用其固定模型。",
        confirmLabel: target.rollbackFromId ? "确认回退" : target.modelId ? "确认发布" : "确认停用",
      });
      if (!allowed || !ownsView(target.view)) return null;
      const intent = JSON.stringify([target.modelId, target.deploymentId, target.rollbackFromId]);
      if (publishRequest.current?.intent !== intent)
        publishRequest.current = {
          intent,
          body: {
            request_id: crypto.randomUUID(),
            expected_generation: releases.data?.generation ?? 0,
            model_version_id: target.modelId,
            deployment_id: target.deploymentId,
            rollback_from_id: target.rollbackFromId,
            sample_confirmed: !!target.modelId,
          },
        };
      return trainedModelsApi.publish(projectId, publishRequest.current.body);
    },
    onSuccess: (release, target) => {
      if (!ownsView(target.view)) return;
      if (release) {
        publishRequest.current = null;
        setError("");
        refresh();
      }
    },
    onError: (reason: Error, target) => {
      if (!ownsView(target.view)) return;
      setError(reason.message);
      if (reason instanceof ApiError && reason.status >= 400 && reason.status < 500)
        publishRequest.current = null;
      refresh();
    },
  });
  const download = useMutation({
    mutationFn: (view: View) => {
      assertView(view);
      return trainedModelsApi.download(projectId, detailId);
    },
    onSuccess: ({ url }, view) => {
      if (!ownsView(view)) return;
      const link = document.createElement("a");
      link.href = url;
      link.download = "best.pt";
      link.rel = "noopener";
      link.click();
    },
    onError: (reason: Error, view) => {
      if (ownsView(view)) setError(reason.message);
    },
  });
  const resetPrediction = () => {
    predictRequest.current = { id: crypto.randomUUID() };
    setError("");
  };
  const predict = useMutation({
    mutationFn: (view: View) => {
      assertView(view);
      if (!predictRequest.current.payload)
        predictRequest.current.payload = {
          ml_backend_id: String(current?.snapshot.registry_id),
          model_id: "project-release",
          task: "detect",
          model_variants: {},
          task_ids: [...selectedTasks].sort(),
          predict_mode: predictMode,
          params: { conf: Number(confidence) },
        };
      return dataManagerTaskActionsApi.preannotate(projectId, predictRequest.current.payload, {
        idempotencyKey: predictRequest.current.id,
      });
    },
    onSuccess: (value, view) => {
      if (!ownsView(view)) return;
      setJobId(value.job_id);
      predictRequest.current = { id: crypto.randomUUID() };
      setError("");
      void client.invalidateQueries({ queryKey: ["learning-pending-tasks", projectId] });
    },
    onError: (reason: Error, view) => {
      if (ownsView(view)) setError(reason.message);
    },
  });
  const validRegistration =
    source.data?.registration_eligibility?.qualified === true &&
    !!name.trim() &&
    !!runId &&
    !!selectedAttempt &&
    nativeClasses.length > 0 &&
    nativeClasses.every((_, index) => classes.includes(bindings[index]));
  const predictionRunning =
    predict.isPending ||
    (!!jobId && job.isPending) ||
    ["pending", "running"].includes(job.data?.status ?? "");
  const modelName = (release: ModelReleaseOut) =>
    (release.model_version_id === currentModel.data?.model.id
      ? currentModel.data.model.name
      : undefined) ??
    models.data?.items.find((item) => item.id === release.model_version_id)?.name ??
    (release.model_version_id ? `模型 ${release.model_version_id.slice(0, 8)}` : "未启用受控模型");
  return (
    <section aria-labelledby="models-title">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 id="models-title" className="text-sm font-semibold">
          模型版本与回流
        </h2>
        <div className="flex flex-wrap gap-2">
          <Link
            to={`/projects/${projectId}/learning/active-learning`}
            className="rounded-md border border-border px-3 py-2 text-xs font-medium text-primary hover:bg-muted"
          >
            主动学习人工选样
          </Link>
          <Link
            to={`/projects/${projectId}/learning/evaluations`}
            className="rounded-md border border-border px-3 py-2 text-xs font-medium text-primary hover:bg-muted"
          >
            逐样本评估与误差复查
          </Link>
        </div>
      </div>
      <p className="mt-2 text-xs text-muted-foreground">
        注册成功训练的权重，确认类别绑定，再加载试用与人工发布。
      </p>
      {error && (
        <p
          role="alert"
          className="mt-3 rounded-md bg-status-danger-soft p-3 text-sm text-status-danger"
        >
          {error}
        </p>
      )}
      {(models.error || releases.error || runs.error || backends.error) && (
        <p role="alert" className="mt-3 text-sm text-status-danger">
          {(models.error ?? releases.error ?? runs.error ?? backends.error)?.message}
        </p>
      )}
      <form
        className="mt-4 grid gap-4 rounded-md border border-border p-4 md:grid-cols-2"
        onSubmit={(event) => {
          event.preventDefault();
          setError("");
          register.mutate(captureView());
        }}
      >
        <label className="space-y-1 text-xs">
          模型名称
          <input
            className={inputClass}
            value={name}
            maxLength={120}
            disabled={register.isPending}
            onChange={(event) => changeRegistration(() => setName(event.target.value))}
          />
        </label>
        <label className="space-y-1 text-xs">
          来源训练实验
          <select
            className={inputClass}
            value={runId}
            disabled={register.isPending}
            onChange={(event) =>
              changeRegistration(() => {
                setRunId(event.target.value);
                setAttemptId("");
                setBindings({});
              })
            }
          >
            <option value="">选择成功实验</option>
            {runs.data?.items
              .filter((run) => run.status === "succeeded")
              .map((run) => (
                <option key={run.id} value={run.id}>
                  {run.name}
                </option>
              ))}
          </select>
        </label>
        <div className="flex items-center gap-2 text-xs md:col-span-2">
          <span className="text-muted-foreground">来源列表第 {sourcePage + 1} 页</span>
          <Button
            type="button"
            size="sm"
            disabled={!sourcePage || register.isPending}
            onClick={() =>
              changeRegistration(() => {
                setSourcePage((p) => p - 1);
                setRunId("");
                setBindings({});
              })
            }
          >
            上一页实验
          </Button>
          <Button
            type="button"
            size="sm"
            disabled={(sourcePage + 1) * 20 >= (runs.data?.total ?? 0) || register.isPending}
            onClick={() =>
              changeRegistration(() => {
                setSourcePage((p) => p + 1);
                setRunId("");
                setBindings({});
              })
            }
          >
            下一页实验
          </Button>
        </div>
        {runId && (
          <label className="space-y-1 text-xs">
            成功执行尝试
            <select
              className={inputClass}
              value={selectedAttempt}
              disabled={register.isPending}
              onChange={(event) => changeRegistration(() => setAttemptId(event.target.value))}
            >
              {source.data?.attempts
                .filter((attempt) => attempt.status === "succeeded")
                .map((attempt) => (
                  <option key={attempt.id} value={attempt.id}>
                    第 {attempt.ordinal} 次执行
                  </option>
                ))}
            </select>
          </label>
        )}
        {source.error && (
          <p role="alert" className="text-xs text-status-danger">
            {source.error.message}
          </p>
        )}
        {source.data && !source.data.registration_eligibility?.qualified && (
          <p className="text-sm text-muted-foreground md:col-span-2">
            {source.data.registration_eligibility?.reason ??
              "尚未取得模型回流资格，请重新加载；仍可在训练实验中下载原生权重。"}
          </p>
        )}
        {source.data?.registration_eligibility?.qualified === true &&
          nativeClasses.map((native, index) => (
            <label key={index} className="space-y-1 text-xs">
              原生类别 {index} · {native}
              <select
                className={inputClass}
                value={bindings[index] ?? ""}
                disabled={register.isPending}
                onChange={(event) =>
                  changeRegistration(() =>
                    setBindings((value) => ({ ...value, [index]: event.target.value })),
                  )
                }
              >
                <option value="">明确选择项目类别</option>
                {classes.map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </label>
          ))}
        <div className="md:col-span-2">
          <Button type="submit" size="sm" disabled={!validRegistration || register.isPending}>
            {register.isPending ? "校验与注册中…" : "注册候选模型"}
          </Button>
        </div>
      </form>
      <label className="mt-5 block max-w-xs space-y-1 text-xs">
        候选显示范围
        <select
          className={inputClass}
          value={archive}
          onChange={(event) =>
            updateModelParams({ archive: event.target.value, model_page: undefined })
          }
        >
          <option value="exclude">默认候选（保留当前发布）</option>
          <option value="include">全部候选，包含归档</option>
          <option value="only">仅已归档候选</option>
        </select>
      </label>
      <div className="mt-5 overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead className="text-muted-foreground">
            <tr>
              <th className="p-2">模型</th>
              <th className="p-2">训练器</th>
              <th className="p-2">操作</th>
            </tr>
          </thead>
          <tbody>
            {models.data?.items.map((model) => (
              <tr key={model.id} className="border-t border-border">
                <td className="p-2 font-medium">
                  {model.name}
                  {model.archived && (
                    <Badge>已归档{model.is_current_release ? " · 当前发布" : ""}</Badge>
                  )}
                </td>
                <td className="p-2">
                  {model.framework} {model.framework_version}
                </td>
                <td className="p-2">
                  <Button size="sm" variant="ghost" onClick={() => showDetail(model.id)}>
                    评估与部署
                  </Button>
                  <Link
                    className="ml-2 inline-block text-primary hover:underline"
                    to={lineageHref(model.id)}
                  >
                    谱系与决策
                  </Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {models.data?.items.length === 0 && (
        <p className="mt-3 text-xs text-muted-foreground">尚未注册模型版本。</p>
      )}
      <div className="mt-3 flex justify-end gap-2">
        <Button size="sm" disabled={!page} onClick={() => setPage((p) => p - 1)}>
          上一页模型
        </Button>
        <Button
          size="sm"
          disabled={(page + 1) * 20 >= (models.data?.total ?? 0)}
          onClick={() => setPage((p) => p + 1)}
        >
          下一页模型
        </Button>
      </div>
      {detail.error && (
        <p role="alert" className="mt-3 text-sm text-status-danger">
          {detail.error.message}
        </p>
      )}
      {activeModel && (
        <div className="mt-4 rounded-md bg-muted p-4" aria-label="模型版本详情">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-sm font-semibold">{activeModel.name}</h3>
            <div className="flex flex-wrap gap-2">
              <Link
                className="rounded-md border border-border px-3 py-2 text-xs text-primary"
                to={lineageHref(activeModel.id)}
              >
                谱系与决策
              </Link>
              <Button
                size="sm"
                disabled={download.isPending}
                onClick={() => download.mutate(captureView())}
              >
                下载模型
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setDetailId("")}>
                收起模型
              </Button>
            </div>
          </div>
          <Evaluation model={activeModel} />
          <label className="mt-4 block space-y-1 text-xs">
            对比另一候选
            <select
              className={inputClass}
              value={compareId}
              onChange={(event) => setCompareId(event.target.value)}
            >
              <option value="">选择模型</option>
              {models.data?.items
                .filter((item) => item.id !== activeModel.id)
                .map((model) => (
                  <option key={model.id} value={model.id}>
                    {model.name}
                  </option>
                ))}
            </select>
          </label>
          {comparison &&
            (comparable ? (
              <div className="mt-3 overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead>
                    <tr>
                      <th className="p-2">同一验证范围</th>
                      <th className="p-2">{activeModel.name}</th>
                      <th className="p-2">{comparison.name}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {[
                      ...new Set([
                        ...Object.keys(modelMetrics(activeModel)),
                        ...Object.keys(modelMetrics(comparison)),
                      ]),
                    ].map((key) => (
                      <tr key={key} className="border-t border-border">
                        <td className="p-2">{metricNames[key] ?? key}</td>
                        <td className="p-2 font-mono">{numeric(modelMetrics(activeModel)[key])}</td>
                        <td className="p-2 font-mono">{numeric(modelMetrics(comparison)[key])}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="mt-3 text-xs text-muted-foreground">
                验证基准、类别绑定或评估配置不同，不计算新旧提升。请分别查看各自报告。
              </p>
            ))}
          <div className="mt-4 flex flex-wrap items-end gap-2">
            <label className="min-w-0 flex-1 space-y-1 text-xs">
              已启用的认证推理实例
              <select
                className={inputClass}
                value={registryId}
                onChange={(event) => {
                  setRegistryId(event.target.value);
                  setError("");
                }}
              >
                <option value="">选择受控 YOLO 实例</option>
                {enabledBackends.map(({ backend }) => (
                  <option
                    key={backend.id}
                    value={backend.id}
                    disabled={backend.state !== "connected"}
                  >
                    {backend.name} · {backend.state === "connected" ? "在线" : "不可用"}
                  </option>
                ))}
              </select>
            </label>
            <Button
              size="sm"
              disabled={!registryId || deploy.isPending}
              onClick={() => deploy.mutate(captureView())}
            >
              {deploy.isPending ? "加载与试用中…" : "加载检查与样本试用"}
            </Button>
          </div>
          {enabledBackends.length === 0 && (
            <p className="mt-2 text-xs text-muted-foreground">
              请管理员注册支持受控权重的 YOLO 实例，配置认证并在项目中启用。
            </p>
          )}
          {detail.data?.deployments.map((deployment) => (
            <div key={deployment.id} className="mt-4 rounded-md border border-border p-3">
              <div className="flex flex-wrap items-center gap-2">
                <Badge
                  variant={
                    deployment.status === "ready"
                      ? "success"
                      : deployment.status === "failed"
                        ? "danger"
                        : "default"
                  }
                >
                  {states[deployment.status] ?? deployment.status}
                </Badge>
                <span className="text-xs">
                  {enabledBackends.find((item) => item.backend.id === deployment.registry_id)
                    ?.backend.name ?? deployment.registry_id.slice(0, 8)}
                </span>
              </div>
              {deployment.error_message && (
                <p role="alert" className="mt-2 text-xs text-status-danger">
                  {deployment.error_message}
                </p>
              )}
              {deployment.status === "ready" && (
                <>
                  <TrialImage trial={deployment.trial} />
                  <label className="mt-3 flex items-center gap-2 text-xs">
                    <input
                      type="checkbox"
                      checked={!!confirmed[deployment.id]}
                      onChange={(event) =>
                        setConfirmed((value) => ({
                          ...value,
                          [deployment.id]: event.target.checked,
                        }))
                      }
                    />
                    已查看固定验证样本，确认用于后续人工试用
                  </label>
                  <Button
                    size="sm"
                    className="mt-3"
                    disabled={!confirmed[deployment.id] || publish.isPending || !releases.data}
                    onClick={() =>
                      publish.mutate({
                        view: captureView(),
                        modelId: activeModel.id,
                        deploymentId: deployment.id,
                      })
                    }
                  >
                    发布此模型
                  </Button>
                </>
              )}
            </div>
          ))}
        </div>
      )}
      <div className="mt-6 rounded-md border border-border p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3 className="text-sm font-semibold">当前项目发布</h3>
          {current?.model_version_id && (
            <Button
              size="sm"
              disabled={publish.isPending}
              onClick={() =>
                publish.mutate({ view: captureView(), modelId: null, deploymentId: null })
              }
            >
              停用受控模型
            </Button>
          )}
        </div>
        <p className="mt-2 text-xs">
          {current ? modelName(current) : "尚未发布受控模型"}
          {current && ` · 发布代次 ${current.generation}`}
        </p>
        {current?.model_version_id && (
          <>
            <p className="mt-3 text-xs text-muted-foreground">
              选择待标图片，用当前发布模型生成候选，再到工作台修正与审核。
            </p>
            <div className="mt-3 max-h-52 space-y-2 overflow-y-auto">
              {pendingTasks.data?.items.map((task) => (
                <label key={task.id} className="flex items-center gap-2 text-xs">
                  <input
                    type="checkbox"
                    checked={selectedTasks.includes(task.id)}
                    disabled={
                      predictionRunning ||
                      (selectedTasks.length >= 200 && !selectedTasks.includes(task.id))
                    }
                    onChange={(event) => {
                      resetPrediction();
                      setSelectedTasks((value) =>
                        event.target.checked
                          ? [...value, task.id]
                          : value.filter((id) => id !== task.id),
                      );
                    }}
                  />
                  <span className="min-w-0 break-all">
                    {task.display_id} · {task.file_name}
                  </span>
                </label>
              ))}
            </div>
            {pendingTasks.data?.items.length === 0 && (
              <p className="mt-2 text-xs text-muted-foreground">暂无待标图片。</p>
            )}
            {pendingTasks.error && (
              <p role="alert" className="mt-2 text-xs text-status-danger">
                {pendingTasks.error.message}
              </p>
            )}
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <Button size="sm" disabled={!taskPage} onClick={() => setTaskPage((p) => p - 1)}>
                上一页任务
              </Button>
              <Button
                size="sm"
                disabled={(taskPage + 1) * 25 >= (pendingTasks.data?.total ?? 0)}
                onClick={() => setTaskPage((p) => p + 1)}
              >
                下一页任务
              </Button>
              <span className="text-xs text-muted-foreground">
                已选 {selectedTasks.length} / 200
              </span>
            </div>
            <div className="mt-3 grid gap-3 sm:grid-cols-2">
              <label className="space-y-1 text-xs">
                已有预测
                <select
                  className={inputClass}
                  value={predictMode}
                  disabled={predictionRunning}
                  onChange={(event) => {
                    resetPrediction();
                    setPredictMode(event.target.value as typeof predictMode);
                  }}
                >
                  <option value="skip_predicted">跳过已有预测</option>
                  <option value="overwrite">覆盖旧预测，保留人工标注</option>
                </select>
              </label>
              <label className="space-y-1 text-xs">
                置信度阈值
                <input
                  className={inputClass}
                  type="number"
                  min="0"
                  max="1"
                  step="0.01"
                  value={confidence}
                  disabled={predictionRunning}
                  onChange={(event) => {
                    resetPrediction();
                    setConfidence(event.target.value);
                  }}
                />
              </label>
            </div>
            <Button
              size="sm"
              className="mt-3"
              disabled={
                !selectedTasks.length ||
                predictionRunning ||
                confidence.trim() === "" ||
                !Number.isFinite(Number(confidence)) ||
                Number(confidence) < 0 ||
                Number(confidence) > 1
              }
              onClick={() => predict.mutate(captureView())}
            >
              {predict.isPending ? "正在排入…" : "用当前发布模型预标注"}
            </Button>
          </>
        )}
        {job.data && (
          <div className="mt-3 text-xs" role="status">
            <p>
              {states[job.data.status] ?? job.data.status} · {job.data.progress_pct}%
            </p>
            <div className="mt-2">
              <ProgressBar value={job.data.progress_pct} />
            </div>
            {job.data.error_message && (
              <p className="mt-2 text-status-danger">{job.data.error_message}</p>
            )}
            <Link
              className="mt-2 inline-block text-primary hover:underline"
              to={`/projects/${projectId}/annotate`}
            >
              前往工作台修正与确认
            </Link>
          </div>
        )}
        {job.error && (
          <p role="alert" className="mt-2 text-xs text-status-danger">
            {job.error.message}
          </p>
        )}
      </div>
      <details className="mt-5 text-xs">
        <summary className="cursor-pointer">发布历史与回退</summary>
        <div className="mt-3 space-y-2">
          {releases.data?.items.map((release) => (
            <div
              key={release.id}
              className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border p-3"
            >
              <span>
                第 {release.generation} 次 · {modelName(release)}
                {release.rollback_from_id ? " · 回退" : ""}
                {release.id === releases.data.current_release_id ? " · 当前" : ""}
              </span>
              {release.id !== releases.data.current_release_id &&
                release.model_version_id &&
                release.deployment_id && (
                  <Button
                    size="sm"
                    disabled={publish.isPending}
                    onClick={() =>
                      publish.mutate({
                        view: captureView(),
                        modelId: release.model_version_id,
                        deploymentId: release.deployment_id,
                        rollbackFromId: release.id,
                      })
                    }
                  >
                    回退到此发布
                  </Button>
                )}
            </div>
          ))}
        </div>
        <div className="mt-3 flex justify-end gap-2">
          <Button size="sm" disabled={!historyPage} onClick={() => setHistoryPage((p) => p - 1)}>
            上一页发布
          </Button>
          <Button
            size="sm"
            disabled={(historyPage + 1) * 20 >= (releases.data?.total ?? 0)}
            onClick={() => setHistoryPage((p) => p + 1)}
          >
            下一页发布
          </Button>
        </div>
      </details>
    </section>
  );
}

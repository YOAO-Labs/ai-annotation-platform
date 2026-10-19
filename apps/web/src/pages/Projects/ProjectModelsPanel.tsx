import { randomId } from "@/utils/id";
import { CopyableError } from "@/components/ui/CopyableError";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/api/client";
import { asyncJobsApi } from "@/api/asyncJobs";
import {
  dataManagerTaskActionsApi,
  type DataManagerTaskPreannotationPayload,
} from "@/api/dataManagerActions";
import { mlBackendsApi, type ProjectMLBackendItem } from "@/api/ml-backends";
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
import { Plus, RadioTower } from "lucide-react";
import { TrainingError, TrainingLoading } from "../Training/TrainingFeedback";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Button as NavigationButton } from "@/components/shadcn/ui/button";
import { ProjectLearningSectionHeader } from "./ProjectLearningSectionHeader";
import { CopyValueButton } from "@/components/ui/CopyValueButton";
import { ProgressBar } from "@/components/ui/ProgressBar";
import { useTrainingSessionRevision, useTrainingViewOwner } from "../Training/trainingViewOwner";
import { confirmDialog } from "@/components/ui/decisionDialog";
import { cn } from "@/lib/utils";
import { newTrainingVersionName } from "../Training/trainingNames";
import { ModelClassBindings } from "./ModelClassBindings";
import { Modal } from "@/components/ui/Modal";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import { usePermissions } from "@/hooks/usePermissions";
import { ProjectModelList } from "./ProjectModelList";
import { trainingRecordPath, useTrainingListFilters } from "../Training/TrainingListControls";

const inputClass =
  "w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
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
          <summary className="cursor-pointer rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
            分类别结果（仅列出验证集中出现的类别）
          </summary>
          <div className="mt-2 overflow-x-auto">
            <table className="w-full text-left">
              <thead>
                <tr>
                  <th className="px-4 py-3">类别</th>
                  <th className="px-4 py-3">Precision</th>
                  <th className="px-4 py-3">Recall</th>
                  <th className="px-4 py-3">mAP50</th>
                  <th className="px-4 py-3">mAP50-95</th>
                </tr>
              </thead>
              <tbody>
                {perClass.map((item, index) => (
                  <tr key={index} className="border-t border-border">
                    <td className="px-4 py-3">{String(item.name ?? item.index)}</td>
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
        <summary className="cursor-pointer rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
          来源与评估范围
        </summary>
        <dl className="mt-2 space-y-1 text-muted-foreground">
          <div className="flex items-center gap-1">
            实验：
            <CopyValueButton value={model.run_id} kind="id" label="复制实验 ID" />
          </div>
          <div className="flex items-center gap-1">
            执行尝试：
            <CopyValueButton value={model.attempt_id} kind="id" label="复制执行尝试 ID" />
          </div>
          <div className="flex items-center gap-1">
            数据版本：
            <CopyValueButton
              value={String(model.provenance.dataset_id ?? "")}
              kind="id"
              label="复制数据版本 ID"
            />
          </div>
          <div className="flex items-center gap-1">
            验证基准：
            <CopyValueButton
              value={String(model.provenance.validation_digest ?? "")}
              label="复制验证基准哈希"
            />
          </div>
          <div className="flex items-center gap-1">
            权重 SHA-256：
            <CopyValueButton value={String(model.artifact.sha256 ?? "")} label="复制权重 SHA-256" />
          </div>
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

type ModelsPanelProps = {
  projectId: string;
  classes: string[];
  mode?: "list" | "detail";
  initialModelId?: string;
};

function inferenceServiceIssues({ backend, enabled }: ProjectMLBackendItem) {
  return [
    ...(!enabled ? ["项目未启用"] : []),
    ...(!["token", "managed:yolo"].includes(backend.auth_method) ? ["未配置推理认证"] : []),
    ...(backend.state !== "connected" ? ["服务当前不可用"] : []),
  ];
}

export function ProjectModelsPanel(props: ModelsPanelProps) {
  const revision = useTrainingSessionRevision();
  const scope = useMemo(
    () => ({
      key: randomId(),
      projectId: props.projectId,
      mode: props.mode,
      modelId: props.initialModelId,
      revision,
    }),
    [props.projectId, props.mode, props.initialModelId, revision],
  );
  return <ModelsContent key={scope.key} {...props} scope={scope.key} />;
}

function ModelsContent({
  projectId,
  classes,
  scope,
  mode = "list",
  initialModelId = "",
}: ModelsPanelProps & { scope: string }) {
  const client = useQueryClient();
  const { role, canAccessPage } = usePermissions();
  const inferenceStatusId = useId();
  const location = useLocation();
  const navigate = useNavigate();
  const session = useTrainingViewOwner(projectId, scope);
  const viewIdentity = useMemo(() => ({ key: randomId(), location: location.key }), [location.key]);
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
  const { filters, page, setPage, update } = useTrainingListFilters("model");
  const q = useDebouncedValue(filters.q, 250, projectId);
  const listFilters = {
    q,
    framework: filters.framework,
    created_from: filters.created_from,
    created_to: filters.created_to,
  };
  const detailId = initialModelId;
  const [registrationOpen, setRegistrationOpen] = useState(false);
  const registrationButton = useRef<HTMLButtonElement>(null);
  const sourceSelect = useRef<HTMLSelectElement>(null);
  const updateModelParams = (values: Record<string, string | undefined>) =>
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      next.set("tab", "models");
      Object.entries(values).forEach(([key, value]) =>
        value === undefined ? next.delete(key) : next.set(key, value),
      );
      return next;
    });
  const lineageHref = (id: string) =>
    `/projects/${projectId}/learning/lineage/model/${id}?returnTo=${encodeURIComponent(location.pathname + location.search)}`;
  const [name, setName] = useState(newTrainingVersionName);
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
  const registrationId = useRef(randomId());
  const registrationRun = params.get("register_run") ?? "";
  const registrationAttempt = params.get("register_attempt") ?? "";
  useEffect(() => {
    if (mode !== "list" || !registrationRun) return;
    setRunId(registrationRun);
    setAttemptId(registrationAttempt);
    setBindings({});
    setError("");
    registrationId.current = randomId();
    setRegistrationOpen(true);
  }, [mode, registrationRun, registrationAttempt]);
  const publishRequest = useRef<{ intent: string; body: ModelReleaseCreate } | null>(null);
  const predictRequest = useRef<{ id: string; payload?: DataManagerTaskPreannotationPayload }>({
    id: randomId(),
  });
  const runs = useQuery({
    queryKey: ["training-runs", projectId, scope, sourcePage],
    queryFn: ({ signal }) => read(signal, () => trainingRunsApi.list(projectId, sourcePage * 20)),
    enabled: registrationOpen,
    refetchInterval: 5000,
  });
  const source = useQuery({
    queryKey: ["training-run", projectId, scope, runId],
    queryFn: ({ signal }) => read(signal, () => trainingRunsApi.get(projectId, runId)),
    enabled: registrationOpen && !!runId,
  });
  const models = useQuery({
    queryKey: ["trained-models", projectId, scope, archive, page, listFilters],
    queryFn: ({ signal }) =>
      read(signal, () =>
        trainedModelsApi.list(projectId, page * 20, { signal }, archive, listFilters),
      ),
    refetchInterval: 5000,
  });
  const detail = useQuery({
    queryKey: ["trained-model", projectId, scope, detailId],
    queryFn: ({ signal }) =>
      read(signal, () => trainedModelsApi.get(projectId, detailId, { signal })),
    enabled: !!detailId,
    refetchInterval: 5000,
  });
  const backends = useQuery({
    queryKey: ["learning-backends", projectId, scope],
    enabled: mode === "detail",
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
    enabled: mode === "list",
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
  const activeModel =
    detail.data?.model.id === detailId && detail.data.model.project_id === projectId
      ? detail.data.model
      : undefined;
  const comparison = models.data?.items.find((item) => item.id === compareId);
  const comparable =
    activeModel &&
    comparison &&
    comparisonIdentity(activeModel) !== null &&
    comparisonIdentity(activeModel) === comparisonIdentity(comparison);
  const enabledBackends =
    backends.data?.items.filter(
      (item) => item.enabled && ["token", "managed:yolo"].includes(item.backend.auth_method),
    ) ?? [];
  const readyBackends = enabledBackends.filter((item) => item.backend.state === "connected");
  const selectedRegistryId =
    registryId || (readyBackends.length === 1 ? readyBackends[0].backend.id : "");
  const selectedBackend = backends.data?.items.find(
    (item) => item.backend.id === selectedRegistryId,
  );
  const canDeploy =
    !backends.isPending &&
    !backends.isError &&
    !!selectedBackend &&
    inferenceServiceIssues(selectedBackend).length === 0;
  const inferenceStatus = backends.isPending
    ? "正在读取推理服务…"
    : backends.isError
      ? "推理服务列表读取失败，请重试。"
      : !backends.data?.items.length
        ? "尚未注册推理服务，请先添加支持加载训练权重的 YOLO 服务。"
        : !backends.data.items.some((item) => item.enabled)
          ? "本项目尚未启用推理服务，暂时无法加载此模型。"
          : !enabledBackends.length
            ? "已启用的推理服务尚未配置系统托管或 Token 认证，无法加载训练权重。"
            : !readyBackends.length
              ? "已启用且配置认证的推理服务当前均不可用，请恢复服务后重试。"
              : selectedRegistryId && !canDeploy
                ? "所选推理服务已不可用，请重新选择。"
                : !selectedRegistryId
                  ? "请选择一个在线且已启用的推理服务，再加载此模型。"
                  : "加载后会在固定验证图片上试用；确认结果并手动发布后，才会用于项目预标注。";
  const refresh = () => {
    void client.invalidateQueries({ queryKey: ["trained-models", projectId] });
    void client.invalidateQueries({ queryKey: ["trained-model", projectId] });
    void client.invalidateQueries({ queryKey: ["model-releases", projectId] });
  };
  const changeRegistration = (change: () => void) => {
    change();
    registrationId.current = randomId();
    setError("");
  };
  const showDetail = (id: string) => {
    navigate(trainingRecordPath(projectId, "models", id, params));
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
      setRegistrationOpen(false);
      showDetail(model.id);
      setName(newTrainingVersionName());
      registrationId.current = randomId();
      refresh();
    },
    onError: (reason: Error, view) => {
      if (ownsView(view)) setError(reason.message);
    },
  });
  const deploy = useMutation({
    mutationFn: (view: View) => {
      assertView(view);
      if (!canDeploy) throw new Error(inferenceStatus);
      return trainedModelsApi.deploy(projectId, detailId, selectedRegistryId);
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
            request_id: randomId(),
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
    predictRequest.current = { id: randomId() };
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
      predictRequest.current = { id: randomId() };
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
    source.data.attempts.some(
      (attempt) => attempt.id === selectedAttempt && attempt.status === "succeeded",
    ) &&
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
      {mode === "list" && (
        <ProjectLearningSectionHeader
          id="models-title"
          title="模型与发布"
          description="从成功的训练结果添加项目模型，绑定类别后加载试用，再人工发布用于预标注。"
          actions={
            <>
              <Button
                ref={registrationButton}
                variant="primary"
                size="sm"
                onClick={() => setRegistrationOpen(true)}
              >
                <Plus aria-hidden="true" />
                从训练结果添加模型
              </Button>
              <NavigationButton asChild variant="outline" size="sm">
                <Link to={`/projects/${projectId}/learning/active-learning`}>主动学习人工选样</Link>
              </NavigationButton>
              <NavigationButton asChild variant="outline" size="sm">
                <Link to={`/projects/${projectId}/learning/evaluations`}>逐样本评估与误差复查</Link>
              </NavigationButton>
            </>
          }
        />
      )}
      {mode === "detail" && (
        <h1 id="models-title" className="text-lg font-semibold">
          模型详情
        </h1>
      )}
      {error && !registrationOpen && (
        <p
          role="alert"
          className="mt-3 rounded-md bg-status-danger-soft p-3 text-sm text-status-danger"
        >
          <CopyableError message={error} />
        </p>
      )}
      {(releases.error || (mode === "detail" && models.error)) && (
        <p role="alert" className="mt-3 text-sm text-status-danger">
          <CopyableError
            message={(releases.error ?? (mode === "detail" ? models.error : null))?.message}
          />
        </p>
      )}
      {mode === "list" && (
        <section
          aria-label="当前项目发布"
          className="mt-5 rounded-xl border border-border bg-card p-4 sm:p-5"
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="flex items-center gap-2 text-sm font-semibold">
              <RadioTower aria-hidden="true" className="size-4 text-muted-foreground" />
              当前项目发布
            </h3>
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
            {releases.isLoading
              ? "正在读取发布状态…"
              : releases.error
                ? "发布状态暂时不可用"
                : current
                  ? modelName(current)
                  : "尚未发布受控模型"}
            {current?.model_version_id && (
              <CopyValueButton
                value={current.model_version_id}
                kind="id"
                label="复制当前受控模型版本 ID"
              />
            )}
            {current && ` · 发布代次 ${current.generation}`}
          </p>
        </section>
      )}
      {mode === "list" && (
        <ProjectModelList
          projectId={projectId}
          filters={filters}
          onChange={update}
          onReset={() => {
            updateModelParams({
              archive: undefined,
              model_page: undefined,
              ...Object.fromEntries(Object.keys(filters).map((key) => [`model_${key}`, undefined])),
            });
          }}
          archive={archive}
          onArchive={(value) => updateModelParams({ archive: value, model_page: undefined })}
          models={models.data}
          loading={models.isLoading}
          error={models.error?.message}
          onRetry={() => void models.refetch()}
          page={page}
          onPage={setPage}
          params={params}
          lineageHref={lineageHref}
          onRegister={() => setRegistrationOpen(true)}
        />
      )}
      {mode === "detail" && (
        <>
          {detail.isLoading && <TrainingLoading label="正在加载模型详情…" />}
          {detail.error && (
            <TrainingError message={detail.error.message} onRetry={() => void detail.refetch()} />
          )}
          {detail.data && !activeModel && (
            <TrainingError
              message="模型详情与当前项目或版本不匹配"
              onRetry={() => void detail.refetch()}
            />
          )}
          {activeModel && (
            <div
              className="mt-5 min-w-0 space-y-4 rounded-xl border border-border bg-card p-4 sm:p-5"
              role="region"
              aria-label="模型版本详情"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h2 className="text-sm font-semibold">{activeModel.name}</h2>
                <div className="flex flex-wrap gap-2">
                  <Link
                    className="rounded-md border border-border px-3 py-2 text-xs text-primary"
                    to={lineageHref(activeModel.id)}
                  >
                    来源与使用记录
                  </Link>
                  <Button
                    size="sm"
                    disabled={download.isPending}
                    onClick={() => download.mutate(captureView())}
                  >
                    下载模型
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
                          <th className="px-4 py-3">同一验证范围</th>
                          <th className="px-4 py-3">{activeModel.name}</th>
                          <th className="px-4 py-3">{comparison.name}</th>
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
                            <td className="px-4 py-3">{metricNames[key] ?? key}</td>
                            <td className="p-2 font-mono">
                              {numeric(modelMetrics(activeModel)[key])}
                            </td>
                            <td className="p-2 font-mono">
                              {numeric(modelMetrics(comparison)[key])}
                            </td>
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
                  用于加载模型的推理服务
                  <select
                    className={inputClass}
                    value={selectedRegistryId}
                    aria-describedby={inferenceStatusId}
                    disabled={backends.isPending || backends.isError || deploy.isPending}
                    onChange={(event) => {
                      setRegistryId(event.target.value);
                      setError("");
                    }}
                  >
                    <option value="">选择 YOLO 推理服务</option>
                    {backends.data?.items.map((item) => (
                      <option
                        key={item.backend.id}
                        value={item.backend.id}
                        disabled={inferenceServiceIssues(item).length > 0}
                      >
                        {item.backend.name} · {inferenceServiceIssues(item).join("、") || "在线"}
                      </option>
                    ))}
                  </select>
                </label>
                <Button
                  size="sm"
                  disabled={!canDeploy || deploy.isPending}
                  aria-describedby={inferenceStatusId}
                  onClick={() => deploy.mutate(captureView())}
                >
                  {deploy.isPending ? "加载与试用中…" : "加载模型并试用"}
                </Button>
              </div>
              <p
                id={inferenceStatusId}
                role="status"
                className={cn(
                  "mt-2 text-xs leading-relaxed",
                  canDeploy || backends.isPending ? "text-muted-foreground" : "text-status-warning",
                )}
              >
                {inferenceStatus}
              </p>
              {backends.error && (
                <TrainingError
                  message={backends.error.message}
                  onRetry={() => void backends.refetch()}
                />
              )}
              <details
                className="mt-3 text-xs leading-relaxed text-muted-foreground"
                open={!backends.isPending && !backends.isError && readyBackends.length === 0}
              >
                <summary className="cursor-pointer rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                  配置推理服务
                </summary>
                <p className="mt-2">
                  推理服务负责加载候选模型并生成预标注。只有本项目已启用、配置 Token
                  认证且在线的服务可用于加载。
                </p>
                {!!backends.data?.items.length && (
                  <ul aria-label="已注册推理服务状态" className="mt-2 space-y-2">
                    {backends.data.items.map((item) => (
                      <li
                        key={item.backend.id}
                        className="flex flex-wrap items-center gap-x-3 gap-y-1"
                      >
                        <span className="break-words">
                          {item.backend.name} ·{" "}
                          {inferenceServiceIssues(item).join("、") || "已就绪"}
                        </span>
                        {canAccessPage("model-market") && (
                          <Link
                            className="text-primary hover:underline"
                            to={`/model-market?${new URLSearchParams({
                              tab: "registry",
                              registry_view: "instances",
                              instance_id: item.backend.id,
                            })}`}
                          >
                            {role === "super_admin" ? "查看并配置" : "查看服务"}
                            <span className="sr-only"> {item.backend.name}</span>
                          </Link>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
                <ol className="mt-2 list-decimal space-y-2 pl-5">
                  <li>
                    超级管理员：进入「模型市场 → 注册管理 → 实例」，在现有 YOLO
                    服务的操作菜单点击「编辑」，本机第一方 YOLO 选择「系统托管」并保存，
                    平台会自动初始化并验证凭据。旧服务需先升级共享目录挂载；远程或已自行配置
                    YOLO_MANAGED_MODELS_TOKEN 的服务继续使用手动 Token（至少 32 个字符）。
                    已有实例无需重复注册。
                  </li>
                  <li>
                    项目负责人：进入「项目设置 → ML 模型」，点击「管理
                    backend」，勾选启用该服务，再返回此页选择。
                  </li>
                </ol>
                <div className="mt-3 flex flex-wrap gap-3">
                  {canAccessPage("model-market") && (
                    <Link
                      className="text-primary hover:underline"
                      to="/model-market?tab=registry&registry_view=instances"
                    >
                      {role === "super_admin" ? "去注册管理" : "查看已注册的推理服务"}
                    </Link>
                  )}
                  <Link
                    className="text-primary hover:underline"
                    to={`/projects/${projectId}/settings?section=ml-backends`}
                  >
                    去项目设置启用
                  </Link>
                </div>
              </details>
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
                    <span className="flex items-center gap-1 text-xs">
                      {enabledBackends.find((item) => item.backend.id === deployment.registry_id)
                        ?.backend.name ?? deployment.registry_id.slice(0, 8)}
                      <CopyValueButton
                        value={deployment.registry_id}
                        kind="id"
                        label="复制部署后端 ID"
                      />
                    </span>
                  </div>
                  {deployment.error_message && (
                    <p role="alert" className="mt-2 text-xs text-status-danger">
                      <CopyableError message={deployment.error_message} />
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
        </>
      )}
      {mode === "list" && (
        <Modal
          open={registrationOpen}
          onClose={() => {
            if (register.isPending) return;
            setRegistrationOpen(false);
            if (registrationRun || registrationAttempt)
              setParams(
                (previous) => {
                  const next = new URLSearchParams(previous);
                  next.delete("register_run");
                  next.delete("register_attempt");
                  return next;
                },
                { replace: true },
              );
          }}
          title="从训练结果添加模型"
          width={640}
          returnFocusRef={registrationButton}
          initialFocusRef={sourceSelect}
        >
          <form
            className="grid gap-4"
            onSubmit={(event) => {
              event.preventDefault();
              setError("");
              register.mutate(captureView());
            }}
          >
            <p className="text-xs leading-relaxed text-muted-foreground">
              直接使用平台内的训练权重，无需下载再上传。绑定项目类别后保存为候选模型，再加载试用和发布。关闭后可继续填写。
            </p>
            {error && (
              <p role="alert" className="text-xs text-status-danger">
                <CopyableError message={error} />
              </p>
            )}
            {runs.isLoading && (
              <p role="status" className="text-xs text-muted-foreground">
                正在加载来源实验…
              </p>
            )}
            {runs.error && (
              <p role="alert" className="text-xs text-status-danger">
                <CopyableError message={runs.error.message} />
              </p>
            )}
            <label className="space-y-1 text-xs">
              来源训练实验
              <select
                ref={sourceSelect}
                className={cn(
                  inputClass,
                  "[&>option]:text-foreground",
                  !runId && "text-muted-foreground",
                )}
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
                <option value="" disabled hidden>
                  选择成功实验
                </option>
                {source.data?.run.id === runId &&
                  !runs.data?.items.some(
                    (run) => run.id === runId && run.status === "succeeded",
                  ) && <option value={runId}>{source.data.run.name}</option>}
                {runs.data?.items
                  .filter((run) => run.status === "succeeded")
                  .map((run) => (
                    <option key={run.id} value={run.id}>
                      {run.name}
                    </option>
                  ))}
              </select>
            </label>
            {((runs.data?.total ?? 0) > 20 || sourcePage > 0) && (
              <div className="flex flex-wrap items-center gap-2 text-xs">
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
            )}
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
            {runId && (
              <label className="space-y-1 text-xs">
                成功执行尝试
                <select
                  className={inputClass}
                  value={selectedAttempt}
                  disabled={register.isPending}
                  onChange={(event) => changeRegistration(() => setAttemptId(event.target.value))}
                >
                  {attemptId &&
                    source.data &&
                    !source.data.attempts.some(
                      (attempt) => attempt.id === attemptId && attempt.status === "succeeded",
                    ) && (
                      <option value={attemptId} disabled hidden>
                        请选择成功的执行记录
                      </option>
                    )}
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
                <CopyableError message={source.error.message} />
              </p>
            )}
            {attemptId &&
              source.data &&
              !source.data.attempts.some(
                (attempt) => attempt.id === attemptId && attempt.status === "succeeded",
              ) && (
                <p role="alert" className="text-xs text-status-warning">
                  指定的执行记录不可用于回流，请选择成功的执行记录。
                </p>
              )}
            {source.data && !source.data.registration_eligibility?.qualified && (
              <p className="text-sm text-muted-foreground ">
                {source.data.registration_eligibility?.reason ??
                  "尚未取得模型回流资格，请重新加载；仍可在训练实验中下载原生权重。"}
              </p>
            )}
            {source.data?.registration_eligibility?.qualified === true && (
              <ModelClassBindings
                key={runId}
                nativeClasses={nativeClasses}
                projectClasses={classes}
                bindings={bindings}
                disabled={register.isPending}
                onChange={(next) => changeRegistration(() => setBindings(next))}
              />
            )}
            <div className="border-t border-border pt-4">
              <Button
                type="submit"
                size="sm"
                variant="primary"
                disabled={!validRegistration || register.isPending}
              >
                {register.isPending ? "校验与添加中…" : "从训练结果添加模型"}
              </Button>
            </div>
          </form>
        </Modal>
      )}
      {mode === "list" && (current?.model_version_id || jobId) && (
        <section
          aria-label="使用当前发布模型"
          className="mt-5 rounded-xl border border-border bg-card p-4 sm:p-5"
        >
          <h3 className="text-sm font-semibold">使用当前发布模型</h3>
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
                  <CopyableError message={pendingTasks.error.message} />
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
                <p className="mt-2 text-status-danger">
                  <CopyableError message={job.data.error_message} />
                </p>
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
              <CopyableError message={job.error.message} />
            </p>
          )}
        </section>
      )}
      {mode === "list" && (
        <details className="mt-5 rounded-xl border border-border bg-card p-4 text-xs">
          <summary className="cursor-pointer rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
            发布历史与回退
          </summary>
          <div className="mt-3 space-y-2">
            {releases.data?.items.map((release) => (
              <div
                key={release.id}
                className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border p-3"
              >
                <span className="inline-flex flex-wrap items-center gap-1">
                  第 {release.generation} 次 · {modelName(release)}
                  {release.model_version_id && (
                    <CopyValueButton
                      value={release.model_version_id}
                      kind="id"
                      label={`复制第 ${release.generation} 次发布的模型版本 ID`}
                    />
                  )}
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
      )}
    </section>
  );
}

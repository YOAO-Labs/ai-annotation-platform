import { ListPagination } from "@/components/ui/ListPagination";
import { listStyles } from "@/components/ui/listStyles";
import { useListPagination } from "@/hooks/useListPagination";
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
  type ModelTask,
  type TrainedModelOut,
} from "@/api/trainedModels";
import { Plus } from "lucide-react";
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
import { ProjectModelMaskTrial } from "./ProjectModelMaskTrial";
import { trainingRecordPath, useTrainingListFilters } from "../Training/TrainingListControls";
import { metricLabel, trainingTaskLabel, trainingTaskOf } from "../Training/trainingPresentation";

const inputClass =
  "w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
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
  const classPage = useListPagination(perClass, model.id);
  const detectionColumns = [
    { key: "precision", label: "Precision" },
    { key: "recall", label: "Recall" },
    { key: "mAP50", label: "mAP50" },
    { key: "mAP50-95", label: "mAP50-95" },
  ];
  const perClassColumns =
    model.task === "segment"
      ? [
          { suffix: "B", label: "框" },
          { suffix: "M", label: "掩码" },
        ].flatMap((kind) =>
          detectionColumns.map((column) => ({
            key: `${column.key}(${kind.suffix})`,
            label: `${kind.label} ${column.label}`,
          })),
        )
      : detectionColumns;
  return (
    <div className="mt-3 space-y-3 text-xs">
      <p className="text-muted-foreground">
        训练版本：{model.framework} {model.framework_version}
      </p>
      <p className="text-muted-foreground">
        评估使用固定验证集，未包含独立测试集；分数不代表实际项目已获得改善。
      </p>
      <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {Object.entries(metrics).map(([key, value]) => (
          <div key={key} className="rounded-md bg-background p-2">
            <dt className="text-muted-foreground">{metricLabel(key)}</dt>
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
            <table className={listStyles.table}>
              <thead>
                <tr>
                  <th className="px-4 py-3">类别</th>
                  {perClassColumns.map((column) => (
                    <th key={column.key} className="px-4 py-3">
                      {column.label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {classPage.items.map((item, index) => (
                  <tr key={index} className="border-t border-border">
                    <td className="px-4 py-3">{String(item.name ?? item.index)}</td>
                    {perClassColumns.map((column) => (
                      <td key={column.key} className="p-2 font-mono">
                        {numeric(item[column.key])}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <ListPagination
            label="模型类别"
            page={classPage.page}
            pageSize={classPage.pageSize}
            total={classPage.total}
            onPageChange={classPage.setPage}
            onPageSizeChange={classPage.setPageSize}
          />
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
  taskClasses?: Partial<Record<ModelTask, string[]>>;
  mode?: "list" | "detail";
  initialModelId?: string;
  publicationOnly?: boolean;
  onPublished?: () => void;
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
  const [params] = useSearchParams();
  const task =
    params.get("model_task") === "all"
      ? "all"
      : params.get("model_task") === "segment" ||
          (!params.has("model_task") && props.taskClasses && !props.taskClasses.detect)
        ? "segment"
        : "detect";
  const scope = useMemo(
    () => ({
      key: randomId(),
      projectId: props.projectId,
      mode: props.mode,
      modelId: props.initialModelId,
      revision,
      task,
    }),
    [props.projectId, props.mode, props.initialModelId, revision, task],
  );
  return <ModelsContent key={scope.key} {...props} scope={scope.key} selectedTask={task} />;
}

function ModelsContent({
  projectId,
  classes: fallbackClasses,
  taskClasses,
  selectedTask,
  scope,
  mode = "list",
  initialModelId = "",
  publicationOnly = false,
  onPublished,
}: ModelsPanelProps & { scope: string; selectedTask: ModelTask | "all" }) {
  const client = useQueryClient();
  const { role, canAccessPage } = usePermissions();
  const inferenceStatusId = useId();
  const modelsTitleId = useId();
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
  const { filters, page, pageSize, setPage, setPageSize, update } = useTrainingListFilters("model");
  const q = useDebouncedValue(filters.q, 250, projectId);
  const listFilters = {
    q,
    framework: filters.framework,
    created_from: filters.created_from,
    created_to: filters.created_to,
    task: selectedTask === "all" ? undefined : selectedTask,
  };
  const detailId = initialModelId;
  const [registrationOpen, setRegistrationOpen] = useState(false);
  const [publicationModelId, setPublicationModelId] = useState("");
  const publicationTrigger = useRef<HTMLButtonElement | null>(null);
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
  const [name, setName] = useState(newTrainingVersionName);
  const [runId, setRunId] = useState("");
  const [attemptId, setAttemptId] = useState("");
  const [bindings, setBindings] = useState<Record<number, string>>({});
  const [sourcePage, setSourcePage] = useState(0);
  const [historyPage, setHistoryPage] = useState(0);
  const [historyPageSize, setHistoryPageSize] = useState(20);
  const [registryId, setRegistryId] = useState("");
  const [confirmed, setConfirmed] = useState<Record<string, boolean>>({});
  const [trialReady, setTrialReady] = useState<Record<string, boolean>>({});
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
    queryFn: ({ signal }) =>
      read(signal, () =>
        trainingRunsApi.list(
          projectId,
          sourcePage * 20,
          { signal },
          {
            task: selectedTask === "all" ? undefined : selectedTask,
          },
        ),
      ),
    enabled: registrationOpen,
    refetchInterval: 5000,
  });
  const source = useQuery({
    queryKey: ["training-run", projectId, scope, runId],
    queryFn: ({ signal }) => read(signal, () => trainingRunsApi.get(projectId, runId)),
    enabled: registrationOpen && !!runId,
    refetchInterval: registrationOpen ? 5000 : false,
  });
  const detail = useQuery({
    queryKey: ["trained-model", projectId, scope, detailId],
    queryFn: ({ signal }) =>
      read(signal, () => trainedModelsApi.get(projectId, detailId, { signal })),
    enabled: !!detailId,
    refetchInterval: 5000,
  });
  const detailTask = detail.data?.model.task ?? "detect";
  const modelTask: ModelTask | undefined =
    mode === "detail"
      ? detail.data && (detailTask === "detect" || detailTask === "segment")
        ? detailTask
        : undefined
      : selectedTask === "all"
        ? undefined
        : selectedTask;
  const sourceTask = source.data?.run.task ?? trainingTaskOf(source.data?.run.config);
  const registrationTask = selectedTask === "all" ? sourceTask : selectedTask;
  const registrationMatchesTask =
    (registrationTask === "detect" || registrationTask === "segment") &&
    sourceTask === registrationTask;
  const classes =
    registrationTask === "detect" || registrationTask === "segment"
      ? (taskClasses?.[registrationTask] ?? (taskClasses ? [] : fallbackClasses))
      : [];
  const models = useQuery({
    queryKey: ["trained-models", projectId, scope, modelTask, archive, page, pageSize, listFilters],
    queryFn: ({ signal }) =>
      read(signal, () =>
        trainedModelsApi.list(
          projectId,
          page * pageSize,
          { signal },
          archive,
          {
            ...listFilters,
            task: modelTask,
          },
          pageSize,
        ),
      ),
    enabled: mode === "list" || (!!modelTask && !publicationOnly),
    refetchInterval: 5000,
  });
  const backends = useQuery({
    queryKey: ["learning-backends", projectId, scope],
    enabled: mode === "detail",
    queryFn: ({ signal }) => read(signal, () => mlBackendsApi.listAvailable(projectId)),
    refetchInterval: 10000,
  });
  const releases = useQuery({
    queryKey: ["model-releases", projectId, scope, modelTask, historyPage, historyPageSize],
    queryFn: ({ signal }) =>
      read(signal, () =>
        trainedModelsApi.releases(
          projectId,
          historyPage * historyPageSize,
          modelTask,
          historyPageSize,
        ),
      ),
    enabled: !!modelTask,
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
  const registeredSourceModel = source.data?.registered_models?.find(
    (model) => model.attempt_id === selectedAttempt,
  );
  const current = releases.data?.current;
  const isCurrentTarget = (modelId: string | null, deploymentId: string | null) =>
    !!current && current.model_version_id === modelId && current.deployment_id === deploymentId;
  const currentModel = useQuery({
    queryKey: ["trained-model", projectId, scope, current?.model_version_id],
    queryFn: ({ signal }) =>
      read(signal, () => trainedModelsApi.get(projectId, current!.model_version_id!)),
    enabled: !!current?.model_version_id,
  });
  const activeModel =
    detail.data?.model.id === detailId && detail.data.model.project_id === projectId && !!modelTask
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
  const refresh = () =>
    Promise.all([
      client.invalidateQueries({ queryKey: ["trained-models", projectId] }),
      client.invalidateQueries({ queryKey: ["trained-model", projectId] }),
      client.invalidateQueries({ queryKey: ["model-releases", projectId] }),
    ]);
  const changeRegistration = (change: () => void) => {
    change();
    registrationId.current = randomId();
    setError("");
  };
  const showDetail = (id: string) => {
    const next = new URLSearchParams(params);
    next.set("model_task", modelTask ?? selectedTask);
    navigate(trainingRecordPath(projectId, "models", id, next));
    setConfirmed({});
    setRegistryId("");
    setCompareId("");
    setError("");
  };
  const register = useMutation({
    mutationFn: (view: View) => {
      assertView(view);
      if (!validRegistration) throw new Error("来源实验或类别绑定不适用于当前任务");
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
      void client.invalidateQueries({ queryKey: ["training-run", projectId] });
      setRegistrationOpen(false);
      showDetail(model.id);
      setName(newTrainingVersionName());
      registrationId.current = randomId();
      refresh();
    },
    onError: (reason: Error, view) => {
      if (ownsView(view)) {
        setError(reason.message);
        void source.refetch();
      }
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
      if (!modelTask || !releases.data) throw new Error("请等待读取该类型的默认预标设置");
      const allowed = await confirmDialog({
        title: target.rollbackFromId
          ? "恢复默认预标模型"
          : target.modelId
            ? "设置默认预标模型"
            : "停用该类型的默认预标",
        description:
          "仅更改该类型的默认预标选择，其他类型及显式指定模型的预标流程不受影响。已排队任务继续使用固定模型。",
        confirmLabel: target.rollbackFromId ? "确认恢复" : target.modelId ? "设为默认" : "确认停用",
      });
      if (!allowed || !ownsView(target.view)) return null;
      const intent = JSON.stringify([
        modelTask,
        target.modelId,
        target.deploymentId,
        target.rollbackFromId,
      ]);
      if (publishRequest.current?.intent !== intent)
        publishRequest.current = {
          intent,
          body: {
            request_id: randomId(),
            task: modelTask,
            expected_generation: releases.data?.generation ?? 0,
            model_version_id: target.modelId,
            deployment_id: target.deploymentId,
            rollback_from_id: target.rollbackFromId,
            sample_confirmed: !!target.modelId,
          },
        };
      return trainedModelsApi.publish(projectId, publishRequest.current.body);
    },
    onSuccess: async (release, target) => {
      if (!ownsView(target.view)) return;
      if (release) {
        publishRequest.current = null;
        setError("");
        await refresh();
        if (ownsView(target.view)) onPublished?.();
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
          model_id: modelTask === "segment" ? "project-release:segment" : "project-release",
          task: modelTask,
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
    !registeredSourceModel &&
    registrationMatchesTask &&
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
    (release.model_version_id
      ? `模型 ${release.model_version_id.slice(0, 8)}`
      : "未设置默认预标模型");
  return (
    <section aria-labelledby={modelsTitleId}>
      {mode === "list" && (
        <ProjectLearningSectionHeader
          id={modelsTitleId}
          title="模型与发布"
          description="从训练结果添加项目模型，绑定类别并加载试用后，可设为对应类型的默认预标模型。"
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
        <h2 id={modelsTitleId} className="sr-only">
          模型详情
        </h2>
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
        <ProjectModelList
          projectId={projectId}
          filters={filters}
          onChange={update}
          onReset={() => {
            updateModelParams({
              archive: undefined,
              model_page: undefined,
              ...Object.fromEntries(Object.keys(filters).map((key) => [`model_${key}`, undefined])),
              model_task: "all",
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
          pageSize={pageSize}
          onPageSize={setPageSize}
          params={params}
          task={selectedTask}
          taskFilter={
            <select
              aria-label="模型与发布任务"
              className="min-w-0 rounded-md border border-border bg-background px-2.5 py-2 text-xs text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              value={selectedTask}
              onChange={(event) =>
                updateModelParams({
                  model_task: event.target.value,
                  model_page: undefined,
                  register_run: undefined,
                  register_attempt: undefined,
                })
              }
            >
              <option value="all">全部类型</option>
              <option value="detect" disabled={!!taskClasses && !taskClasses.detect}>
                目标检测
              </option>
              <option value="segment" disabled={!!taskClasses && !taskClasses.segment}>
                实例分割
              </option>
            </select>
          }
          defaultPreannotation={
            selectedTask === "all"
              ? undefined
              : {
                  modelId: current?.model_version_id,
                  loading: releases.isLoading,
                  pending: publish.isPending || !releases.data || !!releases.error,
                  onDisable: (modelId) => {
                    if (current?.model_version_id === modelId)
                      publish.mutate({ view: captureView(), modelId: null, deploymentId: null });
                  },
                }
          }
          onRegister={() => setRegistrationOpen(true)}
          onSetDefault={(modelId, trigger) => {
            publicationTrigger.current = trigger;
            setPublicationModelId(modelId);
          }}
        >
          {selectedTask === "all" && (
            <p className="border-t border-border p-4 text-xs text-muted-foreground">
              选择目标检测或实例分割，可使用对应默认模型预标注并查看切换记录。
            </p>
          )}
          {mode === "list" && (current?.model_version_id || jobId) && (
            <section aria-label="使用默认预标模型" className="border-t border-border p-4">
              <h3 className="text-sm font-semibold">使用默认预标模型</h3>
              {current && (
                <p className="mt-2 break-all text-xs text-muted-foreground">
                  {trainingTaskLabel(modelTask)} · {modelName(current)}
                </p>
              )}
              {current?.model_version_id && (
                <>
                  <p className="mt-3 text-xs text-muted-foreground">
                    选择待标图片，用该类型的默认模型生成候选，再到工作台修正与审核。
                  </p>
                  {modelTask === "segment" && (
                    <p className="mt-2 text-xs text-muted-foreground">
                      逐实例掩码保留孔洞与多部件；当前仅支持整图预标注。人工修正与审核后，可选择原生实例掩码训练桥接，冻结时仍须通过训练资格预检。
                    </p>
                  )}
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
                    <Button
                      size="sm"
                      disabled={!taskPage}
                      onClick={() => setTaskPage((p) => p - 1)}
                    >
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
                    {predict.isPending ? "正在排入…" : "用默认模型预标注"}
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
          {mode === "list" && modelTask && (
            <details className="border-t border-border p-4 text-xs">
              <summary className="cursor-pointer rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                默认预标切换记录
              </summary>
              <div className="mt-3 space-y-2">
                {releases.data?.items.map((release) => (
                  <div
                    key={release.id}
                    className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border p-3"
                  >
                    <span className="inline-flex flex-wrap items-center gap-1">
                      第 {release.generation} 次切换 · {modelName(release)}
                      {release.model_version_id && (
                        <CopyValueButton
                          value={release.model_version_id}
                          kind="id"
                          label={`复制第 ${release.generation} 次切换的模型版本 ID`}
                        />
                      )}
                      {release.rollback_from_id ? " · 恢复" : ""}
                      {release.id === releases.data.current_release_id ? " · 默认选择" : ""}
                    </span>
                    {release.id !== releases.data.current_release_id &&
                      release.model_version_id &&
                      release.deployment_id && (
                        <Button
                          size="sm"
                          disabled={
                            publish.isPending ||
                            isCurrentTarget(release.model_version_id, release.deployment_id)
                          }
                          onClick={() =>
                            publish.mutate({
                              view: captureView(),
                              modelId: release.model_version_id,
                              deploymentId: release.deployment_id,
                              rollbackFromId: release.id,
                            })
                          }
                        >
                          {isCurrentTarget(release.model_version_id, release.deployment_id)
                            ? "已是默认模型"
                            : "恢复此默认模型"}
                        </Button>
                      )}
                  </div>
                ))}
              </div>
              <ListPagination
                label="发布记录"
                page={historyPage}
                pageSize={historyPageSize}
                total={releases.data?.total ?? 0}
                loading={releases.isFetching}
                onPageChange={setHistoryPage}
                onPageSizeChange={(size) => {
                  setHistoryPageSize(size);
                  setHistoryPage(0);
                }}
                previousLabel="上一页记录"
                nextLabel="下一页记录"
              />
            </details>
          )}
        </ProjectModelList>
      )}
      {mode === "list" && publicationModelId && (
        <Modal
          open
          title="设置默认预标模型"
          width={880}
          onClose={() => setPublicationModelId("")}
          returnFocusRef={publicationTrigger}
        >
          <ProjectModelsPanel
            key={publicationModelId}
            projectId={projectId}
            classes={fallbackClasses}
            taskClasses={taskClasses}
            mode="detail"
            initialModelId={publicationModelId}
            publicationOnly
            onPublished={() => setPublicationModelId("")}
          />
        </Modal>
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
              className={cn(
                "min-w-0 space-y-4",
                !publicationOnly && "mt-5 rounded-xl border border-border bg-card p-4 sm:p-5",
              )}
              role="region"
              aria-label="模型版本详情"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h2 className="text-sm font-semibold">{activeModel.name}</h2>
                <Badge>{trainingTaskLabel(modelTask)}</Badge>
                {!publicationOnly && (
                  <div className="flex flex-wrap gap-2">
                    <Button
                      size="sm"
                      disabled={download.isPending}
                      onClick={() => download.mutate(captureView())}
                    >
                      下载模型
                    </Button>
                  </div>
                )}
              </div>
              {!publicationOnly && (
                <>
                  <Evaluation model={activeModel} />
                  <div className="mt-4 flex min-w-0 items-end gap-2">
                    <label className="min-w-0 flex-1 space-y-1 text-xs">
                      对比另一候选
                      <select
                        className={inputClass}
                        value={compareId}
                        onChange={(event) => setCompareId(event.target.value)}
                      >
                        <option value="" disabled hidden>
                          选择模型
                        </option>
                        {compareId &&
                          !models.data?.items.some(
                            (item) => item.id === compareId && item.id !== activeModel.id,
                          ) && (
                            <option value={compareId} disabled hidden>
                              已选对比模型（未在当前列表中）
                            </option>
                          )}
                        {models.data?.items
                          .filter((item) => item.id !== activeModel.id)
                          .map((model) => (
                            <option key={model.id} value={model.id}>
                              {model.name}
                            </option>
                          ))}
                      </select>
                    </label>
                    {compareId && (
                      <Button
                        type="button"
                        size="sm"
                        variant="ghost"
                        onClick={() => setCompareId("")}
                      >
                        取消模型对比
                      </Button>
                    )}
                  </div>
                  {comparison &&
                    (comparable ? (
                      <div className="mt-3 overflow-x-auto">
                        <table className={listStyles.table}>
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
                                <td className="px-4 py-3">{metricLabel(key)}</td>
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
                </>
              )}
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
                    <option value="" disabled hidden>
                      选择 YOLO 推理服务
                    </option>
                    {selectedRegistryId &&
                      !backends.data?.items.some(
                        (item) => item.backend.id === selectedRegistryId,
                      ) && (
                        <option value={selectedRegistryId} disabled hidden>
                          已选推理服务（暂不可用）
                        </option>
                      )}
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
                  <p className="mt-2 text-xs text-muted-foreground">
                    训练版本：{activeModel.framework_version} · 推理版本：
                    {String(
                      record(deployment.receipt.inference_runtime).version ??
                        "尚未核验，请重新加载并试用",
                    )}
                  </p>
                  {deployment.error_message && (
                    <p role="alert" className="mt-2 text-xs text-status-danger">
                      <CopyableError message={deployment.error_message} />
                    </p>
                  )}
                  {deployment.status === "ready" && (
                    <>
                      {modelTask === "segment" ? (
                        <ProjectModelMaskTrial
                          projectId={projectId}
                          modelId={activeModel.id}
                          deploymentId={deployment.id}
                          trial={deployment.trial}
                          assertOwner={() => session.assert()}
                          onReady={(ready) =>
                            setTrialReady((previous) =>
                              previous[deployment.id] === ready
                                ? previous
                                : { ...previous, [deployment.id]: ready },
                            )
                          }
                        />
                      ) : (
                        <TrialImage trial={deployment.trial} />
                      )}
                      {!isCurrentTarget(activeModel.id, deployment.id) && (
                        <label className="mt-3 flex items-center gap-2 text-xs">
                          <input
                            type="checkbox"
                            checked={!!confirmed[deployment.id]}
                            disabled={modelTask === "segment" && !trialReady[deployment.id]}
                            onChange={(event) =>
                              setConfirmed((value) => ({
                                ...value,
                                [deployment.id]: event.target.checked,
                              }))
                            }
                          />
                          已查看固定验证样本，确认用于后续人工试用
                        </label>
                      )}
                      <Button
                        size="sm"
                        className="mt-3"
                        disabled={
                          isCurrentTarget(activeModel.id, deployment.id) ||
                          !confirmed[deployment.id] ||
                          (modelTask === "segment" && !trialReady[deployment.id]) ||
                          publish.isPending ||
                          !releases.data
                        }
                        onClick={() =>
                          publish.mutate({
                            view: captureView(),
                            modelId: activeModel.id,
                            deploymentId: deployment.id,
                          })
                        }
                      >
                        {isCurrentTarget(activeModel.id, deployment.id)
                          ? "已设为默认预标"
                          : "设为默认预标模型"}
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
                  .filter(
                    (run) =>
                      run.status === "succeeded" &&
                      (selectedTask === "all" ||
                        (run.task ?? trainingTaskOf(run.config)) === selectedTask),
                  )
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
            {source.data && !registrationMatchesTask && (
              <p role="alert" className="text-xs text-status-warning">
                来源实验任务与当前模型任务不同，请切换任务后重新选择。
              </p>
            )}
            {registeredSourceModel && (
              <p role="status" className="text-xs text-muted-foreground">
                本次执行已回流为项目模型“{registeredSourceModel.name}”，无需重复添加。
              </p>
            )}
            {!registeredSourceModel &&
              source.data?.registration_eligibility?.qualified === true &&
              registrationMatchesTask && (
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
    </section>
  );
}

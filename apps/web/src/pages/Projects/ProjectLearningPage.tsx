import { CopyableError } from "@/components/ui/CopyableError";
import { useCallback, useEffect, useMemo, useRef, useState, type RefObject } from "react";
import { Link, Navigate, Outlet, useParams, useSearchParams } from "react-router-dom";
import { ArrowLeft, Database, FlaskConical, GitBranch, Layers, Settings2 } from "lucide-react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/shadcn/ui/tabs";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { batchesApi } from "@/api/batches";
import { asyncJobsApi } from "@/api/asyncJobs";
import { tasksApi } from "@/api/tasks";
import {
  trainingDatasetsApi,
  type TrainingDatasetSummary,
  type TrainingDatasetPreflight,
  type TrainingDatasetSelection,
} from "@/api/trainingDatasets";
import { useProject } from "@/hooks/useProjects";
import { PageContainer } from "@/components/layout/PageContainer";
import { Button } from "@/components/ui/Button";
import { CopyValueButton } from "@/components/ui/CopyValueButton";
import { Modal } from "@/components/ui/Modal";
import { confirmDialog } from "@/components/ui/decisionDialog";
import { ProjectTrainingDataVersions } from "./ProjectTrainingDataVersions";
import { TrainingError } from "../Training/TrainingFeedback";
import { ProjectTrainingPanel } from "./ProjectTrainingPanel";
import { lineageReturn } from "@/api/modelLineage";
import { ProjectModelsPanel } from "./ProjectModelsPanel";
import { ProjectLearningPolicyPanel } from "./ProjectLearningPolicyPanel";
import { TrainingDatasetInsights } from "./TrainingDatasetInsights";
import { useAuthStore } from "@/stores/authStore";
import { segmentationTask, trainingTaskLabel } from "../Training/trainingPresentation";
import { newTrainingVersionName } from "../Training/trainingNames";
import { TASKS } from "@/api/generated/capabilityVocab.gen";
import type { ProjectTaskDatasetOut, TrainingDatasetCreate } from "@/api/generated/types.gen";
import { datasetParentReason } from "./datasetCompatibility";
import { ApiError } from "@/api/client";
import { useTrainingSessionRevision, useTrainingViewOwner } from "../Training/trainingViewOwner";
import {
  readTrainingDatasetDraft,
  saveTrainingDatasetDraft,
  removeMatchingTrainingDatasetDraft,
  type TrainingDatasetDraftOwner,
} from "../Training/trainingDatasetDraftStorage";

type DatasetTask = ProjectTaskDatasetOut["task"];
const datasetTasks: DatasetTask[] = ["detect", "segment", "classify", "pose", "obb"];
const isDatasetTask = (value: unknown): value is DatasetTask =>
  datasetTasks.includes(value as DatasetTask);

export type ProjectLearningOutletContext = {
  createButtonRef: RefObject<HTMLButtonElement>;
};

const inputClass =
  "w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
const exclusionReasons: Record<string, string> = {
  unsupported_media: "不是图片任务",
  unusable_sample: "本轮标记为损坏、模糊或不可用",
  not_reviewed: "尚未审核通过",
  unconfirmed_negative: "空标注尚未明确确认为无目标",
};

export function ProjectLearningPage() {
  const { id = "" } = useParams();
  const createButtonRef = useRef<HTMLButtonElement>(null);
  const [params] = useSearchParams();
  const userId = useAuthStore((state) => state.user?.id);
  const token = useAuthStore((state) => state.token);
  const revision = useTrainingSessionRevision();
  const owner = useMemo(
    () => ({ id, userId, token, revision, key: crypto.randomUUID() }),
    [id, userId, token, revision],
  );
  if (params.get("new") === "1") {
    const next = new URLSearchParams();
    const framework = params.get("framework");
    if (framework === "ultralytics" || framework === "libreyolo") next.set("framework", framework);
    if (params.get("run")) next.set("return_run", params.get("run")!);
    return <Navigate replace to={`/projects/${id}/learning/new${next.size ? `?${next}` : ""}`} />;
  }
  return (
    <>
      <ProjectLearningContent
        key={owner.key}
        projectId={id}
        scope={owner.key}
        createButtonRef={createButtonRef}
      />
      <Outlet context={{ createButtonRef } satisfies ProjectLearningOutletContext} />
    </>
  );
}

function ProjectLearningContent({
  projectId,
  scope,
  createButtonRef,
}: {
  projectId: string;
  scope: string;
  createButtonRef: RefObject<HTMLButtonElement>;
}) {
  const [params, setParams] = useSearchParams();
  const { owner, owns, assert } = useTrainingViewOwner(projectId, scope);
  const datasetOwner = useCallback(
    (task: "detect" | "segment"): TrainingDatasetDraftOwner => ({
      userId: owner.userId ?? "",
      sessionId: owner.sessionId ?? "",
      projectId,
      task,
    }),
    [owner.userId, owner.sessionId, projectId],
  );
  const [storedRequests] = useState(
    () =>
      new Map(
        (["detect", "segment"] as const).map((task) => [
          task,
          owner.userId && owner.sessionId && owns()
            ? readTrainingDatasetDraft(datasetOwner(task))
            : { body: null, error: "登录会话不可用，请重新登录。" },
        ]),
      ),
  );
  const initialTask = params.get("data_task") === segmentationTask ? segmentationTask : "detect";
  const initialRequest = storedRequests.get(initialTask)!;
  const initialBody = initialRequest.body;
  const tab = ["training", "data", "models", "automation"].includes(params.get("tab") ?? "")
    ? params.get("tab")!
    : "training";
  const changeTab = (value: string) =>
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      next.set("tab", value);
      next.delete("new");
      return next;
    });
  const client = useQueryClient();
  const project = useProject(projectId);
  const datasetForm = useRef<HTMLElement>(null);
  const prepareButtonRef = useRef<HTMLButtonElement>(null);
  const versionNameRef = useRef<HTMLInputElement>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [name, setName] = useState(() => initialBody?.name ?? newTrainingVersionName());
  const [batchId, setBatchId] = useState(initialBody?.batch_id ?? "");
  const [parentId, setParentId] = useState(initialBody?.parent_id ?? "");
  const [trainingTask, setTrainingTask] =
    useState<NonNullable<TrainingDatasetSelection["task"]>>(initialTask);
  const [browseTask, setBrowseTask] = useState<DatasetTask>(
    isDatasetTask(params.get("data_task")) ? (params.get("data_task") as DatasetTask) : "detect",
  );
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false);
  const [diagnosticsPage, setDiagnosticsPage] = useState(0);
  const taskChosen = useRef(params.has("data_task") || params.has("dataset"));
  const autoSelectPending = useRef(!params.has("dataset"));
  const [mode, setMode] = useState<"reviewed" | "manager_confirmed">(
    initialBody?.mode ?? "reviewed",
  );
  const [seed, setSeed] = useState(String(initialBody?.seed ?? 42));
  const [fraction, setFraction] = useState(String((initialBody?.validation_fraction ?? 0.2) * 100));
  const [selected, setSelected] = useState<string[]>(initialBody?.task_ids ?? []);
  const [negatives, setNegatives] = useState<string[]>(initialBody?.confirmed_negative_ids ?? []);
  const [taskPage, setTaskPage] = useState(0);
  const [page, setPage] = useState(0);
  const [parentPage, setParentPage] = useState(0);
  const detailId = params.get("dataset") ?? "";
  const setDetailId = (value: string) => {
    if (!owns()) return;
    autoSelectPending.current = false;
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      if (value) next.set("dataset", value);
      else next.delete("dataset");
      ["data_class", "data_split", "data_kind", "data_page"].forEach((key) => next.delete(key));
      if (!value || next.get("data_baseline") === value) {
        next.delete("data_baseline");
        next.delete("data_change");
      }
      if (!value) next.delete("data_view");
      return next;
    });
  };
  const [report, setReport] = useState<TrainingDatasetPreflight | null>(null);
  const [error, setError] = useState("");
  const [pendingCreate, setPendingCreate] = useState<TrainingDatasetCreate | null>(initialBody);
  const [storageError, setStorageError] = useState(initialRequest.error);
  const requestId = useRef(initialBody?.request_id ?? crypto.randomUUID());
  const checkedDigest = useRef<string | null>(null);
  const selectionEpoch = useRef(0);
  const issueTaskOrdinals = useMemo(() => {
    const ordinals = new Map<string, number>();
    report?.issues.forEach((issue) => {
      const taskId = issue.task_id;
      if (typeof taskId !== "string" || !taskId || ordinals.has(taskId)) return;
      ordinals.set(taskId, ordinals.size + 1);
    });
    return ordinals;
  }, [report?.issues]);
  const draftSnapshot = {
    name,
    batchId,
    parentId,
    mode,
    seed,
    fraction,
    selected,
    negatives,
    report,
    requestId: requestId.current,
    checkedDigest: checkedDigest.current,
    error,
    pendingCreate,
    storageError,
  };
  const currentDraft = useRef(draftSnapshot);
  currentDraft.current = draftSnapshot;
  const taskDrafts = useRef(new Map<typeof trainingTask, typeof draftSnapshot>());
  const activeTask = useRef(trainingTask);
  const selection: TrainingDatasetSelection = useMemo(
    () => ({
      task: trainingTask,
      batch_id: batchId || null,
      parent_id: parentId || null,
      mode,
      seed: Number(seed),
      validation_fraction: Number(fraction) / 100,
      task_ids: mode === "manager_confirmed" ? selected : [],
      confirmed_negative_ids: mode === "manager_confirmed" ? negatives : [],
    }),
    [trainingTask, batchId, parentId, mode, seed, fraction, selected, negatives],
  );
  const versions = useQuery({
    queryKey: ["training-datasets", projectId, scope, browseTask, page],
    queryFn: ({ signal }) =>
      trainingDatasetsApi.list(projectId, page * 20, undefined, { signal }, { task: browseTask }),
    enabled: tab === "data",
    refetchInterval: (query) =>
      tab === "data" &&
      query.state.data?.items.some((item) => ["pending", "running"].includes(item.status))
        ? 2000
        : false,
  });
  const taskDatasets = useQuery({
    queryKey: ["task-datasets", projectId, scope],
    queryFn: ({ signal }) => trainingDatasetsApi.taskDatasets(projectId, { signal }),
    enabled: tab === "data",
    retry: false,
    refetchInterval: (query) =>
      query.state.data?.items.some(
        (item) => (item.status_counts.pending ?? 0) + (item.status_counts.running ?? 0) > 0,
      )
        ? 2000
        : false,
  });
  const diagnostics = useQuery({
    queryKey: ["task-dataset-diagnostics", projectId, scope, diagnosticsPage],
    queryFn: ({ signal }) =>
      trainingDatasetsApi.unrecognizedVersions(projectId, diagnosticsPage * 20, { signal }),
    enabled: tab === "data" && diagnosticsOpen,
    retry: false,
  });
  const parents = useQuery({
    queryKey: ["training-dataset-parents", projectId, scope, trainingTask, parentPage],
    queryFn: ({ signal }) =>
      trainingDatasetsApi.list(
        projectId,
        parentPage * 20,
        "ready",
        { signal },
        { task: trainingTask },
      ),
    enabled: tab === "data" && createOpen,
  });
  const selectedParent = useQuery({
    queryKey: ["training-dataset-parent", projectId, scope, parentId],
    queryFn: ({ signal }) => trainingDatasetsApi.getSummary(projectId, parentId, { signal }),
    enabled: createOpen && !!parentId,
  });
  const batches = useQuery({
    queryKey: ["batches", projectId, scope],
    queryFn: () => batchesApi.list(projectId),
    enabled: tab === "data",
  });
  const taskList = useQuery({
    queryKey: ["learning-tasks", projectId, scope, batchId, taskPage],
    queryFn: () =>
      tasksApi.listByProject(projectId, {
        batch_id: batchId || undefined,
        limit: 25,
        offset: taskPage * 25,
      }),
    enabled: tab === "data" && mode === "manager_confirmed",
  });
  const detail = useQuery({
    queryKey: ["training-dataset", projectId, scope, detailId],
    queryFn: ({ signal }) => trainingDatasetsApi.getSummary(projectId, detailId, { signal }),
    enabled: tab === "data" && !!detailId,
    refetchInterval: (query) =>
      query.state.data && ["pending", "running"].includes(query.state.data.status) ? 2000 : false,
  });
  const refresh = () => {
    void client.invalidateQueries({ queryKey: ["task-datasets", projectId] });
    void client.invalidateQueries({ queryKey: ["training-datasets", projectId] });
    void client.invalidateQueries({ queryKey: ["training-dataset", projectId] });
    void client.invalidateQueries({ queryKey: ["training-dataset-parents", projectId] });
  };
  const preflight = useMutation({
    mutationFn: () => {
      assert();
      if (currentDraft.current.pendingCreate || currentDraft.current.storageError)
        throw new DOMException("请先确认原创建请求", "AbortError");
      return trainingDatasetsApi.preflight(projectId, selection);
    },
    onMutate: () => selectionEpoch.current,
    onSuccess: (value, _variables, epoch) => {
      if (!owns() || epoch !== selectionEpoch.current) return;
      if (checkedDigest.current && checkedDigest.current !== value.digest)
        requestId.current = crypto.randomUUID();
      checkedDigest.current = value.digest;
      setReport(value);
      setError("");
    },
    onError: (reason: Error, _variables, epoch) => {
      if (!owns() || epoch !== selectionEpoch.current) return;
      setReport(null);
      setError(reason.message);
    },
  });
  const create = useMutation({
    mutationFn: (body: TrainingDatasetCreate) => {
      assert();
      return trainingDatasetsApi.create(projectId, body);
    },
    onMutate: (body) => {
      assert();
      const failure = saveTrainingDatasetDraft(datasetOwner(body.task ?? "detect"), body);
      if (failure) throw new Error(failure);
      currentDraft.current.pendingCreate = body;
      setPendingCreate(body);
      return selectionEpoch.current;
    },
    onSuccess: (value, body, epoch) => {
      if (!owns()) return;
      const failure = removeMatchingTrainingDatasetDraft(datasetOwner(body.task ?? "detect"), body);
      if (failure) {
        setError(failure);
        return;
      }
      refresh();
      const previous = taskDrafts.current.get(body.task ?? "detect");
      if (previous?.pendingCreate?.request_id === body.request_id)
        taskDrafts.current.set(body.task ?? "detect", {
          ...previous,
          pendingCreate: null,
          report: null,
          error: "",
          requestId: crypto.randomUUID(),
          checkedDigest: null,
        });
      setPendingCreate((current) => (current?.request_id === body.request_id ? null : current));
      if (epoch !== selectionEpoch.current) return;
      setDetailId(value.id);
      setCreateOpen(false);
      setName(newTrainingVersionName());
      setReport(null);
      requestId.current = crypto.randomUUID();
      checkedDigest.current = null;
    },
    onError: (reason: Error, body, epoch) => {
      if (!owns()) return;
      const transientConflict =
        reason instanceof ApiError &&
        reason.status === 409 &&
        typeof reason.detailRaw === "object" &&
        reason.detailRaw !== null &&
        "reason" in reason.detailRaw &&
        ["project_busy", "account_busy", "membership_busy"].includes(
          String(reason.detailRaw.reason),
        );
      const refused =
        reason instanceof ApiError &&
        reason.status >= 400 &&
        reason.status < 500 &&
        ![408, 425, 429].includes(reason.status) &&
        !transientConflict;
      if (refused) {
        const failure = removeMatchingTrainingDatasetDraft(
          datasetOwner(body.task ?? "detect"),
          body,
        );
        if (failure) {
          setError(failure);
          return;
        }
        const previous = taskDrafts.current.get(body.task ?? "detect");
        if (previous?.pendingCreate?.request_id === body.request_id)
          taskDrafts.current.set(body.task ?? "detect", {
            ...previous,
            pendingCreate: null,
            report: null,
            error: reason.message,
          });
        setPendingCreate((current) => (current?.request_id === body.request_id ? null : current));
      }
      if (epoch !== undefined && epoch !== selectionEpoch.current) return;
      setError(reason.message);
      if (refused) setReport(null);
    },
  });
  const action = useMutation({
    mutationFn: async ({
      kind,
      version,
    }: {
      kind: "retry" | "download" | "delete" | "cancel";
      version: TrainingDatasetSummary;
    }) => {
      if (kind === "download") {
        const result = await trainingDatasetsApi.download(projectId, version.id);
        if (!owns()) return;
        const link = document.createElement("a");
        link.href = result.url;
        link.download = `${version.name}.zip`;
        link.rel = "noopener";
        link.click();
      } else if (kind === "retry") await trainingDatasetsApi.retry(projectId, version.id);
      else if (kind === "cancel" && version.async_job_id)
        await asyncJobsApi.cancel(version.async_job_id);
      else if (
        await confirmDialog({
          title: `删除训练数据版本“${version.name}”`,
          description: "将删除冻结图片和训练包。被后续版本引用的数据不能删除。",
          tone: "danger",
          confirmLabel: "删除数据版本",
        })
      ) {
        if (!owns()) return;
        await trainingDatasetsApi.remove(projectId, version.id);
        if (!owns()) return;
        autoSelectPending.current = false;
        setParams((previous) => {
          if (previous.get("dataset") !== version.id) return previous;
          const next = new URLSearchParams(previous);
          [
            "dataset",
            "data_baseline",
            "data_view",
            "data_change",
            "data_class",
            "data_split",
            "data_kind",
            "data_page",
          ].forEach((key) => next.delete(key));
          return next;
        });
      }
    },
    onSuccess: refresh,
    onError: (reason: Error) => setError(reason.message),
  });
  const busy = preflight.isPending || create.isPending;
  const updateSelection = (update: () => void) => {
    if (!owns() || busy || currentDraft.current.pendingCreate || storageError) return;
    update();
    selectionEpoch.current += 1;
    requestId.current = crypto.randomUUID();
    checkedDigest.current = null;
    setReport(null);
    setError("");
  };
  const activateTask = useCallback(
    (task: typeof trainingTask) => {
      if (activeTask.current === task) return;
      taskDrafts.current.set(activeTask.current, currentDraft.current);
      activeTask.current = task;
      selectionEpoch.current += 1;
      const stored = storedRequests.get(task)!;
      const body = stored.body;
      const draft =
        taskDrafts.current.get(task) ??
        (body
          ? {
              name: body.name,
              batchId: body.batch_id ?? "",
              parentId: body.parent_id ?? "",
              mode: body.mode ?? "reviewed",
              seed: String(body.seed ?? 42),
              fraction: String((body.validation_fraction ?? 0.2) * 100),
              selected: body.task_ids ?? [],
              negatives: body.confirmed_negative_ids ?? [],
              report: null,
              error: "",
              pendingCreate: body,
              storageError: stored.error,
              requestId: body.request_id,
              checkedDigest: null,
            }
          : undefined);
      setTrainingTask(task);
      setName(draft?.name ?? newTrainingVersionName());
      setBatchId(draft?.batchId ?? "");
      setParentId(draft?.parentId ?? "");
      setMode(draft?.mode ?? "reviewed");
      setSeed(draft?.seed ?? "42");
      setFraction(draft?.fraction ?? "20");
      setSelected(draft?.selected ?? []);
      setNegatives(draft?.negatives ?? []);
      setReport(draft?.report ?? null);
      setError(draft?.error ?? "");
      setPendingCreate(draft?.pendingCreate ?? null);
      setStorageError(draft?.storageError ?? stored.error);
      requestId.current = draft?.requestId ?? crypto.randomUUID();
      checkedDigest.current = draft?.checkedDigest ?? null;
      setPage(0);
      setParentPage(0);
      setTaskPage(0);
    },
    [storedRequests],
  );
  const chooseTask = (task: DatasetTask) => {
    taskChosen.current = true;
    autoSelectPending.current = true;
    setBrowseTask(task);
    setPage(0);
    if (task === "detect" || task === "segment") activateTask(task);
    else {
      selectionEpoch.current += 1;
      setCreateOpen(false);
    }
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      next.set("data_task", task);
      [
        "dataset",
        "data_baseline",
        "data_view",
        "data_change",
        "data_class",
        "data_split",
        "data_kind",
        "data_page",
      ].forEach((key) => next.delete(key));
      return next;
    });
  };
  const toggleTask = (id: string) =>
    updateSelection(() => {
      setSelected((current) =>
        current.includes(id) ? current.filter((key) => key !== id) : [...current, id],
      );
      setNegatives((current) => current.filter((key) => key !== id));
    });
  const unsupported = project.data && (project.data.data_type ?? "image") !== "image";
  const detectionEnabled = !!project.data?.tool_bindings?.bbox?.enabled;
  const segmentationEnabled = !!project.data?.tool_bindings?.region?.enabled;
  const selectedTool = trainingTask === segmentationTask ? "region" : "bbox";
  const selectedBinding = project.data?.tool_bindings?.[selectedTool];
  const selectedClasses = selectedBinding?.classes ?? [];
  const selectedTaskEnabled =
    trainingTask === segmentationTask ? segmentationEnabled : detectionEnabled;
  const taskDataset = taskDatasets.data?.items.find((item) => item.task === browseTask);
  const createCapability = taskDataset?.capabilities.create_version;
  const qualifiedTask = browseTask === "detect" || browseTask === "segment";
  const parentReason =
    parentId && selectedParent.data ? datasetParentReason(selectedParent.data, trainingTask) : null;
  useEffect(() => {
    if (taskChosen.current || !project.data) return;
    if (!segmentationEnabled && !detectionEnabled) {
      const first = taskDatasets.data?.items[0]?.task;
      if (first) {
        taskChosen.current = true;
        setBrowseTask(first);
      }
      return;
    }
    taskChosen.current = true;
    if (segmentationEnabled && !detectionEnabled) {
      activateTask(segmentationTask);
      setBrowseTask(segmentationTask);
    }
  }, [project.data, segmentationEnabled, detectionEnabled, activateTask, taskDatasets.data]);
  useEffect(() => {
    const rawTask = detailId ? detail.data?.task : params.get("data_task");
    if (!isDatasetTask(rawTask) || rawTask === browseTask) return;
    taskChosen.current = true;
    setBrowseTask(rawTask);
    if (rawTask === "detect" || rawTask === "segment") activateTask(rawTask);
    else {
      selectionEpoch.current += 1;
      setCreateOpen(false);
    }
  }, [detail.data?.task, detailId, params, browseTask, activateTask]);
  useEffect(() => {
    const expectedTask =
      params.get("data_task") ??
      (detectionEnabled
        ? "detect"
        : segmentationEnabled
          ? "segment"
          : taskDatasets.data?.items[0]?.task);
    if (
      !autoSelectPending.current ||
      tab !== "data" ||
      detailId ||
      !project.data ||
      browseTask !== expectedTask ||
      !versions.isSuccess ||
      !versions.data.items[0] ||
      !owns()
    )
      return;
    autoSelectPending.current = false;
    const versionId = versions.data.items[0].id;
    setParams(
      (previous) => {
        if (previous.has("dataset")) return previous;
        const next = new URLSearchParams(previous);
        next.set("dataset", versionId);
        return next;
      },
      { replace: true },
    );
  }, [
    tab,
    detailId,
    params,
    browseTask,
    detectionEnabled,
    segmentationEnabled,
    taskDatasets.data,
    project.data,
    versions.isSuccess,
    versions.data,
    owns,
    setParams,
  ]);

  return (
    <PageContainer>
      {params.get("returnTo") && (
        <Link
          className="mb-4 inline-block text-xs text-primary hover:underline"
          to={lineageReturn(projectId, params.get("returnTo"))}
        >
          返回模型谱系
        </Link>
      )}
      <header className="mb-5 grid grid-cols-[minmax(0,1fr)_auto] items-start gap-3">
        <div className="min-w-0">
          <Link
            to="/training"
            className="inline-flex items-center gap-1.5 rounded-sm text-xs text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <ArrowLeft className="size-3.5" />
            训练队列
          </Link>
          <h1
            title={project.data?.name}
            className="mt-2 line-clamp-2 break-words text-xl font-semibold tracking-tight"
          >
            {project.data?.name ?? "项目训练"}
          </h1>
          <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
            <span className="font-mono">{project.data?.display_id ?? "项目训练"}</span>
            <span aria-hidden="true">/</span>
            <span>数据、实验与模型发布</span>
          </p>
        </div>
        <Link
          to={`/projects/${projectId}/settings`}
          className="inline-flex items-center gap-1.5 rounded-md border border-border bg-card px-3 py-2 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <Settings2 aria-hidden="true" className="size-3.5" />
          项目设置
        </Link>
      </header>
      {project.error && (
        <div className="mb-4">
          <TrainingError message={project.error.message} onRetry={() => void project.refetch()} />
        </div>
      )}
      <Tabs value={tab} activationMode="manual" onValueChange={changeTab}>
        <TabsList
          variant="line"
          aria-label="项目训练功能"
          className="mb-5 w-full justify-start gap-1 overflow-x-auto border-b border-border pb-2 sm:gap-4"
        >
          <TabsTrigger
            value="training"
            className="flex-1 px-1 text-xs sm:flex-none sm:px-2 sm:text-sm"
          >
            <FlaskConical className="hidden size-4 sm:block" />
            训练实验
          </TabsTrigger>
          <TabsTrigger value="data" className="flex-1 px-1 text-xs sm:flex-none sm:px-2 sm:text-sm">
            <Database className="hidden size-4 sm:block" />
            训练数据
          </TabsTrigger>
          <TabsTrigger
            value="models"
            className="flex-1 px-1 text-xs sm:flex-none sm:px-2 sm:text-sm"
          >
            <Layers className="hidden size-4 sm:block" />
            模型与发布
          </TabsTrigger>
          <TabsTrigger
            value="automation"
            className="flex-1 px-1 text-xs sm:flex-none sm:px-2 sm:text-sm"
          >
            <GitBranch className="hidden size-4 sm:block" />
            自动训练
          </TabsTrigger>
        </TabsList>
        <TabsContent value="data" forceMount className="data-[state=inactive]:hidden">
          {error && !createOpen && (
            <p
              role="alert"
              className="mb-4 rounded-md bg-status-danger-soft p-3 text-sm text-status-danger"
            >
              <CopyableError message={error} />
            </p>
          )}
          <header className="mb-3 flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="text-sm font-semibold">项目衍生数据集</h2>
              <p className="mt-1 text-xs text-muted-foreground">
                按任务管理冻结版本，检查数据分布与迭代变化。
              </p>
            </div>
            <div className="flex flex-wrap items-end gap-3">
              <label className="flex shrink-0 items-center gap-2 whitespace-nowrap text-xs font-medium">
                数据集任务
                <select
                  className={inputClass}
                  value={browseTask}
                  disabled={busy}
                  onChange={(event) => chooseTask(event.target.value as DatasetTask)}
                >
                  {(taskDatasets.data?.items.length
                    ? taskDatasets.data.items.map((item) => item.task)
                    : (["detect", "segment"] as DatasetTask[])
                  ).map((task) => (
                    <option value={task} key={task}>
                      {task === "detect" || task === "segment"
                        ? trainingTaskLabel(task)
                        : TASKS[task].label}
                    </option>
                  ))}
                  {taskDatasets.data &&
                    !taskDatasets.data.items.some((item) => item.task === browseTask) && (
                      <option value={browseTask}>{TASKS[browseTask].label} · 尚无任务数据集</option>
                    )}
                </select>
              </label>
              <Button
                ref={prepareButtonRef}
                variant="primary"
                disabled={
                  !qualifiedTask ||
                  (!pendingCreate &&
                    (!!unsupported ||
                      !selectedTaskEnabled ||
                      createCapability?.supported === false))
                }
                onClick={() => setCreateOpen(true)}
              >
                准备新版本
              </Button>
            </div>
          </header>
          {taskDatasets.error && (
            <TrainingError
              message={taskDatasets.error.message}
              onRetry={() => void taskDatasets.refetch()}
            />
          )}
          {taskDataset && (
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2 text-2xs text-muted-foreground">
              <p>
                本任务 {taskDataset.version_count} 个版本 · 已就绪{" "}
                {taskDataset.status_counts.ready ?? 0} 个
                {taskDataset.pending_membership_count > 0 &&
                  ` · ${taskDataset.pending_membership_count} 个历史版本待关联序号`}
              </p>
              <details>
                <summary className="cursor-pointer">查看任务能力</summary>
                <dl className="mt-2 grid gap-2 sm:grid-cols-2">
                  {(
                    [
                      ["create_version", "创建版本"],
                      ["qualified_training", "合格训练"],
                      ["overview_statistics", "概览统计"],
                      ["task_statistics", "任务统计"],
                      ["sample_preview", "样本预览"],
                      ["semantic_comparison", "语义比较"],
                    ] as const
                  ).map(([key, label]) => (
                    <div key={key}>
                      <dt className="inline font-medium text-foreground">{label}：</dt>
                      <dd className="inline">
                        {taskDataset.capabilities[key].supported
                          ? "可用"
                          : (taskDataset.capabilities[key].reason ?? "尚未资格验证")}
                      </dd>
                    </div>
                  ))}
                </dl>
              </details>
            </div>
          )}
          {(!qualifiedTask || createCapability?.supported === false) && (
            <p className="mb-4 rounded-md bg-muted p-3 text-xs text-muted-foreground">
              {createCapability?.reason ??
                "该任务的冻结、导出与训练链路尚未资格验证，暂不能创建版本。"}
            </p>
          )}
          {!!taskDatasets.data?.unrecognized_version_count && (
            <section className="mb-4 rounded-lg border border-border p-3 text-xs">
              <Button
                size="sm"
                variant="ghost"
                aria-expanded={diagnosticsOpen}
                onClick={() => setDiagnosticsOpen((value) => !value)}
              >
                未识别的历史版本 · {taskDatasets.data.unrecognized_version_count}
              </Button>
              {diagnosticsOpen && (
                <div className="mt-2 space-y-2">
                  <p className="text-muted-foreground">
                    这些记录未分配到任务版本流，原始冻结数据保持不变。
                  </p>
                  {diagnostics.isLoading && <p role="status">正在读取诊断…</p>}
                  {diagnostics.error && (
                    <TrainingError
                      message={diagnostics.error.message}
                      onRetry={() => void diagnostics.refetch()}
                    />
                  )}
                  {diagnostics.data?.items.map((item, index) => (
                    <div key={item.id} className="rounded-md bg-muted p-3">
                      <p className="flex min-w-0 items-center gap-1 font-medium">
                        <span className="min-w-0 truncate" title={item.name}>
                          {item.name}
                        </span>
                        <span className="shrink-0 text-xs font-normal text-muted-foreground">
                          记录 {diagnosticsPage * 20 + index + 1}
                        </span>
                        <CopyValueButton value={item.id} kind="id" label="复制历史数据版本 ID" />
                      </p>
                      <p className="mt-1 break-all text-muted-foreground">
                        {item.raw_task ?? "任务缺失或非字符串"} · {item.reason}
                      </p>
                    </div>
                  ))}
                  <div className="flex justify-end gap-2">
                    <Button
                      size="sm"
                      disabled={!diagnosticsPage || diagnostics.isFetching}
                      onClick={() => setDiagnosticsPage((value) => value - 1)}
                    >
                      上一页诊断
                    </Button>
                    <Button
                      size="sm"
                      disabled={
                        (diagnosticsPage + 1) * 20 >= (diagnostics.data?.total ?? 0) ||
                        diagnostics.isFetching
                      }
                      onClick={() => setDiagnosticsPage((value) => value + 1)}
                    >
                      下一页诊断
                    </Button>
                  </div>
                </div>
              )}
            </section>
          )}
          {unsupported && (
            <p className="mb-4 rounded-md bg-muted p-3 text-xs text-muted-foreground">
              当前支持图片矩形框检测和单多边形实例分割数据，请切换到图片项目。
            </p>
          )}
          <div className="flex min-w-0 flex-col gap-4">
            <ProjectTrainingDataVersions
              data={versions.data}
              loading={versions.isLoading}
              error={versions.error?.message}
              pending={action.isPending}
              selectedId={detailId}
              selectedVersion={detail.data}
              page={page}
              onPage={setPage}
              onRetry={() => void versions.refetch()}
              onSelect={setDetailId}
              onAction={(kind, version) => action.mutate({ kind, version })}
            />
            <div className="min-w-0">
              {detailId && detail.isLoading && (
                <p role="status" className="p-5 text-sm text-muted-foreground">
                  正在加载所选数据版本…
                </p>
              )}
              {detail.error && (
                <TrainingError
                  message={detail.error.message}
                  onRetry={() => void detail.refetch()}
                />
              )}
              {tab === "data" && detail.data && (
                <TrainingDatasetInsights
                  projectId={projectId}
                  version={detail.data}
                  onClose={() => setDetailId("")}
                />
              )}
              {!detailId && (
                <section
                  aria-label="数据版本详情"
                  className="rounded-xl border border-dashed border-border bg-card px-5 py-16 text-center"
                >
                  <Database
                    aria-hidden="true"
                    className="mx-auto mb-3 size-6 text-muted-foreground"
                  />
                  <h3 className="text-sm font-medium">选择一个冻结版本</h3>
                  <p className="mx-auto mt-2 max-w-sm text-xs leading-relaxed text-muted-foreground">
                    在上方选择数据版本，或展开“浏览版本”查看摘要、图表与版本变化。
                  </p>
                  {versions.isSuccess && versions.data.items[0] && (
                    <Button
                      variant="primary"
                      className="mt-5"
                      onClick={() => setDetailId(versions.data.items[0].id)}
                    >
                      {page === 0 ? "查看最新版本" : "查看本页首个版本"}
                    </Button>
                  )}
                </section>
              )}
            </div>
          </div>
          <Modal
            open={createOpen}
            onClose={() => setCreateOpen(false)}
            title="新建数据版本"
            width={880}
            returnFocusRef={prepareButtonRef}
            initialFocusRef={versionNameRef}
          >
            <section ref={datasetForm} aria-labelledby="create-dataset-title" className="min-w-0">
              {storageError && (
                <p role="alert" className="mb-4 text-sm text-status-danger">
                  {storageError}
                </p>
              )}
              {error && (
                <p
                  role="alert"
                  className="mb-4 rounded-md bg-status-danger-soft p-3 text-sm text-status-danger"
                >
                  <CopyableError message={error} />
                </p>
              )}
              <h2 id="create-dataset-title" className="mb-1 text-sm font-semibold">
                创建训练数据版本
              </h2>
              <p className="mb-4 text-xs leading-relaxed text-muted-foreground">
                冻结本轮图片、标注与类别，生成包含原图的训练包。后续修改标注不会改变已有版本。
              </p>
              {pendingCreate && (
                <div
                  role="status"
                  className="mb-4 rounded-md bg-status-caution-soft p-3 text-xs text-status-caution"
                >
                  <p>
                    上次创建结果尚未确认，原始请求已保留。确认会复用同一请求，避免重复创建；确认前暂时锁定编辑与重新检查。
                  </p>
                </div>
              )}
              <fieldset
                disabled={busy || !!pendingCreate || !!storageError}
                className="grid gap-4 border-0 p-0 sm:grid-cols-2"
              >
                <label className="grid gap-1.5 text-xs font-medium sm:col-span-2">
                  训练任务
                  <select
                    className={inputClass}
                    value={trainingTask}
                    onChange={(event) => chooseTask(event.target.value as typeof trainingTask)}
                  >
                    <option value="detect" disabled={!detectionEnabled}>
                      图片目标检测 · 矩形框
                    </option>
                    <option value={segmentationTask} disabled={!segmentationEnabled}>
                      图片实例分割 · 单多边形
                    </option>
                  </select>
                </label>
                <label className="grid gap-1.5 text-xs font-medium sm:col-span-2">
                  版本名称
                  <input
                    ref={versionNameRef}
                    className={inputClass}
                    value={name}
                    maxLength={120}
                    onChange={(event) => {
                      if (!owns() || busy || pendingCreate || storageError) return;
                      setName(event.target.value);
                      requestId.current = crypto.randomUUID();
                    }}
                    placeholder="例如：路口车辆首轮数据"
                  />
                </label>
                <label className="grid gap-1.5 text-xs font-medium">
                  数据范围
                  <select
                    className={inputClass}
                    value={batchId}
                    onChange={(event) =>
                      updateSelection(() => {
                        setBatchId(event.target.value);
                        setSelected([]);
                        setNegatives([]);
                        setTaskPage(0);
                      })
                    }
                  >
                    <option value="">整个项目</option>
                    {batches.data?.map((batch) => (
                      <option key={batch.id} value={batch.id}>
                        {batch.name}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="grid gap-1.5 text-xs font-medium">
                  标注纳入方式
                  <select
                    className={inputClass}
                    value={mode}
                    onChange={(event) =>
                      updateSelection(() => {
                        setMode(event.target.value as typeof mode);
                        setSelected([]);
                        setNegatives([]);
                      })
                    }
                  >
                    <option value="reviewed">已审核通过</option>
                    <option value="manager_confirmed">负责人明确确认</option>
                  </select>
                </label>
                <label className="grid gap-1.5 text-xs font-medium sm:col-span-2">
                  继承数据划分
                  <select
                    className={inputClass}
                    value={parentId}
                    onChange={(event) => updateSelection(() => setParentId(event.target.value))}
                  >
                    <option value="">首次划分</option>
                    {parentId &&
                      !parents.data?.items.some((version) => version.id === parentId) && (
                        <option value={parentId}>
                          {selectedParent.data?.name ?? "正在核验父版本…"}
                        </option>
                      )}
                    {parents.data?.items
                      .filter(
                        (version) =>
                          version.status === "ready" && (version.task ?? "detect") === trainingTask,
                      )
                      .map((version) => (
                        <option
                          key={version.id}
                          value={version.id}
                          disabled={!!datasetParentReason(version, trainingTask)}
                        >
                          {version.name}
                          {datasetParentReason(version, trainingTask)
                            ? ` · ${datasetParentReason(version, trainingTask)}`
                            : ""}
                        </option>
                      ))}
                  </select>
                </label>
                {(parents.error || selectedParent.error) && (
                  <p role="alert" className="text-xs text-status-danger sm:col-span-2">
                    <CopyableError
                      message={parents.error?.message ?? selectedParent.error?.message}
                    />
                  </p>
                )}
                {parentReason && (
                  <p role="alert" className="text-xs text-status-caution sm:col-span-2">
                    父版本不可用：{parentReason}。服务端还会核验冻结分组证据。
                  </p>
                )}
                {((parents.data?.total ?? 0) > 20 || parentPage > 0) && (
                  <nav
                    aria-label="父版本分页"
                    className="flex flex-wrap items-center justify-end gap-2 sm:col-span-2"
                  >
                    <span className="mr-auto text-xs text-muted-foreground">
                      第 {parentPage + 1} 页 · 共 {parents.data?.total ?? 0} 个已就绪版本
                    </span>
                    <Button
                      size="sm"
                      disabled={!parentPage || parents.isFetching}
                      onClick={() => setParentPage((p) => p - 1)}
                    >
                      上一页父版本
                    </Button>
                    <Button
                      size="sm"
                      disabled={
                        parents.isFetching || (parentPage + 1) * 20 >= (parents.data?.total ?? 0)
                      }
                      onClick={() => setParentPage((p) => p + 1)}
                    >
                      下一页父版本
                    </Button>
                  </nav>
                )}
                <div className="rounded-md bg-muted p-3 text-xs sm:col-span-2" aria-live="polite">
                  <p className="font-medium">
                    抽取范围：{trainingTaskLabel(trainingTask)} · {selectedTool} 工具
                  </p>
                  <p className="mt-1 text-muted-foreground">
                    类别（{selectedClasses.length}）：
                    {selectedClasses
                      .map((value) => (typeof value === "string" ? value : value.name))
                      .join("、") || "尚未配置"}
                  </p>
                  <p className="mt-1 text-muted-foreground">
                    每个版本固定一个训练任务。同一项目可分别创建检测、分割等数据版本；其他工具标注会排除并计数。无目标确认仅针对当前所选任务。
                  </p>
                </div>
                {trainingTask === segmentationTask && (
                  <p className="text-xs leading-relaxed text-muted-foreground sm:col-span-2">
                    仅接受 region 的无洞单多边形。洞、多部件、栅格 mask
                    会在预检时报错；其他工具的标注会单独计数并排除。训练包保留实例来源；原生 mask
                    的像素覆盖取决于训练分辨率。
                  </p>
                )}
                <label className="grid gap-1.5 text-xs font-medium">
                  验证集比例（%）
                  <input
                    type="number"
                    min={5}
                    max={50}
                    className={inputClass}
                    value={fraction}
                    onChange={(event) => updateSelection(() => setFraction(event.target.value))}
                  />
                </label>
                <label className="grid gap-1.5 text-xs font-medium">
                  划分随机种子
                  <input
                    type="number"
                    min={0}
                    max={2147483647}
                    className={inputClass}
                    value={seed}
                    onChange={(event) => updateSelection(() => setSeed(event.target.value))}
                  />
                </label>
                {mode === "manager_confirmed" && (
                  <div className="sm:col-span-2">
                    <p className="mb-2 text-xs text-muted-foreground">
                      选择本轮确认用于训练的任务。空白图片只有勾选“确认为无目标”后才会作为负样本。
                    </p>
                    {taskList.error && (
                      <p role="alert" className="text-xs text-status-danger">
                        <CopyableError message={taskList.error.message} />
                      </p>
                    )}
                    <div className="max-h-64 overflow-auto rounded-md border border-border">
                      {taskList.data?.items.map((task) => (
                        <div
                          key={task.id}
                          className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-3 py-2 text-xs last:border-b-0"
                        >
                          <label className="flex min-w-0 items-center gap-2">
                            <input
                              type="checkbox"
                              checked={selected.includes(task.id)}
                              onChange={() => toggleTask(task.id)}
                            />
                            <span className="truncate">
                              {task.display_id} · {task.file_name}
                            </span>
                          </label>
                          <label className="flex items-center gap-2 text-muted-foreground">
                            <input
                              type="checkbox"
                              disabled={!selected.includes(task.id)}
                              checked={negatives.includes(task.id)}
                              onChange={() =>
                                updateSelection(() =>
                                  setNegatives((current) =>
                                    current.includes(task.id)
                                      ? current.filter((key) => key !== task.id)
                                      : [...current, task.id],
                                  ),
                                )
                              }
                            />
                            确认为无目标
                          </label>
                        </div>
                      ))}
                    </div>
                    <div className="mt-2 flex items-center gap-3 text-xs text-muted-foreground">
                      <span>已选择 {selected.length} 个任务</span>
                      <Button
                        size="sm"
                        disabled={!taskPage || taskList.isFetching}
                        onClick={() => setTaskPage((value) => value - 1)}
                      >
                        上一页
                      </Button>
                      <Button
                        size="sm"
                        disabled={taskList.isFetching || (taskList.data?.items.length ?? 0) < 25}
                        onClick={() => setTaskPage((value) => value + 1)}
                      >
                        下一页
                      </Button>
                    </div>
                  </div>
                )}
              </fieldset>
              <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-border pt-4">
                <Button
                  disabled={
                    busy ||
                    !!storageError ||
                    !!pendingCreate ||
                    !selectedTaskEnabled ||
                    !selectedClasses.length ||
                    !!parentReason ||
                    (!!parentId && !selectedParent.isSuccess) ||
                    (mode === "manager_confirmed" && !selected.length) ||
                    !seed ||
                    !fraction
                  }
                  onClick={() => preflight.mutate()}
                >
                  {preflight.isPending ? "正在检查素材…" : "检查训练数据"}
                </Button>
                <Button
                  variant="primary"
                  disabled={
                    busy ||
                    !!storageError ||
                    (!pendingCreate &&
                      (!report?.ready ||
                        !name.trim() ||
                        !!parentReason ||
                        (!!parentId && !selectedParent.isSuccess)))
                  }
                  onClick={() =>
                    create.mutate(
                      pendingCreate ?? {
                        ...selection,
                        name: name.trim(),
                        request_id: requestId.current,
                        preflight_digest: report!.digest,
                      },
                    )
                  }
                >
                  {create.isPending
                    ? "正在创建…"
                    : pendingCreate
                      ? "确认原创建请求"
                      : "创建数据版本"}
                </Button>
              </div>
              {report && (
                <div className="mt-4 rounded-md bg-muted p-3 text-xs" aria-live="polite">
                  <p className="font-medium">
                    {report.ready ? "数据检查通过" : "需要处理数据问题"}
                  </p>
                  <p className="mt-2 text-muted-foreground">
                    正样本 {report.summary.positives} · 负样本 {report.summary.negatives} · 排除{" "}
                    {report.summary.excluded} · 类别 {report.classes.length}
                  </p>
                  <p className="mt-1 text-muted-foreground">
                    {trainingTaskLabel(report.task ?? trainingTask)} · 对象{" "}
                    {report.summary.objects ?? 0} · 排除其他工具标注{" "}
                    {report.summary.excluded_annotations ?? 0} 条
                  </p>
                  {report.issues.map((issue, index) => {
                    const taskId = typeof issue.task_id === "string" ? issue.task_id : "";
                    const taskOrdinal = taskId ? issueTaskOrdinals.get(taskId) : undefined;
                    return (
                      <p className="mt-2 text-status-danger" key={index}>
                        {String(issue.message ?? issue.code)}
                        {taskId && taskOrdinal !== undefined && (
                          <span className="ml-1 inline-flex items-center gap-1">
                            任务 {taskOrdinal}
                            <CopyValueButton value={taskId} kind="id" label="复制问题任务 ID" />
                          </span>
                        )}
                      </p>
                    );
                  })}
                  {report.excluded.length > 0 && (
                    <details className="mt-2">
                      <summary className="cursor-pointer">查看排除原因</summary>
                      <ul className="mt-2 space-y-1">
                        {report.excluded.map((item, index) => (
                          <li key={index}>
                            任务 {index + 1}
                            <CopyValueButton
                              value={String(item.task_id)}
                              kind="id"
                              label="复制排除样本任务 ID"
                            />
                            ：{exclusionReasons[String(item.code)] ?? "不符合当前数据条件"}
                          </li>
                        ))}
                      </ul>
                    </details>
                  )}
                </div>
              )}
            </section>
          </Modal>
        </TabsContent>
        <TabsContent value="training">
          {unsupported ? (
            <p className="rounded-lg bg-muted p-5 text-sm text-muted-foreground">
              当前支持图片目标检测和单多边形实例分割训练，请切换到图片项目。
            </p>
          ) : (
            <ProjectTrainingPanel
              projectId={projectId}
              scope={scope}
              createButtonRef={createButtonRef}
              initialRunId={params.get("run") ?? params.get("return_run") ?? ""}
              initialAttemptId={params.get("attempt") ?? ""}
              onAttemptSelect={(attemptId, runId) =>
                setParams((previous) => {
                  const next = new URLSearchParams(previous);
                  next.set("attempt", attemptId);
                  next.set("run", runId);
                  return next;
                })
              }
              onRunSelect={(runId) =>
                setParams((previous) => {
                  const next = new URLSearchParams(previous);
                  next.set("tab", "training");
                  next.set("run", runId);
                  next.delete("attempt");
                  next.delete("new");
                  return next;
                })
              }
            />
          )}
        </TabsContent>
        <TabsContent value="models">
          {(unsupported || !detectionEnabled) && (
            <p className="rounded-lg bg-muted p-5 text-sm text-muted-foreground">
              当前模型回流支持图片矩形框检测项目。
            </p>
          )}
          {!unsupported && detectionEnabled && (
            <ProjectModelsPanel
              projectId={projectId}
              classes={(project.data?.tool_bindings?.bbox?.classes ?? []).map((item) => item.name)}
            />
          )}
        </TabsContent>
        <TabsContent value="automation">
          {(unsupported || !detectionEnabled) && (
            <p className="rounded-lg bg-muted p-5 text-sm text-muted-foreground">
              当前自动训练支持图片矩形框检测项目。
            </p>
          )}
          {!unsupported && detectionEnabled && <ProjectLearningPolicyPanel projectId={projectId} />}
        </TabsContent>
      </Tabs>
    </PageContainer>
  );
}

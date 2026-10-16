import { useEffect, useMemo, useRef, useState, type ChangeEvent } from "react";
import { Link, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Check, Download, FileUp, FlaskConical } from "lucide-react";
import { projectsApi } from "@/api/projects";
import { trainingDatasetsApi } from "@/api/trainingDatasets";
import { trainingRunsApi, type TrainingRunCreate } from "@/api/trainingRuns";
import {
  recipeCandidate,
  initializationReference,
  trainingRecipesApi,
  type ReusableSnapshot,
  type TrainingReuseCandidate,
} from "@/api/trainingRecipes";
import type { TrainingParameterSchema } from "@/api/generated/types.gen";
import { useProject } from "@/hooks/useProjects";
import { useAuthStore } from "@/stores/authStore";
import { PageContainer } from "@/components/layout/PageContainer";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Modal } from "@/components/ui/Modal";
import { confirmDialog } from "@/components/ui/decisionDialog";
import { TrainingConfigurationEditor } from "./TrainingConfigurationEditor";
import { TrainingParameterInventory } from "./TrainingParameterInventory";
import { trainingInputClass, trainingValueLabel } from "./TrainingParameterField";
import { TrainingRunReview } from "./TrainingRunReview";
import { TrainingStudyCreate } from "./TrainingStudyCreate";
import { TrainingAugmentationPreview } from "./TrainingAugmentationPreview";
import {
  TrainingRecipeLibrary,
  TrainingRecipeSave,
  TrainingRecipeStatus,
} from "./TrainingRecipeLibrary";
import { TrainingReuseDialog } from "./TrainingReuseDialog";
import { useTrainingDraft } from "./useTrainingDraft";
import { useTrainingSessionRevision } from "./trainingViewOwner";
import {
  TrainingInitializationSource,
  useTrainingInitializationSource,
} from "./TrainingInitializationSource";
import type { TrainingDraft } from "./trainingDraftStorage";
import { segmentationTask, trainingTaskLabel } from "./trainingPresentation";
import {
  collectOverrides,
  compatibleDrafts,
  CONFIGURATION_FILE_LIMIT,
  importConfiguration,
  portableConfiguration,
  valueToDraft,
  type ParameterDrafts,
} from "./trainingConfiguration";

type Report = {
  snapshot: TrainingDraft;
  body: TrainingRunCreate;
  runtime: string;
  config: Record<string, unknown>;
  config_digest: string;
};
type SwitchPreview = {
  snapshot: TrainingDraft;
  schema: TrainingParameterSchema;
  baseModel: string;
  retained: ParameterDrafts;
  discarded: string[];
  runtime: string;
};
type ImportPreview = ReturnType<typeof importConfiguration> & { snapshot: TrainingDraft };
type ReusePreview = {
  snapshot: TrainingDraft;
  candidate: TrainingReuseCandidate;
  before: ReusableSnapshot | null;
  schema: TrainingParameterSchema;
  runtime: string;
  intent?: string;
  intentOwner?: { intent: string; key: string };
};
const steps = ["数据与模型", "配置参数", "核对并开始"];
const reasonMessage = (reason: unknown) =>
  reason instanceof Error ? reason.message : "操作失败，请重试";
const sameValues = (first: Record<string, unknown>, second: Record<string, unknown>) =>
  JSON.stringify(
    Object.keys(first)
      .sort()
      .map((key) => [key, first[key]]),
  ) ===
  JSON.stringify(
    Object.keys(second)
      .sort()
      .map((key) => [key, second[key]]),
  );

export function TrainingCreatePage() {
  const { id = "" } = useParams();
  const userId = useAuthStore((state) => state.user?.id ?? "");
  const token = useAuthStore((state) => state.token ?? "");
  const sessionRevision = useTrainingSessionRevision();
  const ownerMount = useMemo(
    () => ({
      projectId: id,
      userId,
      sessionToken: token,
      sessionRevision,
      key: crypto.randomUUID(),
    }),
    [id, userId, token, sessionRevision],
  );
  return (
    <TrainingCreateContent
      key={ownerMount.key}
      projectId={id}
      userId={userId}
      sessionToken={token}
    />
  );
}

function TrainingCreateContent({
  projectId,
  userId,
  sessionToken,
}: {
  projectId: string;
  userId: string;
  sessionToken: string;
}) {
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const cloneRun = params.get("clone_run") ?? "";
  const cloneAttempt = params.get("clone_attempt") ?? "";
  const intentOwner = useMemo(
    () => ({ intent: `${cloneRun}:${cloneAttempt}`, key: location.key }),
    [cloneRun, cloneAttempt, location.key],
  );
  const intentOwnerRef = useRef(intentOwner);
  intentOwnerRef.current = intentOwner;
  const hint = params.get("framework");
  const state = useTrainingDraft(
    userId,
    projectId,
    sessionToken,
    hint === "libreyolo" ? hint : "ultralytics",
  );
  const { draft } = state;
  const navigate = useNavigate();
  const client = useQueryClient();
  const [queryScope] = useState(() => crypto.randomUUID());
  const project = useProject(projectId);
  const [step, setStep] = useState(() => (draft.pendingStudy ? 2 : 0));
  const [studyMode, setStudyMode] = useState(
    () => !!draft.pendingStudy || params.get("mode") === "study",
  );
  const [studyBusy, setStudyBusy] = useState(false);
  const studyLock = useRef(false);
  const [error, setError] = useState("");
  const [report, setReport] = useState<Report | null>(null);
  const [checking, setChecking] = useState(false);
  const [creating, setCreating] = useState(false);
  const [switching, setSwitching] = useState(false);
  const [readingFile, setReadingFile] = useState(false);
  const [inventoryOpen, setInventoryOpen] = useState(false);
  const [switchPreview, setSwitchPreview] = useState<SwitchPreview | null>(null);
  const [importPreview, setImportPreview] = useState<ImportPreview | null>(null);
  const [reusePreview, setReusePreview] = useState<ReusePreview | null>(null);
  const [reuseBusy, setReuseBusy] = useState(false);
  const [cloneRetry, setCloneRetry] = useState(0);
  const reuseLock = useRef(false);
  const cloneIntent = useRef("");
  const cloneSequence = useRef(0);
  const cloneUrl = useRef("");
  cloneUrl.current = `${params.get("clone_run") ?? ""}:${params.get("clone_attempt") ?? ""}`;
  const preflightRequest = useRef<AbortController | null>(null);
  const createLock = useRef(false);
  const switchLock = useRef(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const fileSequence = useRef(0);
  const backParams = new URLSearchParams({ tab: "training" });
  if (params.get("return_run")) backParams.set("run", params.get("return_run")!);
  const back = `/projects/${projectId}/learning?${backParams}`;

  const catalog = useQuery({
    queryKey: ["training-catalog", projectId, userId, queryScope],
    queryFn: ({ signal }) => trainingRunsApi.catalog(projectId, { signal }),
    refetchInterval: 5000,
  });
  const template = catalog.data?.items.find((item) => item.framework === draft.framework);
  const catalogRef = useRef(catalog.data);
  catalogRef.current = catalog.data;
  const modelRuntime = (framework: string, model: string) => {
    const item = catalogRef.current?.items.find((candidate) => candidate.framework === framework);
    const base = item?.base_models.find((candidate) => candidate.id === model);
    return item && base
      ? JSON.stringify([
          catalogRef.current?.enabled,
          catalogRef.current?.resource_id,
          item.framework,
          item.version,
          item.image_digest,
          base,
          item.parameter_schemas
            ?.map((summary) => [summary.schema_id, summary.schema_digest])
            .sort(),
        ])
      : null;
  };
  const catalogSchemaMatches = (definition: TrainingParameterSchema | undefined) => {
    if (!definition) return false;
    const item = catalogRef.current?.items.find(
      (candidate) => candidate.framework === definition.framework,
    );
    return (
      !item?.parameter_schemas ||
      item.parameter_schemas.some(
        (summary) =>
          summary.schema_id === definition.schema_id &&
          summary.schema_digest === definition.schema_digest,
      )
    );
  };
  const selectedBase = template?.base_models.find((item) => item.id === draft.baseModel);
  const schemaQuery = useQuery({
    queryKey: [
      "training-parameter-schema",
      projectId,
      userId,
      queryScope,
      draft.framework,
      draft.baseModel,
    ],
    queryFn: ({ signal }) =>
      trainingRunsApi.parameterSchema(projectId, draft.framework, draft.baseModel, { signal }),
    enabled: !!draft.baseModel,
  });
  const schema = schemaQuery.data;
  const currentSchema = useRef(schema);
  currentSchema.current = schema;
  const catalogDefinitionCurrent = catalogSchemaMatches(schema);
  const schemaMatches =
    !!schema &&
    schema.schema_id === draft.schemaId &&
    schema.schema_digest === draft.schemaDigest &&
    catalogDefinitionCurrent;
  const announcedSchema = template?.parameter_schemas
    ?.map((summary) => `${summary.schema_id}:${summary.schema_digest}`)
    .sort()
    .join("|");
  useEffect(() => {
    if (announcedSchema && draft.baseModel && schemaQuery.isSuccess && !catalogDefinitionCurrent)
      void client.invalidateQueries({
        queryKey: [
          "training-parameter-schema",
          projectId,
          userId,
          queryScope,
          draft.framework,
          draft.baseModel,
        ],
      });
  }, [
    announcedSchema,
    catalogDefinitionCurrent,
    schemaQuery.isSuccess,
    client,
    draft.baseModel,
    draft.framework,
    projectId,
    userId,
    queryScope,
  ]);
  const datasets = useInfiniteQuery({
    queryKey: ["training-datasets", projectId, "ready-pages", userId, queryScope],
    initialPageParam: 0,
    queryFn: ({ pageParam }) => trainingDatasetsApi.list(projectId, pageParam, "ready"),
    getNextPageParam: (last, _pages, offset) =>
      offset + 20 < last.total ? offset + 20 : undefined,
  });
  const selectedDataset = useQuery({
    queryKey: ["training-dataset", projectId, draft.datasetId, userId, queryScope],
    queryFn: () => trainingDatasetsApi.get(projectId, draft.datasetId),
    enabled: !!draft.datasetId,
  });
  const choices = datasets.data?.pages.flatMap((page) => page.items) ?? [];
  if (selectedDataset.data && !choices.some((item) => item.id === selectedDataset.data.id))
    choices.push(selectedDataset.data);
  const validDataset =
    selectedDataset.isSuccess &&
    !selectedDataset.isError &&
    selectedDataset.data.project_id === projectId &&
    selectedDataset.data.status === "ready" &&
    !!selectedDataset.data.package_sha256;
  const datasetTask = selectedDataset.data?.task ?? "detect";
  const tasksMatch =
    !!selectedBase &&
    !!schema &&
    datasetTask === (selectedBase.task ?? "detect") &&
    datasetTask === schema.task;
  const segmentation = (selectedBase?.task ?? schema?.task) === segmentationTask;
  const collected = schema ? collectOverrides(schema, draft.fields) : { values: {}, errors: {} };
  const runtime = modelRuntime(draft.framework, draft.baseModel) ?? "";
  const runtimeRef = useRef(runtime);
  runtimeRef.current = runtime;
  const reviewed =
    report && schemaMatches && state.matches(report.snapshot) && report.runtime === runtime
      ? report
      : null;
  const locked =
    !!draft.pending ||
    !!draft.pendingStudy ||
    studyBusy ||
    creating ||
    switching ||
    reuseBusy ||
    !!reusePreview;
  const initialization = useTrainingInitializationSource(
    projectId,
    draft.framework,
    draft.baseModel,
    draft.datasetId,
    draft.initialization,
    selectedBase?.task ?? schema?.task,
  );
  useEffect(() => {
    const captured = state.current.current;
    const pin = initialization.source.data?.pin;
    if (
      !locked &&
      !checking &&
      captured.initialization?.kind === "trained_model" &&
      !captured.initialization.source_digest &&
      !initialization.source.isFetching &&
      initialization.source.data?.eligible &&
      pin &&
      pin.model_version_id === captured.initialization.model_version_id &&
      state.matches(captured)
    ) {
      state.update({ initialization: initializationReference(pin) });
    }
    // Only complete an explicit unpinned selection. Refetches never rewrite a pin or pending body.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialization.source.data, initialization.source.isFetching, locked, checking]);
  const sourceCurrent =
    !draft.source ||
    (!!selectedBase &&
      selectedBase.sha256 === draft.source.baseSha256 &&
      selectedBase.filename === draft.source.baseFilename &&
      template?.image_digest === draft.source.imageDigest &&
      template?.version === draft.source.frameworkVersion &&
      draft.schemaId === draft.source.schemaId &&
      draft.schemaDigest === draft.source.schemaDigest);
  const contextValid =
    !!draft.name.trim() &&
    !!validDataset &&
    !!selectedBase &&
    tasksMatch &&
    schemaMatches &&
    !schemaQuery.isError &&
    !!catalog.data?.enabled &&
    !catalog.isError &&
    sourceCurrent &&
    initialization.valid;
  const valid = contextValid && !Object.keys(collected.errors).length;
  const schemaStillMatches = (snapshot: TrainingDraft) =>
    currentSchema.current?.schema_id === snapshot.schemaId &&
    currentSchema.current?.schema_digest === snapshot.schemaDigest &&
    catalogSchemaMatches(currentSchema.current);
  const switchStillMatches = (preview: SwitchPreview) => {
    const target = client.getQueryData<TrainingParameterSchema>([
      "training-parameter-schema",
      projectId,
      userId,
      queryScope,
      preview.snapshot.framework,
      preview.baseModel,
    ]);
    return (
      state.matches(preview.snapshot) &&
      target?.schema_id === preview.schema.schema_id &&
      target?.schema_digest === preview.schema.schema_digest &&
      catalogSchemaMatches(target) &&
      modelRuntime(preview.snapshot.framework, preview.baseModel) === preview.runtime
    );
  };

  useEffect(() => () => preflightRequest.current?.abort(), []);

  const clearReview = () => {
    preflightRequest.current?.abort();
    preflightRequest.current = null;
    setChecking(false);
    setReport(null);
    setError("");
    setImportPreview(null);
    fileSequence.current += 1;
    setReadingFile(false);
  };
  const update = (patch: Partial<TrainingDraft>) => {
    if (
      createLock.current ||
      studyLock.current ||
      switchLock.current ||
      reuseLock.current ||
      reusePreview ||
      state.current.current.pending ||
      state.current.current.pendingStudy ||
      !state.ownsSession()
    )
      return;
    clearReview();
    state.update(patch);
  };
  const freshAuthority = async (snapshot: TrainingDraft) => {
    const access = await projectsApi.getAccess(projectId);
    if (!state.matches(snapshot)) return false;
    if (
      access.project_id !== projectId ||
      access.user_id !== userId ||
      !access.capabilities?.includes("project.manage")
    )
      throw new Error("当前账号已失去此项目的管理权限");
    return true;
  };

  const reloadCandidate = async (candidate: TrainingReuseCandidate) => {
    const origin = candidate.origin;
    return origin.kind === "recipe"
      ? recipeCandidate(
          await trainingRecipesApi.get(projectId, origin.recipe_id!, origin.revision_id!),
        )
      : trainingRecipesApi.clone(projectId, origin.run_id!, origin.attempt_id ?? undefined);
  };
  const clearCloneIntent = () => {
    const next = new URLSearchParams(params);
    next.delete("clone_run");
    next.delete("clone_attempt");
    setParams(next, { replace: true });
  };
  const previewReuse = async (
    candidate: TrainingReuseCandidate,
    captured = state.current.current,
    intent?: string,
    capturedIntentOwner = intentOwnerRef.current,
  ) => {
    if (
      reuseLock.current ||
      createLock.current ||
      switchLock.current ||
      captured.pending ||
      captured.pendingStudy ||
      preflightRequest.current ||
      readingFile ||
      importPreview ||
      switchPreview ||
      !state.matches(captured) ||
      (intent && (cloneUrl.current !== intent || intentOwnerRef.current !== capturedIntentOwner))
    )
      return;
    reuseLock.current = true;
    setReuseBusy(true);
    setError("");
    try {
      if (!(await freshAuthority(captured))) return;
      const latest = await reloadCandidate(candidate);
      if (
        !state.matches(captured) ||
        (intent && (cloneUrl.current !== intent || intentOwnerRef.current !== capturedIntentOwner))
      )
        return;
      if (!latest.compatible) throw new Error(latest.incompatibility ?? "来源不兼容");
      const value = latest.snapshot as ReusableSnapshot;
      const targetRuntime = modelRuntime(value.framework, value.base_model.id);
      if (!targetRuntime) throw new Error("来源基础权重不在当前目录中");
      const target = await client.fetchQuery({
        queryKey: [
          "training-parameter-schema",
          projectId,
          userId,
          queryScope,
          value.framework,
          value.base_model.id,
        ],
        queryFn: ({ signal }) =>
          trainingRunsApi.parameterSchema(projectId, value.framework, value.base_model.id, {
            signal,
          }),
        staleTime: 0,
      });
      let before: ReusableSnapshot | null = null;
      if (captured.baseModel && schemaStillMatches(captured)) {
        const values = collectOverrides(currentSchema.current!, captured.fields);
        if (Object.keys(values.errors).length) throw new Error("请先修正当前参数，再核对完整差异");
        before = await trainingRecipesApi.resolve(projectId, {
          framework: captured.framework as ReusableSnapshot["framework"],
          base_model: captured.baseModel,
          ...(captured.initialization ? { initialization: captured.initialization } : {}),
          configuration: {
            schema_id: captured.schemaId,
            schema_digest: captured.schemaDigest,
            values: values.values,
          },
        });
      } else if (captured.baseModel) throw new Error("请先核对并迁移当前草稿，再采用配方");
      if (
        !state.matches(captured) ||
        (intent &&
          (cloneUrl.current !== intent || intentOwnerRef.current !== capturedIntentOwner)) ||
        modelRuntime(value.framework, value.base_model.id) !== targetRuntime
      )
        return;
      if (
        target.schema_id !== value.configuration.identity.schema_id ||
        target.schema_digest !== value.configuration.identity.schema_digest ||
        !catalogSchemaMatches(target)
      )
        throw new Error("来源参数定义与当前目录不一致");
      setReusePreview({
        snapshot: captured,
        candidate: latest,
        before,
        schema: target,
        runtime: targetRuntime,
        intent,
        intentOwner: intent ? capturedIntentOwner : undefined,
      });
    } catch (reason) {
      if (
        state.matches(captured) &&
        (!intent || (cloneUrl.current === intent && intentOwnerRef.current === capturedIntentOwner))
      )
        setError(reasonMessage(reason));
    } finally {
      reuseLock.current = false;
      if (state.ownsSession()) setReuseBusy(false);
    }
  };
  const acceptReuse = async () => {
    const preview = reusePreview;
    if (
      !preview ||
      reuseLock.current ||
      createLock.current ||
      state.current.current.pending ||
      !state.matches(preview.snapshot) ||
      (preview.intent &&
        (cloneUrl.current !== preview.intent || intentOwnerRef.current !== preview.intentOwner))
    )
      return;
    reuseLock.current = true;
    setReuseBusy(true);
    setError("");
    try {
      if (!(await freshAuthority(preview.snapshot))) return;
      const latest = await reloadCandidate(preview.candidate);
      const value = latest.snapshot as ReusableSnapshot;
      if (!latest.compatible || latest.snapshot_digest !== preview.candidate.snapshot_digest)
        throw new Error(latest.incompatibility ?? "来源已变化，请重新核对");
      const definition = await trainingRunsApi.parameterSchema(
        projectId,
        value.framework,
        value.base_model.id,
      );
      if (
        !state.matches(preview.snapshot) ||
        (preview.intent &&
          (cloneUrl.current !== preview.intent ||
            intentOwnerRef.current !== preview.intentOwner)) ||
        modelRuntime(value.framework, value.base_model.id) !== preview.runtime
      )
        throw new Error("草稿或目录已变化，请重新核对");
      if (
        !catalogSchemaMatches(definition) ||
        definition.schema_digest !== preview.schema.schema_digest
      )
        throw new Error("参数定义已变化，请重新核对");
      clearReview();
      state.update({
        framework: value.framework,
        baseModel: value.base_model.id,
        schemaId: definition.schema_id,
        schemaDigest: definition.schema_digest,
        fields: Object.fromEntries(
          Object.entries(value.configuration.user_values).map(([key, item]) => [
            key,
            valueToDraft(item),
          ]),
        ),
        source: {
          reference: latest.origin,
          label: latest.label,
          baseSha256: value.base_model.sha256,
          baseFilename: value.base_model.filename,
          imageDigest: value.image_digest,
          frameworkVersion: value.framework_version,
          userValues: value.configuration.user_values,
          schemaId: value.configuration.identity.schema_id,
          schemaDigest: value.configuration.identity.schema_digest,
        },
        initialization: initializationReference(value.initialization),
      });
      setReusePreview(null);
      clearCloneIntent();
    } catch (reason) {
      if (state.matches(preview.snapshot)) setError(reasonMessage(reason));
    } finally {
      reuseLock.current = false;
      if (state.ownsSession()) setReuseBusy(false);
    }
  };
  useEffect(() => {
    if (
      reusePreview?.intent &&
      (reusePreview.intent !== `${cloneRun}:${cloneAttempt}` ||
        reusePreview.intentOwner !== intentOwner) &&
      !reuseLock.current
    )
      setReusePreview(null);
  }, [cloneRun, cloneAttempt, reusePreview, reuseBusy, intentOwner]);
  useEffect(() => {
    const intent = `${cloneRun}:${cloneAttempt}`;
    if (!cloneRun) {
      cloneIntent.current = "";
      return;
    }
    if (
      !cloneRun ||
      !catalog.data ||
      cloneIntent.current === intentOwner.key ||
      draft.pending ||
      draft.pendingStudy ||
      reuseLock.current ||
      reusePreview
    )
      return;
    cloneIntent.current = intentOwner.key;
    const sequence = ++cloneSequence.current;
    const captured = state.current.current;
    setError("");
    void trainingRecipesApi
      .clone(projectId, cloneRun, cloneAttempt || undefined)
      .then((candidate) => {
        if (
          cloneSequence.current === sequence &&
          intentOwnerRef.current === intentOwner &&
          state.matches(captured) &&
          cloneUrl.current === intent
        )
          void previewReuse(candidate, captured, intent, intentOwner);
      })
      .catch((reason) => {
        if (
          cloneSequence.current === sequence &&
          intentOwnerRef.current === intentOwner &&
          state.matches(captured) &&
          cloneUrl.current === intent
        )
          setError(reasonMessage(reason));
      });
    // The intent is captured once; accepting or cancelling removes it from the URL.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    cloneRun,
    cloneAttempt,
    intentOwner,
    catalog.data,
    draft.pending,
    reusePreview,
    reuseBusy,
    cloneRetry,
  ]);

  const chooseModel = async (baseModel: string) => {
    if (!baseModel || locked || switchLock.current || createLock.current) return;
    const snapshot = state.current.current;
    if (!state.matches(snapshot)) return;
    const targetRuntime = modelRuntime(snapshot.framework, baseModel);
    if (!targetRuntime) return;
    switchLock.current = true;
    setSwitching(true);
    setError("");
    try {
      const target = await client.fetchQuery({
        queryKey: [
          "training-parameter-schema",
          projectId,
          userId,
          queryScope,
          snapshot.framework,
          baseModel,
        ],
        queryFn: ({ signal }) =>
          trainingRunsApi.parameterSchema(projectId, snapshot.framework, baseModel, { signal }),
        staleTime: 0,
      });
      if (!state.matches(snapshot) || modelRuntime(snapshot.framework, baseModel) !== targetRuntime)
        return;
      if (!catalogSchemaMatches(target))
        throw new Error("目录的参数定义已变更，请重新加载后再选择基础权重");
      const result = compatibleDrafts(target, snapshot.fields);
      if (
        Object.keys(snapshot.fields).length ||
        (snapshot.schemaId && snapshot.schemaDigest !== target.schema_digest)
      ) {
        setSwitchPreview({
          snapshot,
          schema: target,
          baseModel,
          runtime: targetRuntime,
          ...result,
        });
      } else {
        clearReview();
        state.update({
          baseModel,
          schemaId: target.schema_id,
          schemaDigest: target.schema_digest,
          fields: result.retained,
        });
      }
    } catch (reason) {
      if (state.matches(snapshot)) setError(reasonMessage(reason));
    } finally {
      switchLock.current = false;
      if (state.ownsSession()) setSwitching(false);
    }
  };

  const preflight = async () => {
    if (
      !valid ||
      locked ||
      preflightRequest.current ||
      createLock.current ||
      state.current.current.pending
    )
      return;
    const snapshot = state.current.current;
    if (!schema || !state.matches(snapshot)) return;
    const body: TrainingRunCreate = {
      name: snapshot.name.trim(),
      task: schema.task as TrainingRunCreate["task"],
      dataset_id: snapshot.datasetId,
      framework: snapshot.framework as TrainingRunCreate["framework"],
      base_model: snapshot.baseModel,
      request_id: snapshot.requestId,
      configuration: {
        schema_id: snapshot.schemaId,
        schema_digest: snapshot.schemaDigest,
        values: collectOverrides(schema, snapshot.fields).values,
      },
      ...(snapshot.source ? { origin: snapshot.source.reference } : {}),
      ...(snapshot.initialization ? { initialization: snapshot.initialization } : {}),
    };
    const capturedRuntime = runtime;
    const controller = new AbortController();
    preflightRequest.current = controller;
    setChecking(true);
    setError("");
    setReport(null);
    try {
      if (
        !(await freshAuthority(snapshot)) ||
        controller.signal.aborted ||
        runtimeRef.current !== capturedRuntime ||
        !schemaStillMatches(snapshot)
      )
        return;
      const value = await trainingRunsApi.preflight(projectId, body, { signal: controller.signal });
      if (
        state.matches(snapshot) &&
        !controller.signal.aborted &&
        runtimeRef.current === capturedRuntime &&
        schemaStillMatches(snapshot)
      )
        setReport({ ...value, snapshot, body, runtime: capturedRuntime });
    } catch (reason) {
      if (
        state.matches(snapshot) &&
        !controller.signal.aborted &&
        runtimeRef.current === capturedRuntime &&
        schemaStillMatches(snapshot)
      )
        setError(reasonMessage(reason));
    } finally {
      if (preflightRequest.current === controller) {
        preflightRequest.current = null;
        if (state.ownsSession()) setChecking(false);
      }
    }
  };

  const create = async () => {
    if (
      createLock.current ||
      studyLock.current ||
      state.current.current.pendingStudy ||
      preflightRequest.current ||
      reuseLock.current ||
      reusePreview
    )
      return;
    const snapshot = state.current.current;
    if (!state.matches(snapshot)) return;
    const body =
      snapshot.pending?.body ??
      (reviewed && valid ? { ...reviewed.body, preflight_digest: reviewed.config_digest } : null);
    if (!body) return;
    createLock.current = true;
    setCreating(true);
    setError("");
    try {
      if (!(await freshAuthority(snapshot))) return;
      if (
        !snapshot.pending &&
        (!report || report.runtime !== runtimeRef.current || !schemaStillMatches(snapshot))
      )
        return;
      if (!state.commit({ ...snapshot, pending: { body } })) return;
      const run = await trainingRunsApi.create(projectId, body);
      if (!state.matches(snapshot)) return;
      state.remove(snapshot);
      void client.invalidateQueries({ queryKey: ["training-runs", projectId] });
      void client.invalidateQueries({ queryKey: ["training-hub"] });
      navigate(`/projects/${projectId}/learning?tab=training&run=${encodeURIComponent(run.id)}`, {
        replace: true,
      });
    } catch (reason) {
      if (state.matches(snapshot)) setError(reasonMessage(reason));
    } finally {
      createLock.current = false;
      if (state.ownsSession()) setCreating(false);
    }
  };

  const discardPending = async () => {
    const snapshot = state.current.current;
    if (creating || studyBusy || (!snapshot.pending && !snapshot.pendingStudy)) return;
    const accepted = await confirmDialog({
      title: "放弃尚未确认的创建请求",
      description:
        "服务器可能已经创建实验。请先检查实验列表；放弃后再提交会使用新请求，可能产生另一个实验。",
      confirmLabel: "放弃此请求并编辑",
      tone: "danger",
    });
    if (accepted && state.matches(snapshot) && !createLock.current) {
      clearReview();
      state.update({ pending: undefined });
    }
  };

  const readFile = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file || !schema || locked || !schemaMatches) return;
    const snapshot = state.current.current;
    const sequence = ++fileSequence.current;
    setError("");
    if (file.size > CONFIGURATION_FILE_LIMIT) {
      setError("请选择不超过 64 KiB 的 YAML 文件");
      return;
    }
    setReadingFile(true);
    try {
      const source = await file.text();
      if (
        sequence !== fileSequence.current ||
        !state.matches(snapshot) ||
        state.current.current.pending ||
        state.current.current.pendingStudy ||
        currentSchema.current?.schema_digest !== schema.schema_digest
      )
        return;
      setImportPreview({
        snapshot,
        ...importConfiguration(source, schema, snapshot.baseModel, template?.image_digest ?? ""),
      });
    } catch (reason) {
      if (sequence === fileSequence.current && state.matches(snapshot))
        setError(reasonMessage(reason));
    } finally {
      if (sequence === fileSequence.current && state.ownsSession()) setReadingFile(false);
    }
  };

  const exportFile = () => {
    if (!schema || !valid || locked) return;
    const source = portableConfiguration(
      schema,
      draft.baseModel,
      template?.image_digest ?? "",
      collected.values,
      draft.initialization,
    );
    const url = URL.createObjectURL(new Blob([source], { type: "application/yaml;charset=utf-8" }));
    try {
      const link = document.createElement("a");
      link.href = url;
      link.download = `training-${draft.framework}-${draft.baseModel}.yaml`;
      link.click();
    } finally {
      URL.revokeObjectURL(url);
    }
  };

  const importChanges = importPreview
    ? [...new Set([...Object.keys(draft.fields), ...Object.keys(importPreview.values)])].map(
        (key) => {
          const field = schema?.fields.find((item) => item.key === key);
          const old = collected.values[key] ?? field?.default;
          const next = key in importPreview.values ? importPreview.values[key] : field?.default;
          return {
            key,
            label: field?.label ?? key,
            old,
            next,
            reset: !(key in importPreview.values),
          };
        },
      )
    : [];

  return (
    <PageContainer className="pb-24 max-[900px]:pb-24">
      <Link
        to={back}
        className="mb-4 inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="size-3.5" />
        返回训练实验
      </Link>
      <header className="mb-5 flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="mb-1 text-xs text-muted-foreground">
            {project.data?.name ?? "项目"} / 训练实验
          </p>
          <h1 className="text-lg font-semibold">新建训练实验</h1>
          <p className="mt-1 text-xs text-muted-foreground">
            选择冻结数据，配置训练参数，核对后加入队列。
          </p>
        </div>
        <Badge variant={state.storageError ? "warning" : "default"}>
          {state.storageError ? "草稿保存异常" : state.restored ? "已恢复本地草稿" : "本地草稿"}
        </Badge>
      </header>
      <nav
        aria-label="创建步骤"
        className="mb-5 flex gap-1 rounded-lg border border-border bg-card p-1"
      >
        {steps.map((label, index) => (
          <button
            type="button"
            key={label}
            aria-current={step === index ? "step" : undefined}
            disabled={(index > 0 && !contextValid) || studyBusy}
            onClick={() => setStep(index)}
            className={`flex min-w-0 flex-1 items-center justify-center gap-2 rounded-md px-2 py-2.5 text-xs focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50 ${step === index ? "bg-muted font-medium" : "text-muted-foreground hover:bg-muted"}`}
          >
            <span className="hidden sm:inline">{index + 1}.</span>
            {label}
            {index === 2 && reviewed && <Check className="size-3" />}
          </button>
        ))}
      </nav>
      {(error ||
        state.storageError ||
        catalog.error ||
        datasets.error ||
        selectedDataset.error ||
        schemaQuery.error) && (
        <p
          role="alert"
          className="mb-4 rounded-lg bg-status-danger-soft p-3 text-sm text-status-danger"
        >
          {error ||
            state.storageError ||
            catalog.error?.message ||
            datasets.error?.message ||
            selectedDataset.error?.message ||
            schemaQuery.error?.message}
        </p>
      )}
      {draft.pending && (
        <section
          aria-label="尚未确认的创建请求"
          className="mb-5 rounded-lg border border-status-caution bg-status-caution-soft p-4"
        >
          <h2 className="text-sm font-medium text-status-caution">创建结果尚未确认</h2>
          <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
            请求内容已保留。重试会使用完全相同的请求身份；确认结果前，配置暂时锁定。也可先返回实验列表检查。
          </p>
          <div className="mt-3 flex flex-wrap gap-2">
            <Button size="sm" disabled={creating} onClick={() => void create()}>
              {creating ? "确认中…" : "重试并确认创建"}
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={creating}
              onClick={() => void discardPending()}
            >
              放弃此请求并编辑
            </Button>
          </div>
        </section>
      )}
      {report && !reviewed && (
        <p
          role="status"
          className="mb-4 rounded-lg bg-status-caution-soft p-3 text-xs text-status-caution"
        >
          训练目录或配置有变化，请重新检查。
        </p>
      )}
      {schema && !schemaMatches && (
        <section className="mb-4 rounded-lg bg-status-caution-soft p-4 text-xs">
          <p className="text-status-caution">草稿的参数定义已变更。核对兼容设置后才能继续。</p>
          <Button
            className="mt-3"
            size="sm"
            disabled={locked}
            onClick={() => void chooseModel(draft.baseModel)}
          >
            核对并迁移草稿
          </Button>
        </section>
      )}
      {draft.source && (
        <section
          className="mb-4 rounded-lg border border-border bg-card p-4 text-xs"
          aria-label="配置来源"
        >
          <p className="break-words font-medium">
            基于 {draft.source.label}
            {!sameValues(collected.values, draft.source.userValues) ? " · 已修改参数" : ""}
          </p>
          <TrainingRecipeStatus projectId={projectId} origin={draft.source.reference} />
          <p className="mt-2 break-all text-muted-foreground">
            原始基础权重 SHA256：{draft.source.baseSha256}
          </p>
          {!sourceCurrent && (
            <p role="alert" className="mt-2 text-status-danger">
              基础权重或训练器身份已变化。请明确解除来源并核对新配置，或等待原始受控资源恢复。
            </p>
          )}
          <Button
            size="sm"
            variant="ghost"
            className="mt-2"
            disabled={locked}
            onClick={() => update({ source: undefined })}
          >
            解除配置来源
          </Button>
        </section>
      )}
      {cloneRun && !reusePreview && (
        <div className="mb-4 flex flex-wrap gap-2">
          <Button
            size="sm"
            variant="default"
            disabled={locked || checking || readingFile || !!importPreview || !!switchPreview}
            onClick={() => {
              cloneIntent.current = "";
              setCloneRetry((value) => value + 1);
            }}
          >
            重新核对克隆来源
          </Button>
          <Button
            size="sm"
            variant="ghost"
            disabled={reuseBusy || !!draft.pending}
            onClick={clearCloneIntent}
          >
            取消克隆意图
          </Button>
        </div>
      )}
      <TrainingRecipeLibrary
        projectId={projectId}
        disabled={locked || checking || readingFile || !!importPreview || !!switchPreview}
        onChoose={(candidate) => void previewReuse(candidate)}
      />
      <div className="grid min-w-0 items-start gap-5 xl:grid-cols-[minmax(0,1fr)_280px]">
        <div className="min-w-0">
          {step === 0 && (
            <section
              aria-label="数据与模型"
              className="rounded-xl border border-border bg-card p-5"
            >
              <h2 className="mb-4 text-sm font-semibold">实验、训练数据与基础权重</h2>
              <fieldset disabled={locked} className="grid min-w-0 gap-5 sm:grid-cols-2">
                <label className="text-xs text-muted-foreground">
                  实验名称
                  <input
                    aria-label="实验名称"
                    className={`${trainingInputClass} mt-1.5`}
                    value={draft.name}
                    maxLength={120}
                    onChange={(event) => update({ name: event.target.value })}
                  />
                </label>
                <label className="text-xs text-muted-foreground">
                  训练数据
                  <select
                    aria-label="训练数据"
                    className={`${trainingInputClass} mt-1.5`}
                    value={draft.datasetId}
                    onChange={(event) => update({ datasetId: event.target.value })}
                  >
                    <option value="">选择已就绪的数据版本</option>
                    {draft.datasetId && !choices.some((item) => item.id === draft.datasetId) && (
                      <option value={draft.datasetId}>正在核验原数据版本…</option>
                    )}
                    {choices.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name}
                        {` · ${trainingTaskLabel(item.task)}`}
                        {item.status !== "ready" ? "（尚未就绪）" : ""}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="text-xs text-muted-foreground">
                  训练器
                  <select
                    aria-label="训练器"
                    className={`${trainingInputClass} mt-1.5`}
                    value={draft.framework}
                    onChange={(event) => update({ framework: event.target.value, baseModel: "" })}
                  >
                    {!template && (
                      <option value={draft.framework}>{draft.framework}（目录中不可用）</option>
                    )}
                    {catalog.data?.items.map((item) => (
                      <option
                        value={item.framework}
                        key={item.framework}
                        disabled={
                          validDataset &&
                          !item.base_models.some((base) => (base.task ?? "detect") === datasetTask)
                        }
                      >
                        {item.framework} {item.version}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="text-xs text-muted-foreground">
                  基础权重
                  <select
                    aria-label="基础权重"
                    className={`${trainingInputClass} mt-1.5`}
                    value={draft.baseModel}
                    onChange={(event) => void chooseModel(event.target.value)}
                  >
                    <option value="">选择基础权重</option>
                    {draft.baseModel &&
                      (!selectedBase ||
                        (validDataset && (selectedBase.task ?? "detect") !== datasetTask)) && (
                        <option value={draft.baseModel} disabled>
                          {draft.baseModel}（目录不可用或与数据任务不匹配）
                        </option>
                      )}
                    {template?.base_models
                      .filter((item) => !validDataset || (item.task ?? "detect") === datasetTask)
                      .map((item) => (
                        <option value={item.id} key={item.id}>
                          {item.id} · {trainingTaskLabel(item.task)}
                        </option>
                      ))}
                  </select>
                </label>
              </fieldset>
              <TrainingInitializationSource
                projectId={projectId}
                value={draft.initialization}
                disabled={locked || checking}
                qualification={initialization}
                onChange={(value) => update({ initialization: value })}
              />
              {datasets.hasNextPage && (
                <Button
                  className="mt-4"
                  size="sm"
                  disabled={locked || datasets.isFetchingNextPage}
                  onClick={() => void datasets.fetchNextPage()}
                >
                  {datasets.isFetchingNextPage ? "加载中…" : "加载更早的数据版本"}
                </Button>
              )}
              {datasets.isSuccess && !datasets.data.pages[0]?.total && (
                <p className="mt-4 text-xs text-status-caution">
                  暂无就绪数据版本，请先在「训练数据」中构建训练包。
                </p>
              )}
              {draft.datasetId && selectedDataset.isSuccess && !validDataset && (
                <p className="mt-4 text-xs text-status-danger">
                  此数据版本不可用于训练，请重新选择就绪版本。
                </p>
              )}
              {validDataset && selectedBase && schema && !tasksMatch && (
                <p role="alert" className="mt-4 text-xs text-status-danger">
                  数据版本、基础权重与参数定义的训练任务不匹配。请明确选择对应任务的权重或数据；当前配置已保留。
                </p>
              )}
              {segmentation && (
                <p className="mt-4 text-xs leading-relaxed text-muted-foreground">
                  实例分割支持单多边形原生训练、检查点续训和产物下载。模型回流、项目权重初始化、增强预览与参数搜索尚未开放。
                </p>
              )}
              <p className="mt-5 text-xs leading-relaxed text-muted-foreground">
                切换训练器后请明确选择基础权重。已有参数会保留，选择权重时将列出兼容与需要移除的设置。
              </p>
              <Link
                to={`/projects/${projectId}/learning?tab=data`}
                className="mt-3 inline-flex text-xs text-primary hover:underline"
              >
                管理训练数据版本
              </Link>
            </section>
          )}
          {step === 1 && schema && (
            <section aria-label="配置参数">
              <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
                <h2 className="text-sm font-semibold">训练参数</h2>
                <div className="flex flex-wrap gap-2">
                  <TrainingRecipeSave
                    projectId={projectId}
                    disabled={
                      locked ||
                      !schemaMatches ||
                      !sourceCurrent ||
                      !initialization.valid ||
                      !!Object.keys(collected.errors).length
                    }
                    source={{
                      configuration: {
                        framework: draft.framework as ReusableSnapshot["framework"],
                        base_model: draft.baseModel,
                        ...(draft.initialization ? { initialization: draft.initialization } : {}),
                        configuration: {
                          schema_id: draft.schemaId,
                          schema_digest: draft.schemaDigest,
                          values: collected.values,
                        },
                      },
                      ...(draft.source ? { origin: draft.source.reference } : {}),
                    }}
                  />
                  <input
                    ref={fileInput}
                    aria-label="导入 YAML 文件"
                    type="file"
                    accept=".yaml,.yml,application/yaml,text/yaml"
                    className="sr-only"
                    onChange={(event) => void readFile(event)}
                  />
                  <Button
                    size="sm"
                    disabled={locked || readingFile || !schemaMatches}
                    onClick={() => fileInput.current?.click()}
                  >
                    <FileUp className="size-3.5" />
                    {readingFile ? "读取中…" : "导入 YAML"}
                  </Button>
                  <Button size="sm" disabled={locked || !valid} onClick={exportFile}>
                    <Download className="size-3.5" />
                    导出 YAML
                  </Button>
                </div>
              </div>
              <TrainingConfigurationEditor
                schema={schema}
                fields={draft.fields}
                disabled={locked || !schemaMatches}
                onChange={(key, value) =>
                  update({ fields: { ...state.current.current.fields, [key]: value } })
                }
                onReset={(keys) => {
                  const fields = { ...state.current.current.fields };
                  keys.forEach((key) => delete fields[key]);
                  update({ fields });
                }}
              />
              <TrainingAugmentationPreview
                projectId={projectId}
                input={{
                  dataset_id: draft.datasetId,
                  framework: draft.framework as "ultralytics" | "libreyolo",
                  base_model: draft.baseModel,
                  configuration: {
                    schema_id: draft.schemaId,
                    schema_digest: draft.schemaDigest,
                    values: collected.values,
                  },
                }}
                binding={JSON.stringify([
                  draft.revision,
                  runtime,
                  selectedDataset.data?.snapshot_digest,
                  selectedDataset.data?.package_sha256,
                  selectedDataset.data?.manifest?.content_digest,
                ])}
                epochs={Number(
                  collected.values.epochs ??
                    schema.fields.find((field) => field.key === "epochs")?.default ??
                    50,
                )}
                classes={
                  Array.isArray(selectedDataset.data?.manifest?.classes)
                    ? selectedDataset.data.manifest.classes.map(String)
                    : []
                }
                disabled={locked || !valid}
              />
              {draft.initialization?.kind === "trained_model" && (
                <p className="mt-3 text-xs text-muted-foreground">
                  增强预览仅验证数据变换，使用受控基础模型结构；项目权重的完整校验将在训练预检与执行时进行。
                </p>
              )}
              <details
                className="mt-5 rounded-lg border border-border p-4 text-xs"
                onToggle={(event) => setInventoryOpen(event.currentTarget.open)}
              >
                <summary className="cursor-pointer font-medium">完整原生目录与支持边界</summary>
                {inventoryOpen && (
                  <div className="mt-4">
                    <TrainingParameterInventory
                      projectId={projectId}
                      framework={draft.framework}
                      baseModel={draft.baseModel}
                    />
                  </div>
                )}
              </details>
            </section>
          )}
          {step === 2 && (schema || draft.pendingStudy) && (
            <section aria-label="核对并开始">
              <h2 className="mb-4 text-sm font-semibold">核对训练配置</h2>
              {Object.keys(collected.errors).length > 0 && (
                <p role="alert" className="mb-4 text-xs text-status-danger">
                  有 {Object.keys(collected.errors).length} 项参数需要修正，请返回配置参数查看。
                </p>
              )}
              {schema && (
                <TrainingRunReview
                  schema={schema}
                  values={collected.values}
                  config={reviewed?.config}
                  digest={reviewed?.config_digest}
                />
              )}
              <fieldset
                disabled={locked || checking}
                className="mt-4 flex flex-wrap gap-4 text-xs"
                aria-label="开始方式"
              >
                <label className="flex items-center gap-2">
                  <input
                    type="radio"
                    name="training-start-mode"
                    checked={!studyMode}
                    onChange={() => setStudyMode(false)}
                  />
                  单个训练实验
                </label>
                <label className="flex items-center gap-2">
                  <input
                    type="radio"
                    name="training-start-mode"
                    disabled={segmentation}
                    checked={studyMode}
                    onChange={() => setStudyMode(true)}
                  />
                  有预算的参数搜索
                </label>
              </fieldset>
              {segmentation && (
                <p className="mt-2 text-xs text-muted-foreground">
                  参数搜索目前仅支持图片检测；分割请使用单个训练实验。
                </p>
              )}
              {studyMode && (
                <TrainingStudyCreate
                  key={draft.requestId}
                  projectId={projectId}
                  schema={schema}
                  base={
                    reviewed && valid
                      ? { ...reviewed.body, preflight_digest: reviewed.config_digest }
                      : undefined
                  }
                  pending={draft.pendingStudy}
                  onBusy={(value) => {
                    studyLock.current = value;
                    setStudyBusy(value);
                  }}
                  beforeStart={async () => {
                    if (
                      createLock.current ||
                      preflightRequest.current ||
                      reuseLock.current ||
                      reusePreview ||
                      switchLock.current ||
                      readingFile ||
                      switchPreview ||
                      importPreview
                    )
                      return false;
                    const snapshot = state.current.current;
                    if (
                      !snapshot.pendingStudy &&
                      (!reviewed ||
                        !valid ||
                        reviewed.runtime !== runtimeRef.current ||
                        !schemaStillMatches(snapshot))
                    )
                      return false;
                    return freshAuthority(snapshot);
                  }}
                  persist={(body) => state.commit({ ...state.current.current, pendingStudy: body })}
                  onCreated={(studyId) => {
                    const snapshot = state.current.current;
                    if (!state.matches(snapshot)) return;
                    state.remove(snapshot);
                    void client.invalidateQueries({ queryKey: ["training-studies", projectId] });
                    void client.invalidateQueries({ queryKey: ["training-runs", projectId] });
                    navigate(
                      `/projects/${projectId}/learning?tab=training&study=${encodeURIComponent(studyId)}`,
                      { replace: true },
                    );
                  }}
                  onDiscard={() => void discardPending()}
                />
              )}
            </section>
          )}
        </div>
        <aside
          aria-label="实验摘要"
          className="min-w-0 rounded-xl border border-border bg-card p-4 xl:sticky xl:top-4"
        >
          <div className="mb-4 flex items-center gap-2">
            <FlaskConical className="size-4 text-muted-foreground" />
            <h2 className="text-sm font-medium">实验摘要</h2>
          </div>
          <dl className="space-y-3 text-xs">
            {[
              ["实验名称", draft.name || "尚未填写"],
              ["训练数据", selectedDataset.data?.name ?? "尚未选择"],
              ["训练器", draft.framework],
              ["基础权重", draft.baseModel || "尚未选择"],
              [
                "初始化",
                draft.initialization?.kind === "trained_model"
                  ? (initialization.source.data?.pin?.model_name ??
                    draft.initialization.model_version_id ??
                    "来源待核验")
                  : "受控目录基础权重",
              ],
              ["参数覆盖", `${Object.keys(draft.fields).length} 项`],
              ["检查状态", reviewed ? "已通过" : "尚未检查"],
            ].map(([label, value]) => (
              <div key={label}>
                <dt className="text-muted-foreground">{label}</dt>
                <dd className="mt-1 break-words font-medium">{value}</dd>
              </div>
            ))}
          </dl>
          <p className="mt-4 border-t border-border pt-3 text-xs leading-relaxed text-muted-foreground">
            {catalog.data?.online
              ? catalog.data.busy
                ? "执行器正忙，新实验将排队。"
                : "执行器在线。"
              : "执行器离线，创建后将等待执行器。"}{" "}
            使用固定验证集；取消后可从已保存的完整轮次续训。
          </p>
        </aside>
      </div>
      <div className="sticky bottom-0 z-popover -mx-4 mt-5 flex flex-wrap items-center justify-between gap-2 border-t border-border bg-background/95 py-3 pl-4 pr-16 backdrop-blur-overlay sm:mx-0 sm:rounded-lg sm:border">
        <span className="text-xs text-muted-foreground">
          第 {step + 1} / 3 步{checking ? " · 正在检查" : ""}
        </span>
        <div className="flex flex-wrap gap-2">
          {step > 0 && (
            <Button size="sm" disabled={studyBusy} onClick={() => setStep(step - 1)}>
              上一步
            </Button>
          )}
          {step < 2 ? (
            <Button
              size="sm"
              variant="primary"
              disabled={!contextValid || locked}
              onClick={() => setStep(step + 1)}
            >
              下一步
            </Button>
          ) : (
            <>
              <Button
                size="sm"
                disabled={!valid || locked || checking}
                onClick={() => void preflight()}
              >
                {checking ? "检查中…" : "检查训练配置"}
              </Button>
              {!studyMode && (
                <Button
                  size="sm"
                  variant="primary"
                  disabled={!valid || !reviewed || locked || checking}
                  onClick={() => void create()}
                >
                  {creating ? "提交中…" : "开始训练"}
                </Button>
              )}
            </>
          )}
        </div>
      </div>
      <Modal
        open={!!switchPreview}
        onClose={() => setSwitchPreview(null)}
        title="核对参数切换"
        width={600}
      >
        {switchPreview && (
          <>
            <p className="text-sm">
              目标：{switchPreview.schema.framework} / {switchPreview.baseModel}
            </p>
            <p className="mt-2 text-xs text-muted-foreground">
              实验名称和训练数据会保留。请确认下列参数处理。
            </p>
            <div className="mt-4 space-y-3 text-xs">
              <p>
                保留 {Object.keys(switchPreview.retained).length} 项：
                {Object.keys(switchPreview.retained).join("、") || "无"}
              </p>
              <p
                className={
                  switchPreview.discarded.length ? "text-status-caution" : "text-muted-foreground"
                }
              >
                移除 {switchPreview.discarded.length} 项：
                {switchPreview.discarded.join("、") || "无"}
              </p>
            </div>
            <div className="mt-5 flex justify-end gap-2">
              <Button size="sm" onClick={() => setSwitchPreview(null)}>
                取消
              </Button>
              <Button
                size="sm"
                variant="primary"
                onClick={() => {
                  const preview = switchPreview;
                  setSwitchPreview(null);
                  if (
                    switchStillMatches(preview) &&
                    !state.current.current.pending &&
                    !createLock.current
                  ) {
                    clearReview();
                    state.update({
                      baseModel: preview.baseModel,
                      schemaId: preview.schema.schema_id,
                      schemaDigest: preview.schema.schema_digest,
                      fields: preview.retained,
                    });
                  }
                }}
              >
                接受切换
              </Button>
            </div>
          </>
        )}
      </Modal>
      <Modal
        open={!!importPreview}
        onClose={() => setImportPreview(null)}
        title="核对 YAML 导入"
        width={680}
      >
        {importPreview && (
          <>
            <p className="text-xs leading-relaxed text-muted-foreground">
              导入会替换全部参数覆盖，文件中省略的已修改项将恢复默认值。不会创建实验。
            </p>
            {importPreview.errors.map((message, index) => (
              <p role="alert" key={index} className="mt-3 break-words text-xs text-status-danger">
                {message}
              </p>
            ))}
            {importPreview.warnings.map((message, index) => (
              <p key={index} className="mt-3 text-xs text-status-caution">
                {message}
              </p>
            ))}
            {importPreview.initialization && (
              <p className="mt-3 break-all text-xs text-status-caution">
                文件将初始化来源改为项目候选 {importPreview.initialization.model_version_id}，摘要{" "}
                {importPreview.initialization.source_digest}。应用后须重新核验当前项目的来源资格。
              </p>
            )}
            <div className="mt-4 divide-y divide-border">
              {importChanges.map((change) => (
                <div key={change.key} className="py-3 text-xs">
                  <p className="font-medium">
                    {change.label} <code className="text-muted-foreground">{change.key}</code>
                  </p>
                  <p className="mt-1 break-all font-mono text-muted-foreground">
                    {trainingValueLabel(change.old)} → {trainingValueLabel(change.next)}
                    {change.reset ? "（恢复默认）" : ""}
                  </p>
                </div>
              ))}
            </div>
            {!importChanges.length && (
              <p className="mt-3 text-xs text-muted-foreground">
                没有参数覆盖，将使用全部受控默认值。
              </p>
            )}
            <div className="mt-5 flex justify-end gap-2">
              <Button size="sm" onClick={() => setImportPreview(null)}>
                取消
              </Button>
              <Button
                size="sm"
                variant="primary"
                disabled={
                  !!importPreview.errors.length ||
                  !schemaMatches ||
                  !state.matches(importPreview.snapshot) ||
                  !!draft.pending ||
                  creating
                }
                onClick={() => {
                  const preview = importPreview;
                  if (!preview.errors.length && schemaMatches && state.matches(preview.snapshot))
                    update({
                      fields: Object.fromEntries(
                        Object.entries(preview.values).map(([key, value]) => [
                          key,
                          valueToDraft(value),
                        ]),
                      ),
                      ...(preview.initialization ? { initialization: preview.initialization } : {}),
                    });
                }}
              >
                应用导入
              </Button>
            </div>
          </>
        )}
      </Modal>
      {reusePreview && (
        <TrainingReuseDialog
          candidate={reusePreview.candidate}
          before={reusePreview.before}
          busy={reuseBusy}
          error={error}
          onCancel={() => {
            setReusePreview(null);
            setError("");
            clearCloneIntent();
          }}
          onAccept={() => void acceptReuse()}
        />
      )}
    </PageContainer>
  );
}

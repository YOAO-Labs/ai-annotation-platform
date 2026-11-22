import { Suspense, lazy, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Icon } from "@/components/ui/Icon";
import { TabRow } from "@/components/ui/TabRow";
import { useToastStore } from "@/components/ui/Toast";
import { useProject, useUpdateProject } from "@/hooks/useProjects";
import {
  useApplyProjectPipeline,
  useCreateProjectPipeline,
  useDeleteProjectPipeline,
  useProjectPipelines,
} from "@/hooks/useProjectPipelines";
import { useBatches } from "@/hooks/useBatches";
import { useBatchEventsSocket } from "@/hooks/useBatchEventsSocket";
import { useMLBackends } from "@/hooks/useMLBackends";
import {
  useTriggerPreannotation,
  usePreannotationProgress,
  type PredictMode,
  type PipelineStagePayload,
  type PipelineStageStat,
  type PipelineSource,
} from "@/hooks/usePreannotation";
import { useAsyncJob } from "@/hooks/useAsyncJob";
import { batchPreannotationEligible } from "@/utils/batchPreannotation";
import type { PreannotateArgs } from "./usePreannotateConfig";
import { adminPreannotateApi } from "@/api/adminPreannotate";
import type { ProjectPipeline, ProjectPipelineScope } from "@/api/projectPipelines";
import { HistoryTable } from "./HistoryTable";
import { PredictionImportWizard } from "@/components/predictions/PredictionImportWizard";
import { usePreannotateConfig } from "./usePreannotateConfig";
import { PreannotateConfigForm } from "./PreannotateConfigForm";
import { StageCard } from "./StageCard";
import { usePipelineComposer } from "../hooks/usePipelineComposer";
import {
  ROOT_SID,
  SOURCE_SID,
  classFilterText,
  sourceNodeShape,
  detailOf,
  producesGeometry,
  roiText,
  roleOf,
  stageWarning,
  variantText,
  type GraphNodeModel,
} from "../utils/pipelineGraph";
import styles from "./ProjectDetailPanel.module.css";

const PipelineGraphCanvas = lazy(() => import("./PipelineGraphCanvas"));

const PREDICT_MODE_TABS = ["跳过已预标", "覆盖", "追加"];
const PREDICT_MODE_LABELS: Record<PredictMode, string> = {
  skip_predicted: "跳过已预标",
  overwrite: "覆盖",
  append: "追加",
};
const PREDICT_MODE_BY_LABEL: Record<string, PredictMode> = {
  跳过已预标: "skip_predicted",
  覆盖: "overwrite",
  追加: "append",
};

const PIPELINE_SCOPE_LABELS: Record<ProjectPipelineScope, string> = {
  private: "项目私有",
  organization: "组织",
  public: "公共",
};

type ConcurrencyMode = "serial" | "parallel";

function cx(...classNames: Array<string | false | null | undefined>) {
  return classNames.filter(Boolean).join(" ");
}

function describePipelineError(error: unknown): string {
  const detailRaw = (error as { detailRaw?: unknown })?.detailRaw;
  if (
    detailRaw &&
    typeof detailRaw === "object" &&
    "unenabled_backends" in detailRaw &&
    Array.isArray((detailRaw as { unenabled_backends?: unknown }).unenabled_backends)
  ) {
    const ids = (detailRaw as { unenabled_backends: string[] }).unenabled_backends;
    return `未启用模型服务: ${ids.join("、")}`;
  }
  return (error as Error).message;
}

function argsToStage(
  args: PreannotateArgs,
  stage: number,
  extra: Partial<PipelineStagePayload> = {},
): PipelineStagePayload {
  return {
    stage,
    ml_backend_id: args.ml_backend_id,
    model_id: args.model_id,
    task: args.task,
    model_variants: args.model_variants,
    params: args.params,
    class_filter: args.class_filter,
    ...extra,
  };
}

interface Props {
  initialCatalogModelId?: string | null;
  projectId: string;
  onBack: () => void;
  summary?: {
    project_name: string;
    project_display_id?: string | null;
    data_type?: string | null;
    ml_backend_id?: string | null;
    ml_backend_name?: string | null;
    ml_backend_state?: string | null;
    ml_backend_max_concurrency?: number | null;
  };
}

export function ProjectDetailPanel({ projectId, onBack, summary, initialCatalogModelId }: Props) {
  const pushToast = useToastStore((s) => s.push);
  const navigate = useNavigate();

  useBatchEventsSocket(projectId);

  const projectQ = useProject(projectId);
  const project = projectQ.data as unknown as
    | {
        type_key?: string;
        data_type?: string | null;
        ml_backend_id?: string | null;
        classes_config?: Record<string, { alias?: string | null }>;
        attribute_schema?: { fields?: Array<{ key: string; label?: string }> };
        box_threshold?: number | null;
        text_threshold?: number | null;
        default_variants?: Record<string, Record<string, string>>;
        preannotate_pipeline?: PipelineStagePayload[] | null;
      }
    | undefined;
  const updateProject = useUpdateProject(projectId);
  const createProjectPipeline = useCreateProjectPipeline();
  const applyProjectPipeline = useApplyProjectPipeline(projectId);
  const deleteProjectPipeline = useDeleteProjectPipeline();
  const projectPipelinesQ = useProjectPipelines(undefined, { enabled: !!projectId });
  const projectPipelines = useMemo(() => projectPipelinesQ.data ?? [], [projectPipelinesQ.data]);
  const defaultProjectPipeline = useMemo(
    () =>
      projectPipelines.find(
        (p) => p.scope === "private" && p.project_id === projectId && p.is_default,
      ) ?? null,
    [projectPipelines, projectId],
  );
  const savedPipeline = defaultProjectPipeline?.stages ?? project?.preannotate_pipeline ?? null;
  const savedStageCount = savedPipeline?.length ?? 0;
  const savedPipelineName =
    defaultProjectPipeline?.name ?? (savedStageCount > 0 ? "旧项目编排" : null);
  const [pipelineName, setPipelineName] = useState("");
  const [pipelineScope, setPipelineScope] = useState<ProjectPipelineScope>("private");
  const [selectedLibraryPipelineId, setSelectedLibraryPipelineId] = useState("");
  const libraryPipelines = useMemo(
    () => projectPipelines.filter((p) => (p.stages?.length ?? 0) > 0),
    [projectPipelines],
  );
  const selectedLibraryPipeline = useMemo(
    () => libraryPipelines.find((p) => p.id === selectedLibraryPipelineId) ?? null,
    [libraryPipelines, selectedLibraryPipelineId],
  );
  useEffect(() => {
    if (libraryPipelines.length === 0) {
      if (selectedLibraryPipelineId) setSelectedLibraryPipelineId("");
      return;
    }
    if (
      !selectedLibraryPipelineId ||
      !libraryPipelines.some((p) => p.id === selectedLibraryPipelineId)
    ) {
      setSelectedLibraryPipelineId(libraryPipelines[0].id);
    }
  }, [libraryPipelines, selectedLibraryPipelineId]);
  const dataType = summary?.data_type ?? project?.data_type ?? "image";
  const [executionUnit, setExecutionUnit] = useState<"video" | "frame">("video");

  const backendsQ = useMLBackends(projectId);
  const backends = useMemo(
    () => (backendsQ.data ?? []) as unknown as Array<{ id: string; name: string }>,
    [backendsQ.data],
  );
  const [selectedBackendId, setSelectedBackendId] = useState<string | null>(null);
  const firstBackendId = backends[0]?.id ?? null;
  useEffect(() => {
    setSelectedBackendId(project?.ml_backend_id ?? firstBackendId);
  }, [projectId, project?.ml_backend_id, firstBackendId]);
  const selectedBackend = backends.find((b) => b.id === selectedBackendId) ?? null;
  const savedPipelineMissingBackendCount = useMemo(() => {
    if (!savedPipeline?.length) return 0;
    const known = new Set(backends.map((b) => b.id));
    const missing = new Set<string>();
    for (const s of savedPipeline) {
      if (s.ml_backend_id && !known.has(s.ml_backend_id)) missing.add(s.ml_backend_id);
    }
    return missing.size;
  }, [savedPipeline, backends]);

  const cfg = usePreannotateConfig({
    projectId,
    backendId: selectedBackendId,
    executionUnit: dataType === "video" ? executionUnit : undefined,
  });

  const appliedCatalogLink = useRef("");
  useEffect(() => {
    const key = `${projectId}:${initialCatalogModelId}`;
    if (initialCatalogModelId && cfg.catalogQ?.isSuccess && appliedCatalogLink.current !== key) {
      cfg.setCatalogModel(initialCatalogModelId);
      appliedCatalogLink.current = key;
    }
  }, [projectId, initialCatalogModelId, cfg]);

  const hasCompatibleDownstream = cfg.catalogQ?.data
    ? cfg.catalogQ.data.items.some(
        (entry) =>
          entry.operations.pipeline?.allowed &&
          !entry.capabilities.is_interactive &&
          entry.capabilities.composition !== "composite" &&
          ((entry.capabilities.supported_inputs ?? []).includes("crop") ||
            (entry.capabilities.supported_inputs ?? []).includes("bbox_prompt")),
      )
    : backends.length >= 2;
  const {
    stagesGraph,
    selectedSid,
    setSelectedSid,
    onStageChange,
    onStageCaps,
    downstreamPayloads,
    allDownstreamReady,
    addStage,
    removeStage,
    onReparent,
    canReparentConn,
    canAddChildAt,
    conflictInfo,
    hasKeyConflict,
    reset: resetComposer,
    stageCapsRef,
  } = usePipelineComposer({
    availableBackendCount: backends.length,
    hasCompatibleDownstream,
    onWarn: (msg, sub) => pushToast({ msg, sub, kind: "warning" }),
    onCascadeDelete: (n) => pushToast({ msg: "已删除阶段", sub: `连带移除 ${n} 个子阶段` }),
  });

  const [lastPipelineJobId, setLastPipelineJobId] = useState<string | null>(null);
  const pipelineJobQ = useAsyncJob(lastPipelineJobId, true);
  const terminalStageStats =
    (pipelineJobQ.data?.result?.pipeline_stages as PipelineStageStat[] | undefined) ?? null;

  const { progress: liveProgress } = usePreannotationProgress(projectId);

  const batchesQ = useBatches(projectId, "active,draft");
  const batches = (
    (batchesQ.data ?? []) as unknown as Array<{
      id: string;
      display_id: string;
      name: string;
      status: string;
      annotator_id?: string | null;
      reviewer_id?: string | null;
      total_tasks?: number | null;
    }>
  ).filter(batchPreannotationEligible);

  const queueQ = useQuery({
    queryKey: ["admin", "preannotate-queue", projectId],
    queryFn: ({ signal }) => adminPreannotateApi.projectQueue(projectId, signal),
    staleTime: 1000 * 30,
  });
  const projectQueue = useMemo(
    () => (queueQ.data?.items ?? []).filter((it) => it.project_id === projectId),
    [queueQ.data, projectId],
  );

  const [selectedBatchIds, setSelectedBatchIds] = useState<Set<string>>(new Set());
  const [predictMode, setPredictMode] = useState<PredictMode>("skip_predicted");
  const [concurrency, setConcurrency] = useState<ConcurrencyMode>("serial");
  const [submitting, setSubmitting] = useState(false);
  const [importOpen, setImportOpen] = useState(false);

  const liveStageStats = liveProgress?.pipeline_stages ?? null;
  const stagesRunning = pipelineJobQ.data?.status === "running";
  const stageStats = liveStageStats ?? terminalStageStats;
  const stageStatByIndex = useMemo(() => {
    const m = new Map<number, PipelineStageStat>();
    for (const s of stageStats ?? []) m.set(s.stage, s);
    return m;
  }, [stageStats]);
  const sourceDetected = stageStatByIndex.get(0)?.detected;
  const stageRunState = (si: number): "pending" | "running" | "done" => {
    if (stagesRunning) return "running";
    if (stageStatByIndex.has(si)) return "done";
    return "pending";
  };

  useEffect(() => {
    setSelectedBatchIds(new Set());
    resetComposer();
    setLastPipelineJobId(null);
    setKeyConflictLastWins(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  const trigger = useTriggerPreannotation(projectId);

  const toggleBatch = (id: string) => {
    setSelectedBatchIds((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  };

  const allSelected = batches.length > 0 && batches.every((b) => selectedBatchIds.has(b.id));
  const toggleAll = () => {
    setSelectedBatchIds((s) => {
      const n = new Set(s);
      if (allSelected) {
        for (const b of batches) n.delete(b.id);
      } else {
        for (const b of batches) n.add(b.id);
      }
      return n;
    });
  };

  const projectClasses = useMemo(
    () => Object.keys(project?.classes_config ?? {}),
    [project?.classes_config],
  );
  const projectAttributeKeys = useMemo(
    () => (project?.attribute_schema?.fields ?? []).map((f) => f.key),
    [project?.attribute_schema],
  );

  const sourceEffectiveClasses = useMemo<string[]>(() => {
    const all = cfg.geometricModel?.classes ?? [];
    if (all.length === 0) return [];
    const sel = cfg.selectedClassIdx;
    const picked = sel.size > 0 ? all.filter((c) => sel.has(c.index)) : all;
    return [
      ...new Set(picked.map((c) => cfg.selectedCatalogEntry?.output_classes?.[c.index] ?? c.name)),
    ];
  }, [cfg.geometricModel, cfg.selectedClassIdx, cfg.selectedCatalogEntry]);

  const [keyConflictLastWins, setKeyConflictLastWins] = useState(false);

  const sourceMeta = useMemo<PipelineSource>(
    () => ({
      kind: "dataset",
      data_type: dataType,
      execution_unit: dataType === "video" ? executionUnit : undefined,
    }),
    [dataType, executionUnit],
  );

  const graphNodes = useMemo<GraphNodeModel[]>(() => {
    const nameOf = (id?: string | null) =>
      id ? (backends.find((b) => b.id === id)?.name ?? undefined) : undefined;
    const srcShape = sourceNodeShape(sourceMeta, cfg.primaryModel);
    return stagesGraph.map<GraphNodeModel>((e, i) => {
      if (e.parentSid == null) {
        return {
          sid: e.sid,
          parentSid: null,
          role: { label: "数据源", variant: "accent", icon: "box" },
          detail: srcShape.sourceTypeLabel,
          runState: "done",
          producesGeometry: true,
          canAddChild: false,
          conflict: false,
          ready: true,
          sourceTypeLabel: srcShape.sourceTypeLabel,
          executionUnitLabel: srcShape.executionUnitLabel,
          warning: null,
        };
      }
      if (e.sid === SOURCE_SID) {
        return {
          sid: e.sid,
          parentSid: e.parentSid,
          role: srcShape.role,
          detail: srcShape.productLabel,
          runState: stageRunState(0),
          ok: sourceDetected ?? undefined,
          producesGeometry: true,
          canAddChild: canAddChildAt(e.sid),
          conflict: false,
          ready: cfg.configReady,
          backendName: selectedBackend?.name,
          modelId: cfg.primaryModel?.id,
          taskType: cfg.primaryModel?.task,
          warning: null,
        };
      }
      const p = downstreamPayloads[i];
      const stat = stageStatByIndex.get(i - 1);
      return {
        sid: e.sid,
        parentSid: e.parentSid,
        role: roleOf(p),
        detail: detailOf(p),
        runState: stageRunState(i - 1),
        targeted: stat?.targeted,
        ok: stat?.ok,
        producesGeometry: producesGeometry(p),
        canAddChild: canAddChildAt(e.sid),
        conflict: (conflictInfo.perCard[e.sid]?.size ?? 0) > 0,
        ready: p != null,
        backendName: nameOf(p?.ml_backend_id),
        warning: stageWarning(p, stageCapsRef.current[e.sid]),
        classFilter: classFilterText(p),
        modelId: p?.model_id,
        taskType: p?.task,
        roiInfo: roiText(p),
        variantInfo: variantText(p),
      };
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    stagesGraph,
    downstreamPayloads,
    canAddChildAt,
    stageStatByIndex,
    stagesRunning,
    sourceDetected,
    selectedBackend?.name,
    conflictInfo,
    cfg.configReady,
    cfg.primaryModel?.id,
    cfg.primaryModel?.task,
    sourceMeta,
    backends,
  ]);

  const hasDownstreamCards = stagesGraph.some((e) => e.parentSid != null && e.sid !== SOURCE_SID);

  const canRun =
    cfg.configReady &&
    (!hasDownstreamCards || allDownstreamReady) &&
    selectedBatchIds.size > 0 &&
    !submitting &&
    (!hasKeyConflict || keyConflictLastWins);

  const buildDownstreamStages = (baseArgs: PreannotateArgs): PipelineStagePayload[] | undefined => {
    if (!hasDownstreamCards || !allDownstreamReady) return undefined;
    const numberBySid: Record<string, number> = {};
    stagesGraph.forEach((e, i) => {
      if (e.parentSid != null) numberBySid[e.sid] = i - 1;
    });
    return stagesGraph.flatMap((e, i) => {
      if (e.parentSid == null) return [];
      if (e.sid === SOURCE_SID) return [argsToStage(baseArgs, 0, { source: sourceMeta })];
      return [
        {
          ...(downstreamPayloads[i] as PipelineStagePayload),
          stage: i - 1,
          parent_stage: numberBySid[e.parentSid],
        },
      ];
    });
  };

  const saveNamedPipeline = async (stages: PipelineStagePayload[]) => {
    const name = pipelineName.trim();
    if (!name) {
      pushToast({ msg: "请输入编排名称", kind: "warning" });
      return;
    }
    try {
      const created = await createProjectPipeline.mutateAsync({
        name,
        scope: pipelineScope,
        project_id: pipelineScope === "private" ? projectId : null,
        organization_id: null,
        stages,
        is_default: pipelineScope === "private",
      });
      if (pipelineScope !== "private") {
        await applyProjectPipeline.mutateAsync({
          pipelineId: created.id,
          setDefault: true,
        });
      }
      setSelectedLibraryPipelineId(created.id);
      pushToast({
        msg: "已保存为命名编排",
        sub:
          pipelineScope === "private"
            ? `${stages.length} 阶段 · 已设为项目默认`
            : `${PIPELINE_SCOPE_LABELS[pipelineScope]} · 已套用为项目默认`,
        kind: "success",
      });
    } catch (e) {
      pushToast({
        msg: "保存命名编排失败",
        sub: describePipelineError(e),
        kind: "warning",
      });
    }
  };

  const onSavePipeline = async () => {
    const baseArgs = cfg.buildArgs(predictMode);
    if (!baseArgs) {
      pushToast({ msg: "配置未就绪", sub: "请先选模型 / 配齐参数", kind: "warning" });
      return;
    }
    if (hasDownstreamCards && !allDownstreamReady) {
      pushToast({ msg: "下游阶段未就绪", sub: "请配齐各阶段卡", kind: "warning" });
      return;
    }
    const stages = buildDownstreamStages(baseArgs) ?? [
      argsToStage(baseArgs, 0, { source: sourceMeta }),
    ];
    await saveNamedPipeline(stages);
  };

  const onClearPipeline = () => {
    if (defaultProjectPipeline) {
      deleteProjectPipeline.mutate(defaultProjectPipeline.id, {
        onSuccess: () => pushToast({ msg: "已清除项目默认编排", kind: "success" }),
        onError: (e) =>
          pushToast({
            msg: "清除项目默认编排失败",
            sub: (e as Error).message,
            kind: "warning",
          }),
      });
      return;
    }
    updateProject.mutate(
      { preannotate_pipeline: null },
      {
        onSuccess: () => pushToast({ msg: "已清除项目编排", kind: "success" }),
        onError: (e) =>
          pushToast({
            msg: "清除项目编排失败",
            sub: (e as Error).message,
            kind: "warning",
          }),
      },
    );
  };

  const onApplyLibraryPipeline = () => {
    if (!selectedLibraryPipeline) return;
    applyProjectPipeline.mutate(
      { pipelineId: selectedLibraryPipeline.id, setDefault: true },
      {
        onSuccess: (p: ProjectPipeline) =>
          pushToast({
            msg: "已套用命名编排",
            sub: `${p.name} · ${p.stages.length} 阶段`,
            kind: "success",
          }),
        onError: (e) =>
          pushToast({
            msg: "套用命名编排失败",
            sub: describePipelineError(e),
            kind: "warning",
          }),
      },
    );
  };

  const onRun = async () => {
    const baseArgs = cfg.buildArgs(predictMode);
    if (!baseArgs || selectedBatchIds.size === 0) return;
    const pipelineStages = buildDownstreamStages(baseArgs);
    if (hasDownstreamCards && !pipelineStages) return;
    const ids = Array.from(selectedBatchIds);
    setSubmitting(true);
    try {
      let okCount = 0;
      let failCount = 0;
      const errors: string[] = [];
      const firedJobIds: string[] = [];
      const pipelineWarnings = new Set<string>();
      const fireOne = async (bid: string) => {
        try {
          const resp = await trigger.mutateAsync({
            ...baseArgs,
            batch_id: bid,
            ...(pipelineStages
              ? {
                  pipeline_stages: pipelineStages,
                  on_key_conflict: keyConflictLastWins ? "last_wins" : "reject",
                }
              : {}),
          });
          if (resp?.job_id) firedJobIds.push(resp.job_id);
          for (const w of resp?.warnings ?? []) pipelineWarnings.add(w);
          okCount += 1;
        } catch (err) {
          failCount += 1;
          errors.push(`${bid.slice(0, 8)}: ${(err as Error).message}`);
        }
      };
      if (concurrency === "serial") {
        for (const bid of ids) {
          await fireOne(bid);
        }
      } else {
        await Promise.all(ids.map(fireOne));
      }
      pushToast({
        msg: okCount > 0 ? "预标已提交" : "预标提交失败",
        sub: `${okCount} 个批次已受理 · ${failCount} 个批次提交失败`,
        kind: failCount > 0 ? "warning" : "success",
      });
      for (const w of pipelineWarnings) {
        pushToast({ msg: w, kind: "warning" });
      }
      if (failCount > 0 && errors.length > 0) {
        console.warn("[ai-pre] 多批次预标部分失败:", errors);
      }
      if (okCount > 0) {
        setSelectedBatchIds(new Set());
        if (pipelineStages && firedJobIds.length > 0) {
          setLastPipelineJobId(firedJobIds[firedJobIds.length - 1]);
        }
      }
    } finally {
      setSubmitting(false);
    }
  };

  const headerName = summary?.project_name ?? `项目 ${projectId.slice(0, 8)}`;

  if (dataType === "lidar") {
    return (
      <div className={styles.root}>
        <div className={styles.header}>
          <Button size="sm" variant="ghost" onClick={onBack}>
            <Icon name="chevLeft" size={11} /> 返回项目列表
          </Button>
          <h2 className={styles.title}>{headerName}</h2>
        </div>
        <Card>
          <div className={styles.mutedText}>点云（lidar）项目暂不支持 AI 预标。</div>
        </Card>
      </div>
    );
  }

  return (
    <div className={styles.root}>
      <div className={styles.header}>
        <Button size="sm" variant="ghost" onClick={onBack}>
          <Icon name="chevLeft" size={11} /> 返回项目列表
        </Button>
        <h2 className={styles.title}>{headerName}</h2>
        {summary?.project_display_id && (
          <span className={styles.displayId}>({summary.project_display_id})</span>
        )}
        {selectedBackend ? (
          <Badge variant="ai">{selectedBackend.name}</Badge>
        ) : (
          <Badge variant="warning">未绑定模型服务</Badge>
        )}
        {summary?.ml_backend_max_concurrency != null && (
          <span className={styles.backendLimit}>
            最多 {summary.ml_backend_max_concurrency} 并发
          </span>
        )}
        <Button
          size="sm"
          variant="ghost"
          onClick={() => navigate(`/ai-pre/jobs?project_id=${projectId}`)}
          title="查看该项目所有预标任务历史"
        >
          <Icon name="history" size={11} /> 任务历史
        </Button>
        <Button
          size="sm"
          variant="ghost"
          onClick={() => setImportOpen(true)}
          title="上传 COCO / 原生 JSON，将外部模型预测导入项目供人工审阅"
        >
          <Icon name="upload" size={11} /> 导入预测
        </Button>
      </div>

      <PredictionImportWizard
        open={importOpen}
        onClose={() => setImportOpen(false)}
        projectId={projectId}
      />

      <Card>
        <div className={styles.cardHeader}>
          <strong className={styles.sectionTitle}>待预标批次（{batches.length}）</strong>
          {batches.length > 0 && (
            <label className={styles.inlineCheckbox}>
              <input
                type="checkbox"
                checked={allSelected}
                onChange={toggleAll}
                aria-label="全选可预标批次"
              />
              全选
            </label>
          )}
        </div>
        <div className={styles.cardBodyCompact}>
          {batchesQ.isLoading ? (
            <div className={styles.mutedText}>加载中…</div>
          ) : batches.length === 0 ? (
            <div className={styles.mutedText}>
              暂无可预标批次（active 批次，或未分派标注员与质检员的 draft
              批次）。在项目设置中创建批次后再回到这里跑预标。
            </div>
          ) : (
            <ul className={styles.batchList}>
              {batches.map((b) => (
                <li
                  key={b.id}
                  className={cx(
                    styles.batchItem,
                    selectedBatchIds.has(b.id) && styles.batchItemSelected,
                  )}
                  onClick={() => toggleBatch(b.id)}
                >
                  <input
                    type="checkbox"
                    aria-label={`选择 ${b.name}`}
                    checked={selectedBatchIds.has(b.id)}
                    onChange={() => toggleBatch(b.id)}
                    onClick={(e) => e.stopPropagation()}
                  />
                  <span className={styles.batchName}>
                    {b.name} <span className={styles.subtleText}>({b.display_id})</span>
                  </span>
                  {b.status === "draft" && (
                    <Badge variant="default" dot>
                      草稿·未分派
                    </Badge>
                  )}
                  <span className={styles.taskCount}>{b.total_tasks ?? "—"} 任务</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </Card>

      <Card>
        <div className={styles.runPanel}>
          <strong className={styles.sectionTitle}>
            {selectedBatchIds.size > 0
              ? `对已选 ${selectedBatchIds.size} 批跑预标`
              : "批跑预标设置"}
          </strong>

          <div className={styles.pipelineLibraryBar}>
            <div className={styles.pipelineLibraryGroup}>
              <label className={styles.pipelineLibraryField}>
                <span className={styles.fieldLabel}>保存名称</span>
                <input
                  className={styles.textInput}
                  value={pipelineName}
                  onChange={(e) => setPipelineName(e.target.value)}
                  placeholder="例如 detect → 车辆属性"
                />
              </label>
              <label className={styles.pipelineLibraryField}>
                <span className={styles.fieldLabel}>可见范围</span>
                <select
                  className={styles.selectInput}
                  value={pipelineScope}
                  onChange={(e) => setPipelineScope(e.target.value as ProjectPipelineScope)}
                >
                  <option value="private">项目私有</option>
                </select>
              </label>
            </div>

            <div className={styles.pipelineLibraryGroup}>
              <label className={styles.pipelineLibraryField}>
                <span className={styles.fieldLabel}>命名编排库</span>
                <select
                  className={styles.selectInput}
                  value={selectedLibraryPipelineId}
                  onChange={(e) => setSelectedLibraryPipelineId(e.target.value)}
                  disabled={projectPipelinesQ.isLoading || libraryPipelines.length === 0}
                >
                  {libraryPipelines.length === 0 ? (
                    <option value="" disabled hidden>
                      暂无可套用编排
                    </option>
                  ) : (
                    libraryPipelines.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name} · {PIPELINE_SCOPE_LABELS[p.scope]} · {p.stages.length} 阶段
                      </option>
                    ))
                  )}
                </select>
              </label>
              <Button
                size="sm"
                variant="default"
                onClick={onApplyLibraryPipeline}
                disabled={!selectedLibraryPipeline || applyProjectPipeline.isPending}
                title="把所选命名编排 copy-on-write 套用到当前项目，并设为项目默认"
              >
                <Icon name="download" size={12} />
                套用为默认
              </Button>
            </div>
          </div>

          <div className={styles.editorTwoCol}>
            <div className={styles.editorCanvas}>
              <Suspense fallback={<div className={styles.canvasFallback}>加载编排画布…</div>}>
                <PipelineGraphCanvas
                  models={graphNodes}
                  selectedSid={selectedSid}
                  onSelect={setSelectedSid}
                  onAddChild={addStage}
                  onRemove={removeStage}
                  onReparent={onReparent}
                  canReparentConn={canReparentConn}
                />
              </Suspense>
              {!hasCompatibleDownstream ? (
                <span className={styles.stageEmptyHint}>
                  暂无兼容的下游模型。请在项目设置启用兼容的模型服务，或加载并发布检测模型；同一
                  YOLO 实例可执行多个检测阶段。
                </span>
              ) : !hasDownstreamCards ? (
                <span className={styles.stageEmptyHint}>
                  从源模型的 <Icon name="plus" size={10} /> 拖出（或点
                  +）加下游阶段：检出框后，下游对每个框跑分类补属性 / 检测子物体。
                </span>
              ) : null}
            </div>

            <div className={styles.editorInspector}>
              <div hidden={selectedSid !== ROOT_SID}>
                <strong className={styles.sectionTitle}>输入节点 · 数据源</strong>
                <div className={styles.mutedText}>
                  数据类型：
                  {dataType === "video" ? "视频" : dataType === "image" ? "图像" : dataType}
                </div>
                {dataType === "video" && (
                  <label className={styles.field}>
                    <span className={styles.fieldLabel}>执行单位</span>
                    <select
                      className={styles.selectInput}
                      value={executionUnit}
                      onChange={(e) => setExecutionUnit(e.target.value as "video" | "frame")}
                    >
                      <option value="video">整段序列</option>
                      <option value="frame">逐帧</option>
                    </select>
                    <span className={styles.mutedText}>
                      {executionUnit === "video"
                        ? "对整段视频做多目标追踪，产跨帧轨迹。"
                        : "对每一帧独立跑图像检测，产逐帧单帧框（无跨帧关联）。"}
                    </span>
                  </label>
                )}
              </div>
              <div hidden={selectedSid !== SOURCE_SID}>
                <strong className={styles.sectionTitle}>
                  {dataType === "video" && executionUnit === "video"
                    ? "源模型 · 追踪参数"
                    : "源模型 · 检测参数"}
                </strong>
                <PreannotateConfigForm
                  cfg={cfg}
                  backends={backends}
                  selectedBackendId={selectedBackendId}
                  onSelectBackend={setSelectedBackendId}
                  projectMlBackendId={project?.ml_backend_id}
                />
              </div>
              {stagesGraph.map((e, i) =>
                e.parentSid == null || e.sid === SOURCE_SID ? null : (
                  <StageCard
                    key={e.sid}
                    id={e.sid}
                    displayIndex={i}
                    projectId={projectId}
                    backends={backends}
                    projectMlBackendId={project?.ml_backend_id}
                    sourceBackendId={selectedBackendId}
                    projectClasses={projectClasses}
                    parentClassOptions={
                      e.parentSid === SOURCE_SID
                        ? sourceEffectiveClasses
                        : stageCapsRef.current[e.parentSid]?.outputClasses
                    }
                    projectAttributeKeys={projectAttributeKeys}
                    conflictKeys={conflictInfo.perCard[e.sid]}
                    stat={stageStatByIndex.get(i - 1)}
                    runState={stageRunState(i - 1)}
                    hidden={selectedSid !== e.sid}
                    onChange={onStageChange}
                    onCaps={onStageCaps}
                    onRemove={removeStage}
                  />
                ),
              )}
            </div>
          </div>

          {hasKeyConflict && (
            <div className={styles.stageWarn}>
              <Icon name="warning" size={12} />
              <div className={styles.field}>
                <span>
                  多个阶段写同一属性键：
                  {Array.from(conflictInfo.displayFinals).join("、")}。默认拦截，无法运行。
                </span>
                <label className={styles.inlineCheckbox}>
                  <input
                    type="checkbox"
                    checked={keyConflictLastWins}
                    onChange={(e) => setKeyConflictLastWins(e.target.checked)}
                  />
                  允许末位覆盖（last_wins）
                </label>
              </div>
            </div>
          )}

          <div className={styles.field}>
            <span className={styles.fieldLabel}>
              已预标任务
              {predictMode === "overwrite" && (
                <span className={styles.fieldHint}> · 覆盖会删除已有 AI 标注</span>
              )}
            </span>
            <TabRow
              tabs={PREDICT_MODE_TABS}
              active={PREDICT_MODE_LABELS[predictMode]}
              onChange={(label) => {
                const m = PREDICT_MODE_BY_LABEL[label];
                if (m) setPredictMode(m);
              }}
            />
          </div>

          {selectedBatchIds.size > 1 && (
            <div role="radiogroup" aria-label="并发模式" className={styles.concurrencyGroup}>
              <label className={styles.radioLabel}>
                <input
                  type="radio"
                  name="concurrency"
                  checked={concurrency === "serial"}
                  onChange={() => setConcurrency("serial")}
                />
                串行（依次入队）
              </label>
              <label className={styles.radioLabel}>
                <input
                  type="radio"
                  name="concurrency"
                  checked={concurrency === "parallel"}
                  onChange={() => setConcurrency("parallel")}
                />
                并行（同时入队）
              </label>
              {summary?.ml_backend_max_concurrency != null && (
                <span className={styles.mutedInline}>
                  （模型服务最多 {summary.ml_backend_max_concurrency} 并发）
                </span>
              )}
            </div>
          )}

          <div className={styles.actions}>
            <Button
              onClick={onRun}
              disabled={!canRun}
              title={
                selectedBatchIds.size === 0
                  ? "请先选择至少一个批次"
                  : hasKeyConflict && !keyConflictLastWins
                    ? "存在属性键冲突，请勾选「允许末位覆盖」或调整各阶段写回键"
                    : undefined
              }
            >
              <Icon name="bot" size={12} />
              {submitting
                ? "提交中…"
                : selectedBatchIds.size === 0
                  ? "跑预标（先选批次）"
                  : `跑预标（${selectedBatchIds.size} 批）`}
            </Button>
            <Button
              variant="default"
              onClick={onSavePipeline}
              disabled={
                !cfg.configReady ||
                (stagesGraph.length > 0 && !allDownstreamReady) ||
                createProjectPipeline.isPending ||
                applyProjectPipeline.isPending
              }
              title="把当前配置（含多阶段编排）保存为命名编排，并设为当前项目默认"
            >
              <Icon name="save" size={12} />
              保存为命名编排
            </Button>
            {savedStageCount > 0 && (
              <>
                <Badge>
                  {savedPipelineName} · {savedStageCount} 阶段
                </Badge>
                {savedPipelineMissingBackendCount > 0 && (
                  <span title="编排引用的模型服务不在本项目注册列表中；工作台无法按该编排运行，请重新注册或修改编排">
                    <Badge variant="warning">
                      引用 {savedPipelineMissingBackendCount} 个模型服务不可用
                    </Badge>
                  </span>
                )}
                <Button
                  variant="ghost"
                  onClick={onClearPipeline}
                  disabled={updateProject.isPending || deleteProjectPipeline.isPending}
                  title="清除项目默认命名编排；若仅有旧项目编排，则清除旧兼容列"
                >
                  清除
                </Button>
              </>
            )}
          </div>
        </div>
      </Card>

      <HistoryTable key={projectId} items={projectQueue} isLoading={queueQ.isLoading} />
    </div>
  );
}

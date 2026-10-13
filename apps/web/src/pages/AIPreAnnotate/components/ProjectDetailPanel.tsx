/**
 * v0.9.12 · BUG B-17 · 项目详情面板 (多选 batch + 串/并行预标 + 已就绪 HistoryTable).
 *
 * 进入条件: ProjectCardGrid 点击某项目卡片;此面板替代主视图渲染.
 */

import { Suspense, lazy, useEffect, useMemo, useState } from "react";
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

// react-flow 经 lazy 隔离: chunk 不进主包, 仅 /ai-pre 详情面板按需加载 (同 App.tsx 惯例)。
const PipelineGraphCanvas = lazy(() => import("./PipelineGraphCanvas"));

// v0.11.24 · 预标幂等模式
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
    return `未启用 backend: ${ids.join("、")}`;
  }
  return (error as Error).message;
}

/**
 * v0.18.1 · 把单节点配置 (PreannotateArgs) 投影成一个 pipeline stage。
 * batch 级字段 (prompt / output_mode / predict_mode) 不进 stage —— 源阶段的 prompt 由顶层请求
 * 承载, 下游阶段吃 crop 不需要 prompt。
 */
function argsToStage(
  args: PreannotateArgs,
  stage: number,
  extra: Partial<PipelineStagePayload> = {},
): PipelineStagePayload {
  return {
    stage,
    ml_backend_id: args.ml_backend_id,
    model_id: args.model_id,
    task_type: args.task_type,
    model_variants: args.model_variants,
    params: args.params,
    class_filter: args.class_filter,
    ...extra,
  };
}

interface Props {
  projectId: string;
  onBack: () => void;
  /** 项目卡片传入的聚合摘要（用于头部 ml_backend chip + 并发上限）；不可省的部分会再用 hooks 拉. */
  summary?: {
    project_name: string;
    project_display_id?: string | null;
    /** v0.10.38 · 媒体维度, 用于按模态分流 (image=文本批量预标 / video=引导卡片). */
    data_type?: string | null;
    ml_backend_id?: string | null;
    ml_backend_name?: string | null;
    ml_backend_state?: string | null;
    ml_backend_max_concurrency?: number | null;
  };
}

export function ProjectDetailPanel({ projectId, onBack, summary }: Props) {
  const pushToast = useToastStore((s) => s.push);
  const navigate = useNavigate();

  // v0.9.13 · 订阅 batch 状态变更, 让"待预标批次"列表实时刷新 (B-15)
  useBatchEventsSocket(projectId);

  const projectQ = useProject(projectId);
  const project = projectQ.data as unknown as
    | {
        type_key?: string;
        data_type?: string | null;
        ml_backend_id?: string | null;
        classes_config?: Record<string, { alias?: string | null }>;
        // v0.18.5 · 项目属性 schema, 多阶段下游卡「写回属性键」多选的回落选项。
        attribute_schema?: { fields?: Array<{ key: string; label?: string }> };
        box_threshold?: number | null;
        text_threshold?: number | null;
        // v0.14.13 · 项目级 variant 偏好 (按 backend_id 分桶), 详见 ProjectOut.default_variants.
        default_variants?: Record<string, Record<string, string>>;
        // v0.18.27 · 项目级「已保存的编排」(方案 A); null/缺 = 未配编排.
        preannotate_pipeline?: PipelineStagePayload[] | null;
      }
    | undefined;
  // v0.18.27 · 项目编排保存 / 清除 (方案 A, 一项目一条)。
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
  // v0.10.38 · 模态分流: summary 优先 (列表已带), 回落 project 查询.
  const dataType = summary?.data_type ?? project?.data_type ?? "image";
  // v0.21.7 · 视频项目执行单位 (输入节点顶层分叉): "video"=整段序列(tracker) / "frame"=逐帧(图像检测)。
  //   默认整段序列 (视频主能力)。图像项目无此选择 (恒图像检测)。
  const [executionUnit, setExecutionUnit] = useState<"video" | "frame">("video");

  const backendsQ = useMLBackends(projectId);
  const backends = useMemo(
    () => (backendsQ.data ?? []) as unknown as Array<{ id: string; name: string }>,
    [backendsQ.data],
  );
  // v0.10.38 · 多 backend 选择: 默认绑定值, 用户可在项目已注册 backend 间切换 (epic 阶段 2).
  const [selectedBackendId, setSelectedBackendId] = useState<string | null>(null);
  const firstBackendId = backends[0]?.id ?? null;
  useEffect(() => {
    // 项目切换 / 列表加载后, 默认选绑定 backend (否则第一个)
    setSelectedBackendId(project?.ml_backend_id ?? firstBackendId);
  }, [projectId, project?.ml_backend_id, firstBackendId]);
  const selectedBackend = backends.find((b) => b.id === selectedBackendId) ?? null;
  // claude[bot] P1 #5 · 已保存编排里引用的 backend id 在本项目当前 backends 列表里缺多少 (= 被删/停)。
  // 编排可能跨多个 backend, 集合用 backends.map(b.id); 工作台 popover 入口同步据此禁用。
  const savedPipelineMissingBackendCount = useMemo(() => {
    if (!savedPipeline?.length) return 0;
    const known = new Set(backends.map((b) => b.id));
    const missing = new Set<string>();
    for (const s of savedPipeline) {
      if (s.ml_backend_id && !known.has(s.ml_backend_id)) missing.add(s.ml_backend_id);
    }
    return missing.size;
  }, [savedPipeline, backends]);

  // 预标配置区共享状态 (任务类型 / 几何 task / 类别白名单 / variant / 参数 / prompt / 预设 /
  // 输出形态 / buildArgs); 详见 usePreannotateConfig. 工作台 AI 面板复用同一 hook + PreannotateConfigForm.
  const cfg = usePreannotateConfig({
    projectId,
    backendId: selectedBackendId,
    executionUnit: dataType === "video" ? executionUnit : undefined,
  });

  // v0.18.2 · 多阶段预标注 (路径 B M2): 下游阶段卡列表 (并行兄弟, 单层扇出)。每张卡 (StageCard)
  // 自持一份 usePreannotateConfig + PreannotateConfigForm 实例 —— 共享 hook/组件本身不感知阶段
  // 编排 (红线)。卡片把派生 stage payload 上抛, 容器在运行时组装成 pipeline_stages。
  // v0.18.15 · 受限树形 (max depth 3): 下游阶段为 {sid, parentSid} 列表, parentSid="root"=源阶段。
  // 数组顺序即添加顺序 (子总在父之后追加 → 运行期分配的 stage 号天然满足「父序号 < 子序号」)。
  // v0.21.0 收尾优化 · 编排状态机层提取到 usePipelineComposer, 与全局编排页共用 (方案 B refactor).
  // 下游须用不同于源检测的 backend, backends 数<2 时不允许加子 (原 canAddBackend 语义)。
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
    onWarn: (msg, sub) => pushToast({ msg, sub, kind: "warning" }),
    onCascadeDelete: (n) => pushToast({ msg: "已删除阶段", sub: `连带移除 ${n} 个子阶段` }),
  });

  // v0.18.3 · 运行态可视化: 跑批后轮询最后一个多阶段 job 的 result.pipeline_stages (终态真值)。
  const [lastPipelineJobId, setLastPipelineJobId] = useState<string | null>(null);
  const pipelineJobQ = useAsyncJob(lastPipelineJobId, true);
  const terminalStageStats =
    (pipelineJobQ.data?.result?.pipeline_stages as PipelineStageStat[] | undefined) ?? null;

  // v0.18.6 · 运行态实时化: 订阅项目预标 WS, 拿 worker 跑批中途推的逐阶段累加快照。
  const { progress: liveProgress } = usePreannotationProgress(projectId);

  // issue #124 · 可预标批次 = active 批次 + 未分派标注员与质检员的 draft 批次,
  // 让管理员先跑 AI 再分派人工。准入规则与后端同源 (batchPreannotationEligible),
  // 前端仅负责入口展示, 提交时后端仍会复校验。
  // 查询限定 active,draft, 不再拉取 approved/rejected/archived 全量历史。
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

  // pre_annotated 队列 (复用 /admin/preannotate-queue 端点 + 客户端按 project 过滤)
  const queueQ = useQuery({
    queryKey: ["admin", "preannotate-queue"],
    queryFn: () => adminPreannotateApi.queue(50),
    staleTime: 1000 * 30,
  });
  const projectQueue = useMemo(
    () => (queueQ.data?.items ?? []).filter((it) => it.project_id === projectId),
    [queueQ.data, projectId],
  );

  const [selectedBatchIds, setSelectedBatchIds] = useState<Set<string>>(new Set());
  // v0.11.24 · 默认跳过已预标 task，避免重复预标叠加重复标注
  const [predictMode, setPredictMode] = useState<PredictMode>("skip_predicted");
  const [concurrency, setConcurrency] = useState<ConcurrencyMode>("serial");
  const [running, setRunning] = useState(false);
  // v0.10.15 · 外部预测导入向导 (COCO / AAP JSON)
  const [importOpen, setImportOpen] = useState(false);

  // v0.18.6 · 逐阶段统计: 运行中用 WS 实时快照, 终态/重连回落 job result (终态真值, 不丢)。
  const liveStageStats = liveProgress?.pipeline_stages ?? null;
  const stagesRunning = running || pipelineJobQ.data?.status === "running";
  const stageStats = liveStageStats ?? terminalStageStats;
  // stage 序号 → 统计, 供各卡按自身 stage 取数。stage 0=源检测; i+1=第 i 张下游卡。
  const stageStatByIndex = useMemo(() => {
    const m = new Map<number, PipelineStageStat>();
    for (const s of stageStats ?? []) m.set(s.stage, s);
    return m;
  }, [stageStats]);
  const sourceDetected = stageStatByIndex.get(0)?.detected;
  // 单卡运行态: 未跑=pending; 跑批中=running; 已出统计且非运行中=done。
  const stageRunState = (si: number): "pending" | "running" | "done" => {
    if (stagesRunning) return "running";
    if (stageStatByIndex.has(si)) return "done";
    return "pending";
  };

  // 项目切换时重置批次选择 + 下游阶段卡 (prompt / outputMode 的重置在 usePreannotateConfig 内).
  useEffect(() => {
    setSelectedBatchIds(new Set());
    resetComposer();
    setLastPipelineJobId(null);
    setKeyConflictLastWins(false);
    // resetComposer 已是稳定引用, 无需入依赖.
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

  // downstreamPayloads / allDownstreamReady / payloadBySid 现由 usePipelineComposer 提供 (v0.21.0).

  // v0.18.5 · 选择器化数据源: 项目类别 (类名) + 项目属性 schema 字段键, 传给 StageCard 多选。
  const projectClasses = useMemo(
    () => Object.keys(project?.classes_config ?? {}),
    [project?.classes_config],
  );
  const projectAttributeKeys = useMemo(
    () => (project?.attribute_schema?.fields ?? []).map((f) => f.key),
    [project?.attribute_schema],
  );

  // v0.20.x · 源阶段「筛完的有效类别」(类名): 源模型类别表 (静态 COCO80 等) ∩ 源类别白名单
  //   (白名单空 = 全类)。下游卡的「父框类别」选项优先取此 (下游只会见到源筛出的框), 取不到回落
  //   projectClasses。注: 仅按源阶段算 (覆盖主流单层 det→下游); 多层父为中间阶段时回落自由文本。
  const sourceEffectiveClasses = useMemo<string[]>(() => {
    const all = cfg.geometricModel?.classes ?? [];
    if (all.length === 0) return [];
    const sel = cfg.selectedClassIdx;
    const picked = sel.size > 0 ? all.filter((c) => sel.has(c.index)) : all;
    return picked.map((c) => c.name);
  }, [cfg.geometricModel, cfg.selectedClassIdx]);

  // 键冲突策略: reject (默认, 后端校验期拦) | last_wins (末位覆盖, 用户显式允许).
  // conflictInfo/hasKeyConflict/onReparent/canReparentConn 现由 usePipelineComposer 提供.
  const [keyConflictLastWins, setKeyConflictLastWins] = useState(false);

  // v0.21.5 / v0.21.7 · 输入节点数据源描述: 数据类型随项目 (image/video); 视频项目执行单位随
  //   顶层选择 (整段序列=video / 逐帧=frame), 串进 source → 序列化 → 后端逐帧 fan-out。
  const sourceMeta = useMemo<PipelineSource>(
    () => ({
      kind: "dataset",
      data_type: dataType,
      execution_unit: dataType === "video" ? executionUnit : undefined,
    }),
    [dataType, executionUnit],
  );

  // v0.18.16 · DAG 图节点模型 (源 + 各下游): 角色徽标 / 运行态 / 迷你计数 / 可加子 / 键冲突。
  // 下游须用不同于检测的 backend → composer.canAddChildAt 已合并 backends 数<2 判据.
  const graphNodes = useMemo<GraphNodeModel[]>(() => {
    const nameOf = (id?: string | null) =>
      id ? (backends.find((b) => b.id === id)?.name ?? undefined) : undefined;
    // v0.21.6 · 输入节点纯数据源(源类型/执行单位徽标); 源模型 stage(SOURCE_SID)承接 cfg 检测/tracker。
    const srcShape = sourceNodeShape(sourceMeta, cfg.primaryModel);
    // 后端 stage 号 = 数组索引-1 (输入节点 index0 不入后端; 源模型 index1→stage0; 下游 index k→stage k-1)。
    return stagesGraph.map<GraphNodeModel>((e, i) => {
      if (e.parentSid == null) {
        // 输入节点: 纯数据源, 无模型/后端; 显数据类型 + 执行单位徽标。
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
        // 源模型 stage: cfg 配置的整图检测/tracker, 后端 stage 0。
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
        taskType: p?.task_type,
        roiInfo: roiText(p),
        variantInfo: variantText(p),
      };
    });
    // stageRunState 每渲染重建 (依赖 stagesRunning/统计); stageCapsRef 是 ref, composer 内 tick 触发重算.
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

  // v0.21.6 · 有无 StageCard 下游卡 (父=模型 stage, 非输入节点直子 SOURCE_SID)。
  const hasDownstreamCards = stagesGraph.some((e) => e.parentSid != null && e.sid !== SOURCE_SID);

  // 配置层就绪 (源 cfg.configReady) && 下游卡 (若有) 全就绪 && 选了批次 && 不在跑 &&
  // (无键冲突 || 已选末位覆盖)。
  const canRun =
    cfg.configReady &&
    (!hasDownstreamCards || allDownstreamReady) &&
    selectedBatchIds.size > 0 &&
    !running &&
    (!hasKeyConflict || keyConflictLastWins);

  // v0.21.6 · 组装后端 pipeline_stages: 输入节点(纯数据源)不入; 源模型 stage(SOURCE_SID)=后端 stage 0
  // (parent_stage=null, 携 source 元数据); 下游卡=stage 1+ (后端号=数组索引-1)。仅源模型无下游卡时
  // 返回 undefined → onRun 走单阶段 (向后兼容); 保存编排时兜成单元素数组 (见 onSavePipeline)。
  const buildDownstreamStages = (baseArgs: PreannotateArgs): PipelineStagePayload[] | undefined => {
    if (!hasDownstreamCards || !allDownstreamReady) return undefined;
    // sid → 后端 stage 号 = 数组索引-1 (SOURCE_SID index1→0, 下游 index k→k-1; 输入节点不计)。
    const numberBySid: Record<string, number> = {};
    stagesGraph.forEach((e, i) => {
      if (e.parentSid != null) numberBySid[e.sid] = i - 1;
    });
    return stagesGraph.flatMap((e, i) => {
      if (e.parentSid == null) return []; // 输入节点纯数据源, 不入后端
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

  // v0.18.27 · 保存当前配置为「项目编排」(方案 A, 一项目一条)。单阶段也存成单元素数组,
  // 保持「一项目一编排」语义; v0.18.28 popover 据此对当前图跑完整链。本版只存不跑。
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
    // 有下游卡但未就绪 → 不发; 仅源模型(无下游卡)走单阶段执行。
    if (hasDownstreamCards && !pipelineStages) return;
    const ids = Array.from(selectedBatchIds);
    setRunning(true);
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
        msg: `${concurrency === "serial" ? "串行" : "并行"} 预标已分发`,
        sub: `${okCount} 成功 · ${failCount} 失败`,
        kind: failCount > 0 ? "warning" : "success",
      });
      // v0.20.21 · 派发期软提示 (如 output=both + 多阶段致下游重复处理), 去重后逐条弹出。
      for (const w of pipelineWarnings) {
        pushToast({ msg: w, kind: "warning" });
      }
      if (failCount > 0 && errors.length > 0) {
        console.warn("[ai-pre] 多批次预标部分失败:", errors);
      }
      if (okCount > 0) {
        setSelectedBatchIds(new Set());
        // v0.14.13 · 至少一批成功 → 记 variant 已热 (异步 trigger 拿不到 cache_hit, 走兜底).
        cfg.markHot();
        // v0.18.3 · 多阶段时盯最后一个 job 的逐阶段统计 (单阶段无 pipeline_stages, 不显示)。
        if (pipelineStages && firedJobIds.length > 0) {
          setLastPipelineJobId(firedJobIds[firedJobIds.length - 1]);
        }
      }
    } finally {
      setRunning(false);
    }
  };

  const headerName = summary?.project_name ?? `项目 ${projectId.slice(0, 8)}`;

  // v0.21.5 · 解除 video 早退: 视频项目进统一编排画布, 输入节点 data_type=video + 执行单位=video。
  // 下游 tracker 接线由 v0.21.6 落地; 本版仅让 video 入编排 + 输入节点徽标正确。
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
          <Badge variant="warning">未绑定 ML backend</Badge>
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
          title="该项目所有 prediction job 历史"
        >
          <Icon name="history" size={11} /> 历史 job
        </Button>
        <Button
          size="sm"
          variant="ghost"
          onClick={() => setImportOpen(true)}
          title="上传 COCO / AAP JSON 把外部模型预测灌进本项目"
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
                  {/* issue #124 · 未分派人员的 draft 批次也可预标; 标注状态提醒管理员
                      该批尚未激活, 跑完 AI 后仍需分派人员并激活才进入人工流程。 */}
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

      {/* v0.14.16 · 配置区常驻: 不再被 selectedBatchIds gate, 未选批次也可预先配置 / 存预设,
          仅"运行"按钮禁用 (见 canRun). */}
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
                {/* 组织 / 公共 scope 需 organization_id / 超管权限, 首版未提供对应 UI 与选项,
                      故项目内保存只放行 private, 避免选了必定 400/403。跨项目复用走全局 Pipeline 库。 */}
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
                    <option value="">暂无可套用编排</option>
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

          {/* v0.18.16 · 两列编排: 左 DAG 画布 (点选/增删/拖边改父), 右选中节点参数检查器。
                页面高度恒定 (右列永远一张卡), 结构所见即所得。 */}
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
              {backends.length < 2 ? (
                <span className={styles.stageEmptyHint}>
                  需在项目设置绑定第二个 ML
                  backend，才能加下游分类/检测子阶段（下游须用不同于检测的后端）。
                </span>
              ) : !hasDownstreamCards ? (
                <span className={styles.stageEmptyHint}>
                  从源模型的 <Icon name="plus" size={10} /> 拖出（或点
                  +）加下游阶段：检出框后，下游对每个框跑分类补属性 / 检测子物体。
                </span>
              ) : null}
            </div>

            {/* 检查器: 所有配置体常驻挂载, 非选中者 CSS 隐藏 (保住各自 usePreannotateConfig 状态)。 */}
            <div className={styles.editorInspector}>
              {/* 输入节点: 纯数据源 (data_type 只读 + 执行单位可选); 选中时显示。 */}
              <div hidden={selectedSid !== ROOT_SID}>
                <strong className={styles.sectionTitle}>输入节点 · 数据源</strong>
                <div className={styles.mutedText}>
                  数据类型：
                  {dataType === "video" ? "视频" : dataType === "image" ? "图像" : dataType}
                </div>
                {/* v0.21.7 · 视频项目执行单位顶层分叉: 换它会重置源模型下拉 (整段=tracker / 逐帧=图像检测)。 */}
                {dataType === "video" && (
                  <label className={styles.field}>
                    <span className={styles.fieldLabel}>执行单位</span>
                    <select
                      className={styles.selectInput}
                      value={executionUnit}
                      onChange={(e) => setExecutionUnit(e.target.value as "video" | "frame")}
                    >
                      <option value="video">整段序列（detect-then-track 追踪）</option>
                      <option value="frame">逐帧（图像检测逐帧跑，落单帧框）</option>
                    </select>
                    <span className={styles.mutedText}>
                      {executionUnit === "video"
                        ? "对整段视频做多目标追踪，产跨帧轨迹。"
                        : "对每一帧独立跑图像检测，产逐帧单帧框（无跨帧关联）。"}
                    </span>
                  </label>
                )}
              </div>
              {/* 源模型参数 (整图检测/tracker, 后端 stage 0): 选中 SOURCE_SID 时显示。 */}
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
              {/* 各下游阶段卡: 全部挂载, 非选中 hidden。输入节点/源模型不出卡 (走上方配置体)。 */}
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
                    parentClassOptions={sourceEffectiveClasses}
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

          {/* v0.18.5 · 键冲突配置期预警: 多个 attributes 阶段写同一最终键 → 红字提示 + 末位覆盖开关。 */}
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
                  （后端最多 {summary.ml_backend_max_concurrency} 并发）
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
              {running
                ? cfg.isCurrentVariantWarm
                  ? "分发中..."
                  : "加载模型中…（首次约 5-15s）"
                : selectedBatchIds.size === 0
                  ? "跑预标（先选批次）"
                  : `跑预标（${selectedBatchIds.size} 批）`}
            </Button>
            {/* v0.18.27 · 把当前配置存为「项目编排」(方案 A); v0.18.28 popover「运行当前题（按项目编排）」据此读取。 */}
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
                {/* claude[bot] P1 #5 · 引用的 backend 被删/停 → 工作台 popover「按编排跑」会禁用; 这里同时提示。 */}
                {savedPipelineMissingBackendCount > 0 && (
                  <span title="编排引用的 backend 不在本项目注册列表里 (被删或未注册); 工作台 popover「按编排跑」会被禁用, 请重新注册或修改编排">
                    <Badge variant="warning">
                      引用 {savedPipelineMissingBackendCount} 个后端不可用
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

      <HistoryTable items={projectQueue} isLoading={queueQ.isLoading} />
    </div>
  );
}

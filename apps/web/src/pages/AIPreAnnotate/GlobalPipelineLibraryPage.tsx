import { ListPagination } from "@/components/ui/ListPagination";
import { listStyles } from "@/components/ui/listStyles";
import { useListPagination } from "@/hooks/useListPagination";
import { Suspense, lazy, useCallback, useEffect, useMemo, useState } from "react";

import { useCapabilityInstances } from "@/api/mlCapabilities";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { useToastStore } from "@/components/ui/Toast";
import {
  useCreateProjectPipeline,
  useDeleteProjectPipeline,
  useProjectPipelines,
} from "@/hooks/useProjectPipelines";
import type { ProjectPipeline, ProjectPipelineScope } from "@/api/projectPipelines";
import type { PipelineSource, PipelineStagePayload } from "@/hooks/usePreannotation";
import { GEOMETRIC_TASKS } from "./components/usePreannotateConfig";
import { usePipelineComposer } from "./hooks/usePipelineComposer";
import { GlobalStageInspector, type GlobalModelOption } from "./components/GlobalStageInspector";
import {
  ROOT_SID,
  SOURCE_SID,
  classFilterText,
  sourceNodeShape,
  detailOf,
  producesGeometry,
  roleOf,
  stageWarning,
  variantText,
  roiText,
  type GraphNodeModel,
  type StageEntry,
} from "./utils/pipelineGraph";

const SCOPE_LABELS: Record<ProjectPipelineScope, string> = {
  private: "私有",
  organization: "组织",
  public: "公共",
};
import styles from "./GlobalPipelineLibraryPage.module.css";

const PipelineGraphCanvas = lazy(() => import("./components/PipelineGraphCanvas"));

export default function GlobalPipelineLibraryPage() {
  const pushToast = useToastStore((s) => s.push);
  const createProjectPipeline = useCreateProjectPipeline();
  const capabilityInstancesQ = useCapabilityInstances();
  const [pipelineName, setPipelineName] = useState("");
  const [loadedPipelineId, setLoadedPipelineId] = useState<string | null>(null);
  const [pipelineScope, setPipelineScope] =
    useState<Extract<ProjectPipelineScope, "public" | "organization">>("public");

  const globalModelOptions = useMemo<GlobalModelOption[]>(() => {
    const out: GlobalModelOption[] = [];
    for (const inst of capabilityInstancesQ.data?.instances ?? []) {
      for (const model of inst.models ?? []) {
        out.push({
          key: `${inst.backend_id}::${model.id}`,
          backendId: inst.backend_id,
          backendName: inst.name,
          model,
          disabled: inst.state === "error",
        });
      }
    }
    return out;
  }, [capabilityInstancesQ.data]);

  const uniqueBackendCount = useMemo(() => {
    const set = new Set<string>();
    for (const o of globalModelOptions) if (!o.disabled) set.add(o.backendId);
    return set.size;
  }, [globalModelOptions]);

  const {
    stagesGraph,
    setStagesGraph,
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
    stageCapsRef,
    reset: resetComposer,
  } = usePipelineComposer({
    availableBackendCount: uniqueBackendCount,
    hasCompatibleDownstream: globalModelOptions.some(
      (option) =>
        !option.disabled &&
        !option.model.is_interactive &&
        option.model.composition !== "composite" &&
        ((option.model.supported_inputs ?? []).includes("crop") ||
          (option.model.supported_inputs ?? []).includes("bbox_prompt")),
    ),
    onWarn: (msg, sub) => pushToast({ msg, sub, kind: "warning" }),
    onCascadeDelete: (n) => pushToast({ msg: "已删除阶段", sub: `连带移除 ${n} 个子阶段` }),
  });

  const [sourcePayload, setSourcePayload] = useState<PipelineStagePayload | null>(null);
  const [loadedSourceSeed, setLoadedSourceSeed] = useState<PipelineStagePayload | null>(null);
  const [loadedDownstreamSeedBySid, setLoadedDownstreamSeedBySid] = useState<
    Record<string, PipelineStagePayload>
  >({});
  const onSourceChange = useCallback((payload: PipelineStagePayload | null) => {
    setSourcePayload(payload);
  }, []);
  const onSourceCaps = useCallback(
    (caps: Parameters<typeof onStageCaps>[1]) => onStageCaps(SOURCE_SID, caps),
    [onStageCaps],
  );

  const nameOfBackend = useCallback(
    (id?: string | null) => globalModelOptions.find((o) => o.backendId === id)?.backendName,
    [globalModelOptions],
  );

  const classesForSid = useCallback(
    (sid: string): string[] => {
      const payload =
        sid === SOURCE_SID
          ? sourcePayload
          : (downstreamPayloads[stagesGraph.findIndex((s) => s.sid === sid)] ?? null);
      if (!payload?.ml_backend_id || !payload?.model_id) return [];
      const opt = globalModelOptions.find(
        (o) => o.key === `${payload.ml_backend_id}::${payload.model_id}`,
      );
      return (opt?.model.classes ?? []).map((c) => c.name);
    },
    [sourcePayload, downstreamPayloads, stagesGraph, globalModelOptions],
  );

  const [srcDataType, setSrcDataType] = useState<"image" | "video">("image");
  const [srcExecUnit, setSrcExecUnit] = useState<"video" | "frame">("video");
  const isVideoTracking = srcDataType === "video" && srcExecUnit === "video";
  const srcMeta = useMemo<PipelineSource>(
    () => ({
      kind: "dataset",
      data_type: srcDataType,
      execution_unit: srcDataType === "video" ? srcExecUnit : undefined,
    }),
    [srcDataType, srcExecUnit],
  );
  const sourcePool = useMemo(
    () =>
      globalModelOptions.filter((o) =>
        isVideoTracking ? o.model.task === "tracker" : GEOMETRIC_TASKS.includes(o.model.task ?? ""),
      ),
    [globalModelOptions, isVideoTracking],
  );

  const allWarnings = useMemo(() => {
    const out: string[] = [];
    const srcWarn = stageWarning(sourcePayload, stageCapsRef.current[SOURCE_SID]);
    if (srcWarn) out.push(`源: ${srcWarn}`);
    stagesGraph.forEach((e, i) => {
      if (e.parentSid == null || e.sid === SOURCE_SID) return;
      const w = stageWarning(downstreamPayloads[i], stageCapsRef.current[e.sid]);
      if (w) out.push(`阶段 ${i - 1}: ${w}`);
    });
    return out;
  }, [sourcePayload, stagesGraph, downstreamPayloads, stageCapsRef]);

  const graphNodes = useMemo<GraphNodeModel[]>(() => {
    const srcOption = sourcePayload
      ? globalModelOptions.find(
          (o) => o.key === `${sourcePayload.ml_backend_id}::${sourcePayload.model_id}`,
        )
      : null;
    const srcShape = sourceNodeShape(srcMeta, srcOption?.model);
    return stagesGraph.map<GraphNodeModel>((e, i) => {
      if (e.parentSid == null) {
        return {
          sid: e.sid,
          parentSid: null,
          role: { label: "数据源", variant: "accent", icon: "box" },
          detail: srcShape.sourceTypeLabel,
          runState: "pending",
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
          detail: srcOption?.model.display_name ?? "请配置源模型",
          runState: "pending",
          producesGeometry: true,
          canAddChild: canAddChildAt(e.sid),
          conflict: false,
          ready: !!sourcePayload,
          backendName: srcOption?.backendName,
          modelId: srcOption?.model.id,
          taskType: srcOption?.model.task,
          warning: stageWarning(sourcePayload, stageCapsRef.current[SOURCE_SID]),
        };
      }
      const p = downstreamPayloads[i];
      return {
        sid: e.sid,
        parentSid: e.parentSid,
        role: roleOf(p),
        detail: detailOf(p),
        runState: "pending",
        producesGeometry: producesGeometry(p),
        canAddChild: canAddChildAt(e.sid),
        conflict: (conflictInfo.perCard[e.sid]?.size ?? 0) > 0,
        ready: p != null,
        backendName: nameOfBackend(p?.ml_backend_id),
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
    sourcePayload,
    srcMeta,
    stagesGraph,
    downstreamPayloads,
    canAddChildAt,
    conflictInfo,
    nameOfBackend,
    globalModelOptions,
  ]);

  const stageNumberBySid = useMemo(() => {
    const m: Record<string, number> = {};
    stagesGraph.forEach((e, i) => {
      if (e.parentSid != null) m[e.sid] = i - 1;
    });
    return m;
  }, [stagesGraph]);

  const hasDownstreamCards = stagesGraph.some((e) => e.parentSid != null && e.sid !== SOURCE_SID);

  const savePipeline = async () => {
    const name = pipelineName.trim();
    if (!name) {
      pushToast({ msg: "请输入编排名称", kind: "warning" });
      return;
    }
    if (!sourcePayload) {
      pushToast({ msg: "请配置源阶段模型", kind: "warning" });
      return;
    }
    if (hasDownstreamCards && !allDownstreamReady) {
      pushToast({ msg: "下游阶段未就绪", sub: "请配齐各阶段参数", kind: "warning" });
      return;
    }
    if (hasKeyConflict) {
      pushToast({
        msg: "存在属性键冲突",
        sub: `冲突键: ${Array.from(conflictInfo.displayFinals).join("、")}`,
        kind: "warning",
      });
      return;
    }

    const stages: PipelineStagePayload[] = stagesGraph.flatMap<PipelineStagePayload>((e, i) => {
      if (e.parentSid == null) return [];
      if (e.sid === SOURCE_SID)
        return [
          {
            ...(sourcePayload as PipelineStagePayload),
            stage: 0,
            source: srcMeta,
          } as PipelineStagePayload,
        ];
      return [
        {
          ...(downstreamPayloads[i] as PipelineStagePayload),
          stage: i - 1,
          parent_stage: stageNumberBySid[e.parentSid],
        } as PipelineStagePayload,
      ];
    });

    try {
      await createProjectPipeline.mutateAsync({
        name,
        scope: pipelineScope,
        project_id: null,
        organization_id: null,
        stages,
        is_default: false,
      });
      pushToast({
        msg: pipelineScope === "public" ? "已保存为公共编排" : "已保存为组织编排",
        sub: `${stages.length} 阶段 · 可在项目预标入口套用`,
        kind: "success",
      });
    } catch (e) {
      pushToast({
        msg: "保存编排失败",
        sub: (e as Error).message,
        kind: "warning",
      });
    }
  };

  useEffect(() => {
    if (!stagesGraph.some((e) => e.sid === selectedSid)) {
      setSelectedSid(SOURCE_SID);
    }
  }, [selectedSid, stagesGraph, setSelectedSid]);

  const projectPipelinesQ = useProjectPipelines(undefined);
  const deleteProjectPipeline = useDeleteProjectPipeline();
  const libraryPipelines = useMemo(
    () =>
      (projectPipelinesQ.data ?? []).filter(
        (p) => p.scope === "public" || p.scope === "organization",
      ),
    [projectPipelinesQ.data],
  );

  const libraryPagination = useListPagination(libraryPipelines);

  const loadFromPipeline = useCallback(
    (p: ProjectPipeline) => {
      resetComposer();
      setLoadedPipelineId(p.id);
      setPipelineName(p.name);
      if (p.scope === "public" || p.scope === "organization") {
        setPipelineScope(p.scope);
      }
      const stages = p.stages ?? [];
      if (stages.length === 0) return;
      setLoadedSourceSeed(stages[0]);
      setSourcePayload(stages[0]);
      const newGraph: StageEntry[] = [
        { sid: ROOT_SID, parentSid: null },
        { sid: SOURCE_SID, parentSid: ROOT_SID },
      ];
      const seed: Record<string, PipelineStagePayload> = {};
      for (let i = 1; i < stages.length; i++) {
        const s = stages[i];
        const sid = `loaded-${s.stage}`;
        const parentSid =
          s.parent_stage == null || s.parent_stage === 0 ? SOURCE_SID : `loaded-${s.parent_stage}`;
        newGraph.push({ sid, parentSid });
        seed[sid] = s;
      }
      setStagesGraph(newGraph);
      setLoadedDownstreamSeedBySid(seed);
      setSelectedSid(SOURCE_SID);
      pushToast({
        msg: "已加载到画布",
        sub: `${p.name} · ${stages.length} 阶段`,
        kind: "success",
      });
    },
    [resetComposer, setStagesGraph, setSelectedSid, pushToast],
  );

  const onDeletePipeline = (id: string, name: string) => {
    deleteProjectPipeline.mutate(id, {
      onSuccess: () => pushToast({ msg: "已删除命名编排", sub: name, kind: "success" }),
      onError: (e) =>
        pushToast({
          msg: "删除命名编排失败",
          sub: (e as Error).message,
          kind: "warning",
        }),
    });
  };

  return (
    <div className={styles.page}>
      <Card>
        <div className={styles.toolbar}>
          <div className={styles.toolbarGroup}>
            <label className={styles.field}>
              <span className={styles.fieldLabel}>保存名称</span>
              <input
                className={styles.textInput}
                value={pipelineName}
                onChange={(e) => setPipelineName(e.target.value)}
                placeholder="例如 detect → 车辆属性"
              />
            </label>
            <label className={styles.field}>
              <span className={styles.fieldLabel}>可见范围</span>
              <select
                className={styles.selectInput}
                value={pipelineScope}
                onChange={(e) =>
                  setPipelineScope(
                    e.target.value as Extract<ProjectPipelineScope, "public" | "organization">,
                  )
                }
                aria-label="全局编排可见范围"
              >
                <option value="public">公共</option>
                <option value="organization">组织</option>
              </select>
              {pipelineScope === "organization" && (
                <span className={styles.hint}>
                  页面尚不能指定组织，选择组织范围保存会被拒绝；请通过 API 创建并指定
                  organization_id。
                </span>
              )}
            </label>
          </div>
          <div className={styles.actions}>
            <Button
              size="sm"
              variant="primary"
              onClick={savePipeline}
              disabled={!sourcePayload || createProjectPipeline.isPending}
              title="把画布上的编排保存为命名编排"
            >
              <Icon name="save" size={12} />
              保存公共编排
            </Button>
          </div>
        </div>

        {allWarnings.length > 0 && (
          <div className={styles.warnBar}>
            <Icon name="warning" size={12} />
            <div>
              <strong>{allWarnings.length} 个警示</strong>
              <ul className={styles.warnList}>
                {allWarnings.map((w, i) => (
                  <li key={i}>{w}</li>
                ))}
              </ul>
            </div>
          </div>
        )}

        {hasKeyConflict && (
          <div className={styles.warnBar}>
            <Icon name="warning" size={12} />
            <span>
              多个阶段写同一属性键：
              {Array.from(conflictInfo.displayFinals).join("、")}
              。保存会被拒绝，请调整各阶段的属性标签或写回键。
            </span>
          </div>
        )}

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
            {uniqueBackendCount < 2 ? (
              <span className={styles.stageEmptyHint}>
                下游阶段需要兼容裁剪图或框提示的非交互、非组合模型；请查看已注册服务的模型能力。
              </span>
            ) : !hasDownstreamCards ? (
              <span className={styles.stageEmptyHint}>
                从源模型的 <Icon name="plus" size={10} /> 拖出（或点 +）加下游阶段：源出框后，
                下游对每个框跑分类/检测子物体。
              </span>
            ) : null}
          </div>

          <div className={styles.editorInspector}>
            <div hidden={selectedSid !== ROOT_SID}>
              <strong className={styles.sectionTitle}>输入节点 · 数据源</strong>
              <label className={styles.field}>
                <span>数据类型</span>
                <select
                  className={styles.selectInput}
                  value={srcDataType}
                  onChange={(e) => setSrcDataType(e.target.value as "image" | "video")}
                >
                  <option value="image">图像</option>
                  <option value="video">视频</option>
                </select>
              </label>
              {srcDataType === "video" && (
                <label className={styles.field}>
                  <span>执行单位</span>
                  <select
                    className={styles.selectInput}
                    value={srcExecUnit}
                    onChange={(e) => setSrcExecUnit(e.target.value as "video" | "frame")}
                  >
                    <option value="video">整段序列</option>
                    <option value="frame">逐帧</option>
                  </select>
                </label>
              )}
              <div className={styles.stageEmptyHint}>
                {isVideoTracking
                  ? "对整段视频做多目标追踪，生成跨帧轨迹；源模型只列追踪模型，下游对轨迹补充结果。"
                  : srcDataType === "video"
                    ? "对每一帧独立做图像检测，生成无跨帧关联的单帧框；下游对检出框富集。"
                    : "源模型只列图像检测；下游对检出框富集。"}
              </div>
            </div>
            <GlobalStageInspector
              key={loadedPipelineId ?? "source-new"}
              kind="source"
              stageIndex={0}
              pool={sourcePool}
              value={sourcePayload ?? loadedSourceSeed}
              onChange={onSourceChange}
              onCaps={onSourceCaps}
              hidden={selectedSid !== SOURCE_SID}
              displayIndex={1}
            />
            {stagesGraph.map((e, i) =>
              e.parentSid == null || e.sid === SOURCE_SID ? null : (
                <GlobalStageInspector
                  key={e.sid}
                  kind="stage"
                  stageIndex={i - 1}
                  parentStageIndex={stageNumberBySid[e.parentSid]}
                  pool={globalModelOptions}
                  parentClasses={classesForSid(e.parentSid)}
                  value={downstreamPayloads[i] ?? loadedDownstreamSeedBySid[e.sid] ?? null}
                  onChange={(payload) => onStageChange(e.sid, payload)}
                  onCaps={(caps) => onStageCaps(e.sid, caps)}
                  onRemove={() => removeStage(e.sid)}
                  warning={stageWarning(downstreamPayloads[i], stageCapsRef.current[e.sid])}
                  conflictKeys={conflictInfo.perCard[e.sid]}
                  hidden={selectedSid !== e.sid}
                  displayIndex={i}
                />
              ),
            )}
          </div>
        </div>
      </Card>

      <Card>
        <div className={styles.libraryHeader}>
          <strong className={styles.sectionTitle}>命名编排库（公共 / 组织）</strong>
          <span className={styles.hint}>列出公共和组织编排；可加载到画布编辑，或删除。</span>
        </div>
        {projectPipelinesQ.isLoading ? (
          <div className={styles.stageEmptyHint}>加载中…</div>
        ) : libraryPipelines.length === 0 ? (
          <div className={styles.stageEmptyHint}>
            暂无命名编排。保存一条上方画布后会出现在这里。
          </div>
        ) : (
          <ul className={listStyles.surface}>
            {libraryPagination.items.map((p) => (
              <li
                key={p.id}
                className={`${listStyles.row} flex flex-wrap items-center justify-between gap-3`}
              >
                <div className={styles.libraryRowMain}>
                  <strong>{p.name}</strong>
                  <span className={styles.libraryMeta}>
                    {SCOPE_LABELS[p.scope]} · {p.stages.length} 阶段 · 已套用 {p.usage_count ?? 0}{" "}
                    次
                  </span>
                </div>
                <div className={styles.libraryRowActions}>
                  <Button
                    size="sm"
                    variant="default"
                    onClick={() => loadFromPipeline(p)}
                    title="把这条编排回填到上方画布, 可继续编辑保存为新条"
                  >
                    <Icon name="download" size={12} />
                    加载编辑
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => onDeletePipeline(p.id, p.name)}
                    disabled={deleteProjectPipeline.isPending}
                    title="删除该命名编排 (仅所有者/超管可删)"
                  >
                    <Icon name="trash" size={12} />
                    删除
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        )}
        <ListPagination
          label="命名编排"
          {...libraryPagination}
          loading={projectPipelinesQ.isLoading}
          onPageChange={libraryPagination.setPage}
          onPageSizeChange={libraryPagination.setPageSize}
        />
      </Card>
    </div>
  );
}

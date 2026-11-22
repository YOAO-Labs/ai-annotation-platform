import { useEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Badge } from "@/components/ui/Badge";
import { Card } from "@/components/ui/Card";
import { ProgressBar } from "@/components/ui/ProgressBar";
import { hasInput, INPUT_BBOX_PROMPT_ID, INPUT_CROP_ID } from "@/api/capabilityInputs";
import { useCropRecognitionConfig, usePreannotateConfig } from "./usePreannotateConfig";
import { CatalogModelSelect } from "./CatalogModelSelect";
import { PreannotateConfigForm } from "./PreannotateConfigForm";
import { ChipMultiSelect } from "./ChipMultiSelect";
import type { PipelineStagePayload, PipelineStageStat } from "@/hooks/usePreannotation";
import { classifyDownstream, type StageCaps } from "../utils/pipelineGraph";
import styles from "./ProjectDetailPanel.module.css";

const RUN_STATE_BADGE: Record<
  string,
  { variant: "outline" | "ai" | "success" | "danger"; label: string; dot?: boolean }
> = {
  pending: { variant: "outline", label: "待运行" },
  running: { variant: "ai", label: "运行中", dot: true },
  done: { variant: "success", label: "已完成" },
  failed: { variant: "danger", label: "失败" },
};

function cx(...names: Array<string | false | null | undefined>) {
  return names.filter(Boolean).join(" ");
}

interface Props {
  id: string;
  displayIndex: number;
  projectId: string;
  backends: Array<{ id: string; name: string }>;
  projectMlBackendId?: string | null;
  sourceBackendId: string | null;
  projectClasses: string[];
  parentClassOptions?: string[];
  projectAttributeKeys: string[];
  conflictKeys?: Set<string>;
  stat?: PipelineStageStat;
  runState?: "pending" | "running" | "done";
  hidden?: boolean;
  onChange: (id: string, payload: PipelineStagePayload | null) => void;
  onCaps?: (id: string, caps: StageCaps | null) => void;
  onRemove: (id: string) => void;
}

export function StageCard({
  id,
  displayIndex,
  projectId,
  backends,
  projectMlBackendId,
  sourceBackendId,
  projectClasses,
  parentClassOptions,
  projectAttributeKeys,
  conflictKeys,
  stat,
  runState = "pending",
  hidden = false,
  onChange,
  onCaps,
  onRemove,
}: Props) {
  const [backendId, setBackendId] = useState<string | null>(() => {
    const other = backends.find((b) => b.id !== sourceBackendId);
    return (other ?? backends[0])?.id ?? null;
  });
  const cfg = usePreannotateConfig({ projectId, backendId, inputMode: "crop" });

  const [classFilter, setClassFilter] = useState<Set<string>>(new Set());
  const [pad, setPad] = useState(0.05);
  const [writeKeys, setWriteKeys] = useState<Set<string>>(new Set());
  const [label, setLabel] = useState("");

  const backendAttrOptions = useMemo(() => {
    const models = cfg.capabilitiesQ.data?.models ?? [];
    const seen = new Map<string, string>();
    for (const m of models) {
      for (const a of m.output_attribute_schema ?? []) {
        if (a?.key && !seen.has(a.key)) seen.set(a.key, a.label || a.key);
      }
    }
    return Array.from(seen, ([value, label]) => ({ value, label }));
  }, [cfg.capabilitiesQ.data]);

  const attrKeyOptions =
    backendAttrOptions.length > 0
      ? backendAttrOptions
      : projectAttributeKeys.map((k) => ({ value: k, label: k }));

  const capabilitiesReady = !cfg.capabilitiesQ.isLoading && cfg.capabilitiesQ.data != null;
  const producesAttributes = useMemo(() => {
    const models = cfg.capabilitiesQ.data?.models ?? [];
    return models.some(
      (m) =>
        (m.output_attribute_schema?.length ?? 0) > 0 || (m.output_attribute_types?.length ?? 0) > 0,
    );
  }, [cfg.capabilitiesQ.data]);
  const showNoAttrWarning = backendId != null && capabilitiesReady && !producesAttributes;

  const downstreamModels = useMemo(() => {
    const models = cfg.capabilitiesQ.data?.models ?? [];
    return models.filter(
      (m) =>
        m.composition !== "composite" &&
        !m.is_interactive &&
        (m.task === "classify" ||
          m.task === "detect" ||
          (m.task === "ocr" && hasInput(m.supported_inputs, INPUT_CROP_ID)) ||
          (m.task === "segment" && (m.supported_prompts ?? []).includes("bbox"))),
    );
  }, [cfg.capabilitiesQ.data]);

  const [selectedModelId, setSelectedModelId] = useState<string | null>(null);
  useEffect(() => {
    setSelectedModelId((prev) => {
      if (prev && downstreamModels.some((m) => m.id === prev)) return prev;
      const classify = downstreamModels.find((m) => m.task === "classify");
      return classify?.id ?? downstreamModels[0]?.id ?? null;
    });
  }, [backendId, downstreamModels]);
  const selectedModel = cfg.selectedCatalogId
    ? (cfg.geometricModel ?? null)
    : (downstreamModels.find((m) => m.id === selectedModelId) ?? null);

  const { isBoxSegGeometry, isCropDetectGeometry, isGeometryDownstream, isOcrRecognize } =
    classifyDownstream(selectedModel);

  const cropConfig = useCropRecognitionConfig(
    projectId,
    cfg.backendId,
    isOcrRecognize ? selectedModel : null,
  );
  const stageArgs = isOcrRecognize
    ? cropConfig.buildArgs("skip_predicted")
    : cfg.configReady
      ? cfg.buildArgs("skip_predicted")
      : null;
  const stageArgsKey = JSON.stringify(stageArgs);
  const paramsKey = JSON.stringify(cfg.paramsValue);
  const variantSliceKey = JSON.stringify(cfg.currentVariantSlice);
  const payload = useMemo<Omit<PipelineStagePayload, "stage" | "parent_stage"> | null>(() => {
    const classArr = Array.from(classFilter);
    if (cfg.selectedCatalogId && !stageArgs) return null;
    if (isGeometryDownstream && cfg.backendId && selectedModel) {
      const axisKeys = new Set((selectedModel.supported_variants ?? []).map((g) => g.key));
      const variantSlice: Record<string, string> = {};
      for (const [k, v] of Object.entries(cfg.currentVariantSlice)) {
        if (axisKeys.has(k)) variantSlice[k] = v;
      }
      return {
        ml_backend_id: cfg.backendId,
        model_id:
          stageArgs?.model_id && cfg.selectedCatalogId ? stageArgs.model_id : selectedModel.id,
        task: selectedModel.task,
        model_variants: cfg.selectedCatalogId ? stageArgs?.model_variants : variantSlice,
        params: cfg.selectedCatalogId
          ? (stageArgs?.params ?? {})
          : Object.fromEntries(
              Object.entries(cfg.paramsValue).filter(
                ([key]) => key in (selectedModel.params?.properties ?? {}),
              ),
            ),
        ...(isCropDetectGeometry && cfg.selectedClassIdx.size
          ? { class_filter: [...cfg.selectedClassIdx].sort((a, b) => a - b) }
          : {}),
        parent_class_filter: classArr.length > 0 ? classArr : undefined,
        ...(isCropDetectGeometry
          ? {
              roi: { mode: "crop" as const, pad },
              input: { mode: "crop" as const },
              write: { target: "geometry" as const },
            }
          : { roi: { mode: "geometry" as const, pad }, write: { target: "geometry" as const } }),
      };
    }
    if (!stageArgs || (!selectedModel && stageArgs.task === "ocr")) return null;
    const keyArr = Array.from(writeKeys);
    const labelTrim = label.trim();
    const downstreamAxisKeys = new Set((selectedModel?.supported_variants ?? []).map((g) => g.key));
    const downstreamVariants: Record<string, string> = {};
    for (const [k, v] of Object.entries(cfg.currentVariantSlice)) {
      if (downstreamAxisKeys.has(k)) downstreamVariants[k] = v;
    }
    return {
      ml_backend_id: stageArgs.ml_backend_id,
      model_id: selectedModel?.id ?? stageArgs.model_id,
      task: selectedModel?.task ?? stageArgs.task,
      model_variants: isOcrRecognize ? stageArgs.model_variants : downstreamVariants,
      params: stageArgs.params,
      parent_class_filter: classArr.length > 0 ? classArr : undefined,
      roi: { mode: "crop", pad },
      ...(labelTrim ? { label: labelTrim } : {}),
      write: {
        target: "attributes",
        ...(keyArr.length > 0 ? { keys: keyArr } : {}),
      },
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    stageArgsKey,
    isGeometryDownstream,
    isCropDetectGeometry,
    isOcrRecognize,
    backendId,
    selectedModel?.id,
    variantSliceKey,
    cfg.selectedCatalogId,
    cfg.backendId,
    cfg.selectedClassIdx,
    paramsKey,
    classFilter,
    pad,
    writeKeys,
    label,
  ]);

  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;
  useEffect(() => {
    onChangeRef.current(id, payload as PipelineStagePayload | null);
  }, [id, payload]);
  useEffect(() => {
    return () => onChangeRef.current(id, null);
  }, [id]);

  const selectedModelTypes = selectedModel?.output_attribute_types ?? [];
  const producesClass =
    selectedModelTypes.length > 0 ? selectedModelTypes.includes("class") : undefined;
  const showNoClassWarning =
    !isGeometryDownstream && !isOcrRecognize && capabilitiesReady && producesClass === false;
  const schemaKeySet = new Set(
    (selectedModel?.output_attribute_schema ?? [])
      .map((a) => a?.key)
      .filter((k): k is string => !!k),
  );
  const unknownWriteKeys =
    schemaKeySet.size > 0 ? Array.from(writeKeys).filter((k) => !schemaKeySet.has(k)) : [];

  const rpBatchable = selectedModel?.resource_profile?.batchable;
  const batchable = typeof rpBatchable === "boolean" ? rpBatchable : undefined;
  const showNotBatchableWarning = capabilitiesReady && batchable === false;

  const supportedInputs = selectedModel?.supported_inputs ?? [];
  const supportedInputsKey = supportedInputs.join(",");
  const outputClassesKey = JSON.stringify(
    (selectedModel?.classes ?? [])
      .filter((c) => !cfg.selectedClassIdx.size || cfg.selectedClassIdx.has(c.index))
      .map((c) => cfg.selectedCatalogEntry?.output_classes?.[c.index] ?? c.name),
  );
  const caps = useMemo<StageCaps>(
    () => ({
      hasCapabilities: capabilitiesReady,
      knownInputs: supportedInputs.length > 0,
      acceptsCrop: hasInput(supportedInputs, INPUT_CROP_ID),
      acceptsBboxPrompt: hasInput(supportedInputs, INPUT_BBOX_PROMPT_ID),
      producesAttributes,
      producesClass,
      batchable,
      outputClasses: JSON.parse(outputClassesKey) as string[],
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [
      supportedInputsKey,
      capabilitiesReady,
      producesAttributes,
      producesClass,
      batchable,
      outputClassesKey,
    ],
  );
  const onCapsRef = useRef(onCaps);
  onCapsRef.current = onCaps;
  useEffect(() => {
    onCapsRef.current?.(id, caps);
  }, [id, caps]);
  useEffect(() => {
    return () => onCapsRef.current?.(id, null);
  }, [id]);

  const badge = RUN_STATE_BADGE[runState] ?? RUN_STATE_BADGE.pending;
  const targeted = stat?.targeted ?? 0;
  const okPct = targeted > 0 ? ((stat?.ok ?? 0) / targeted) * 100 : 0;

  return (
    <Card className={cx(styles.stageCard, hidden && styles.stageHidden)}>
      <div className={styles.stageCardHeader}>
        <span className={styles.stageRole}>
          <Icon name="brain" size={13} />
          <Badge variant="accent">
            {isCropDetectGeometry
              ? "检测"
              : isBoxSegGeometry
                ? "分割"
                : isOcrRecognize
                  ? "识别"
                  : "分类"}
          </Badge>
          <strong className={styles.sectionTitle}>阶段 {displayIndex}</strong>
        </span>
        <span className={styles.stageHeaderRight}>
          <Badge variant={badge.variant} dot={badge.dot}>
            {badge.label}
          </Badge>
          <Button size="sm" variant="ghost" onClick={() => onRemove(id)} title="移除该阶段">
            <Icon name="trash" size={11} />
          </Button>
        </span>
      </div>

      {stat && (
        <div className={styles.stageRun}>
          <ProgressBar value={okPct} />
          <div className={styles.stageCounts}>
            <span className={styles.stageCount}>
              <span className={styles.stageCountLabel}>目标</span>
              <span className={styles.stageCountValue}>{targeted}</span>
            </span>
            <span className={styles.stageCount}>
              <span className={styles.stageCountLabel}>成功</span>
              <span className="text-status-positive">{stat.ok ?? 0}</span>
            </span>
            {(stat.failed ?? 0) > 0 && (
              <span className={styles.stageCount}>
                <span className={styles.stageCountLabel}>失败</span>
                <span className="text-status-caution">{stat.failed}</span>
              </span>
            )}
            {(stat.skipped_geometry ?? 0) > 0 && (
              <span className={cx(styles.stageCount, styles.mutedText)}>
                <span className={styles.stageCountLabel}>几何跳过</span>
                <span>{stat.skipped_geometry}</span>
              </span>
            )}
          </div>
        </div>
      )}

      {backends.length > 1 && (
        <label className={styles.field}>
          <span className={styles.fieldLabel}>模型服务</span>
          <select
            value={backendId ?? ""}
            onChange={(e) => {
              cfg.setCatalogModel?.("");
              setBackendId(e.target.value || null);
            }}
            className={styles.promptInput}
          >
            {backends.map((b) => (
              <option key={b.id} value={b.id}>
                {b.name}
                {b.id === projectMlBackendId ? "（主服务）" : ""}
              </option>
            ))}
          </select>
        </label>
      )}

      <CatalogModelSelect cfg={cfg} />
      {!cfg.selectedCatalogId && downstreamModels.length > 1 && (
        <label className={styles.field}>
          <span className={styles.fieldLabel}>下游模型</span>
          <select
            className={styles.promptInput}
            value={selectedModelId ?? ""}
            onChange={(e) => setSelectedModelId(e.target.value || null)}
          >
            {downstreamModels.map((m) => (
              <option key={m.id} value={m.id}>
                {(m.display_name || m.id) +
                  (m.task === "segment"
                    ? "（框→分割）"
                    : m.task === "ocr"
                      ? "（识别）"
                      : m.task === "detect"
                        ? "（检测）"
                        : "（分类）")}
              </option>
            ))}
          </select>
        </label>
      )}

      {isCropDetectGeometry ? (
        <span className={styles.mutedText}>
          下游模型：{selectedModel?.display_name || selectedModel?.id}（在父框 crop 上检测子物体，
          检出几何回映回原图坐标并追加为新框；可作下游阶段的父）
        </span>
      ) : isBoxSegGeometry ? (
        <span className={styles.mutedText}>
          下游模型：{selectedModel?.display_name || selectedModel?.id}（框→分割：消费上游检测框出
          掩膜，无需提示词；上方提示词和输出形态对本阶段不生效，仅 SAM 变体有效）
        </span>
      ) : isOcrRecognize ? (
        <span className={styles.mutedText}>
          下游模型：{selectedModel?.display_name || selectedModel?.id}（在父框 crop 上识别文本，
          写回父框属性；仅使用当前识别模型声明的变体和推理参数）
        </span>
      ) : (
        selectedModel && (
          <span className={styles.mutedText}>
            下游模型：{selectedModel.display_name || selectedModel.id}（纯分类，跳过检测）
          </span>
        )
      )}

      {!isGeometryDownstream && showNoAttrWarning && (
        <div className={styles.stageWarn}>
          <Icon name="warning" size={12} />
          <span>
            该模型未声明输出属性，作下游分类只会重新检测、属性恒空。请改选会产属性的模型。
          </span>
        </div>
      )}

      {showNotBatchableWarning && (
        <div className={styles.stageWarn}>
          <Icon name="warning" size={12} />
          <span>
            该模型为交互/有状态模型（batchable=false），不能用于批量预标流水线，运行将被端点拒绝。
          </span>
        </div>
      )}

      {showNoClassWarning && (
        <div className={styles.stageWarn}>
          <Icon name="warning" size={12} />
          <span>
            该模型不产类别属性（output_attribute_types 不含
            class），作分类下游属性恒空，运行将被端点拒绝。
          </span>
        </div>
      )}

      <div className={styles.field}>
        <span className={styles.fieldLabel}>父框类别（留空=对全部父框跑；按检测框类名匹配）</span>
        <ChipMultiSelect
          options={(parentClassOptions?.length ? parentClassOptions : projectClasses).map((c) => ({
            value: c,
            label: c,
          }))}
          selected={classFilter}
          onChange={setClassFilter}
          allowFreeText
          freeTextPlaceholder="输入类名添加（匹配检测框类名）"
          emptyHint="留空=对全部父框跑"
        />
      </div>

      {!isBoxSegGeometry && (
        <div className={styles.field}>
          <span className={styles.fieldLabel}>ROI 扩展 pad（0–0.5）</span>
          <input
            className={styles.textInput}
            type="number"
            min={0}
            max={0.5}
            step={0.01}
            value={pad}
            onChange={(e) => {
              const v = parseFloat(e.target.value);
              if (!Number.isNaN(v)) setPad(Math.min(0.5, Math.max(0, v)));
            }}
          />
        </div>
      )}

      {!isGeometryDownstream && (
        <div className={styles.field}>
          <span className={styles.fieldLabel}>
            子物体命名（可选，写回属性键前缀，如 hat → hat_color）
          </span>
          <input
            className={styles.textInput}
            type="text"
            placeholder="留空=不加前缀"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
          />
        </div>
      )}

      {!isGeometryDownstream && (
        <div className={styles.field}>
          <span className={styles.fieldLabel}>
            写回属性键（留空=接收下游返回的全部键）
            {backendAttrOptions.length === 0 && attrKeyOptions.length > 0 && (
              <span className={styles.fieldHint}> · 选项来自项目属性定义</span>
            )}
          </span>
          <ChipMultiSelect
            options={attrKeyOptions}
            selected={writeKeys}
            onChange={setWriteKeys}
            conflictKeys={conflictKeys}
            emptyHint="下游模型未声明输出属性，且项目无属性字段；留空=接收全部键"
          />
          {unknownWriteKeys.length > 0 && (
            <div className={styles.stageWarn}>
              <Icon name="warning" size={12} />
              <span>
                属性键 {unknownWriteKeys.join("、")} 不在该模型声明的输出属性中，可能不会被产出。
              </span>
            </div>
          )}
        </div>
      )}

      <PreannotateConfigForm
        cfg={cfg}
        projectMlBackendId={projectMlBackendId}
        hideModelTaskSelector
        hideClassWhitelist={!isCropDetectGeometry}
        stageParameters={isOcrRecognize ? cropConfig : undefined}
      />
    </Card>
  );
}

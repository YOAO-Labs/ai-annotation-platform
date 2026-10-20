/**
 * v0.21.0 收尾优化 · 全局编排页阶段 Inspector.
 *
 * 与项目侧 StageCard 对齐, 字段分两类:
 *   ① 手配 (用户填): 模型选择 / 模型变体 (version/size/lang 轴) / 类别 (源 class_filter、
 *      下游 parent_class_filter) / 写回属性键 write.keys / 子物体命名 label / ROI pad。
 *      候选真源全部取 CapabilityInstanceModel 现有字段 (classes / output_attribute_schema /
 *      supported_variants), 与项目侧同源; 项目侧那条"回落 projectClasses / 项目 attr schema"
 *      的候选链全局侧不需要 (编排与项目解耦)。
 *   ② 内生 (选完 model 自动派生, 不给下拉): roi.mode / input.mode / write.target ——
 *      由下游 model 的 task/prompts/composition 定死 (与 StageCard 派生逻辑逐条一致):
 *        - 检测 (detection 非交互):    roi=crop,     input=crop, write=geometry
 *        - 分割 (segmentation+bbox):    roi=geometry,             write=geometry
 *        - 识别 (ocr 原子) / 分类:      roi=crop,                 write=attributes
 *      只读展示"阶段类型 + 投递/写回", 让用户看清将发生什么, 但不能选出 model 不支持的组合。
 *
 * 不做: params (推理阈值) —— 后端刻意不下发到 /instances (schema 注释"不暴露 params 等运维信息"),
 *   全局侧无数据源; 编排 copy-on-write 套用到项目后由项目侧 StageCard 拉 /setup 配 (见 plan)。
 */
import { useEffect, useMemo, useState } from "react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { VariantSelector } from "@/components/ml/VariantSelector";
import { hasInput, INPUT_BBOX_PROMPT_ID, INPUT_CROP_ID } from "@/api/capabilityInputs";
import type { CapabilityInstanceModel } from "@/api/mlCapabilities";
import type { MLBackendSupportedVariantGroup } from "@/api/ml-backends";
import type { PipelineStagePayload } from "@/hooks/usePreannotation";
import {
  deriveDownstreamShape,
  sourceNodeShape,
  type DownstreamShape,
  type StageCaps,
} from "../utils/pipelineGraph";
import { ChipMultiSelect } from "./ChipMultiSelect";
import styles from "./ProjectDetailPanel.module.css";

export interface GlobalModelOption {
  key: string;
  backendId: string;
  backendName: string;
  model: CapabilityInstanceModel;
  disabled: boolean;
}

interface Props {
  kind: "source" | "stage";
  /** 阶段号 (源=0, 下游 1..N). 组装 payload 时用. */
  stageIndex: number;
  /** 下游卡才有; 源留 undefined. */
  parentStageIndex?: number;
  pool: GlobalModelOption[];
  /** 下游卡专用: 上游(父)阶段选中 model 的 classes.name 列表, 供父框类别 chip 候选; 空=只能自由文本. */
  parentClasses?: string[];
  /** 当前 payload (null=未选中 model, canvas 会渲染 "未就绪" 提示). */
  value: PipelineStagePayload | null;
  onChange: (payload: PipelineStagePayload | null) => void;
  onCaps?: (caps: StageCaps | null) => void;
  /** 下游卡才有; 源不可删. */
  onRemove?: () => void;
  /** 顶部 warning (容器侧从 stageWarning() 派生). */
  warning?: string | null;
  /** 键冲突时命中的 keys (容器传, 用于标红提示). */
  conflictKeys?: Set<string>;
  /** 检查器多张常挂载, 非选中 hidden. */
  hidden?: boolean;
  /** 显示标号 (源=1, 下游=2..N; 与项目侧 StageCard displayIndex 语义一致). */
  displayIndex: number;
}

function stageCapsFromModel(model: CapabilityInstanceModel | undefined): StageCaps | null {
  if (!model) return null;
  const supportedInputs = model.supported_inputs ?? [];
  const outputTypes = model.output_attribute_types ?? [];
  const rpBatchable = model.resource_profile?.batchable;
  return {
    hasCapabilities: true,
    knownInputs: supportedInputs.length > 0,
    acceptsCrop: hasInput(supportedInputs, INPUT_CROP_ID),
    acceptsBboxPrompt: hasInput(supportedInputs, INPUT_BBOX_PROMPT_ID),
    producesAttributes: outputTypes.length > 0 || (model.output_attribute_schema?.length ?? 0) > 0,
    producesClass: outputTypes.length > 0 ? outputTypes.includes("class") : undefined,
    batchable: typeof rpBatchable === "boolean" ? rpBatchable : undefined,
  };
}

export function GlobalStageInspector({
  kind,
  stageIndex,
  parentStageIndex,
  pool,
  parentClasses,
  value,
  onChange,
  onCaps,
  onRemove,
  warning,
  conflictKeys,
  hidden,
  displayIndex,
}: Props) {
  // ── 手配表单 state ─────────────────────────────────────
  const [modelKey, setModelKey] = useState<string>("");
  const [roiPad, setRoiPad] = useState<string>("0.05");
  const [writeKeysSet, setWriteKeysSet] = useState<Set<string>>(new Set());
  const [label, setLabel] = useState<string>("");
  const [parentClassSet, setParentClassSet] = useState<Set<string>>(new Set());
  // 源字段: class_filter 存 model 原生 class index (数字), UI 用 Set<string> 承载 chip 选中态,
  //   落 payload 前 Number() 转回. 空集=全部类别 (不下发 class_filter, 与项目侧一致).
  const [sourceClassIdxSet, setSourceClassIdxSet] = useState<Set<string>>(new Set());
  // 模型变体轴 (version/size/lang/series...): 源与下游共用. 数据取自 model 自报的 supported_variants
  //   (全局 /instances 已下发, 与项目侧同源); 空对象=未选, VariantSelector 按 default/recommended 回落.
  const [variantValue, setVariantValue] = useState<Record<string, unknown>>({});

  const optionByKey = useMemo(() => new Map(pool.map((o) => [o.key, o])), [pool]);
  const selectedOption = modelKey ? (optionByKey.get(modelKey) ?? null) : null;

  // 下游阶段的内生形态 (roi/input/write); 源阶段恒 null (源吃整图、恒产几何、不写属性)。
  const shape = useMemo<DownstreamShape | null>(
    () => (kind === "stage" && selectedOption ? deriveDownstreamShape(selectedOption.model) : null),
    [kind, selectedOption],
  );

  // 该 model 的变体轴 (顺序即 supported_variants 顺序); 组装 model_variants 时按此过滤.
  const variantGroups = useMemo<MLBackendSupportedVariantGroup[]>(
    () => (selectedOption?.model.supported_variants ?? []) as MLBackendSupportedVariantGroup[],
    [selectedOption],
  );
  const variantAxisKeys = useMemo(() => variantGroups.map((g) => g.key), [variantGroups]);
  const defaultVariants = useMemo(
    () => selectedOption?.model.default_variants ?? {},
    [selectedOption],
  );

  // 派生候选 chip 列表 (源类别/下游属性键).
  const sourceClassOptions = useMemo(() => {
    if (kind !== "source") return [];
    const classes = selectedOption?.model.classes ?? [];
    return classes.map((c) => ({
      value: String(c.index),
      label: `[${c.index}] ${c.name}`,
    }));
  }, [kind, selectedOption]);

  const writeKeyOptions = useMemo(() => {
    if (kind !== "stage") return [];
    const schema = selectedOption?.model.output_attribute_schema ?? [];
    return schema.filter((a) => !!a?.key).map((a) => ({ value: a.key, label: a.label || a.key }));
  }, [kind, selectedOption]);

  const parentClassChipOptions = useMemo(() => {
    if (kind !== "stage") return [];
    return (parentClasses ?? []).map((name) => ({ value: name, label: name }));
  }, [kind, parentClasses]);

  // 上抛 payload + caps.
  useEffect(() => {
    if (!selectedOption || selectedOption.disabled) {
      onChange(null);
      onCaps?.(null);
      return;
    }
    onCaps?.(stageCapsFromModel(selectedOption.model));

    // model_variants: 只保留本 model 声明的轴 (与项目侧 StageCard 语义一致). 每轴取值优先级
    //   用户显式选 > model 自报 default_variants; 都无=该轴不下发, 后端按启动默认解析. 空=整个不下发.
    const modelVariants: Record<string, string> = {};
    for (const k of variantAxisKeys) {
      const picked =
        typeof variantValue[k] === "string" && variantValue[k]
          ? (variantValue[k] as string)
          : defaultVariants[k];
      if (typeof picked === "string" && picked) modelVariants[k] = picked;
    }
    const hasVariants = Object.keys(modelVariants).length > 0;

    if (kind === "source") {
      const clsIdx = Array.from(sourceClassIdxSet)
        .map((t) => Number(t))
        .filter((n) => Number.isInteger(n) && n >= 0);
      onChange({
        stage: stageIndex,
        ml_backend_id: selectedOption.backendId,
        model_id: selectedOption.model.id,
        task: selectedOption.model.task,
        ...(hasVariants ? { model_variants: modelVariants } : {}),
        ...(clsIdx.length > 0
          ? { class_filter: Array.from(new Set(clsIdx)).sort((a, b) => a - b) }
          : {}),
      });
      return;
    }
    // 下游: roi/input/write 由 shape 内生; pad/write.keys/label/parent_class_filter 手配。
    if (!shape) return;
    const parsedPad = Number(roiPad);
    const pad = Number.isFinite(parsedPad) ? parsedPad : 0.05;
    const writeKeys = Array.from(writeKeysSet);
    const parentClassesArr = Array.from(parentClassSet);
    onChange({
      stage: stageIndex,
      ml_backend_id: selectedOption.backendId,
      model_id: selectedOption.model.id,
      task: selectedOption.model.task,
      parent_stage: parentStageIndex ?? 0,
      ...(hasVariants ? { model_variants: modelVariants } : {}),
      roi: { mode: shape.roiMode, pad },
      ...(shape.inputMode ? { input: { mode: shape.inputMode } } : {}),
      write: {
        target: shape.writeTarget,
        ...(shape.isAttributes && writeKeys.length > 0 ? { keys: writeKeys } : {}),
      },
      ...(shape.isAttributes && label.trim() ? { label: label.trim() } : {}),
      ...(parentClassesArr.length > 0 ? { parent_class_filter: parentClassesArr } : {}),
    });
    // 明确列出依赖 — 保证任一字段变更都重算 payload 并上抛.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    modelKey,
    selectedOption,
    kind,
    stageIndex,
    parentStageIndex,
    shape,
    roiPad,
    writeKeysSet,
    label,
    parentClassSet,
    sourceClassIdxSet,
    variantValue,
    variantAxisKeys,
    defaultVariants,
  ]);

  // 反向同步: value 从外部载入 (加载编辑某条命名编排时) → 回填手配字段.
  // 只在 modelKey 空且 value 有 ml_backend_id/model_id 时匹配一次, 避免与用户键入互撞.
  // roi.mode/input.mode/write.target 内生, 不回填 (选中 model 后自动派生一致)。
  useEffect(() => {
    if (modelKey || !value?.ml_backend_id || !value?.model_id) return;
    const key = `${value.ml_backend_id}::${value.model_id}`;
    if (!optionByKey.has(key)) return;
    setModelKey(key);
    if (value.model_variants && Object.keys(value.model_variants).length > 0) {
      setVariantValue({ ...value.model_variants });
    }
    if (kind === "stage") {
      if (value.roi?.pad != null) setRoiPad(String(value.roi.pad));
      if (value.write?.keys?.length) setWriteKeysSet(new Set(value.write.keys));
      if (value.label) setLabel(value.label);
      if (value.parent_class_filter?.length) setParentClassSet(new Set(value.parent_class_filter));
    } else {
      if (value.class_filter?.length)
        setSourceClassIdxSet(new Set(value.class_filter.map((n) => String(n))));
    }
  }, [modelKey, value, optionByKey, kind]);

  // v0.21.6 · 源模型阶段标题按 model.task 派生 (不 hardcode「检测」): 目标检测 / 视频追踪 参数。
  //   (输入节点已收敛为独立纯数据源, 此处是其子源模型 stage。)
  const roleLabel =
    kind === "source"
      ? `源模型 · ${sourceNodeShape(undefined, selectedOption?.model).role.label}参数`
      : `阶段 ${displayIndex} · 参数`;

  return (
    <div hidden={hidden}>
      <div className={styles.stageCardHeader}>
        <strong className={styles.sectionTitle}>{roleLabel}</strong>
        {kind === "stage" && onRemove && (
          <span className={styles.stageHeaderRight}>
            <Button size="sm" variant="ghost" onClick={onRemove} title="移除该阶段">
              <Icon name="trash" size={11} />
            </Button>
          </span>
        )}
      </div>

      {warning && (
        <div className={styles.stageWarn}>
          <Icon name="warning" size={12} />
          <span>{warning}</span>
        </div>
      )}

      <label className={styles.field}>
        <span className={styles.fieldLabel}>后端 · 模型</span>
        <select
          className={styles.selectInput}
          value={modelKey}
          disabled={pool.length === 0}
          onChange={(e) => setModelKey(e.target.value)}
          aria-label={kind === "source" ? "源阶段模型" : `阶段 ${displayIndex} 模型`}
        >
          {modelKey && !pool.some((option) => option.key === modelKey) && (
            <option value={modelKey} disabled hidden>
              已选模型（不适用于当前输入）
            </option>
          )}
          {pool.length === 0 ? (
            <option value="" disabled hidden>
              暂无全局模型
            </option>
          ) : (
            <>
              <option value="" disabled hidden>
                请选择模型
              </option>
              {pool.map((opt) => (
                <option key={opt.key} value={opt.key} disabled={opt.disabled}>
                  {opt.backendName} · {opt.model.display_name}
                  {opt.disabled ? " · 探测失败" : ""}
                </option>
              ))}
            </>
          )}
        </select>
      </label>

      {variantGroups.length > 0 && (
        <div className={styles.field}>
          <span className={styles.fieldLabel}>模型变体（版本 / 尺寸 / 语言等）</span>
          <VariantSelector
            supportedVariants={variantGroups}
            variantCombinations={selectedOption?.model.variant_combinations}
            defaults={defaultVariants}
            value={variantValue}
            onChange={setVariantValue}
          />
        </div>
      )}

      {kind === "source" ? (
        <div className={styles.field}>
          <span className={styles.fieldLabel}>类别白名单（留空=全部；勾选 model 原生类别）</span>
          {selectedOption == null ? (
            <span className={styles.fieldHint}>请先选择模型以加载类别表</span>
          ) : sourceClassOptions.length === 0 ? (
            <span className={styles.fieldHint}>
              该模型未在 capability 中自报 classes（可能需在项目侧预热后拉到）
            </span>
          ) : (
            <ChipMultiSelect
              options={sourceClassOptions}
              selected={sourceClassIdxSet}
              onChange={setSourceClassIdxSet}
              emptyHint="留空=全部类别"
            />
          )}
        </div>
      ) : (
        <>
          {/* 阶段类型 + 内生 roi/input/write (只读; 由选中 model 派生, 不可手选)。 */}
          {shape && (
            <div className={styles.field}>
              <span className={styles.fieldLabel}>阶段类型（由模型能力自动派生）</span>
              <div className={styles.readonlyValue}>
                {shape.role} · ROI {shape.roiMode}
                {shape.inputMode ? ` · 投递 ${shape.inputMode}` : ""} · 写回{" "}
                {shape.writeTarget === "attributes" ? "父框属性" : "独立形状"}
              </div>
            </div>
          )}

          <div className={styles.field}>
            <span className={styles.fieldLabel}>
              父框类别（留空=对全部父框跑；按检测框类名匹配）
            </span>
            <ChipMultiSelect
              options={parentClassChipOptions}
              selected={parentClassSet}
              onChange={setParentClassSet}
              allowFreeText
              freeTextPlaceholder="输入类名添加（匹配上游检测框类名）"
              emptyHint="留空=对全部父框跑"
            />
          </div>

          <label className={styles.field}>
            <span className={styles.fieldLabel}>ROI pad (0-1)</span>
            <input
              className={styles.textInput}
              type="number"
              min="0"
              max="1"
              step="0.01"
              value={roiPad}
              onChange={(e) => setRoiPad(e.target.value)}
            />
          </label>

          {shape?.isAttributes && (
            <>
              <div className={styles.field}>
                <span className={styles.fieldLabel}>写回属性键（留空=接收下游返回的全部键）</span>
                <ChipMultiSelect
                  options={writeKeyOptions}
                  selected={writeKeysSet}
                  onChange={setWriteKeysSet}
                  allowFreeText
                  freeTextPlaceholder="输入属性键添加（如 color）"
                  conflictKeys={conflictKeys}
                  emptyHint="留空=接收全部键"
                />
              </div>

              <label className={styles.field}>
                <span className={styles.fieldLabel}>标签前缀 label (作为写回键前缀)</span>
                <input
                  className={styles.textInput}
                  value={label}
                  onChange={(e) => setLabel(e.target.value)}
                  placeholder="例如: hat"
                />
              </label>
            </>
          )}
        </>
      )}
    </div>
  );
}

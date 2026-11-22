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
  stageIndex: number;
  parentStageIndex?: number;
  pool: GlobalModelOption[];
  parentClasses?: string[];
  value: PipelineStagePayload | null;
  onChange: (payload: PipelineStagePayload | null) => void;
  onCaps?: (caps: StageCaps | null) => void;
  onRemove?: () => void;
  warning?: string | null;
  conflictKeys?: Set<string>;
  hidden?: boolean;
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
  const [modelKey, setModelKey] = useState<string>("");
  const [roiPad, setRoiPad] = useState<string>("0.05");
  const [writeKeysSet, setWriteKeysSet] = useState<Set<string>>(new Set());
  const [label, setLabel] = useState<string>("");
  const [parentClassSet, setParentClassSet] = useState<Set<string>>(new Set());
  const [sourceClassIdxSet, setSourceClassIdxSet] = useState<Set<string>>(new Set());
  const [variantValue, setVariantValue] = useState<Record<string, unknown>>({});

  const optionByKey = useMemo(() => new Map(pool.map((o) => [o.key, o])), [pool]);
  const selectedOption = modelKey ? (optionByKey.get(modelKey) ?? null) : null;

  const shape = useMemo<DownstreamShape | null>(
    () => (kind === "stage" && selectedOption ? deriveDownstreamShape(selectedOption.model) : null),
    [kind, selectedOption],
  );

  const variantGroups = useMemo<MLBackendSupportedVariantGroup[]>(
    () => (selectedOption?.model.supported_variants ?? []) as MLBackendSupportedVariantGroup[],
    [selectedOption],
  );
  const variantAxisKeys = useMemo(() => variantGroups.map((g) => g.key), [variantGroups]);
  const defaultVariants = useMemo(
    () => selectedOption?.model.default_variants ?? {},
    [selectedOption],
  );

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

  useEffect(() => {
    if (!selectedOption || selectedOption.disabled) {
      onChange(null);
      onCaps?.(null);
      return;
    }
    onCaps?.(stageCapsFromModel(selectedOption.model));

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
        <span className={styles.fieldLabel}>模型服务 · 模型</span>
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
          <span className={styles.fieldLabel}>类别白名单（留空=全部；勾选模型原生类别）</span>
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

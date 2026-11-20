/**
 * VariantSelector · 能力声明协议 v2 的通用变体选择器
 *
 * 数据来源优先级 (高 → 低):
 * 1. props.supportedVariants (协议 v2 富 metadata 数组, axis_key 任意, 不限于
 *    sam_variant/dino_variant) — yolo 的 series/size 也走这条路径
 * 2. props.schema (JSON Schema enum, 老协议 v1 fallback)
 *
 * 联动 (协议 v2):
 * - props.variantCombinations 声明时, 第 N+1 轴的可选项受前 N 轴当前值约束.
 *   yolo 例: series=yolov9 → size 只显示 {t,s,m,c,e} (没有 n/l/x).
 *   未声明 → 按 axes 笛卡尔积渲染 (gsam2 sam_variant × dino_variant 是真笛卡尔积).
 *
 * 初值 (协议 v2):
 * - props.defaults: 当 value 没该 axis_key 时用 defaults; 项目级偏好覆盖了 backend
 *   默认时, 调用方 (ProjectDetailPanel 等) 合并出 defaults 传入.
 * - schema.default 仍作最末 fallback (老 backend 兼容).
 */

import type {
  MLBackendSupportedVariantGroup,
  MLBackendSupportedVariantOption,
} from "@/api/ml-backends";
import {
  VARIANT_FIELD_KEYS,
  isVariantField,
  type JsonSchemaField,
  type JsonSchemaObject,
} from "@/pages/Workbench/components/SchemaForm";
import styles from "./VariantSelector.module.css";

interface VariantSelectorProps {
  schema?: JsonSchemaObject;
  supportedVariants?: MLBackendSupportedVariantGroup[];
  /** 多轴非真笛卡尔积时声明合法组合; inner array 与 supportedVariants 轴顺序一致. */
  variantCombinations?: string[][];
  /** backend 自报 / 项目级合并后的默认 variant 组合 (axis_key → value). */
  defaults?: Record<string, string>;
  value: Record<string, unknown>;
  onChange: (next: Record<string, unknown>) => void;
  disabled?: boolean;
  /**
   * 紧凑内联模式 (工作台交互工具栏浮块用): 每轴渲染为带轴名前缀的裸 select, 横排排列,
   * 不显示显存/推荐 pill 与描述 note。复用同一套 normalizeFields / 联动逻辑。
   */
  compact?: boolean;
}

interface NormalizedVariantField {
  key: string;
  title: string;
  description?: string;
  fallback: string;
  options: MLBackendSupportedVariantOption[];
}

function asField(raw: unknown): JsonSchemaField | null {
  return raw && typeof raw === "object" ? (raw as JsonSchemaField) : null;
}

function optionLabel(option: MLBackendSupportedVariantOption) {
  const meta = [
    option.vram_gb != null ? `${option.vram_gb}GB` : null,
    option.tier ? tierLabel(option.tier) : null,
    option.recommended ? "推荐" : null,
  ].filter(Boolean);
  return meta.length > 0
    ? `${option.label ?? option.value} · ${meta.join(" · ")}`
    : (option.label ?? option.value);
}

function tierLabel(tier: string) {
  if (tier === "fast") return "快速";
  if (tier === "balanced") return "均衡";
  if (tier === "accurate") return "精度";
  return tier;
}

function tierClass(tier: string | undefined) {
  if (tier === "fast") return styles.tierFast;
  if (tier === "balanced") return styles.tierBalanced;
  if (tier === "accurate") return styles.tierAccurate;
  return undefined;
}

function normalizeOptions(raw: MLBackendSupportedVariantOption[] | undefined) {
  if (!Array.isArray(raw)) return [];
  return raw.filter((item): item is MLBackendSupportedVariantOption => {
    return item != null && typeof item === "object" && typeof item.value === "string";
  });
}

/**
 * 派生当前轴的可选项, 应用 variantCombinations 联动约束.
 * @param axisIndex 当前轴在 supportedVariants 中的下标
 * @param baseOptions 该轴的全部 metadata 选项
 * @param axes 完整 axis_key 顺序 (与 supportedVariants 同序)
 * @param value 当前已选值映射
 * @param combinations 合法组合 (可选)
 */
function filterByCombinations(
  axisIndex: number,
  baseOptions: MLBackendSupportedVariantOption[],
  axes: string[],
  value: Record<string, unknown>,
  combinations: string[][] | undefined,
): MLBackendSupportedVariantOption[] {
  if (!combinations || combinations.length === 0) return baseOptions;
  const allowed = new Set<string>();
  for (const combo of combinations) {
    let ok = true;
    for (let i = 0; i < axisIndex; i++) {
      const current = typeof value[axes[i]!] === "string" ? (value[axes[i]!] as string) : null;
      if (current && combo[i] !== current) {
        ok = false;
        break;
      }
    }
    if (ok && typeof combo[axisIndex] === "string") {
      allowed.add(combo[axisIndex]!);
    }
  }
  return baseOptions.filter((opt) => allowed.has(opt.value));
}

function normalizeFields(
  schema: JsonSchemaObject | undefined,
  supportedVariants: MLBackendSupportedVariantGroup[] | undefined,
  defaults: Record<string, string> | undefined,
  variantCombinations: string[][] | undefined,
  value: Record<string, unknown>,
): NormalizedVariantField[] {
  const richList = (supportedVariants ?? []).filter(
    (g): g is MLBackendSupportedVariantGroup => g != null && typeof g.key === "string",
  );

  // 路径 A · 协议 v2 富元数据: axis_key 任意, 顺序按 supportedVariants 给的顺序;
  // variantCombinations 按本顺序对齐每个 inner array 元素.
  if (richList.length > 0) {
    const axes = richList.map((g) => g.key);
    const fields: NormalizedVariantField[] = [];
    richList.forEach((group, idx) => {
      const baseOptions = normalizeOptions(group.variants);
      if (baseOptions.length === 0) return;
      const filtered = filterByCombinations(idx, baseOptions, axes, value, variantCombinations);
      const options = filtered.length > 0 ? filtered : baseOptions;
      // 优先级: defaults > 该轴 recommended option > schema.default > 第一个选项
      const schemaField = asField(schema?.properties?.[group.key]);
      const recommended = options.find((opt) => opt.recommended);
      const fallback =
        defaults?.[group.key] ??
        recommended?.value ??
        (typeof schemaField?.default === "string" ? schemaField.default : options[0]!.value);
      fields.push({
        key: group.key,
        title: group.title ?? schemaField?.title ?? group.key,
        description: group.description ?? schemaField?.description,
        fallback,
        options,
      });
    });
    return fields;
  }

  // 路径 B · schema-only fallback: 兼容 legacy variant key 与 x-platform-role=modelVariant.
  const fields: NormalizedVariantField[] = [];
  const seen = new Set<string>();
  const schemaEntries = Object.entries(schema?.properties ?? {});
  const keys = [
    ...VARIANT_FIELD_KEYS,
    ...schemaEntries
      .filter(([key, raw]) => isVariantField(key, asField(raw) ?? {}))
      .map(([key]) => key),
  ];
  for (const key of keys) {
    if (seen.has(key)) continue;
    seen.add(key);
    const schemaField = asField(schema?.properties?.[key]);
    if (!Array.isArray(schemaField?.enum) || schemaField.enum.length === 0) continue;
    const options: MLBackendSupportedVariantOption[] = schemaField.enum.map((v) => ({
      value: v,
      label: v,
    }));
    const fallback =
      defaults?.[key] ??
      (typeof schemaField?.default === "string" ? schemaField.default : options[0]!.value);
    fields.push({
      key,
      title: schemaField?.title ?? key,
      description: schemaField?.description,
      fallback,
      options,
    });
  }
  return fields;
}

export function VariantSelector({
  schema,
  supportedVariants,
  variantCombinations,
  defaults,
  value,
  onChange,
  disabled = false,
  compact = false,
}: VariantSelectorProps) {
  const fields = normalizeFields(schema, supportedVariants, defaults, variantCombinations, value);
  if (fields.length === 0) return null;

  // 联动: 切换第 N 轴时, 若当前后续轴的 value 已变非法, 自动清空让 fallback 重算.
  const handleChange = (axisKey: string, nextValue: string) => {
    const next: Record<string, unknown> = { ...value, [axisKey]: nextValue };
    if (variantCombinations && variantCombinations.length > 0) {
      const axes = fields.map((f) => f.key);
      const idx = axes.indexOf(axisKey);
      // 重新过滤每个后续轴, 若 next[axes[k]] 已不在 allowed 内, 清掉 (UI 用 fallback).
      for (let k = idx + 1; k < axes.length; k++) {
        const filtered = filterByCombinations(
          k,
          normalizeOptions(supportedVariants?.[k]?.variants),
          axes,
          next,
          variantCombinations,
        );
        const cur = typeof next[axes[k]!] === "string" ? (next[axes[k]!] as string) : null;
        if (cur && !filtered.find((o) => o.value === cur)) {
          delete next[axes[k]!];
        }
      }
    }
    onChange(next);
  };

  // 紧凑内联模式: 横排裸 select, 每轴前缀轴名, 不渲染显存/推荐 pill 与 note。
  if (compact) {
    return (
      <>
        {fields.map((field) => {
          const current =
            typeof value[field.key] === "string" ? (value[field.key] as string) : field.fallback;
          const selected =
            field.options.find((option) => option.value === current) ?? field.options[0]!;
          return (
            <div key={field.key} className="flex shrink-0 items-center gap-1">
              <span className="shrink-0 whitespace-nowrap text-2xs text-muted-foreground">
                {field.title}
              </span>
              <select
                data-testid={`ai-variant-${field.key}`}
                value={selected.value}
                disabled={disabled}
                onChange={(e) => handleChange(field.key, e.target.value)}
                className="cursor-pointer appearance-none rounded-sm border border-border bg-muted px-1.5 py-1 text-xs text-foreground"
                title={field.title}
              >
                {field.options.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label ?? option.value}
                  </option>
                ))}
              </select>
            </div>
          );
        })}
      </>
    );
  }

  return (
    <div data-testid="ai-variant-selector" className={styles.root}>
      {fields.map((field) => {
        const current =
          typeof value[field.key] === "string" ? (value[field.key] as string) : field.fallback;
        const selected =
          field.options.find((option) => option.value === current) ?? field.options[0]!;
        const note = selected.note ?? field.description;
        const hasMeta =
          selected.vram_gb != null || Boolean(selected.tier) || Boolean(selected.recommended);
        return (
          <div key={field.key} className={styles.field}>
            <div className={styles.fieldHeader}>
              <span className={styles.label}>{field.title}</span>
            </div>
            <select
              data-testid={`ai-variant-${field.key}`}
              value={selected.value}
              disabled={disabled}
              onChange={(e) => handleChange(field.key, e.target.value)}
              className={styles.select}
            >
              {field.options.map((option) => (
                <option key={option.value} value={option.value}>
                  {optionLabel(option)}
                </option>
              ))}
            </select>
            {hasMeta && (
              <div className={styles.metaRow}>
                {selected.vram_gb != null && (
                  <span className={styles.pill}>显存约 {selected.vram_gb}GB</span>
                )}
                {selected.tier && (
                  <span className={`${styles.pill} ${tierClass(selected.tier) ?? ""}`}>
                    {tierLabel(selected.tier)}
                  </span>
                )}
                {selected.recommended && (
                  <span className={`${styles.pill} ${styles.recommended}`}>推荐</span>
                )}
              </div>
            )}
            {note && <span className={styles.note}>{note}</span>}
          </div>
        );
      })}
    </div>
  );
}

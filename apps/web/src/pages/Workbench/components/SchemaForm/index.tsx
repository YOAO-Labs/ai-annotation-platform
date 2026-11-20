// 最小 JSON Schema 表单 (Draft-07 子集).
// 支持类型: number / integer / boolean / string (含 enum).
// 不依赖 @rjsf/core 以节省 ~50KB bundle; array / nested object 后续按需扩展.

import { useMemo } from "react";

export interface JsonSchemaField {
  type?: "number" | "integer" | "boolean" | "string";
  title?: string;
  description?: string;
  default?: unknown;
  minimum?: number;
  maximum?: number;
  enum?: string[];
  "x-platform-role"?:
    | "confidence"
    | "iou"
    | "maxDet"
    | "textThreshold"
    | "simplifyTolerance"
    | "modelVariant";
  /** 只读字段 (如模型版本 / 缓存容量): 渲染为禁用控件展示, 不进 aiToolParams。 */
  readOnly?: boolean;
}

export interface JsonSchemaObject {
  type?: string;
  /**
   * 与后端 `/setup.params.properties` 同形, 因来自 unknown JSON 故宽容地用 unknown.
   * 内部按 JsonSchemaField 子集解释; 不匹配的字段渲染为 text input fallback.
   */
  properties?: Record<string, unknown>;
}

function asField(v: unknown): JsonSchemaField {
  if (v && typeof v === "object") return v as JsonSchemaField;
  return {};
}

/**
 * 模型变体字段移到 AI 面板 (会话级设置), 不在每个子工具 drawer 重复渲染.
 * SchemaForm / deriveDefaults 统一排除这些 key; AI 面板单独消费 (见 VARIANT_FIELD_KEYS 引用方).
 */
export const VARIANT_FIELD_KEYS = ["sam_variant", "dino_variant", "model_variant"] as const;
const VARIANT_FIELD_SET = new Set<string>(VARIANT_FIELD_KEYS);

const PLATFORM_ROLE_LABELS: Record<NonNullable<JsonSchemaField["x-platform-role"]>, string> = {
  confidence: "置信度阈值",
  iou: "IoU 阈值",
  maxDet: "最大检出数",
  textThreshold: "文本阈值",
  simplifyTolerance: "轮廓简化容差",
  modelVariant: "模型变体",
};

export function isVariantField(key: string, field: JsonSchemaField = {}): boolean {
  return VARIANT_FIELD_SET.has(key) || field["x-platform-role"] === "modelVariant";
}

function fieldTitle(name: string, field: JsonSchemaField): string {
  const role = field["x-platform-role"];
  return (role ? PLATFORM_ROLE_LABELS[role] : undefined) ?? field.title ?? name;
}

export interface SchemaFormProps {
  schema: JsonSchemaObject | undefined;
  value: Record<string, unknown>;
  onChange: (next: Record<string, unknown>) => void;
  /** 整组 disabled, AI 工具不可用时灰显. */
  disabled?: boolean;
}

/** 从 schema.properties 派生 defaults; 用于 InteractiveToolBar 切换工具时 reset. */
export function deriveDefaults(schema: JsonSchemaObject | undefined): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  if (!schema?.properties) return out;
  for (const [key, raw] of Object.entries(schema.properties)) {
    const field = asField(raw);
    if (isVariantField(key, field)) continue; // 变体字段归 AI 面板, 不进 aiToolParams.
    if (field.readOnly) continue; // 只读字段仅展示, 不作为可调参数发给后端.
    if (field.default !== undefined) out[key] = field.default;
  }
  return out;
}

export function SchemaForm({ schema, value, onChange, disabled = false }: SchemaFormProps) {
  const entries = useMemo(
    () =>
      schema?.properties
        ? Object.entries(schema.properties).filter(
            ([key, raw]) => !isVariantField(key, asField(raw)),
          )
        : [],
    [schema],
  );
  if (entries.length === 0) {
    return (
      <div data-testid="schema-form-empty" className="text-muted-foreground text-xs py-1.5">
        当前后端无可配置参数
      </div>
    );
  }
  const setField = (key: string, next: unknown) => {
    onChange({ ...value, [key]: next });
  };
  return (
    <div data-testid="schema-form" className="flex flex-col gap-2.5">
      {entries.map(([key, raw]) => (
        <SchemaField
          key={key}
          name={key}
          field={asField(raw)}
          value={value[key]}
          disabled={disabled}
          onChange={(v) => setField(key, v)}
        />
      ))}
    </div>
  );
}

interface SchemaFieldProps {
  name: string;
  field: JsonSchemaField;
  value: unknown;
  disabled: boolean;
  onChange: (next: unknown) => void;
}

function SchemaField({ name, field, value, disabled, onChange }: SchemaFieldProps) {
  const title = fieldTitle(name, field);
  // 只读字段 (model_variant / embedding_cache_size 等): 禁用控件, 仅作信息展示。
  const ro = disabled || field.readOnly === true;
  const desc = field.description ? (
    <span className="text-muted-foreground text-2xs leading-[1.4]">{field.description}</span>
  ) : null;

  if (field.type === "boolean") {
    const v = typeof value === "boolean" ? value : Boolean(field.default ?? false);
    return (
      <div data-testid={`schema-field-${name}`} className="flex flex-col gap-1">
        <label
          className={`flex items-center gap-1.5 cursor-pointer ${ro ? "cursor-not-allowed" : ""}`}
        >
          <input
            type="checkbox"
            checked={v}
            disabled={ro}
            onChange={(e) => onChange(e.target.checked)}
          />
          <span className="text-muted-foreground text-xs">{title}</span>
        </label>
        {desc}
      </div>
    );
  }

  if (field.type === "string" && Array.isArray(field.enum) && field.enum.length > 0) {
    const v = typeof value === "string" ? value : String(field.default ?? field.enum[0]);
    return (
      <div data-testid={`schema-field-${name}`} className="flex flex-col gap-1">
        <span className="text-muted-foreground text-xs">{title}</span>
        <select
          value={v}
          disabled={ro}
          onChange={(e) => onChange(e.target.value)}
          className="appearance-none border border-border rounded-[var(--radius-sm)] bg-card text-foreground text-xs px-1.5 py-1"
        >
          {field.enum.map((opt) => (
            <option key={opt} value={opt}>
              {opt}
            </option>
          ))}
        </select>
        {desc}
      </div>
    );
  }

  if (field.type === "number" || field.type === "integer") {
    const isInt = field.type === "integer";
    const def = typeof field.default === "number" ? field.default : 0;
    const v = typeof value === "number" ? value : def;
    const min = field.minimum;
    const max = field.maximum;
    const step = isInt ? 1 : max != null && min != null ? (max - min) / 100 : 0.01;
    const hasRange = min != null && max != null;
    return (
      <div data-testid={`schema-field-${name}`} className="flex flex-col gap-1">
        <div className="flex items-baseline justify-between">
          <span className="text-muted-foreground text-xs">{title}</span>
          <span className="mono text-foreground text-xs">{isInt ? v : Number(v).toFixed(2)}</span>
        </div>
        {hasRange && (
          <input
            type="range"
            min={min}
            max={max}
            step={step}
            value={v}
            disabled={ro}
            onChange={(e) =>
              onChange(isInt ? parseInt(e.target.value, 10) : parseFloat(e.target.value))
            }
            className="w-full"
          />
        )}
        {!hasRange && (
          <input
            type="number"
            min={min}
            max={max}
            step={step}
            value={v}
            disabled={ro}
            onChange={(e) =>
              onChange(isInt ? parseInt(e.target.value, 10) : parseFloat(e.target.value))
            }
            className="appearance-none border border-border rounded-[var(--radius-sm)] bg-card text-foreground text-xs px-1.5 py-1"
          />
        )}
        {desc}
      </div>
    );
  }

  // string (no enum) → text input
  const v = typeof value === "string" ? value : String(field.default ?? "");
  return (
    <div data-testid={`schema-field-${name}`} className="flex flex-col gap-1">
      <span className="text-muted-foreground text-xs">{title}</span>
      <input
        type="text"
        value={v}
        disabled={ro}
        onChange={(e) => onChange(e.target.value)}
        className="appearance-none border border-border rounded-[var(--radius-sm)] bg-card text-foreground text-xs px-1.5 py-1"
      />
      {desc}
    </div>
  );
}

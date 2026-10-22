import { RotateCcw } from "lucide-react";
import type { TrainingParameterField as Field } from "@/api/generated/types.gen";
import { Button } from "@/components/ui/Button";
import { cn } from "@/lib/utils";
import {
  describeRule,
  parseParameterDraft,
  ruleForDraft,
  type ParameterDraft,
  type ParameterRule,
} from "./trainingConfiguration";

export const trainingInputClass =
  "w-full min-w-0 rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60";
const parameterInputClass = cn(trainingInputClass, "py-1.5 text-xs tabular-nums");
export function trainingValueLabel(value: unknown) {
  return value === null
    ? "未设置"
    : typeof value === "boolean"
      ? value
        ? "开启"
        : "关闭"
      : typeof value === "string"
        ? value
        : JSON.stringify(value);
}
export function trainingOriginLabel(origin: string | undefined) {
  if (origin === "user") return "自行设置";
  if (origin === "platform") return "平台管理";
  if (origin?.startsWith("dependency:")) return `依赖调整（${origin.slice(11)}）`;
  return "受控默认值";
}

export function TrainingParameterField({
  field,
  value,
  changed,
  disabled,
  onChange,
  onReset,
}: {
  field: Field;
  value: ParameterDraft;
  changed: boolean;
  disabled: boolean;
  onChange: (value: ParameterDraft) => void;
  onReset: () => void;
}) {
  const id = `training-parameter-${field.key}`;
  const rules = field.rules as ParameterRule;
  const rule = ruleForDraft(rules, value);
  const parsed = parseParameterDraft(field, value);
  const error = changed && !parsed.ok ? parsed.error : "";
  const description = `${id}-help${error ? ` ${id}-error` : ""}`;
  return (
    <div className="min-w-0 border-b border-border py-3 last:border-b-0">
      <div className="mb-1.5 flex items-start justify-between gap-3">
        <div className="flex min-w-0 flex-1 flex-wrap items-baseline gap-x-3 gap-y-1">
          <div className="min-w-0">
            <label
              id={`${id}-label`}
              htmlFor={value.kind === "scalar" ? id : undefined}
              className="text-xs font-medium"
            >
              {field.label}
            </label>
            <code className="ml-2 break-all text-2xs text-muted-foreground">{field.key}</code>
          </div>
          <p id={`${id}-help`} className="min-w-0 text-2xs leading-relaxed text-muted-foreground">
            {describeRule(rules)} · 默认 {trainingValueLabel(field.default)}
            {JSON.stringify(field.default) !== JSON.stringify(field.native_default) &&
              ` · 原生默认 ${trainingValueLabel(field.native_default)}`}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {changed && <span className="text-xs text-primary">已修改</span>}
          <Button
            type="button"
            size="xs"
            variant="ghost"
            aria-label={`重置${field.label}`}
            disabled={!changed || disabled}
            onClick={onReset}
          >
            <RotateCcw className="size-3" />
          </Button>
        </div>
      </div>
      {rules.anyOf && (
        <select
          aria-label={`${field.label}的取值形式`}
          className={`${parameterInputClass} mb-2`}
          disabled={disabled}
          value={value.kind}
          onChange={(event) => {
            const kind = event.target.value;
            const arrayRule = rules.anyOf?.find((option) => option.type === "array");
            onChange(
              kind === "null"
                ? { kind: "null" }
                : kind === "array"
                  ? {
                      kind: "array",
                      items: Array.from({ length: arrayRule?.minItems ?? 2 }, () => ""),
                    }
                  : { kind: "scalar", text: "" },
            );
          }}
        >
          {rules.anyOf.map((option, index) => (
            <option
              key={index}
              value={option.type === "null" ? "null" : option.type === "array" ? "array" : "scalar"}
            >
              {option.type === "null"
                ? "未设置"
                : option.type === "array"
                  ? "数值区间"
                  : "指定数值"}
            </option>
          ))}
        </select>
      )}
      {value.kind === "null" ? (
        <p className="rounded-md bg-muted px-3 py-2 text-xs text-muted-foreground">
          使用未设置值；实际行为由当前训练器处理。
        </p>
      ) : value.kind === "array" ? (
        <div role="group" aria-labelledby={`${id}-label`} className="grid grid-cols-2 gap-2">
          {value.items.map((item, index) => (
            <label className="min-w-0 text-xs text-muted-foreground" key={index}>
              {rule.ordered && value.items.length === 2
                ? index === 0
                  ? "下界"
                  : "上界"
                : `第 ${index + 1} 项`}
              <input
                className={`${parameterInputClass} mt-1`}
                inputMode="decimal"
                aria-label={`${field.label}${rule.ordered && value.items.length === 2 ? (index === 0 ? "下界" : "上界") : `第 ${index + 1} 项`}`}
                aria-invalid={!!error}
                aria-describedby={description}
                disabled={disabled}
                value={item}
                onChange={(event) =>
                  onChange({
                    kind: "array",
                    items: value.items.map((old, position) =>
                      position === index ? event.target.value : old,
                    ),
                  })
                }
              />
            </label>
          ))}
        </div>
      ) : rule.enum ? (
        <select
          id={id}
          className={parameterInputClass}
          disabled={disabled}
          value={value.text}
          aria-invalid={!!error}
          aria-describedby={description}
          onChange={(event) => onChange({ kind: "scalar", text: event.target.value })}
        >
          {rule.enum.map((option) => (
            <option key={String(option)} value={String(option)}>
              {String(option)}
            </option>
          ))}
        </select>
      ) : rule.type === "boolean" ? (
        <label className="flex w-fit items-center gap-2 text-xs">
          <input
            id={id}
            type="checkbox"
            className="size-4 accent-primary"
            disabled={disabled}
            checked={value.text === "true"}
            aria-describedby={description}
            onChange={(event) => onChange({ kind: "scalar", text: String(event.target.checked) })}
          />
          {value.text === "true" ? "开启" : "关闭"}
        </label>
      ) : (
        <input
          id={id}
          className={parameterInputClass}
          inputMode={rule.type === "integer" ? "numeric" : "decimal"}
          disabled={disabled}
          value={value.text}
          aria-invalid={!!error}
          aria-describedby={description}
          autoComplete="off"
          onChange={(event) => onChange({ kind: "scalar", text: event.target.value })}
        />
      )}
      {error && (
        <p id={`${id}-error`} role="alert" className="mt-1 text-xs text-status-danger">
          {error}
        </p>
      )}
    </div>
  );
}

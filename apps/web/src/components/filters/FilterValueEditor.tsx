import { useEffect, useMemo, useState } from "react";

import { Button } from "@/components/ui/Button";
import { Input } from "@/components/shadcn/ui/input";
import { cn } from "@/lib/utils";
import { formatFilterDraft, parseFilterValue, splitFilterValues } from "@/lib/filters/filterValues";
import type { FilterFieldDefinition } from "@/lib/filters/types";
import type { TaskFilterOp } from "@/api/taskViews";

export type FilterDraftValidityChange = (valid: boolean, editorId?: string) => void;

const FIELD_CLASS =
  "h-8 w-full appearance-none rounded-sm border border-border bg-background px-2 py-1.5 text-foreground disabled:bg-muted disabled:text-muted-foreground";

export interface FilterValueEditorProps {
  field: FilterFieldDefinition;
  operator: TaskFilterOp;
  appliedValue?: unknown;
  editorId?: string;
  onCommit: (value: unknown) => void;
  onDraftValidityChange?: FilterDraftValidityChange;
  autoFocus?: boolean;
  className?: string;
}

export function FilterValueEditor({
  field,
  operator,
  appliedValue,
  editorId,
  onCommit,
  onDraftValidityChange,
  autoFocus,
  className,
}: FilterValueEditorProps) {
  const [draft, setDraft] = useState(() => formatFilterDraft(appliedValue, field, operator));
  const [betweenDraft, setBetweenDraft] = useState<[string, string]>(() => {
    const values = Array.isArray(appliedValue)
      ? appliedValue
      : appliedValue === null || appliedValue === undefined
        ? []
        : [appliedValue];
    return [String(values[0] ?? ""), String(values[1] ?? "")];
  });
  const [editingNull, setEditingNull] = useState(false);
  const retainedNull =
    appliedValue === null && !editingNull && (operator === "eq" || operator === "ne");
  const parsedDraft = operator === "between" ? betweenDraft.join(",") : draft;
  const parsed = useMemo(
    () => parseFilterValue(field, operator, parsedDraft),
    [field, operator, parsedDraft],
  );

  useEffect(() => {
    setDraft(formatFilterDraft(appliedValue, field, operator));
    const values = Array.isArray(appliedValue)
      ? appliedValue
      : appliedValue === null || appliedValue === undefined
        ? []
        : [appliedValue];
    setBetweenDraft([String(values[0] ?? ""), String(values[1] ?? "")]);
    setEditingNull(false);
  }, [appliedValue, editorId, field, operator]);

  useEffect(() => {
    onDraftValidityChange?.(retainedNull || parsed.ok, editorId);
    return () => onDraftValidityChange?.(true, editorId);
  }, [editorId, onDraftValidityChange, parsed.ok, retainedNull]);

  const commit = () => {
    if (parsed.ok) onCommit(parsed.value);
  };

  if (operator === "exists" || operator === "missing") {
    return (
      <Input
        className={cn(FIELD_CLASS, className)}
        value="无需填写"
        disabled
        readOnly
        aria-label="条件值"
      />
    );
  }

  if (retainedNull) {
    return (
      <div className="flex items-center gap-2">
        <Input
          className={cn(FIELD_CLASS, className)}
          value="空值（已保存）"
          disabled
          readOnly
          aria-label="条件值"
        />
        <Button type="button" size="sm" variant="ghost" onClick={() => setEditingNull(true)}>
          替换
        </Button>
      </div>
    );
  }

  const commitDraft = (nextDraft = draft) => {
    const next = parseFilterValue(field, operator, nextDraft);
    if (next.ok) onCommit(next.value);
  };

  const input =
    (field.value_type === "select" || field.value_type === "multiselect") &&
    ["in", "contains_any", "contains_all"].includes(operator) ? (
      <div
        className="flex flex-col gap-1 rounded-sm border border-border p-1.5"
        role="group"
        aria-label="条件值"
      >
        {Array.from(
          new Set([...splitFilterValues(draft), ...field.options.map((option) => option.value)]),
        ).map((value) => {
          const option = field.options.find((item) => item.value === value);
          const checked = splitFilterValues(draft).includes(value);
          return (
            <label key={value} className="flex min-h-8 items-center gap-2 px-1 text-xs">
              <input
                type="checkbox"
                checked={checked}
                onChange={() => {
                  const next = checked
                    ? splitFilterValues(draft).filter((item) => item !== value)
                    : [...splitFilterValues(draft), value];
                  const nextDraft = formatFilterDraft(next, field, operator);
                  setDraft(nextDraft);
                  commitDraft(nextDraft);
                }}
              />
              <span>{option?.label ?? `${value}（已保存）`}</span>
            </label>
          );
        })}
        {!field.options.length && (
          <span className="px-1 text-xs text-muted-foreground">暂无可选值</span>
        )}
      </div>
    ) : field.value_type === "boolean" ? (
      <select
        className={cn(FIELD_CLASS, className)}
        value={draft}
        aria-label="条件值"
        onChange={(event) => {
          setDraft(event.target.value);
          if (event.target.value === "true" || event.target.value === "false") {
            onCommit(event.target.value === "true");
          }
        }}
        onBlur={commit}
      >
        <option value="" disabled hidden>
          请选择
        </option>
        <option value="true">是</option>
        <option value="false">否</option>
      </select>
    ) : field.options.length && (operator === "eq" || operator === "ne") ? (
      <select
        className={cn(FIELD_CLASS, className)}
        value={draft}
        aria-label="条件值"
        onChange={(event) => {
          setDraft(event.target.value);
          if (event.target.value.trim()) onCommit(event.target.value);
        }}
        onBlur={commit}
      >
        <option value="" disabled hidden>
          请选择
        </option>
        {draft && !field.options.some((option) => option.value === draft) && (
          <option value={draft}>{draft}（已保存）</option>
        )}
        {field.options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    ) : operator === "between" ? (
      <div className="grid grid-cols-2 gap-2">
        {(["起始值", "结束值"] as const).map((label, index) => {
          return (
            <label key={label} className="flex flex-col gap-1 text-xs">
              <span className="text-muted-foreground">{label}</span>
              <Input
                className={cn(FIELD_CLASS, className)}
                value={betweenDraft[index]}
                aria-label={label}
                aria-invalid={betweenDraft.some(Boolean) && !parsed.ok}
                inputMode={field.value_type === "number" ? "decimal" : undefined}
                autoFocus={autoFocus && index === 0}
                onChange={(event) => {
                  const next: [string, string] = [...betweenDraft];
                  next[index] = event.target.value;
                  setBetweenDraft(next);
                  setDraft(next.join(","));
                }}
                onBlur={commit}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    event.preventDefault();
                    commit();
                  }
                }}
              />
            </label>
          );
        })}
      </div>
    ) : (
      <Input
        className={cn(FIELD_CLASS, className)}
        autoFocus={autoFocus}
        value={draft}
        aria-label="条件值"
        aria-invalid={draft.trim() !== "" && !parsed.ok}
        inputMode={field.value_type === "number" ? "decimal" : undefined}
        placeholder={
          ["in", "between", "contains_any", "contains_all"].includes(operator)
            ? "多个值用逗号分隔"
            : undefined
        }
        onChange={(event) => setDraft(event.target.value)}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            commit();
          }
        }}
      />
    );

  return (
    <div className="flex flex-col gap-1.5">
      {input}
      {!parsed.ok && draft.trim() && (
        <div role="alert" className="text-xs text-destructive">
          {parsed.error}
        </div>
      )}
      {parsed.ok && (
        <Button type="button" size="sm" variant="ghost" onClick={commit} className="self-end">
          应用
        </Button>
      )}
    </div>
  );
}

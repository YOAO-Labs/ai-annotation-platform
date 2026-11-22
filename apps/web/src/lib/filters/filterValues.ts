import type { TaskFilterOp } from "@/api/taskViews";
import type { FilterFieldDefinition } from "./types";

export interface ParsedFilterValue {
  ok: boolean;
  value?: unknown;
  error?: string;
}

interface DelimitedValues {
  values: string[];
  balanced: boolean;
}

const ARRAY_OPERATORS = new Set<TaskFilterOp>(["in", "between", "contains_any", "contains_all"]);

function isCompleteNumber(value: string) {
  return /^[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(value.trim());
}

function parseDelimitedValues(input: string): DelimitedValues {
  const values: string[] = [];
  let current = "";
  let quoted = false;
  for (let index = 0; index < input.length; index += 1) {
    const character = input[index];
    if (character === '"') {
      if (quoted && input[index + 1] === '"') {
        current += '"';
        index += 1;
      } else {
        quoted = !quoted;
      }
    } else if (character === "," && !quoted) {
      values.push(current.trim());
      current = "";
    } else {
      current += character;
    }
  }
  values.push(current.trim());
  return { values, balanced: !quoted };
}

export function splitFilterValues(input: string): string[] {
  return parseDelimitedValues(input).values.filter(Boolean);
}

function formatDelimitedValue(value: string) {
  return value.includes(",") || value.includes('"') ? `"${value.split('"').join('""')}"` : value;
}

function arrayDraft(value: unknown) {
  if (Array.isArray(value)) return value.map(String).map(formatDelimitedValue).join(", ");
  return String(value ?? "");
}

export function formatFilterDraft(
  value: unknown,
  field?: FilterFieldDefinition,
  operator?: TaskFilterOp,
): string {
  if (operator === "exists" || operator === "missing") return "";
  if (ARRAY_OPERATORS.has(operator as TaskFilterOp)) return arrayDraft(value);
  if (value === null || value === undefined) return "";
  if (field?.value_type === "boolean")
    return value === true ? "true" : value === false ? "false" : String(value);
  return String(value);
}

function parseArrayDraft(
  input: string,
  field: FilterFieldDefinition,
  operator: TaskFilterOp,
): ParsedFilterValue {
  const parsed = parseDelimitedValues(input);
  if (!parsed.balanced) return { ok: false, error: "请完成引号中的值" };
  if (parsed.values.some((value) => !value)) return { ok: false, error: "列表中不能有空值" };
  const values = parsed.values;
  if (!values.length) return { ok: false, error: "请输入至少一个值" };
  if (operator === "between" && values.length !== 2) {
    return { ok: false, error: "区间需要两个值" };
  }
  if (field.value_type === "number") {
    if (values.some((value) => !isCompleteNumber(value))) {
      return { ok: false, error: "请输入完整数字" };
    }
    const numbers = values.map(Number);
    if (numbers.some((value) => !Number.isFinite(value))) {
      return { ok: false, error: "请输入有效数字" };
    }
    if (operator === "between" && numbers[0] > numbers[1]) {
      return { ok: false, error: "区间起点不能大于终点" };
    }
    return { ok: true, value: numbers };
  }
  if (field.value_type === "datetime") {
    if (values.some((value) => !Number.isFinite(Date.parse(value)))) {
      return { ok: false, error: "请输入有效日期" };
    }
    if (operator === "between" && Date.parse(values[0]) > Date.parse(values[1])) {
      return { ok: false, error: "区间起点不能晚于终点" };
    }
  }
  return { ok: true, value: values };
}

export function parseFilterValue(
  field: FilterFieldDefinition,
  operator: TaskFilterOp,
  draft: string,
): ParsedFilterValue {
  if (operator === "exists" || operator === "missing") return { ok: true, value: true };
  const trimmed = draft.trim();
  if (!trimmed) return { ok: false, error: "请输入条件值" };
  if (ARRAY_OPERATORS.has(operator)) return parseArrayDraft(draft, field, operator);
  if (field.value_type === "number") {
    if (!isCompleteNumber(trimmed)) return { ok: false, error: "请输入完整数字" };
    const number = Number(trimmed);
    return Number.isFinite(number)
      ? { ok: true, value: number }
      : { ok: false, error: "请输入有效数字" };
  }
  if (field.value_type === "boolean") {
    if (trimmed !== "true" && trimmed !== "false") return { ok: false, error: "请选择是或否" };
    return { ok: true, value: trimmed === "true" };
  }
  if (field.value_type === "datetime" && !Number.isFinite(Date.parse(trimmed))) {
    return { ok: false, error: "请输入有效日期" };
  }
  return { ok: true, value: trimmed };
}

export function filterValueError(
  field: FilterFieldDefinition,
  operator: TaskFilterOp,
  draft: string,
) {
  return parseFilterValue(field, operator, draft).error;
}

export function normalizeFilterValue(
  field: FilterFieldDefinition,
  operator: TaskFilterOp,
  value: unknown,
) {
  if (operator === "exists" || operator === "missing") return true;
  const parsed = parseFilterValue(field, operator, formatFilterDraft(value, field, operator));
  return parsed.ok ? parsed.value : value;
}

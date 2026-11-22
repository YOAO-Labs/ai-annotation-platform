import type { TaskFilterGroup, TaskFilterOp, TaskFilterRule } from "@/api/taskViews";
import { formatFilterDraft, parseFilterValue } from "@/lib/filters/filterValues";
import type { FilterFieldDefinition, FilterExpression, FilterPath } from "@/lib/filters/types";

export type DataManagerFilterExpression = FilterExpression | Record<string, unknown>;

export interface EditableDataManagerRule {
  field: string;
  op: TaskFilterOp;
  value: string;
}

export const TASK_FILTER_OPERATORS: TaskFilterOp[] = [
  "eq",
  "ne",
  "in",
  "gt",
  "gte",
  "lt",
  "lte",
  "exists",
  "missing",
  "contains",
  "between",
  "contains_any",
  "contains_all",
];

const ARRAY_FILTER_OPERATORS = new Set<TaskFilterOp>([
  "in",
  "between",
  "contains_any",
  "contains_all",
]);

export const MAX_FILTER_DEPTH = 32;
export const MAX_FILTER_NODES = 4096;

export function validateFilterStructure(expression: unknown): string | null {
  const pending: Array<{ node: unknown; depth: number }> = [{ node: expression, depth: 1 }];
  let count = 0;
  while (pending.length) {
    const current = pending.pop()!;
    count += 1;
    if (count > MAX_FILTER_NODES) return `筛选条件超过 ${MAX_FILTER_NODES} 个节点`;
    if (current.depth > MAX_FILTER_DEPTH) return `筛选条件嵌套超过 ${MAX_FILTER_DEPTH} 层`;
    if (!current.node || typeof current.node !== "object" || Array.isArray(current.node)) {
      return "筛选条件节点格式无效";
    }
    if (isFilterGroup(current.node)) {
      for (let index = current.node.rules.length - 1; index >= 0; index -= 1) {
        pending.push({ node: current.node.rules[index], depth: current.depth + 1 });
      }
      continue;
    }
    if (!isFilterRule(current.node) && !isEmptyFilter(current.node)) {
      return "筛选条件节点格式无效";
    }
  }
  return null;
}

export function isFilterRule(value: unknown): value is TaskFilterRule {
  return Boolean(
    value &&
    typeof value === "object" &&
    typeof (value as { field?: unknown }).field === "string" &&
    typeof (value as { op?: unknown }).op === "string",
  );
}

export function isFilterGroup(value: unknown): value is TaskFilterGroup {
  return Boolean(
    value &&
    typeof value === "object" &&
    ((value as { op?: unknown }).op === "and" || (value as { op?: unknown }).op === "or") &&
    Array.isArray((value as { rules?: unknown }).rules),
  );
}

export function isEmptyFilter(value: unknown): value is Record<string, never> {
  return Boolean(
    value && typeof value === "object" && !Array.isArray(value) && !Object.keys(value).length,
  );
}

export function cloneFilter<T extends DataManagerFilterExpression>(expression: T): T {
  return structuredClone(expression);
}

function makeRule(field: FilterFieldDefinition): TaskFilterRule {
  return { field: field.key, op: field.operators[0] ?? "eq" };
}

export function ruleToDraft(
  rule: TaskFilterRule,
  field?: FilterFieldDefinition,
): EditableDataManagerRule {
  return {
    field: rule.field,
    op: rule.op,
    value: formatFilterDraft(rule.value, field, rule.op),
  };
}

export function draftToRule(
  draft: EditableDataManagerRule,
  fields: FilterFieldDefinition[],
): TaskFilterRule | null {
  const field = fields.find((item) => item.key === draft.field);
  if (!field || !draft.field || !draft.op) return null;
  const parsed = parseFilterValue(field, draft.op, draft.value);
  if (!parsed.ok) return null;
  return { field: draft.field, op: draft.op, value: parsed.value };
}

export function splitKeyword(expression: DataManagerFilterExpression): {
  query: string;
  filter: DataManagerFilterExpression;
} {
  if (validateFilterStructure(expression)) return { query: "", filter: expression };
  if (isFilterRule(expression) && expression.field === "task.keyword") {
    return expression.op === "contains" && typeof expression.value === "string"
      ? { query: String(expression.value ?? ""), filter: {} }
      : { query: "", filter: cloneFilter(expression) };
  }
  if (!isFilterGroup(expression) || expression.op !== "and") {
    return { query: "", filter: cloneFilter(expression) };
  }
  const keywordIndexes = expression.rules
    .map((node, index) => ({ node, index }))
    .filter(
      ({ node }) =>
        isFilterRule(node) &&
        node.field === "task.keyword" &&
        node.op === "contains" &&
        typeof node.value === "string",
    );
  if (keywordIndexes.length !== 1) {
    return { query: "", filter: cloneFilter(expression) };
  }
  const keyword = keywordIndexes[0].node as TaskFilterRule;
  const rules = expression.rules.filter((_, index) => index !== keywordIndexes[0].index);
  return {
    query: String(keyword.value ?? ""),
    filter:
      rules.length === 0
        ? {}
        : rules.length === 1
          ? cloneFilter(rules[0])
          : { op: "and", rules: rules.map((rule) => cloneFilter(rule)) },
  };
}

export function combineKeyword(
  query: string,
  expression: DataManagerFilterExpression,
): DataManagerFilterExpression {
  const trimmed = query.trim();
  const filter = isEmptyFilter(expression)
    ? null
    : validateFilterStructure(expression)
      ? expression
      : cloneFilter(expression);
  if (!trimmed) return filter ?? {};
  const keyword: TaskFilterRule = { field: "task.keyword", op: "contains", value: trimmed };
  return filter ? { op: "and", rules: [keyword, filter] } : keyword;
}

export function expressionRules(expression: DataManagerFilterExpression): Array<{
  path: FilterPath;
  rule: TaskFilterRule;
}> {
  const result: Array<{ path: FilterPath; rule: TaskFilterRule }> = [];
  const pending: Array<{ node: unknown; path: FilterPath }> = [{ node: expression, path: [] }];
  let count = 0;
  while (pending.length && count < MAX_FILTER_NODES) {
    const current = pending.pop()!;
    count += 1;
    if (isFilterRule(current.node)) {
      result.push({ path: current.path, rule: current.node });
      continue;
    }
    if (isFilterGroup(current.node)) {
      for (let index = current.node.rules.length - 1; index >= 0; index -= 1) {
        pending.push({ node: current.node.rules[index], path: [...current.path, index] });
      }
    }
  }
  return result;
}

function atPath(expression: DataManagerFilterExpression, path: FilterPath): unknown {
  let node: unknown = expression;
  for (const index of path) {
    if (!isFilterGroup(node) || !node.rules[index]) return undefined;
    node = node.rules[index];
  }
  return node;
}

export function updateRuleAtPath(
  expression: DataManagerFilterExpression,
  path: FilterPath,
  update: (rule: TaskFilterRule) => TaskFilterRule,
): DataManagerFilterExpression {
  const next = cloneFilter(expression);
  const rule = atPath(next, path);
  if (!isFilterRule(rule)) return next;
  if (!path.length) return update(rule);
  let parent: unknown = next;
  for (const index of path.slice(0, -1)) {
    if (!isFilterGroup(parent)) return next;
    parent = parent.rules[index];
  }
  if (isFilterGroup(parent)) parent.rules[path[path.length - 1]] = update(rule);
  return next;
}

export function removeAtPath(
  expression: DataManagerFilterExpression,
  path: FilterPath,
): DataManagerFilterExpression {
  if (!path.length) return {};
  const next = cloneFilter(expression);
  let parent: unknown = next;
  for (const index of path.slice(0, -1)) {
    if (!isFilterGroup(parent)) return next;
    parent = parent.rules[index];
  }
  if (!isFilterGroup(parent)) return next;
  parent.rules.splice(path[path.length - 1], 1);
  return collapseEmptyGroups(next);
}

export function appendRule(
  expression: DataManagerFilterExpression,
  field: FilterFieldDefinition,
): DataManagerFilterExpression {
  const rule = makeRule(field);
  if (isEmptyFilter(expression)) return rule;
  return { op: "and", rules: [cloneFilter(expression), rule] };
}

export function appendGroup(
  expression: DataManagerFilterExpression,
  op: "and" | "or" = "and",
): DataManagerFilterExpression {
  const group: TaskFilterGroup = { op, rules: [] };
  if (isEmptyFilter(expression)) return group;
  return { op: "and", rules: [cloneFilter(expression), group] };
}

export function setGroupOperator(
  expression: DataManagerFilterExpression,
  path: FilterPath,
  op: "and" | "or",
): DataManagerFilterExpression {
  const next = cloneFilter(expression);
  const group = atPath(next, path);
  if (isFilterGroup(group)) group.op = op;
  return next;
}

export function addToGroup(
  expression: DataManagerFilterExpression,
  path: FilterPath,
  node: TaskFilterRule | TaskFilterGroup,
): DataManagerFilterExpression {
  const next = cloneFilter(expression);
  const group = atPath(next, path);
  if (isFilterGroup(group)) group.rules.push(cloneFilter(node));
  return next;
}

function collapseEmptyGroupsInternal(
  expression: DataManagerFilterExpression,
): DataManagerFilterExpression {
  if (isEmptyFilter(expression) || isFilterRule(expression)) return expression;
  if (!isFilterGroup(expression)) return expression;
  const children = expression.rules.map(collapseEmptyGroupsInternal);
  if (expression.op === "or" && children.some(isEmptyFilter)) return {};
  const remaining = children.filter((node) => !isEmptyFilter(node));
  if (!remaining.length) return {};
  return {
    op: expression.op,
    rules: remaining.filter((node): node is FilterExpression => !isEmptyFilter(node)),
  };
}

export function collapseEmptyGroups(
  expression: DataManagerFilterExpression,
): DataManagerFilterExpression {
  if (validateFilterStructure(expression)) return expression;
  return collapseEmptyGroupsInternal(expression);
}

export function hasNestedGroups(expression: DataManagerFilterExpression) {
  return isFilterGroup(expression) && expression.rules.some((node) => isFilterGroup(node));
}

export function expressionSignature(expression: DataManagerFilterExpression) {
  if (validateFilterStructure(expression)) return "";
  return JSON.stringify(expression);
}

function isStoredScalarValid(field: FilterFieldDefinition, value: unknown): boolean {
  if (field.value_type === "number") {
    return typeof value === "number" && Number.isFinite(value);
  }
  if (field.value_type === "boolean") return typeof value === "boolean";
  if (field.value_type === "datetime") {
    return typeof value === "string" && Number.isFinite(Date.parse(value));
  }
  return typeof value === "string";
}

function isStoredFilterValueValid(
  field: FilterFieldDefinition,
  operator: TaskFilterOp,
  value: unknown,
): boolean {
  if (operator === "exists" || operator === "missing") return true;
  if (ARRAY_FILTER_OPERATORS.has(operator)) {
    if (!Array.isArray(value) || !value.length) return false;
    if (operator === "between" && value.length !== 2) return false;
    if (field.value_type === "boolean") return false;
    if (!value.every((item) => isStoredScalarValid(field, item))) return false;
    if (operator === "between" && field.value_type === "number") {
      return (value[0] as number) <= (value[1] as number);
    }
    if (operator === "between" && field.value_type === "datetime") {
      return Date.parse(value[0] as string) <= Date.parse(value[1] as string);
    }
    return true;
  }
  return isStoredScalarValid(field, value);
}

export function isExpressionValid(
  expression: DataManagerFilterExpression,
  fields: FilterFieldDefinition[],
): boolean {
  if (validateFilterStructure(expression)) return false;
  if (isEmptyFilter(expression)) return true;
  if (isFilterRule(expression)) {
    const field = fields.find((item) => item.key === expression.field);
    if (
      !field ||
      !TASK_FILTER_OPERATORS.includes(expression.op) ||
      !field.operators.includes(expression.op)
    )
      return false;
    if (expression.op !== "exists" && expression.op !== "missing") {
      if (!Object.prototype.hasOwnProperty.call(expression, "value")) return false;
      if (expression.value === undefined) return false;
    }
    if (expression.value === null && (expression.op === "eq" || expression.op === "ne"))
      return field.value_type !== "boolean";
    return isStoredFilterValueValid(field, expression.op, expression.value);
  }
  return (
    isFilterGroup(expression) &&
    expression.rules.length > 0 &&
    expression.rules.every((node) => isExpressionValid(node, fields))
  );
}

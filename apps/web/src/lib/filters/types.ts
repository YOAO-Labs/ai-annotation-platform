import type {
  DataManagerFilterField,
  TaskFilterGroup,
  TaskFilterOp,
  TaskFilterRule,
} from "@/api/taskViews";
import type { ReactNode } from "react";

export type { TaskFilterGroup, TaskFilterOp, TaskFilterRule };

export const FILTER_OPERATOR_LABELS: Record<TaskFilterOp, string> = {
  eq: "=",
  ne: "!=",
  in: "属于",
  gt: ">",
  gte: ">=",
  lt: "<",
  lte: "<=",
  exists: "已填写",
  missing: "缺失",
  contains: "包含",
  between: "区间",
  contains_any: "包含任一",
  contains_all: "包含全部",
};

export function filterOperatorLabel(operator: TaskFilterOp) {
  return FILTER_OPERATOR_LABELS[operator] ?? operator;
}

export type FilterExpression = TaskFilterRule | TaskFilterGroup;
export type FilterPath = number[];

export type FilterFieldDefinition = Pick<
  DataManagerFilterField,
  "key" | "label" | "group" | "value_type" | "operators" | "options"
> &
  Partial<Pick<DataManagerFilterField, "expensive" | "tool_unit_id" | "attribute_key">>;

export interface FilterDraftRule {
  field: string;
  op: TaskFilterOp;
  value: string;
}

export interface FilterChip {
  id: string;
  label: string;
  value: string;
  invalid?: boolean;
  editor?: ReactNode;
  onRemove?: () => void;
}

export interface QuickFilter {
  key: string;
  label: string;
  active: boolean;
  onClick: () => void;
}

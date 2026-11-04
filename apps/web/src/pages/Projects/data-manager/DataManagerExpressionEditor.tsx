import { FilterSelect } from "@/components/filters/FilterControls";
import { useEffect, useMemo } from "react";

import type {
  DataManagerFilterField,
  TaskFilterGroup,
  TaskFilterOp,
  TaskFilterRule,
} from "@/api/taskViews";
import {
  FilterValueEditor,
  type FilterDraftValidityChange,
} from "@/components/filters/FilterValueEditor";
import { useFilterDraftValidity } from "@/components/filters/useFilterDraftValidity";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { cn } from "@/lib/utils";
import { filterOperatorLabel, type FilterPath } from "@/lib/filters/types";
import {
  addToGroup,
  appendGroup,
  appendRule,
  isEmptyFilter,
  isExpressionValid,
  isFilterGroup,
  isFilterRule,
  removeAtPath,
  setGroupOperator,
  updateRuleAtPath,
  validateFilterStructure,
  type DataManagerFilterExpression,
} from "./dataManagerFilterExpression";

const FIELD_CLASS =
  "h-8 w-full appearance-none rounded-sm border border-border bg-background px-2 py-1.5 text-foreground disabled:bg-muted disabled:text-muted-foreground";

function nodeLabel(node: unknown, fields: DataManagerFilterField[]): string {
  if (isFilterRule(node)) {
    const field = fields.find((item) => item.key === node.field);
    return `${field?.label ?? node.field} ${filterOperatorLabel(node.op)}`;
  }
  if (isFilterGroup(node)) {
    return `${node.op === "and" ? "全部条件" : "任一条件"}（${node.rules.length}）`;
  }
  return "无条件";
}

function GroupEditor({
  expression,
  path,
  fields,
  onChange,
  editorId,
  onDraftValidityChange,
}: {
  expression: DataManagerFilterExpression;
  path: FilterPath;
  fields: DataManagerFilterField[];
  onChange: (expression: DataManagerFilterExpression) => void;
  editorId: string;
  onDraftValidityChange: FilterDraftValidityChange;
}) {
  const group = path.length
    ? (() => {
        let node: unknown = expression;
        for (const index of path) node = isFilterGroup(node) ? node.rules[index] : undefined;
        return node;
      })()
    : expression;
  if (!isFilterGroup(group)) return null;

  return (
    <div
      className={cn(
        "flex flex-col gap-2 rounded-md border border-border bg-background/60 p-2",
        path.length && "ml-2",
      )}
      data-filter-path={path.join(".") || "root"}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-medium text-muted-foreground">组合条件</span>
        <span className="text-2xs text-muted-foreground">{nodeLabel(group, fields)}</span>
        <FilterSelect
          className={cn(FIELD_CLASS, "w-36")}
          value={group.op}
          aria-label={`${path.join(".") || "root"} 逻辑关系`}
          onChange={(event) =>
            onChange(setGroupOperator(expression, path, event.target.value as "and" | "or"))
          }
        >
          <option value="and">全部满足（AND）</option>
          <option value="or">任一满足（OR）</option>
        </FilterSelect>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          onClick={() => {
            const field = fields[0];
            if (field)
              onChange(
                addToGroup(expression, path, { field: field.key, op: field.operators[0] ?? "eq" }),
              );
          }}
          disabled={!fields.length}
        >
          <Icon name="plus" size={12} />
          条件
        </Button>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          onClick={() => onChange(addToGroup(expression, path, { op: "and", rules: [] }))}
        >
          <Icon name="plus" size={12} />
          组合
        </Button>
        {path.length > 0 && (
          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={() => onChange(removeAtPath(expression, path))}
          >
            <Icon name="trash" size={12} />
            移除组合
          </Button>
        )}
      </div>
      {!group.rules.length && (
        <div role="alert" className="text-xs text-destructive">
          组合至少需要一个条件
        </div>
      )}
      {group.rules.map((node, index) => {
        const childPath = [...path, index];
        if (isFilterGroup(node)) {
          return (
            <GroupEditor
              key={childPath.join(".")}
              expression={expression}
              path={childPath}
              fields={fields}
              onChange={onChange}
              editorId={editorId}
              onDraftValidityChange={onDraftValidityChange}
            />
          );
        }
        if (!isFilterRule(node)) {
          return (
            <div key={childPath.join(".")} role="alert" className="text-xs text-destructive">
              条件格式无效
            </div>
          );
        }
        const field = fields.find((item) => item.key === node.field);
        const operators = field ? Array.from(new Set([...field.operators, node.op])) : [node.op];
        return (
          <div
            key={childPath.join(".")}
            className="grid gap-2 rounded-sm border border-border p-2 md:grid-cols-[minmax(0,1fr)_9rem_minmax(0,1fr)_auto]"
          >
            <div className="flex min-w-0 flex-col gap-1">
              <span className="text-2xs text-muted-foreground">字段</span>
              <FilterSelect
                className={FIELD_CLASS}
                value={node.field}
                aria-label="筛选字段"
                aria-invalid={!field}
                onChange={(event) => {
                  const nextField = fields.find((item) => item.key === event.target.value);
                  if (!nextField) return;
                  onChange(
                    updateRuleAtPath(expression, childPath, (rule) => ({
                      field: nextField.key,
                      op: nextField.operators[0] ?? rule.op,
                      value: undefined,
                    })),
                  );
                }}
              >
                {!field && <option value={node.field}>{node.field}（字段不可用）</option>}
                {fields.map((item) => (
                  <option key={item.key} value={item.key}>
                    {item.label}
                  </option>
                ))}
              </FilterSelect>
            </div>
            <div className="flex min-w-0 flex-col gap-1">
              <span className="text-2xs text-muted-foreground">操作符</span>
              <FilterSelect
                className={FIELD_CLASS}
                value={node.op}
                aria-label="筛选操作符"
                onChange={(event) =>
                  onChange(
                    updateRuleAtPath(expression, childPath, (rule) => ({
                      ...rule,
                      op: event.target.value as TaskFilterOp,
                    })),
                  )
                }
              >
                {operators.map((operator) => (
                  <option key={operator} value={operator}>
                    {filterOperatorLabel(operator)}
                  </option>
                ))}
              </FilterSelect>
            </div>
            <div className="flex min-w-0 flex-col gap-1">
              <span className="text-2xs text-muted-foreground">值</span>
              {field ? (
                <FilterValueEditor
                  field={field}
                  operator={node.op}
                  appliedValue={node.value}
                  editorId={`${editorId}:field:${childPath.join(".")}`}
                  onDraftValidityChange={onDraftValidityChange}
                  onCommit={(value) =>
                    onChange(
                      updateRuleAtPath(expression, childPath, (rule) => ({ ...rule, value })),
                    )
                  }
                />
              ) : (
                <div role="alert" className="py-2 text-xs text-destructive">
                  此字段在当前项目和查看粒度下不可用，请更换字段或移除条件后再查询。
                </div>
              )}
            </div>
            <Button
              type="button"
              size="sm"
              variant="ghost"
              className="self-end"
              onClick={() => onChange(removeAtPath(expression, childPath))}
            >
              <Icon name="trash" size={12} />
              移除
            </Button>
          </div>
        );
      })}
    </div>
  );
}

export interface DataManagerExpressionEditorProps {
  expression: DataManagerFilterExpression;
  fields: DataManagerFilterField[];
  onChange: (expression: DataManagerFilterExpression) => void;
  editorId?: string;
  onValidityChange?: FilterDraftValidityChange;
  className?: string;
}

export function DataManagerExpressionEditor({
  expression,
  fields,
  onChange,
  editorId,
  onValidityChange,
  className,
}: DataManagerExpressionEditorProps) {
  const structureIssue = validateFilterStructure(expression);
  const expressionValid = useMemo(
    () => isExpressionValid(expression, fields),
    [expression, fields],
  );
  const resolvedEditorId = editorId ?? "expression";
  const { hasInvalidDraft: hasInvalidChildDraft, onDraftValidityChange } =
    useFilterDraftValidity(resolvedEditorId);
  const valid = expressionValid && !hasInvalidChildDraft;
  useEffect(() => {
    onValidityChange?.(valid, resolvedEditorId);
  }, [onValidityChange, resolvedEditorId, valid]);
  useEffect(
    () => () => onValidityChange?.(true, resolvedEditorId),
    [onValidityChange, resolvedEditorId],
  );

  const summary = useMemo(() => {
    if (isEmptyFilter(expression)) return "全部条件";
    if (isFilterGroup(expression)) return nodeLabel(expression, fields);
    return nodeLabel(expression, fields);
  }, [expression, fields]);

  return (
    <div className={cn("flex flex-col gap-2", className)} data-filter-expression-editor>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0 text-xs text-muted-foreground">
          <span className="font-medium text-foreground">{summary}</span>
          {!valid && <span className="ml-2 text-destructive">存在未完成或无效条件</span>}
        </div>
        <div className="flex shrink-0 gap-1">
          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={() => {
              const field = fields[0];
              if (field) onChange(appendRule(expression, field));
            }}
            disabled={!fields.length}
          >
            <Icon name="plus" size={12} />
            条件
          </Button>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={() => onChange(appendGroup(expression))}
          >
            <Icon name="plus" size={12} />
            组合
          </Button>
        </div>
      </div>
      {structureIssue ? (
        <div
          role="alert"
          className="rounded-md border border-status-danger/40 bg-status-danger-soft p-3 text-xs text-status-danger"
        >
          {structureIssue}。请移除或重新创建该筛选条件。
        </div>
      ) : isFilterGroup(expression) ? (
        <GroupEditor
          expression={expression}
          path={[]}
          fields={fields}
          onChange={onChange}
          editorId={resolvedEditorId}
          onDraftValidityChange={onDraftValidityChange}
        />
      ) : isFilterRule(expression) ? (
        <GroupEditor
          expression={{ op: "and", rules: [expression] } as TaskFilterGroup}
          path={[]}
          fields={fields}
          onChange={(next) => {
            if (isFilterGroup(next) && next.rules.length === 1)
              onChange(next.rules[0] as TaskFilterRule);
            else onChange(next);
          }}
          editorId={resolvedEditorId}
          onDraftValidityChange={onDraftValidityChange}
        />
      ) : (
        <div className="rounded-md border border-dashed border-border p-3 text-xs text-muted-foreground">
          暂无筛选条件。点击“条件”开始添加。
        </div>
      )}
    </div>
  );
}

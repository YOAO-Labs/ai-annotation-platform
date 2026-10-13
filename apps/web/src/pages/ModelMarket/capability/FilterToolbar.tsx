import { useState } from "react";
import { FilterGroup, FilterToggle } from "@/components/filters/FilterControls";
import { FilterPanel } from "@/components/filters/FilterPanel";
import { FilterTrigger } from "@/components/filters/FilterTrigger";
import { ActiveFilterChip } from "@/components/filters/ActiveFilterChip";
import { Button } from "@/components/ui/Button";
// 能力目录过滤工具栏（plan §4.1 阶段二）：
//   第 2 行 —— 任务 / 模态快捷筛选 + 更多筛选（模型族 / 推理框架）;
//   第 3 行 —— 结果计数（匹配 N / 总计 M，协议分组附能力类别数）+ 已应用条件标签。
// 搜索 / 分组 / 视图在第 1 行，由面板渲染；清除条件只清多选轴，保留搜索/分组/视图。
// 无效的目录 URL 条件以 chip 提示并提供移除入口（回落默认枚举，plan §5）。
import type { UrlStateIssue } from "@/hooks/useUrlFilterState";

import { infraLabel, modalityLabel, taskLabel } from "./labels";

interface FilterToolbarProps {
  facets: { tasks: string[]; families: string[]; infras: string[]; modalities: string[] };
  taskFilter: Set<string>;
  familyFilter: Set<string>;
  infraFilter: Set<string>;
  modalityFilter: Set<string>;
  onToggleTask: (v: string) => void;
  onToggleFamily: (v: string) => void;
  onToggleInfra: (v: string) => void;
  onToggleModality: (v: string) => void;
  hasActiveFilter: boolean;
  onClear: () => void;
  /** URL 条件 issue（无效的 catalog_group / catalog_view 枚举）。 */
  issues?: UrlStateIssue[];
  /** 移除一个无效 URL 条件（回落默认枚举并把非法参数从 URL 清掉）。 */
  onDismissIssue: (key: string) => void;
  /** 当前实际渲染集合的计数（plan §4.1：匹配 N / 总计 M）。 */
  matchedCount: number;
  totalCount: number;
  /** 协议能力类别数（仅协议分组展示，与模型条目数分开表述）。 */
  categoryCount: number | null;
}

export function FilterToolbar(p: FilterToolbarProps) {
  const [open, setOpen] = useState(false);
  const groups: {
    label: string;
    values: string[];
    active: Set<string>;
    toggle: (v: string) => void;
    render: (v: string) => string;
  }[] = [
    {
      label: "任务",
      values: p.facets.tasks,
      active: p.taskFilter,
      toggle: p.onToggleTask,
      render: taskLabel,
    },
    {
      label: "模态",
      values: p.facets.modalities,
      active: p.modalityFilter,
      toggle: p.onToggleModality,
      render: modalityLabel,
    },
    {
      label: "模型族",
      values: p.facets.families,
      active: p.familyFilter,
      toggle: p.onToggleFamily,
      render: (v) => v,
    },
    {
      label: "推理框架",
      values: p.facets.infras,
      active: p.infraFilter,
      toggle: p.onToggleInfra,
      render: infraLabel,
    },
  ];

  const anyFacet = groups.some((group) => group.values.length > 0);
  if (!anyFacet && !p.issues?.length) return null;
  const renderGroup = (group: (typeof groups)[number]) => (
    <div key={group.label} className="flex min-w-0 flex-wrap items-center gap-2">
      <span className="shrink-0 text-xs font-medium text-muted-foreground">{group.label}</span>
      <div className="flex flex-wrap gap-1">
        {group.values.map((value) => (
          <FilterToggle
            key={value}
            active={group.active.has(value)}
            onClick={() => group.toggle(value)}
          >
            {group.render(value)}
          </FilterToggle>
        ))}
      </div>
    </div>
  );
  return (
    <div className="space-y-2 border-b border-border px-4 py-3">
      {/* 第 2 行：任务 / 模态快捷筛选 + 更多筛选。 */}
      <FilterGroup label="筛选" hideLabel>
        {groups
          .filter((group) => group.label === "任务" || group.label === "模态")
          .filter((group) => group.values.length > 0)
          .map(renderGroup)}
        <FilterPanel
          open={open}
          onOpenChange={setOpen}
          trigger={
            <FilterTrigger count={Number(p.familyFilter.size > 0) + Number(p.infraFilter.size > 0)}>
              更多筛选
            </FilterTrigger>
          }
          title="模型筛选"
          description="即时生效 · 按模型族和推理框架进一步筛选。"
          align="start"
          footer={
            <div className="flex justify-end">
              <Button size="sm" onClick={() => setOpen(false)}>
                完成
              </Button>
            </div>
          }
        >
          <div className="space-y-4">
            {groups
              .filter((group) => group.label === "模型族" || group.label === "推理框架")
              .filter((group) => group.values.length > 0)
              .map(renderGroup)}
          </div>
        </FilterPanel>
        {p.hasActiveFilter && (
          <Button size="sm" variant="ghost" onClick={p.onClear}>
            清除条件
          </Button>
        )}
      </FilterGroup>

      {/* 第 3 行：结果计数 + 已应用条件标签（含任务 / 模态，plan §4.1）。 */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <span className="text-2xs text-muted-foreground" data-testid="catalog-result-count">
          {p.categoryCount != null && <span>{p.categoryCount} 类协议能力 · </span>}
          匹配 {p.matchedCount} / 总计 {p.totalCount} 个模型条目
        </span>
        {p.issues && p.issues.length > 0 && (
          <div
            className="flex flex-wrap items-center gap-1.5"
            role="group"
            aria-label="无效的目录条件"
          >
            {p.issues.map((issue) => (
              <ActiveFilterChip
                key={issue.key}
                label={issue.message}
                invalid
                onRemove={() => p.onDismissIssue(issue.key)}
              />
            ))}
          </div>
        )}
      </div>
      {(p.taskFilter.size > 0 ||
        p.modalityFilter.size > 0 ||
        p.familyFilter.size > 0 ||
        p.infraFilter.size > 0) && (
        <div className="flex flex-wrap gap-1.5" role="group" aria-label="已应用的模型筛选">
          {groups
            .filter((group) => group.active.size > 0)
            .flatMap((group) =>
              [...group.active].map((value) => (
                <ActiveFilterChip
                  key={`${group.label}:${value}`}
                  label={group.label}
                  value={group.render(value)}
                  onClick={() => {
                    if (group.label === "模型族" || group.label === "推理框架") setOpen(true);
                  }}
                  onRemove={() => group.toggle(value)}
                />
              )),
            )}
        </div>
      )}
    </div>
  );
}

import { useMemo, useState, type ReactNode } from "react";

import { Input } from "@/components/shadcn/ui/input";
import {
  Popover,
  PopoverContent,
  PopoverDescription,
  PopoverHeader,
  PopoverTitle,
  PopoverTrigger,
} from "@/components/shadcn/ui/popover";
import { Separator } from "@/components/shadcn/ui/separator";
import { Button } from "@/components/ui/Button";
import { ActiveFilterChip } from "./ActiveFilterChip";
import { FilterToggle } from "./FilterControls";
import { FilterPanel } from "./FilterPanel";
import { FilterTrigger } from "./FilterTrigger";
import type { FilterChip, FilterFieldDefinition, QuickFilter } from "@/lib/filters/types";

export type { FilterChip, QuickFilter };

export interface FilterBarProps {
  fields: FilterFieldDefinition[];
  chips?: FilterChip[];
  quickFilters?: QuickFilter[];
  onAdd: (field: FilterFieldDefinition) => void;
  onClear: () => void;
  clearLabel?: string;
  hasConditions?: boolean;
  children?: ReactNode;
}

export function FilterBar({
  fields,
  chips = [],
  quickFilters = [],
  onAdd,
  onClear,
  clearLabel = "清除条件",
  hasConditions,
  children,
}: FilterBarProps) {
  const [pickerOpen, setPickerOpen] = useState(false);
  const [fieldQuery, setFieldQuery] = useState("");
  const groupedFields = useMemo(() => {
    const query = fieldQuery.trim().toLocaleLowerCase();
    const groups = new Map<string, FilterFieldDefinition[]>();
    for (const field of fields) {
      if (
        query &&
        !`${field.label} ${field.key} ${field.group}`.toLocaleLowerCase().includes(query)
      ) {
        continue;
      }
      const items = groups.get(field.group) ?? [];
      items.push(field);
      groups.set(field.group, items);
    }
    return [...groups.entries()];
  }, [fieldQuery, fields]);
  const canClear =
    hasConditions ?? (chips.length > 0 || quickFilters.some((filter) => filter.active));

  return (
    <div className="flex min-w-0 flex-wrap items-center gap-1.5">
      {quickFilters.map((filter) => (
        <FilterToggle
          key={filter.key}
          active={filter.active}
          onClick={filter.onClick}
          className="h-8"
        >
          {filter.label}
        </FilterToggle>
      ))}
      {!!quickFilters.length && (
        <Separator orientation="vertical" className="mx-0.5 data-[orientation=vertical]:h-6" />
      )}
      {chips.map((chip) => (
        <Popover key={chip.id}>
          <PopoverTrigger asChild>
            <ActiveFilterChip
              label={chip.label}
              value={chip.value}
              invalid={chip.invalid}
              onRemove={chip.onRemove}
              aria-label={`${chip.label} ${chip.value}${chip.invalid ? "（条件无效）" : ""}`}
            />
          </PopoverTrigger>
          <PopoverContent
            align="start"
            sideOffset={8}
            collisionPadding={12}
            aria-label={chip.label}
            className="max-h-[var(--radix-popover-content-available-height)] w-96 max-w-[calc(100vw-24px)] overflow-y-auto rounded-xl"
          >
            <PopoverHeader>
              <PopoverTitle>{chip.label}</PopoverTitle>
              <PopoverDescription>修改操作符或条件值。</PopoverDescription>
            </PopoverHeader>
            {chip.editor && <div className="mt-3">{chip.editor}</div>}
          </PopoverContent>
        </Popover>
      ))}
      <FilterPanel
        open={pickerOpen}
        onOpenChange={setPickerOpen}
        trigger={<FilterTrigger />}
        title="添加筛选"
        description="按名称或字段标识搜索。"
        align="start"
        className="w-80"
      >
        <Input
          value={fieldQuery}
          onChange={(event) => setFieldQuery(event.target.value)}
          placeholder="搜索筛选字段"
          aria-label="搜索筛选字段"
          className="my-2 h-8"
        />
        <div className="max-h-80 overflow-y-auto">
          {groupedFields.map(([group, items]) => (
            <div key={group} className="mb-2 last:mb-0">
              <div className="px-2 py-1 text-2xs font-medium text-muted-foreground">{group}</div>
              {items.map((field) => (
                <Button
                  key={field.key}
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="w-full justify-between gap-3 px-2 text-left font-normal"
                  onClick={() => {
                    onAdd(field);
                    setPickerOpen(false);
                    setFieldQuery("");
                  }}
                >
                  <span>{field.label}</span>
                  <span className="truncate font-mono text-2xs text-muted-foreground">
                    {field.key}
                  </span>
                </Button>
              ))}
            </div>
          ))}
          {!groupedFields.length && (
            <div className="px-2 py-6 text-center text-sm text-muted-foreground">没有匹配字段</div>
          )}
        </div>
      </FilterPanel>
      {canClear && (
        <Button
          size="sm"
          variant="ghost"
          className="h-8 text-muted-foreground"
          aria-label={clearLabel}
          onClick={onClear}
        >
          {clearLabel}
        </Button>
      )}
      {children}
    </div>
  );
}

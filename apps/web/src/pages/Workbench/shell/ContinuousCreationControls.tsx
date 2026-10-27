import { useEffect, useId, useMemo, useState } from "react";
import { Switch } from "@/components/ui/Switch";
import { cn } from "@/lib/utils";
import { ClassPalette } from "./ClassPalette";
import type { ClassesConfig } from "@/api/projects";

export interface ContinuousCreationControlsProps {
  enabled: boolean;
  toolUnitId: string;
  units: { id: string; label: string; classes: string[] }[];
  activeClass: string;
  onEnabledChange: (enabled: boolean) => void;
  onSelectUnit: (unitId: string) => void;
  onPickClass: (cls: string) => void;
  readOnly?: boolean;
  legend?: {
    classes: string[];
    classesConfig?: ClassesConfig;
    recent?: string[];
    activeClass?: string | null;
  };
}

const INPUT_CLASS =
  "w-full min-w-0 rounded-sm border border-input bg-background px-2 py-1.5 text-xs text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50";
const EMPTY_CLASSES: string[] = [];

export function ContinuousCreationControls({
  enabled,
  toolUnitId,
  units,
  activeClass,
  onEnabledChange,
  onSelectUnit,
  onPickClass,
  readOnly = false,
  legend,
}: ContinuousCreationControlsProps) {
  const unitInputId = useId();
  const [query, setQuery] = useState("");
  const unit = units.find((item) => item.id === toolUnitId);
  const classes = unit?.classes ?? EMPTY_CLASSES;
  const visibleClasses = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    return normalized
      ? classes.filter((cls) => cls.toLocaleLowerCase().includes(normalized))
      : classes;
  }, [classes, query]);

  useEffect(() => setQuery(""), [toolUnitId, enabled]);

  return (
    <div className="flex min-w-0 flex-col gap-2" data-testid="continuous-creation-controls">
      <Switch
        label="连续创建"
        checked={enabled}
        onChange={onEnabledChange}
        disabled={readOnly || (!enabled && units.length === 0)}
        data-testid="continuous-creation-toggle"
      />
      {!enabled && (
        <ClassPalette classes={classes} activeClass={activeClass} {...legend} readOnly />
      )}
      {enabled && (
        <>
          <label htmlFor={unitInputId} className="flex min-w-0 flex-col gap-1 text-xs">
            <span className="text-muted-foreground">创建工具</span>
            <select
              id={unitInputId}
              aria-label="创建工具单元"
              value={unit?.id ?? ""}
              onChange={(event) => onSelectUnit(event.target.value)}
              disabled={readOnly || units.length === 0}
              className={INPUT_CLASS}
            >
              {!unit && (
                <option value="" disabled hidden>
                  选择工具
                </option>
              )}
              {units.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
          <div className="text-xs text-muted-foreground">选择下一对象的类别</div>
          {classes.length > 9 && (
            <input
              aria-label="搜索创建类别"
              placeholder="搜索类别..."
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              disabled={readOnly}
              className={INPUT_CLASS}
            />
          )}
          <div className="flex min-w-0 flex-col gap-px" role="group" aria-label="创建类别">
            {visibleClasses.map((cls) => (
              <button
                key={cls}
                type="button"
                aria-pressed={activeClass === cls}
                disabled={readOnly}
                onClick={() => onPickClass(cls)}
                className={cn(
                  "w-full cursor-pointer appearance-none break-words rounded-sm border border-transparent bg-transparent px-2 py-1.5 text-left text-xs text-foreground hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50",
                  activeClass === cls && "bg-muted font-medium",
                )}
              >
                {cls}
              </button>
            ))}
            {visibleClasses.length === 0 && (
              <div className="px-2 py-1.5 text-xs text-muted-foreground">
                {query.trim() ? "没有匹配的类别" : "当前工具没有可用类别"}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}

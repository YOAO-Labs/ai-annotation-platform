import { Icon } from "@/components/ui/Icon";
import { clsx } from "clsx";
import { TOOL_UNIT_GROUPS, type ProjectDataType, type ToolUnitId } from "@/constants/toolUnits";
import type { UnitBindingMap } from "./useProjectToolBindings";

interface Props {
  bindings: UnitBindingMap;
  activeUnit: ToolUnitId;
  onSelect: (unit: ToolUnitId) => void;
  dataType?: ProjectDataType;
  allowToggle?: boolean;
  onToggle?: (unit: ToolUnitId, enabled: boolean) => void;
}

export function ToolUnitTabs({
  bindings,
  activeUnit,
  onSelect,
  dataType,
  allowToggle = false,
  onToggle,
}: Props) {
  const visible = TOOL_UNIT_GROUPS.filter((g) => g.available && bindings[g.id] !== undefined);
  return (
    <div className="mb-3 flex flex-wrap gap-1.5 border-b border-border pb-2">
      {visible.map((g) => {
        const ub = bindings[g.id];
        const active = activeUnit === g.id;
        const enabled = !!ub?.enabled;
        const label = toolUnitLabel(g.id, g.label, dataType);
        return (
          <div
            key={g.id}
            className={clsx(
              "inline-flex items-center gap-1 rounded-md border border-transparent bg-transparent px-1 py-0.5",
              active && "border-brand bg-brand/10",
              !enabled && "[&_button]:opacity-50",
            )}
          >
            {allowToggle && onToggle && (
              <input
                type="checkbox"
                checked={enabled}
                onChange={(e) => onToggle(g.id, e.target.checked)}
                className="mx-0.5 ml-1 cursor-pointer accent-brand"
                title={enabled ? "点击禁用此工具单位" : "点击启用此工具单位"}
              />
            )}
            <button
              type="button"
              onClick={() => onSelect(g.id)}
              className={clsx(
                "inline-flex cursor-pointer appearance-none items-center gap-1.5 rounded-sm border-0 bg-transparent px-2 py-1 text-xs text-foreground hover:bg-muted",
                active && "font-medium text-brand",
              )}
            >
              <Icon name={g.icon} size={12} />
              <span>{label}</span>
              {ub && ub.classRows.length > 0 && (
                <span className="rounded-sm bg-border px-1.5 py-px text-2xs text-muted-foreground">
                  {ub.classRows.length}
                </span>
              )}
            </button>
          </div>
        );
      })}
    </div>
  );
}

function toolUnitLabel(unit: ToolUnitId, fallback: string, dataType?: ProjectDataType): string {
  if (dataType === "video") {
    if (unit === "bbox") return "矩形框 / 轨迹";
    if (unit === "region") return "多边形";
    if (unit === "polyline") return "折线";
  }
  return fallback;
}

import { cn } from "@/lib/utils";

import { Icon, type IconName } from "./Icon";
import { Sparkline } from "./Sparkline";

/**
 * StatCard —— 统计卡(v0.17.2,module.css → Tailwind)。
 * 保持原布局(图标-左 + 标签 + 数值 + trend + sparkline),仅 token 化:中性卡 + 发丝边,
 * trend 走设计 §2.2 emerald/rose。更丰富的「彩色图标片」范式留待 Dashboard 迁移(v0.17.3)。
 */
interface StatCardProps {
  icon?: IconName;
  label: string;
  value: string | number;
  trend?: number;
  sparkValues?: number[];
  sparkColor?: string;
  hint?: string;
}

export function StatCard({
  icon,
  label,
  value,
  trend,
  sparkValues,
  sparkColor,
  hint,
}: StatCardProps) {
  return (
    <div className="surface-shadow-sm surface-shadow-md-on-hover rounded-lg border border-border bg-card px-4 py-3.5 transition-[border-color,box-shadow,transform] duration-200 hover:-translate-y-px">
      <div className="mb-2 flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-1 text-xs text-muted-foreground">
        {icon && <Icon name={icon} size={13} className="shrink-0" />}
        <span className="min-w-0 flex-1 overflow-hidden text-ellipsis whitespace-nowrap">
          {label}
        </span>
        {hint && (
          <span className="w-full text-xs text-muted-foreground sm:ml-auto sm:w-auto">{hint}</span>
        )}
      </div>
      <div className="flex flex-wrap items-baseline gap-x-1.5">
        <span className="break-words text-xl font-semibold tabular-nums sm:text-2xl">
          {typeof value === "number" ? value.toLocaleString() : value}
        </span>
        {trend !== undefined && (
          <span
            className={cn(
              "text-xs font-medium tabular-nums",
              trend >= 0 ? "text-status-positive" : "text-status-danger",
            )}
          >
            {trend >= 0 ? "↑" : "↓"} {Math.abs(trend)}%
          </span>
        )}
      </div>
      {sparkValues && (
        <div className="mt-2 h-7">
          <Sparkline values={sparkValues} color={sparkColor} width={240} />
        </div>
      )}
    </div>
  );
}

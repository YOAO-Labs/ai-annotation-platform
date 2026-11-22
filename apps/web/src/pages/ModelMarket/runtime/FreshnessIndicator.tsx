import { CopyableError } from "@/components/ui/CopyableError";
import type { ReactNode } from "react";

import { Badge } from "@/components/ui/Badge";
import { Icon } from "@/components/ui/Icon";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/shadcn/ui/tooltip";
import type { FreshnessViewModel } from "../runtimeTopology";

function formatTime(iso: string | null): string {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
  } catch {
    return iso;
  }
}

export interface FreshnessIndicatorProps {
  source: FreshnessViewModel;
}

export function FreshnessIndicator({ source }: FreshnessIndicatorProps): ReactNode {
  const variant = source.stale ? "warning" : "outline";
  const icon = source.stale ? "alert-triangle" : "clock";
  const time = formatTime(source.updated_at);
  const tooltipText = source.error
    ? `${source.label}：${source.error}（更新于 ${time || "未知"}）`
    : source.stale
      ? `${source.label}：数据陈旧（更新于 ${time || "未知"}）`
      : `${source.label}：新鲜（更新于 ${time || "未知"}）`;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Badge variant={variant} className="cursor-help">
          <Icon name={icon} size={11} />
          {source.error ? (
            <CopyableError message={source.error} preview={source.label} />
          ) : (
            <span>{source.label}</span>
          )}
          <span className="text-muted-foreground">{time}</span>
        </Badge>
      </TooltipTrigger>
      <TooltipContent>{tooltipText}</TooltipContent>
    </Tooltip>
  );
}

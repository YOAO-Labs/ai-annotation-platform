/**
 * v0.23.4 P3 · tiny shared presentational helpers used by every registry
 * section. Kept in one file to avoid a sprawl of one-off micro-components
 * (plan §10: "不为每个字段创建组件").
 */
import { useState, type ReactNode } from "react";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { ActiveFilterChip } from "@/components/filters/ActiveFilterChip";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/shadcn/ui/tooltip";
import type { UrlStateIssue } from "@/hooks/useUrlFilterState";

import { copyToClipboard, formatShortId } from "./registryShared";

/** A monospace id with a copy button and a tooltip exposing the full value. */
export function CopyableId({
  value,
  className,
  label,
}: {
  value: string;
  className?: string;
  label?: string;
}): ReactNode {
  const [copied, setCopied] = useState(false);
  const onCopy = async () => {
    const ok = await copyToClipboard(value);
    setCopied(ok);
    if (ok) window.setTimeout(() => setCopied(false), 1200);
  };
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          onClick={onCopy}
          className={
            "inline-flex max-w-[180px] items-center gap-1 text-xs text-muted-foreground " +
            (className ?? "")
          }
          title={label ? `${label} · 点击复制` : "点击复制"}
        >
          <span className="mono truncate">{value}</span>
          <Icon name={copied ? "check" : "copy"} size={11} />
        </button>
      </TooltipTrigger>
      <TooltipContent>{value}</TooltipContent>
    </Tooltip>
  );
}

/**
 * Short-id variant of {@link CopyableId} (plan §4.3): rows show 名称 + 短 ID;
 * the full value stays available via tooltip, keyboard-accessible copy and the
 * detail views.
 */
export function ShortCopyableId({
  value,
  className,
  label,
}: {
  value: string;
  className?: string;
  label?: string;
}): ReactNode {
  const [copied, setCopied] = useState(false);
  const onCopy = async () => {
    const ok = await copyToClipboard(value);
    setCopied(ok);
    if (ok) window.setTimeout(() => setCopied(false), 1200);
  };
  const short = formatShortId(value);
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          onClick={onCopy}
          aria-label={`${label ?? "ID"} ${short}，点击复制完整值`}
          className={
            "inline-flex max-w-[140px] cursor-pointer items-center gap-1 text-2xs text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring " +
            (className ?? "")
          }
          title={value}
        >
          <span className="mono">{short}</span>
          <Icon name={copied ? "check" : "copy"} size={10} />
        </button>
      </TooltipTrigger>
      <TooltipContent>{value}</TooltipContent>
    </Tooltip>
  );
}

/** Inline "未声明" / "—" sentinel. */
export function NullCell({ children = "—" }: { children?: ReactNode }): ReactNode {
  return <span className="text-xs text-muted-foreground">{children}</span>;
}

/** Loading placeholder used inside section bodies. */
export function LoadingState({ label = "加载中…" }: { label?: string }): ReactNode {
  return (
    <div className="flex items-center justify-center gap-2 p-8 text-sm text-muted-foreground">
      <Icon name="loader2" size={14} className="spin" />
      {label}
    </div>
  );
}

/** Empty-state block with an icon + message + optional hint. */
export function EmptyState({
  icon = "bot",
  message,
  hint,
}: {
  icon?: Parameters<typeof Icon>[0]["name"];
  message: string;
  hint?: string;
}): ReactNode {
  return (
    <div className="flex flex-col items-center gap-1 p-8 text-center text-sm text-muted-foreground">
      <Icon name={icon} size={28} className="opacity-25" />
      <div>{message}</div>
      {hint && <div className="text-xs">{hint}</div>}
    </div>
  );
}

/** Error block with a retry button — partial failures must not nuke the page. */
export function ErrorState({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}): ReactNode {
  return (
    <div className="flex flex-col items-center gap-2 p-8 text-center text-sm text-status-danger">
      <Icon name="warning" size={18} />
      <div>{message}</div>
      {onRetry && (
        <Button size="sm" variant="ghost" onClick={onRetry}>
          <Icon name="refresh" size={11} />
          重试
        </Button>
      )}
    </div>
  );
}

/** A compact "N 个受影响" chip used by Issue Center / pool / GPU rows. */
export function AffectedCountChip({ count }: { count: number }): ReactNode {
  if (count <= 0) return null;
  return (
    <Badge variant="outline" className="text-2xs">
      影响 {count}
    </Badge>
  );
}

/**
 * Visible hint for invalid URL filter enums (plan §5: 非法枚举给可见提示和移除
 * 入口，不静默丢掉非法值扩大结果)。Codec already fell back to the default, so
 * results stay correct; the chip explains why and offers removal.
 */
export function UrlIssueChips({
  issues,
  onDismiss,
}: {
  issues: UrlStateIssue[];
  onDismiss: (key: string) => void;
}): ReactNode {
  if (issues.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="无效的 URL 条件">
      {issues.map((issue) => (
        <ActiveFilterChip
          key={issue.key}
          label={issue.message}
          invalid
          onRemove={() => onDismiss(issue.key)}
        />
      ))}
    </div>
  );
}

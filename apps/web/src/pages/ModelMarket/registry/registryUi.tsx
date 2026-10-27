import { CopyableError } from "@/components/ui/CopyableError";
/**
 * v0.23.4 P3 · tiny shared presentational helpers used by every registry
 * section. Kept in one file to avoid a sprawl of one-off micro-components
 * (plan §10: "不为每个字段创建组件").
 */
import type { ReactNode } from "react";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { ActiveFilterChip } from "@/components/filters/ActiveFilterChip";
import type { UrlStateIssue } from "@/hooks/useUrlFilterState";

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
      <div>
        <CopyableError message={message} />
      </div>
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

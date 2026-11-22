import { Icon } from "@/components/ui/Icon";

export type QueryStateLike = {
  fetchStatus?: string;
  isPaused?: boolean;
};

export function isQueryPaused(query: QueryStateLike): boolean {
  return query.isPaused === true || query.fetchStatus === "paused";
}

export function isInitialQueryPaused(query: QueryStateLike, hasData: boolean): boolean {
  return !hasData && isQueryPaused(query);
}

export function isRefreshQueryPaused(query: QueryStateLike, hasData: boolean): boolean {
  return hasData && isQueryPaused(query);
}

function pausedMessage(resource: string, hasData: boolean): string {
  return hasData
    ? `网络连接已断开，${resource}当前内容已保留，恢复连接后会自动更新。`
    : `网络连接已断开，${resource}会在恢复后自动继续。`;
}

export function QueryPausedState({
  resource,
  compact = false,
}: {
  resource: string;
  compact?: boolean;
}) {
  return (
    <div
      role="status"
      aria-live="polite"
      className={`flex flex-col items-center gap-2 rounded-md border border-border bg-status-caution-soft text-center text-muted-foreground ${compact ? "m-2 p-6" : "p-10"}`}
    >
      <Icon name="pause" size={compact ? 20 : 28} className="text-status-caution" />
      <div className="text-sm font-semibold text-foreground">等待网络连接</div>
      <div className="max-w-[360px] text-xs leading-relaxed">{pausedMessage(resource, false)}</div>
    </div>
  );
}

export function QueryPausedNotice({ resource }: { resource: string }) {
  return (
    <div
      role="status"
      aria-live="polite"
      className="mb-3 flex items-center gap-2 rounded-md border border-border bg-status-caution-soft px-3 py-2.5 text-xs"
    >
      <Icon name="pause" size={14} className="shrink-0 text-status-caution" />
      <span className="text-muted-foreground">{pausedMessage(resource, true)}</span>
    </div>
  );
}

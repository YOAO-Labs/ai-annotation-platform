/**
 * Reviewer 实时 mini 仪表（在 ReviewWorkbench 右侧栏渲染）。
 *
 * 三个数：本日通过 / 本日退回 / 平均审核耗时秒。
 * 20s 自动 refetch（useReviewerTodayMini staleTime+interval）。
 */
import { useReviewerTodayMini } from "@/hooks/useDashboard";

function formatSeconds(s: number | null): string {
  if (s === null || !Number.isFinite(s)) return "—";
  if (s < 60) return `${Math.round(s)} s`;
  const m = Math.floor(s / 60);
  const rest = Math.round(s - m * 60);
  return `${m}m ${rest}s`;
}

export function ReviewerMiniPanel() {
  const { data, isLoading } = useReviewerTodayMini();
  const approved = data?.approved_today ?? 0;
  const rejected = data?.rejected_today ?? 0;
  const avg = data?.avg_review_seconds ?? null;

  return (
    <div
      data-testid="reviewer-mini-panel"
      className="grid grid-cols-3 gap-2 border-b border-border bg-card px-3 py-2.5 text-xs"
    >
      <Stat label="今日通过" value={isLoading ? "…" : approved.toString()} accent="success" />
      <Stat label="今日退回" value={isLoading ? "…" : rejected.toString()} accent="danger" />
      <Stat label="平均耗时" value={isLoading ? "…" : formatSeconds(avg)} accent="muted" />
    </div>
  );
}

function Stat({
  label,
  value,
  accent,
}: {
  label: string;
  value: string;
  accent: "success" | "danger" | "muted";
}) {
  const accentClass =
    accent === "success"
      ? "text-status-positive"
      : accent === "danger"
        ? "text-status-danger"
        : "text-muted-foreground";
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-2xs text-muted-foreground">{label}</span>
      <span className={`mono text-base font-semibold ${accentClass}`}>{value}</span>
    </div>
  );
}

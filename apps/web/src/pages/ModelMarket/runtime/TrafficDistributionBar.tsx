/**
 * compact traffic distribution segment bar.
 *
 * When real selection counts are unavailable, the entire column shows
 * "暂无路由指标". When counts exist, render a compact segment bar and
 * the numeric counts.
 */
import type { ReactNode } from "react";

import { NO_METRICS_LABEL } from "../runtimeTopology";

export interface TrafficSegment {
  instance_id: string;
  instance_name: string;
  /** Selection count over the window; null = unknown. */
  count: number | null;
}

export interface TrafficDistributionBarProps {
  segments: TrafficSegment[];
  /** Window total requests; null = unknown. */
  total: number | null;
}

const SEGMENT_COLORS = [
  "bg-status-info-alt-soft",
  "bg-status-info-soft",
  "bg-status-positive-soft",
  "bg-status-caution-soft",
  "bg-status-danger-soft",
] as const;

export function TrafficDistributionBar({
  segments,
  total,
}: TrafficDistributionBarProps): ReactNode {
  // Plan Appendix A.2: metrics absent → sentinel, never 0.
  const hasMetrics =
    total !== null && segments.length > 0 && segments.some((s) => s.count !== null);
  if (!hasMetrics) {
    return (
      <span className="text-xs text-muted-foreground" title="暂无路由计数">
        {NO_METRICS_LABEL}
      </span>
    );
  }
  const realTotal = total ?? segments.reduce((sum, s) => sum + (s.count ?? 0), 0);
  if (realTotal === 0) {
    return <span className="text-xs text-muted-foreground">0 次请求</span>;
  }
  return (
    <div className="flex items-center gap-2">
      <div
        className="flex h-2 w-24 overflow-hidden rounded-full bg-muted"
        role="img"
        aria-label={`流量分布：总计 ${realTotal} 次`}
      >
        {segments.map((s, i) => {
          if (s.count === null || s.count === 0) return null;
          const pct = (s.count / realTotal) * 100;
          return (
            <div
              key={s.instance_id}
              className={SEGMENT_COLORS[i % SEGMENT_COLORS.length]}
              // eslint-disable-next-line no-restricted-syntax -- 流量分段宽度由实时计数派生，经 inline style 注入（同 EntityDataManagerLens 虚拟行偏移）。
              style={{ width: `${pct}%` }}
              title={`${s.instance_name}: ${s.count} (${pct.toFixed(0)}%)`}
            />
          );
        })}
      </div>
      <span className="text-xs text-muted-foreground">{realTotal}</span>
    </div>
  );
}

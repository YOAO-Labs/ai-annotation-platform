import type { Metric } from "./geometryMetrics";

const GRID_CLASS = "m-0 grid grid-cols-2 gap-x-3.5 gap-y-[7px]";
const CELL_CLASS = "flex min-w-0 flex-col gap-[1px]";
const LABEL_CLASS = "text-xs leading-[1.3] text-muted-foreground";
const VALUE_CLASS =
  "m-0 flex min-w-0 items-baseline gap-1 text-sm leading-[1.35] tabular-nums text-foreground";
const VALUE_TEXT_CLASS = "truncate";
const HINT_CLASS = "flex-none text-xs text-muted-foreground";

export interface MetricGridProps {
  metrics: Metric[];
}

export function MetricGrid({ metrics }: MetricGridProps) {
  if (metrics.length === 0) return null;
  return (
    <dl className={GRID_CLASS}>
      {metrics.map((m) => (
        <div key={m.label} className={CELL_CLASS}>
          <dt className={LABEL_CLASS}>{m.label}</dt>
          <dd className={VALUE_CLASS} title={m.hint ? `${m.value} · ${m.hint}` : m.value}>
            <span className={VALUE_TEXT_CLASS}>{m.value}</span>
            {m.hint && <span className={HINT_CLASS}>{m.hint}</span>}
          </dd>
        </div>
      ))}
    </dl>
  );
}

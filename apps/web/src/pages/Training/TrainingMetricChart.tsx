import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { metricLabel } from "./trainingPresentation";

const chartColors = [
  "var(--sc-status-info)",
  "var(--sc-status-info-alt)",
  "var(--sc-status-positive)",
  "var(--sc-status-caution)",
];

export function TrainingMetricChart({
  title,
  points,
  keys,
  empty,
  labels = {},
  domain,
  headingLevel = 4,
  xDataKey = "epoch",
  xLabel = "训练轮次",
  pointUnit = "轮上报数据",
}: {
  title: string;
  points: Record<string, number | null>[];
  keys: string[];
  empty: string;
  labels?: Record<string, string>;
  domain?: [number | string, number | string];
  headingLevel?: 2 | 4;
  xDataKey?: string;
  xLabel?: string;
  pointUnit?: string;
}) {
  const Heading = headingLevel === 2 ? "h2" : "h4";
  return (
    <section className="min-w-0 rounded-lg border border-border bg-card p-4" aria-label={title}>
      <Heading className="text-sm font-medium">{title}</Heading>
      {!keys.length ? (
        <p className="flex h-44 items-center justify-center text-xs text-muted-foreground">
          {empty}
        </p>
      ) : (
        <>
          <div className="mt-2 flex flex-wrap gap-3 text-xs text-muted-foreground">
            {keys.map((key, i) => (
              <span key={key} className="flex items-center gap-1.5">
                <svg width="12" height="4" aria-hidden="true">
                  <rect width="12" height="4" rx="2" fill={chartColors[i % chartColors.length]} />
                </svg>
                {labels[key] ?? metricLabel(key)}
              </span>
            ))}
          </div>
          <div
            className="mt-4 h-44 w-full min-w-0"
            role="img"
            aria-label={`${title}，${points.length} ${pointUnit}`}
          >
            <ResponsiveContainer
              width="100%"
              height="100%"
              minWidth={0}
              initialDimension={{ width: 320, height: 176 }}
            >
              <LineChart data={points} margin={{ top: 5, right: 8, bottom: 0, left: -22 }}>
                <CartesianGrid stroke="var(--sc-border)" strokeDasharray="3 3" vertical={false} />
                <XAxis
                  dataKey={xDataKey}
                  type="number"
                  domain={["dataMin", "dataMax"]}
                  allowDecimals={false}
                  tick={{ fill: "var(--sc-muted-foreground)", fontSize: 11 }}
                  tickLine={false}
                  axisLine={false}
                />
                <YAxis
                  domain={domain ?? (title === "验证表现" ? [0, 1] : [0, "auto"])}
                  tick={{ fill: "var(--sc-muted-foreground)", fontSize: 11 }}
                  tickLine={false}
                  axisLine={false}
                  width={60}
                  tickFormatter={(n: number) => n.toFixed(2)}
                />
                <Tooltip
                  content={({ active, payload, label }) =>
                    active && payload?.length ? (
                      <div className="rounded-md border border-border bg-popover p-3 text-xs text-popover-foreground shadow-md">
                        <p className="mb-1 font-medium">
                          {xLabel} {label}
                        </p>
                        {payload.map((item) => (
                          <p key={String(item.dataKey)}>
                            {labels[String(item.dataKey)] ?? metricLabel(String(item.dataKey))}：
                            {Number(item.value).toFixed(4)}
                          </p>
                        ))}
                      </div>
                    ) : null
                  }
                />
                {keys.map((key, i) => (
                  <Line
                    key={key}
                    type="linear"
                    dataKey={key}
                    stroke={chartColors[i % chartColors.length]}
                    strokeWidth={2}
                    dot={points.length < 20 ? { r: 2.5 } : false}
                    activeDot={{ r: 4 }}
                    connectNulls={false}
                    isAnimationActive={false}
                  />
                ))}
              </LineChart>
            </ResponsiveContainer>
          </div>
          <p className="mt-1 text-center text-2xs text-muted-foreground">{xLabel}</p>
        </>
      )}
    </section>
  );
}

import { FilterGroup, FilterSelect } from "@/components/filters/FilterControls";
/**
 * v0.10.16 · DuckDB 离线分析面板（ROADMAP §1.6）。
 *
 * super_admin only，4 个固定面板：团队日吞吐 / reject 率分类型 / 标注耗时分布 / 工时热力图。
 * 数据来源 = Celery beat 每日 02:30 UTC 同步的 DuckDB 文件；尚未首次同步时端点
 * 返回 503，前端展示「数据初始化中」提示。
 *
 * v0.12.7 · 吞吐 / reject 面板升级为 recharts；新增工时热力图（星期几 × 小时）。
 * Recharts / heatmap colors read shadcn runtime tokens.
 */

import { useMemo, useState, type CSSProperties } from "react";
import { useQuery } from "@tanstack/react-query";
import { useElementStyle } from "@/components/ui/useElementStyle";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { adminAnalyticsApi } from "@/api/adminAnalytics";
import { useTheme } from "@/hooks/useTheme";
import { REJECT_REASON_TYPE_LABELS } from "@/pages/Review/rejectReasonTypes";

/** 运行时读取设计 token 语义色(组件不写死颜色;迁移后走 shadcn.css 的 --sc-*)。 */
function cssVar(name: string): string {
  if (typeof window === "undefined") return "";
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

const RANGE_OPTIONS = [
  { value: 7, label: "近 7 天" },
  { value: 30, label: "近 30 天" },
  { value: 90, label: "近 90 天" },
];

// DuckDB dayofweek() 约定 0=周日 .. 6=周六。下标对齐该约定。
const WEEKDAY_LABELS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
const HOURS = Array.from({ length: 24 }, (_, h) => h);

const CARD_CLASS = "rounded-md border border-border bg-card p-4";
const TITLE_CLASS = "mb-2 text-sm font-semibold";
const HINT_CLASS = "mb-3 text-xs text-muted-foreground";
const EMPTY_CLASS = "py-2 text-xs text-muted-foreground";

export function AnalyticsPage() {
  const [days, setDays] = useState(30);
  // 订阅主题变化:resolved 切换会触发本组件 re-render,
  // accent/gridColor 的 useMemo 重读 token,recharts 拿到新色重绘。
  const { resolved } = useTheme();

  const throughputQ = useQuery({
    queryKey: ["admin", "analytics", "throughput", days],
    queryFn: () => adminAnalyticsApi.throughputDaily(days),
  });
  const rejectQ = useQuery({
    queryKey: ["admin", "analytics", "reject", days],
    queryFn: () => adminAnalyticsApi.rejectRateByType(days),
  });
  const durationQ = useQuery({
    queryKey: ["admin", "analytics", "duration", days],
    queryFn: () => adminAnalyticsApi.durationDist(days),
  });
  const heatmapQ = useQuery({
    queryKey: ["admin", "analytics", "heatmap", days],
    queryFn: () => adminAnalyticsApi.activityHeatmap(days),
  });

  const accent = useMemo(() => {
    void resolved;
    return cssVar("--sc-brand");
  }, [resolved]);
  const gridColor = useMemo(() => {
    void resolved;
    return cssVar("--sc-border");
  }, [resolved]);

  const aggThroughput = useMemo(() => {
    const data = throughputQ.data?.data ?? [];
    const byDay = new Map<string, number>();
    for (const r of data) {
      byDay.set(r.day, (byDay.get(r.day) ?? 0) + r.event_count);
    }
    return Array.from(byDay.entries())
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([day, n]) => ({ day: day.slice(0, 10), n }));
  }, [throughputQ.data]);

  const rejectData = useMemo(() => {
    return (rejectQ.data?.data ?? []).map((r) => ({
      name:
        REJECT_REASON_TYPE_LABELS[r.reason_type as keyof typeof REJECT_REASON_TYPE_LABELS] ??
        r.reason_type,
      count: r.count,
    }));
  }, [rejectQ.data]);

  const heatmap = useMemo(() => {
    const cells = heatmapQ.data?.data ?? [];
    const byKey = new Map<string, number>();
    let max = 0;
    for (const c of cells) {
      byKey.set(`${c.weekday}-${c.hour}`, c.count);
      if (c.count > max) max = c.count;
    }
    return { byKey, max };
  }, [heatmapQ.data]);

  return (
    <div className="flex flex-col gap-4 px-6 py-5 text-foreground max-md:p-4">
      <div className="flex items-center justify-between max-md:flex-col max-md:items-start max-md:gap-2.5">
        <div>
          <h1 className="text-xl font-semibold">离线分析（DuckDB）</h1>
          <p className="mt-1 text-xs text-muted-foreground">
            数据由 Celery beat 每日 02:30 UTC 增量同步；面板查询为固定 SQL，禁用任意 SQL 输入。
          </p>
        </div>
        <FilterGroup label="时间范围" hideLabel compact>
          <FilterSelect
            aria-label="时间范围"
            compact
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
            data-testid="analytics-range-select"
          >
            {RANGE_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </FilterSelect>
        </FilterGroup>
      </div>

      <div className="grid grid-cols-[repeat(auto-fit,minmax(320px,1fr))] gap-4">
        {/* 团队日吞吐（按日聚合） */}
        <div className={CARD_CLASS} data-testid="panel-throughput">
          <div className={TITLE_CLASS}>团队日吞吐（task_events.kind=annotate）</div>
          <div className={HINT_CLASS}>每点 = 1 天的全团队提交事件计数</div>
          {throughputQ.isError && <NotReady error={throughputQ.error} />}
          {!throughputQ.isError && aggThroughput.length === 0 && (
            <div className={EMPTY_CLASS}>所选范围内暂无数据</div>
          )}
          {aggThroughput.length > 0 && (
            <div className="w-full">
              <ResponsiveContainer width="100%" height={240}>
                <LineChart data={aggThroughput} margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke={gridColor} />
                  <XAxis dataKey="day" tick={{ fontSize: 11 }} />
                  <YAxis tick={{ fontSize: 11 }} allowDecimals={false} />
                  <Tooltip />
                  <Line
                    type="monotone"
                    dataKey="n"
                    name="提交数"
                    stroke={accent}
                    strokeWidth={2}
                    dot={{ r: 2 }}
                  />
                </LineChart>
              </ResponsiveContainer>
            </div>
          )}
        </div>

        {/* reject 率按类型分布 */}
        <div className={CARD_CLASS} data-testid="panel-reject-rate">
          <div className={TITLE_CLASS}>Reject 原因分布</div>
          <div className={HINT_CLASS}>
            分母只算 reject_reason_type IS NOT NULL（v0.10.16 起标注）
          </div>
          {rejectQ.isError && <NotReady error={rejectQ.error} />}
          {!rejectQ.isError && rejectData.length === 0 && (
            <div className={EMPTY_CLASS}>所选范围内无 reject 记录</div>
          )}
          {rejectData.length > 0 && (
            <div className="w-full">
              <ResponsiveContainer width="100%" height={240}>
                <BarChart
                  layout="vertical"
                  data={rejectData}
                  margin={{ top: 4, right: 12, bottom: 4, left: 0 }}
                >
                  <CartesianGrid strokeDasharray="3 3" stroke={gridColor} horizontal={false} />
                  <XAxis type="number" tick={{ fontSize: 11 }} allowDecimals={false} />
                  <YAxis type="category" dataKey="name" width={84} tick={{ fontSize: 11 }} />
                  <Tooltip />
                  <Bar dataKey="count" name="次数" fill={accent} radius={[0, 3, 3, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}
        </div>

        {/* 标注耗时分布 */}
        <div className={CARD_CLASS} data-testid="panel-duration">
          <div className={TITLE_CLASS}>标注耗时分布</div>
          <div className={HINT_CLASS}>task_events claim → submit 间隔（ms）</div>
          {durationQ.isError && <NotReady error={durationQ.error} />}
          {!durationQ.isError && durationQ.data && (
            <div className="grid grid-cols-[80px_1fr] gap-x-3 gap-y-1.5 text-sm">
              <span className="text-muted-foreground">样本数</span>
              <span className="font-semibold tabular-nums">{durationQ.data.data.n}</span>
              <span className="text-muted-foreground">中位 (p50)</span>
              <span className="font-semibold tabular-nums">
                {(durationQ.data.data.p50 / 1000).toFixed(1)}s
              </span>
              <span className="text-muted-foreground">p95</span>
              <span className="font-semibold tabular-nums">
                {(durationQ.data.data.p95 / 1000).toFixed(1)}s
              </span>
              <span className="text-muted-foreground">均值</span>
              <span className="font-semibold tabular-nums">
                {(durationQ.data.data.mean / 1000).toFixed(1)}s
              </span>
            </div>
          )}
        </div>

        {/* 工时热力图（星期几 × 小时） */}
        <div className={`${CARD_CLASS} col-span-full`} data-testid="panel-heatmap">
          <div className={TITLE_CLASS}>工时热力图（started_at · 星期 × 小时）</div>
          <div className={HINT_CLASS}>颜色深浅 = 该时段 annotate 事件计数占比</div>
          {heatmapQ.isError && <NotReady error={heatmapQ.error} />}
          {!heatmapQ.isError && heatmap.max === 0 && (
            <div className={EMPTY_CLASS}>所选范围内暂无工时数据</div>
          )}
          {!heatmapQ.isError && heatmap.max > 0 && (
            <div className="flex flex-col gap-1 overflow-x-auto">
              {WEEKDAY_LABELS.map((label, weekday) => (
                <div
                  key={weekday}
                  className="grid grid-cols-[36px_repeat(24,1fr)] items-center gap-1"
                >
                  <span className="whitespace-nowrap text-xs text-muted-foreground">{label}</span>
                  {HOURS.map((hour) => {
                    const count = heatmap.byKey.get(`${weekday}-${hour}`) ?? 0;
                    const intensity = count > 0 ? Math.max(0.06, count / heatmap.max) : 0.06;
                    return (
                      <HeatCell
                        key={hour}
                        intensity={intensity}
                        title={`${label} ${String(hour).padStart(2, "0")}:00 · ${count}`}
                      />
                    );
                  })}
                </div>
              ))}
              <div className="mt-0.5 grid grid-cols-[36px_repeat(24,1fr)] gap-1">
                <span className="text-xs text-muted-foreground" />
                {HOURS.map((hour) => (
                  <span key={hour} className="text-left text-2xs text-muted-foreground">
                    {hour % 6 === 0 ? hour : ""}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// 热力图格子:强度走 CSS 变量(opacity),底色取 token --sc-brand。
function HeatCell({ intensity, title }: { intensity: number; title: string }) {
  const ref = useElementStyle<HTMLDivElement>({
    "--heat-opacity": String(intensity),
  } as CSSProperties);
  return (
    <div
      ref={ref}
      className="h-4 min-w-3 rounded-[2px] border border-border bg-brand opacity-[var(--heat-opacity)]"
      title={title}
    />
  );
}

function NotReady({ error }: { error: unknown }) {
  const msg =
    error && typeof error === "object" && "message" in error
      ? String((error as { message: unknown }).message)
      : "数据初始化中（Celery beat 尚未首次同步）。";
  return <div className="rounded-sm bg-muted p-3 text-xs text-muted-foreground">{msg}</div>;
}

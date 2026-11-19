/**
 * 我的绩效（取经合集 §4.1 个人页）。
 *
 * 任意已认证用户看自己的 4 周产出趋势（叠加团队均线）+ 耗时直方图 + hero KPI。
 * 数据走实时 PG（/dashboard/me/performance），强制 self。
 */

import { useMemo, type CSSProperties } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { dashboardApi } from "@/api/dashboard";
import { Icon, type IconName } from "@/components/ui/Icon";
import { useTheme } from "@/hooks/useTheme";
import { REJECT_REASON_TYPE_LABELS } from "@/pages/Review/rejectReasonTypes";
import styles from "./MyPerformancePage.module.css";

// 统计窗口固定 4 周：趋势图 / 团队均线 / 质量分 / 周环比均按周聚合写死 4 周，
// 故 KPI 也锁定同一窗口，避免「KPI 跟 period、趋势写死 4 周」的口径割裂。
const PERIOD = "4w";

const WEEK_LABELS = ["前 3 周", "前 2 周", "上周", "本周"];
type KpiTone = "accent" | "success" | "warning" | "danger" | "neutral";

/** 读取 shadcn 运行时 token，避免图表在主题切换后滞留旧色。 */
function cssVar(name: string): string {
  if (typeof window === "undefined") return "";
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

export function MyPerformancePage() {
  const perfQ = useQuery({
    queryKey: ["me", "performance", PERIOD],
    queryFn: () => dashboardApi.getMyPerformance(PERIOD),
  });

  // 订阅主题变化，切换时 re-render 后重读 token，避免 recharts 颜色滞留。
  useTheme();
  const accent = cssVar("--sc-brand");
  const muted = cssVar("--sc-muted-foreground");
  const danger = cssVar("--sc-destructive");
  const gridColor = cssVar("--sc-border");
  const axisTick = useMemo(() => ({ fontSize: 11, fill: muted }), [muted]);
  const axisLine = useMemo(() => ({ stroke: gridColor }), [gridColor]);
  const tooltipContentStyle = useMemo<CSSProperties>(
    () => ({
      background: "var(--sc-card)",
      border: "1px solid var(--sc-border)",
      borderRadius: "var(--radius-md)",
      boxShadow: "var(--shadow-md)",
      color: "var(--sc-foreground)",
    }),
    [],
  );
  const tooltipLabelStyle = useMemo<CSSProperties>(
    () => ({ color: "var(--sc-foreground)", fontWeight: 600 }),
    [],
  );
  const tooltipItemStyle = useMemo<CSSProperties>(
    () => ({ color: "var(--sc-muted-foreground)" }),
    [],
  );

  const data = perfQ.data;

  const trendData = useMemo(() => {
    if (!data) return [];
    return WEEK_LABELS.map((label, i) => ({
      name: label,
      我的产出: data.trend_throughput[i] ?? 0,
      团队均线: data.team_trend_throughput[i] ?? 0,
    }));
  }, [data]);

  const histogramData = useMemo(() => {
    if (!data) return [];
    return data.duration_histogram.map((b) => ({
      name: `${Math.round(b.upper_ms / 1000)}s`,
      count: b.count,
    }));
  }, [data]);

  const rejectData = useMemo(() => {
    if (!data) return [];
    return data.reject_reason_breakdown.map((r) => ({
      name:
        REJECT_REASON_TYPE_LABELS[r.reason_type as keyof typeof REJECT_REASON_TYPE_LABELS] ??
        r.reason_type,
      value: r.count,
    }));
  }, [data]);

  const classData = useMemo(() => {
    if (!data) return [];
    return data.class_distribution.map((c) => ({ name: c.class_name, value: c.count }));
  }, [data]);

  const durationSampleCount = useMemo(() => {
    if (!data) return 0;
    return data.duration_histogram.reduce((sum, b) => sum + b.count, 0);
  }, [data]);

  const rejectTotal = useMemo(() => {
    if (!data) return 0;
    return data.reject_reason_breakdown.reduce((sum, r) => sum + r.count, 0);
  }, [data]);

  const avgQuality = useMemo(() => {
    if (!data || data.trend_quality.length === 0) return null;
    const sum = data.trend_quality.reduce((total, value) => total + value, 0);
    return Math.round(sum / data.trend_quality.length);
  }, [data]);

  const latestMine = data?.trend_throughput[WEEK_LABELS.length - 1] ?? null;
  const latestTeam = data?.team_trend_throughput[WEEK_LABELS.length - 1] ?? null;
  const topClass = classData[0];

  // top-N 类别覆盖率之外的剩余占比;类多时 ΣP 不到 100% 是 backend 有意行为。
  const otherClassPct = useMemo(() => {
    if (!data || data.class_distribution.length === 0) return 0;
    const sum = data.class_distribution.reduce((s, c) => s + c.pct, 0);
    return Math.max(0, 100 - sum);
  }, [data]);

  const qualityTone: KpiTone =
    !data || data.quality_score >= 90 ? "success" : data.quality_score >= 70 ? "warning" : "danger";

  const firstPassTone: KpiTone =
    !data || data.first_pass_yield === null
      ? "neutral"
      : data.first_pass_yield >= 0.9
        ? "success"
        : data.first_pass_yield >= 0.75
          ? "warning"
          : "danger";

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <div>
          <h1 className={styles.title}>我的绩效</h1>
          <p className={styles.subtitle}>
            自己的产出趋势对标团队均线，帮助自我改进。统计窗口：近 4 周，数据实时统计。
          </p>
        </div>
      </div>

      {perfQ.isError && <div className={styles.notReady}>绩效数据暂不可用，请稍后再试。</div>}

      {data && (
        <>
          <div className={styles.kpiGrid}>
            <KpiCard
              icon="activity"
              label="本期产出"
              value={data.throughput.toLocaleString()}
              caption={`本周 ${formatMaybeNumber(latestMine)} / 团队 ${formatMaybeNumber(
                latestTeam,
              )}`}
              tone="accent"
            />
            <KpiCard
              icon="checkCircle"
              label="质量分"
              value={`${data.quality_score}`}
              suffix="/100"
              caption={avgQuality === null ? "暂无趋势样本" : `4 周均值 ${avgQuality}`}
              tone={qualityTone}
            />
            <KpiCard
              icon="arrowRight"
              label="周环比"
              value={
                data.weekly_compare_pct === null
                  ? "—"
                  : `${data.weekly_compare_pct > 0 ? "+" : ""}${data.weekly_compare_pct}%`
              }
              positive={data.weekly_compare_pct !== null && data.weekly_compare_pct >= 0}
              caption={data.weekly_compare_pct === null ? "暂无上周对比" : "相对上周产出"}
              tone={
                data.weekly_compare_pct === null
                  ? "neutral"
                  : data.weekly_compare_pct >= 0
                    ? "success"
                    : "danger"
              }
            />
            <KpiCard
              icon="clock"
              label="耗时 p50 / p95"
              value={
                data.p50_duration_ms !== null
                  ? `${Math.round(data.p50_duration_ms / 1000)}s / ${Math.round(
                      (data.p95_duration_ms ?? 0) / 1000,
                    )}s`
                  : "—"
              }
              caption={`耗时样本 ${durationSampleCount.toLocaleString()} 次`}
              tone="neutral"
            />
            <KpiCard
              icon="shield"
              label="首过率"
              value={
                data.first_pass_yield === null ? "—" : `${Math.round(data.first_pass_yield * 100)}%`
              }
              suffix={data.first_pass_yield === null ? undefined : "一次通过"}
              caption={`Reject ${rejectTotal.toLocaleString()} 次`}
              tone={firstPassTone}
            />
          </div>

          <div className={styles.grid}>
            {/* 产出趋势 vs 团队均线 */}
            <div className={`${styles.card} ${styles.cardWide}`}>
              <div className={styles.cardHeader}>
                <div>
                  <h2 className={styles.cardTitle}>产出趋势 · 我 vs 团队均线（近 4 周）</h2>
                  <p className={styles.cardHint}>
                    本周个人 {formatMaybeNumber(latestMine)}，团队均线{" "}
                    {formatMaybeNumber(latestTeam)}
                  </p>
                </div>
                <span className={styles.cardBadge}>4 周</span>
              </div>
              <div className={styles.chartWrap}>
                <ResponsiveContainer width="100%" height={280}>
                  <LineChart data={trendData} margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke={gridColor} />
                    <XAxis dataKey="name" tick={axisTick} axisLine={axisLine} tickLine={axisLine} />
                    <YAxis
                      tick={axisTick}
                      axisLine={axisLine}
                      tickLine={axisLine}
                      allowDecimals={false}
                    />
                    <Tooltip
                      contentStyle={tooltipContentStyle}
                      labelStyle={tooltipLabelStyle}
                      itemStyle={tooltipItemStyle}
                      cursor={{ stroke: gridColor, strokeDasharray: "4 4" }}
                    />
                    <Legend wrapperStyle={{ color: muted, fontSize: 12 }} />
                    <Line
                      type="monotone"
                      dataKey="我的产出"
                      stroke={accent}
                      strokeWidth={2}
                      dot={{ r: 3, strokeWidth: 2, fill: "var(--sc-card)" }}
                      activeDot={{ r: 5 }}
                    />
                    <Line
                      type="monotone"
                      dataKey="团队均线"
                      stroke={muted}
                      strokeWidth={2}
                      strokeDasharray="5 4"
                      dot={{ r: 2, strokeWidth: 2, fill: "var(--sc-card)" }}
                    />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </div>

            {/* 耗时直方图 */}
            <div className={styles.card}>
              <div className={styles.cardHeader}>
                <div>
                  <h2 className={styles.cardTitle}>标注耗时分布</h2>
                  <p className={styles.cardHint}>
                    样本 {durationSampleCount.toLocaleString()} 次，横轴为分桶上限
                  </p>
                </div>
              </div>
              {histogramData.length === 0 ? (
                <EmptyState icon="clock" text="所选范围内暂无耗时样本" />
              ) : (
                <div className={styles.chartWrap}>
                  <ResponsiveContainer width="100%" height={240}>
                    <BarChart
                      data={histogramData}
                      margin={{ top: 8, right: 8, bottom: 8, left: 0 }}
                    >
                      <CartesianGrid strokeDasharray="3 3" stroke={gridColor} />
                      <XAxis
                        dataKey="name"
                        tick={axisTick}
                        axisLine={axisLine}
                        tickLine={axisLine}
                      />
                      <YAxis
                        tick={axisTick}
                        axisLine={axisLine}
                        tickLine={axisLine}
                        allowDecimals={false}
                      />
                      <Tooltip
                        contentStyle={tooltipContentStyle}
                        labelStyle={tooltipLabelStyle}
                        itemStyle={tooltipItemStyle}
                        cursor={{ fill: "var(--sc-muted)" }}
                      />
                      <Bar dataKey="count" name="次数" fill={accent} radius={[3, 3, 0, 0]} />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              )}
            </div>

            {/* Reject 原因分布(质量归因 A1) */}
            <div className={styles.card}>
              <div className={styles.cardHeader}>
                <div>
                  <h2 className={styles.cardTitle}>Reject 原因分布</h2>
                  <p className={styles.cardHint}>累计 {rejectTotal.toLocaleString()} 次驳回</p>
                </div>
              </div>
              {rejectData.length === 0 ? (
                <EmptyState icon="checkCircle" text="所选范围内无被驳回记录" />
              ) : (
                <div className={styles.chartWrap}>
                  <ResponsiveContainer width="100%" height={200}>
                    <BarChart
                      layout="vertical"
                      data={rejectData}
                      margin={{ top: 4, right: 12, bottom: 4, left: 0 }}
                    >
                      <CartesianGrid strokeDasharray="3 3" stroke={gridColor} horizontal={false} />
                      <XAxis
                        type="number"
                        tick={axisTick}
                        axisLine={axisLine}
                        tickLine={axisLine}
                        allowDecimals={false}
                      />
                      <YAxis
                        type="category"
                        dataKey="name"
                        width={72}
                        tick={axisTick}
                        axisLine={axisLine}
                        tickLine={axisLine}
                      />
                      <Tooltip
                        contentStyle={tooltipContentStyle}
                        labelStyle={tooltipLabelStyle}
                        itemStyle={tooltipItemStyle}
                        cursor={{ fill: "var(--sc-muted)" }}
                      />
                      <Bar dataKey="value" name="次数" fill={danger} radius={[0, 3, 3, 0]} />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              )}
            </div>

            {/* 类别覆盖 top-N(质量归因 A1) */}
            <div className={styles.card}>
              <div className={styles.cardHeader}>
                <div>
                  <h2 className={styles.cardTitle}>类别覆盖(top {classData.length || ""})</h2>
                  <p className={styles.cardHint}>
                    {topClass
                      ? `最多：${topClass.name} · ${topClass.value.toLocaleString()} 次`
                      : "按标注数排序"}
                  </p>
                </div>
              </div>
              {classData.length === 0 ? (
                <EmptyState icon="tag" text="所选范围内暂无标注" />
              ) : (
                <div className={styles.chartWrap}>
                  <ResponsiveContainer width="100%" height={240}>
                    <BarChart
                      layout="vertical"
                      data={classData}
                      margin={{ top: 4, right: 12, bottom: 4, left: 0 }}
                    >
                      <CartesianGrid strokeDasharray="3 3" stroke={gridColor} horizontal={false} />
                      <XAxis
                        type="number"
                        tick={axisTick}
                        axisLine={axisLine}
                        tickLine={axisLine}
                        allowDecimals={false}
                      />
                      <YAxis
                        type="category"
                        dataKey="name"
                        width={90}
                        tick={axisTick}
                        axisLine={axisLine}
                        tickLine={axisLine}
                      />
                      <Tooltip
                        contentStyle={tooltipContentStyle}
                        labelStyle={tooltipLabelStyle}
                        itemStyle={tooltipItemStyle}
                        cursor={{ fill: "var(--sc-muted)" }}
                      />
                      <Bar dataKey="value" name="标注数" fill={accent} radius={[0, 3, 3, 0]} />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              )}
              {classData.length > 0 && otherClassPct > 0 && (
                <div className={styles.cardFooter}>其他类别合计 {otherClassPct}%</div>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function formatMaybeNumber(value: number | null): string {
  if (value === null) return "—";
  return value.toLocaleString(undefined, { maximumFractionDigits: 1 });
}

function KpiCard({
  icon,
  label,
  value,
  suffix,
  positive,
  caption,
  tone = "neutral",
}: {
  icon: IconName;
  label: string;
  value: string;
  suffix?: string;
  positive?: boolean;
  caption?: string;
  tone?: KpiTone;
}) {
  return (
    <div className={styles.kpiCard} data-tone={tone}>
      <div className={styles.kpiTop}>
        <span className={styles.kpiIcon}>
          <Icon name={icon} size={14} />
        </span>
        <span className={styles.kpiLabel}>{label}</span>
      </div>
      <div
        className={styles.kpiValue}
        data-positive={positive === undefined ? undefined : positive}
      >
        {value}
        {suffix && <span className={styles.kpiSuffix}>{suffix}</span>}
      </div>
      {caption && <div className={styles.kpiCaption}>{caption}</div>}
    </div>
  );
}

function EmptyState({ icon, text }: { icon: IconName; text: string }) {
  return (
    <div className={styles.empty}>
      <Icon name={icon} size={18} className={styles.emptyIcon} />
      <span>{text}</span>
    </div>
  );
}

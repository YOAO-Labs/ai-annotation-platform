import { useMemo, type CSSProperties } from "react";
import { Bar, BarChart, LabelList, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import type {
  DataManagerEntityFacets,
  DataManagerEntityScope,
  DataManagerFilterField,
  DataManagerSummary,
} from "@/api/taskViews";
import { Skeleton } from "@/components/shadcn/ui/skeleton";
import { useTheme } from "@/hooks/useTheme";

const SOURCE_LABELS: Record<string, string> = {
  manual: "人工",
  prediction_based: "接受 AI",
  ai_tracker: "AI 追踪",
  interpolated: "插值",
};

const STATUS_LABELS: Record<string, string> = {
  pending: "待标注",
  in_progress: "标注中",
  review: "待审核",
  rejected: "已退回",
  completed: "已完成",
};

const QUALITY_LABELS: Record<string, string> = {
  missing_keyframes: "缺少关键帧",
  track_identity_mismatch: "轨迹标识不一致",
  inconsistent_class: "类别不一致",
  inconsistent_attributes: "属性不一致",
  multiple_scenes: "跨多个 Scene",
  duplicate_frame: "同帧重复",
  duplicate_keyframe: "关键帧重复",
};

const CONFIDENCE_BUCKET_LABELS: Record<string, string> = {
  lt_025: "0–24%",
  "025_049": "25–49%",
  "050_074": "50–74%",
  gte_075: "75–100%",
};

const STATUS_COLOR_VARS: Record<string, string> = {
  pending: "--sc-chart-1",
  in_progress: "--sc-chart-4",
  review: "--sc-chart-3",
  completed: "--sc-chart-2",
  rejected: "--sc-destructive",
};

function cssVar(name: string) {
  if (typeof window === "undefined") return "";
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function rows(
  values: Record<string, number>,
  labels: Record<string, string> = {},
  order?: string[],
) {
  return Object.entries(values)
    .filter(([, value]) => value > 0)
    .sort((a, b) => (order ? order.indexOf(a[0]) - order.indexOf(b[0]) : b[1] - a[1]))
    .slice(0, 8)
    .map(([key, value]) => ({ key, name: labels[key] ?? key, value }));
}

interface ChartSpec {
  title: string;
  description: string;
  data: Array<{ key: string; name: string; value: number }>;
  kind?: "status";
  filterField?: string;
}

function chartAria(spec: ChartSpec) {
  return `${spec.title}：${spec.data.map((item) => `${item.name} ${item.value}`).join("，")}`;
}

export function DataManagerCharts({
  scope,
  summary,
  facets,
  fields,
  isLoading = false,
  onSelect,
}: {
  scope: DataManagerEntityScope;
  summary?: DataManagerSummary;
  facets?: DataManagerEntityFacets;
  fields?: DataManagerFilterField[];
  isLoading?: boolean;
  onSelect?: (field: string, value: string) => void;
}) {
  useTheme();
  const muted = cssVar("--sc-muted-foreground");
  const rankColor = cssVar("--sc-chart-1");
  const statusColors: Record<string, string> = Object.fromEntries(
    Object.entries(STATUS_COLOR_VARS).map(([key, varName]) => [key, cssVar(varName)]),
  );
  const tooltipStyle = useMemo<CSSProperties>(
    () => ({
      background: "var(--sc-card)",
      border: "1px solid var(--sc-border)",
      borderRadius: "var(--radius-md)",
      boxShadow: "var(--shadow-md)",
      color: "var(--sc-foreground)",
    }),
    [],
  );
  const tick = useMemo(() => ({ fontSize: 11, fill: muted }), [muted]);
  const specs = useMemo<ChartSpec[]>(() => {
    if (scope === "tasks" && summary) {
      const base: ChartSpec[] = [
        {
          title: "任务状态",
          description: "当前筛选范围内的任务状态分布",
          data: rows(summary.task_status, STATUS_LABELS),
          kind: "status",
          filterField: "task.status",
        },
        {
          title: "标注来源",
          description: "active annotation 按来源统计",
          data: rows(summary.annotations.by_source, SOURCE_LABELS),
          filterField: "annotation.source",
        },
        {
          title: "标注类别",
          description: "数量最多的前 8 个类别",
          data: rows(summary.annotations.by_class),
          filterField: "annotation.class_name",
        },
        {
          title: "待审模型版本",
          description: "仅统计当前仍待审的 AI 检测候选",
          data: rows(summary.ai_review.by_model_version),
        },
        {
          title: "待审置信度",
          description: "候选级分布，低于 50% 计为低置信",
          data: rows(summary.ai_review.confidence_buckets, CONFIDENCE_BUCKET_LABELS, [
            "lt_025",
            "025_049",
            "050_074",
            "gte_075",
          ]),
        },
      ];
      const attributeSpecs: ChartSpec[] = (summary.attributes ?? [])
        .filter((attr) => Object.keys(attr.values).length > 0)
        .map((attr) => {
          const fieldKey = `annotation.attribute.${attr.tool_unit_id}.${attr.key}`;
          const field = fields?.find((item) => item.key === fieldKey);
          const valueLabels = Object.fromEntries(
            (field?.options ?? []).map((option) => [option.value, option.label]),
          );
          return {
            title: attr.label,
            description: `「${attr.label}」属性值分布 · 点击下钻`,
            data: rows(attr.values, valueLabels),
            filterField: field ? fieldKey : undefined,
          };
        });
      return [...base, ...attributeSpecs];
    }
    if (!facets) return [];
    return [
      {
        title: scope === "objects" ? "对象来源" : "轨迹来源",
        description: "当前筛选结果按 annotation source 统计",
        data: rows(facets.by_source, SOURCE_LABELS),
        filterField: "annotation.source",
      },
      {
        title: scope === "objects" ? "对象类别" : "轨迹类别",
        description: "数量最多的前 8 个类别",
        data: rows(facets.by_class),
        filterField: "annotation.class_name",
      },
      {
        title: scope === "tracks" ? "质量异常" : "几何类型",
        description: scope === "tracks" ? "需要进一步审阅的轨迹异常" : "对象按几何类型统计",
        data: scope === "tracks" ? rows(facets.by_quality, QUALITY_LABELS) : rows(facets.by_type),
        filterField: scope === "objects" ? "annotation.annotation_type" : undefined,
      },
    ];
  }, [facets, fields, scope, summary]);

  const barColor = (spec: ChartSpec, key: string) =>
    (spec.kind === "status" ? statusColors[key] : undefined) ?? rankColor;

  return (
    <section aria-label="标注统计图表" className="grid gap-x-6 gap-y-5 lg:grid-cols-2">
      {(isLoading
        ? Array.from({ length: scope === "tasks" ? 5 : 3 }, (_, index) => index)
        : specs
      ).map((spec) => (
        <div key={typeof spec === "number" ? spec : spec.title} className="min-w-0">
          {typeof spec === "number" ? (
            <Skeleton className="h-40 w-full" />
          ) : (
            <>
              <div className="mb-2">
                <h3 className="text-sm font-medium text-foreground">{spec.title}</h3>
                <p className="mt-0.5 text-xs text-muted-foreground">{spec.description}</p>
              </div>
              {spec.data.length === 0 ? (
                <div className="flex min-h-[2.5rem] items-center text-xs text-muted-foreground">
                  当前筛选范围暂无可绘制数据
                </div>
              ) : (
                <div
                  className={`h-40 w-full [&_*]:outline-none${spec.filterField && onSelect ? " [&_.recharts-bar-rectangle]:cursor-pointer" : ""}`}
                  role="img"
                  aria-label={chartAria(spec)}
                >
                  <ResponsiveContainer
                    width="100%"
                    height="100%"
                    minWidth={0}
                    initialDimension={{ width: 320, height: 160 }}
                  >
                    <BarChart
                      accessibilityLayer={false}
                      data={spec.data.map((entry) => ({
                        ...entry,
                        fill: barColor(spec, entry.key),
                      }))}
                      layout="vertical"
                      margin={{ top: 0, right: 44, bottom: 0, left: 4 }}
                    >
                      <XAxis type="number" hide />
                      <YAxis
                        type="category"
                        dataKey="name"
                        width={80}
                        tick={tick}
                        axisLine={false}
                        tickLine={false}
                      />
                      <Tooltip
                        cursor={false}
                        contentStyle={tooltipStyle}
                        labelStyle={{ color: "var(--sc-foreground)", fontWeight: 500 }}
                        itemStyle={{ color: "var(--sc-muted-foreground)" }}
                        formatter={(value) => [Number(value).toLocaleString(), "数量"]}
                      />
                      <Bar
                        dataKey="value"
                        maxBarSize={22}
                        radius={[0, 4, 4, 0]}
                        isAnimationActive={false}
                        onClick={
                          spec.filterField && onSelect
                            ? (data: unknown) => {
                                const key = (data as { payload?: { key?: unknown } }).payload?.key;
                                if (spec.filterField && onSelect && typeof key === "string") {
                                  onSelect(spec.filterField, key);
                                }
                              }
                            : undefined
                        }
                      >
                        <LabelList
                          dataKey="value"
                          position="right"
                          fill={muted}
                          fontSize={11}
                          formatter={(value) => Number(value).toLocaleString()}
                        />
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              )}
            </>
          )}
        </div>
      ))}
    </section>
  );
}

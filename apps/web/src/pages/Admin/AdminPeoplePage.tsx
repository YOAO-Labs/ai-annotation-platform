import { FilterGroup, FilterToggle, FilterSelect } from "@/components/filters/FilterControls";
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type MouseEvent,
} from "react";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Icon } from "@/components/ui/Icon";
import { Card } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { UserAvatar } from "@/components/ui/UserAvatar";
import { Sparkline } from "@/components/ui/Sparkline";
import { Histogram } from "@/components/ui/Histogram";
import { RadialProgress } from "@/components/ui/RadialProgress";
import { useElementStyle } from "@/components/ui/useElementStyle";
import { useAdminPeople, useAdminPersonDetail } from "@/hooks/useDashboard";
import { useProjects } from "@/hooks/useProjects";
import { usePermissions } from "@/hooks/usePermissions";
import { REJECT_REASON_TYPE_LABELS } from "@/pages/Review/rejectReasonTypes";
import { ROLE_LABELS } from "@/constants/roles";
import type { PlatformRole } from "@/types";
import { dashboardApi, type AdminPersonItem } from "@/api/dashboard";
import { tasksApi } from "@/api/tasks";
import { useToastStore } from "@/components/ui/Toast";
import { isCurrentAuthOwner, useAuthStore } from "@/stores/authStore";
import { PageContainer } from "@/components/layout/PageContainer";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import { useUrlFilterState } from "@/hooks/useUrlFilterState";
import {
  ADMIN_PEOPLE_URL_DEFAULTS,
  ADMIN_PEOPLE_URL_KEYS,
  adminPeopleUrlCodec,
  type PeoplePeriod,
  type PeopleSort,
} from "./adminPeopleUrlState";

const ROLE_OPTS = [
  { v: "", label: "全部" },
  { v: "annotator", label: "标注员" },
  { v: "reviewer", label: "审核员" },
];

/**
 * Platform account role label.  The card never renders a raw enum and never
 * compares the platform role to a project duty (that comparison is always false
 * after the employee cutover).
 */
export function platformRoleLabel(role: string): string {
  return ROLE_LABELS[role as PlatformRole] ?? role;
}

type BadgeVariant = "default" | "accent" | "ai" | "danger";

/** Badge colour by platform account role (not by project duty). */
export const PLATFORM_ROLE_BADGE_VARIANT: Record<string, BadgeVariant> = {
  super_admin: "danger",
  project_admin: "accent",
  employee: "ai",
  viewer: "default",
};

export function platformRoleBadgeVariant(role: string): BadgeVariant {
  return PLATFORM_ROLE_BADGE_VARIANT[role] ?? "default";
}
const PERIOD_OPTS = [
  { v: "today", label: "今日" },
  { v: "7d", label: "最近 7 天" },
  { v: "1m", label: "最近 30 天" },
];
const SORT_OPTS = [
  { v: "throughput", label: "产能↓" },
  { v: "quality", label: "质量↓" },
  { v: "activity", label: "活跃↓" },
  { v: "weekly_compare", label: "周环比↓" },
];

// UA-safe 表单基线(无全局 preflight 期间,原生 select/input 需消浏览器默认样式)
const FIELD_CLASS =
  "appearance-none rounded-md border border-border bg-card px-2.5 py-1.5 text-sm text-foreground";
const SECTION_TITLE_CLASS = "border-b border-border px-3.5 py-2.5 text-xs font-semibold";
const SECTION_TITLE_META_CLASS = "ml-2 text-xs font-normal text-muted-foreground";
const DISTRIBUTION_ROW_CLASS = "flex justify-between px-3.5 py-1.5 text-sm";
const DISTRIBUTION_LINK_CLASS = `${DISTRIBUTION_ROW_CLASS} w-full cursor-pointer appearance-none rounded-md border-0 bg-transparent text-left [font:inherit] hover:bg-muted`;

export function AdminPeoplePage() {
  const navigate = useNavigate();
  const authOwnerId = useAuthStore((state) => state.user?.id);
  const urlState = useUrlFilterState({
    codec: adminPeopleUrlCodec,
    defaults: ADMIN_PEOPLE_URL_DEFAULTS,
    ownedKeys: ADMIN_PEOPLE_URL_KEYS,
  });
  const { state: filters, issues, patch } = urlState;
  const [queryDraft, setQueryDraft] = useState(filters.q);
  const syncingQueryDraft = useRef(false);
  const lastUrlQuery = useRef(filters.q);
  const debouncedQuery = useDebouncedValue(queryDraft, 250);
  const role = filters.role;
  const period = filters.period;
  const sort = filters.sort;
  const q = filters.q;
  const project = filters.project;

  const [activeUserId, setActiveUserId] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const pushToast = useToastStore((s) => s.push);

  // v0.12.6 (A3) · 项目级范围:project_admin 仅见自有项目且必须选定一个项目。
  const { role: userRole } = usePermissions();
  const isProjectAdmin = userRole === "project_admin";
  const { data: projects } = useProjects();
  const projectOpts = useMemo(() => projects ?? [], [projects]);

  useEffect(() => {
    if (lastUrlQuery.current === filters.q) return;
    lastUrlQuery.current = filters.q;
    if (queryDraft !== filters.q) {
      syncingQueryDraft.current = true;
      setQueryDraft(filters.q);
    }
  }, [filters.q, queryDraft]);

  useEffect(() => {
    if (syncingQueryDraft.current) {
      if (debouncedQuery === filters.q) syncingQueryDraft.current = false;
      return;
    }
    const nextQuery = debouncedQuery.trim();
    if (nextQuery !== queryDraft.trim()) return;
    if (nextQuery === filters.q) return;
    patch({ q: nextQuery }, { replace: true });
  }, [debouncedQuery, filters.q, patch, queryDraft]);

  // project_admin 未选项目时自动选第一个(后端对其强制项目范围,不选会 403)。
  useEffect(() => {
    if (isProjectAdmin && !project && projectOpts.length > 0) {
      patch({ project: projectOpts[0].id });
    }
  }, [isProjectAdmin, patch, project, projectOpts]);

  const peopleParams = useMemo(
    () => ({
      role: role || undefined,
      project: project || undefined,
      period,
      sort,
      q: q || undefined,
    }),
    [period, project, q, role, sort],
  );

  const handleExport = async () => {
    const ownerId = authOwnerId;
    const token = useAuthStore.getState().token;
    const isCurrentOwner = () =>
      Boolean(
        ownerId && token && token === useAuthStore.getState().token && isCurrentAuthOwner(ownerId),
      );
    if (!isCurrentOwner()) return;
    setExporting(true);
    try {
      await dashboardApi.exportPeople(peopleParams);
      if (!isCurrentOwner()) return;
    } catch (e) {
      if (!isCurrentOwner()) return;
      pushToast({
        kind: "error",
        msg: "导出失败",
        sub: e instanceof Error ? e.message : undefined,
      });
    } finally {
      setExporting(false);
    }
  };

  const { data, isLoading } = useAdminPeople({
    ...peopleParams,
    // project_admin 在自动选定项目前不发请求(避免 403 噪声)
    enabled: !(isProjectAdmin && !project),
  });

  const items = data?.items ?? [];

  return (
    <PageContainer>
      <div className="mb-4 flex items-end justify-between">
        <div>
          <h1 className="mb-1 text-xl font-semibold">成员绩效</h1>
          <p className="text-sm text-muted-foreground">全员效率卡片网格 · 点击卡片查看详情</p>
          {!!issues.length && (
            <div role="alert" className="mt-1 text-xs text-status-caution">
              URL 绩效筛选无法完整恢复，已使用安全默认值。
            </div>
          )}
        </div>
        <div className="flex gap-2">
          {project && (
            <Button onClick={() => navigate(`/projects/${project}/data-manager?section=members`)}>
              项目成员看板
            </Button>
          )}
          <Button variant="ghost" onClick={handleExport} disabled={exporting}>
            <Icon name="download" size={13} />
            {exporting ? "导出中…" : "导出 CSV"}
          </Button>
          <Button variant="ghost" onClick={() => navigate("/dashboard")}>
            <Icon name="chevron-left" size={13} />
            返回总览
          </Button>
        </div>
      </div>

      {/* sticky filter bar */}
      <div className="sticky top-16 z-local-5 mb-4">
        <Card>
          <div className="flex flex-wrap items-center gap-2 p-3">
            <FilterGroup label="角色" compact>
              {ROLE_OPTS.map((option) => (
                <FilterToggle
                  key={option.v}
                  active={role === option.v}
                  onClick={() => patch({ role: option.v }, { replace: false })}
                >
                  {option.label}
                </FilterToggle>
              ))}
            </FilterGroup>
            <FilterGroup label="时间" compact>
              {PERIOD_OPTS.map((option) => (
                <FilterToggle
                  key={option.v}
                  active={period === option.v}
                  onClick={() => patch({ period: option.v as PeoplePeriod }, { replace: false })}
                >
                  {option.label}
                </FilterToggle>
              ))}
            </FilterGroup>
            <SortOptions
              label="排序"
              opts={SORT_OPTS}
              value={sort}
              onChange={(v: string) => patch({ sort: v as PeopleSort }, { replace: false })}
            />
            {/* v0.12.6 (A3) · 项目级范围下拉 */}
            <FilterGroup label="项目" compact>
              <FilterSelect
                className="max-w-[200px]"
                value={project}
                onChange={(e) => patch({ project: e.target.value }, { replace: false })}
                aria-label="项目范围"
              >
                {!isProjectAdmin && <option value="">全部项目（全局）</option>}
                {projectOpts.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </FilterSelect>
            </FilterGroup>
            <input
              type="search"
              placeholder="姓名 / 邮箱"
              value={queryDraft}
              onChange={(e) => {
                syncingQueryDraft.current = false;
                setQueryDraft(e.target.value);
              }}
              onKeyDown={(e: KeyboardEvent<HTMLInputElement>) => {
                if (e.key === "Enter")
                  patch({ q: e.currentTarget.value.trim() }, { replace: true });
              }}
              className={`${FIELD_CLASS} ml-auto min-w-[200px]`}
            />
          </div>
        </Card>
      </div>

      {isLoading ? (
        <div className="p-15 text-center text-muted-foreground">加载中...</div>
      ) : items.length === 0 ? (
        <Card>
          <div className="px-4 py-12 text-center text-muted-foreground">
            <Icon name="users" size={36} className="mb-2.5 opacity-25" />
            <div className="mb-1 text-sm">暂无成员数据</div>
            <div className="text-xs text-muted-foreground">调整筛选条件重试</div>
          </div>
        </Card>
      ) : (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(280px,1fr))] gap-3">
          {items.map((it) => (
            <PersonCard key={it.user_id} item={it} onClick={() => setActiveUserId(it.user_id)} />
          ))}
        </div>
      )}

      {activeUserId && (
        <PersonDrawer
          key={`${authOwnerId}:${project}:${period}:${activeUserId}`}
          userId={activeUserId}
          project={project || undefined}
          period={period}
          onClose={() => setActiveUserId(null)}
        />
      )}
    </PageContainer>
  );
}

function SortOptions({
  label,
  opts,
  value,
  onChange,
}: {
  label: string;
  opts: Array<{ v: string; label: string }>;
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className="flex items-center gap-1.5">
      <span className="text-xs text-muted-foreground">{label}</span>
      <div className="flex gap-0.5">
        {opts.map((o) => (
          <button
            key={o.v}
            type="button"
            onClick={() => onChange(o.v)}
            className={`cursor-pointer appearance-none rounded-sm border border-border px-2.5 py-1 text-xs ${
              value === o.v
                ? "bg-brand/10 font-semibold text-brand"
                : "bg-card font-normal text-foreground"
            }`}
          >
            {o.label}
          </button>
        ))}
      </div>
    </div>
  );
}

function PersonCard({ item, onClick }: { item: AdminPersonItem; onClick: () => void }) {
  const trend = item.weekly_compare_pct;
  return (
    <Card onClick={onClick} className="cursor-pointer">
      <div className="flex flex-col gap-2.5 p-3.5">
        <div className="flex items-center gap-2.5">
          <UserAvatar
            user={{ name: item.name, email: item.email, avatar_ref: item.avatar_ref }}
            size="md"
          />
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-1.5 text-sm font-semibold">
              <span className="truncate">{item.name}</span>
              <span
                className={`size-1.5 rounded-full ${
                  item.status === "online" ? "bg-emerald-500" : "bg-muted-foreground"
                }`}
              />
            </div>
            <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <Badge variant={platformRoleBadgeVariant(item.role)}>
                {platformRoleLabel(item.role)}
              </Badge>
              {item.project_count} 项目
            </div>
          </div>
          <RadialProgress
            value={Math.round(
              (item.throughput_score + item.quality_score + item.activity_score) / 3,
            )}
            size={36}
            thickness={4}
          />
        </div>

        <div>
          <div className="text-2xl font-semibold tabular-nums">
            {item.main_metric.toLocaleString()}
            {trend != null && (
              <span
                className={`ml-1.5 text-xs font-medium ${
                  trend >= 0 ? "text-status-positive" : "text-status-danger"
                }`}
              >
                {trend >= 0 ? "↑" : "↓"} {Math.abs(trend)}%
              </span>
            )}
          </div>
          <div className="text-xs text-muted-foreground">{item.main_metric_label}</div>
        </div>

        <PercentBars
          rows={[
            { label: "产能", value: item.throughput_score },
            { label: "质量", value: item.quality_score },
            { label: "活跃", value: item.activity_score },
          ]}
        />

        <Sparkline values={item.sparkline_7d} color="var(--sc-brand)" width={252} height={24} />

        {item.alerts.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {item.alerts.includes("high_rejected") && (
              <Badge variant="danger">退回率 {item.rejected_rate}% &gt; 15%</Badge>
            )}
            {item.alerts.includes("drop_30") && (
              <Badge variant="warning">周环比降幅 &gt; 30%</Badge>
            )}
          </div>
        )}
      </div>
    </Card>
  );
}

function PercentBars({ rows }: { rows: Array<{ label: string; value: number }> }) {
  return (
    <div className="flex flex-col gap-1">
      {rows.map((r) => (
        <div key={r.label} className="flex items-center gap-2 text-xs">
          <span className="w-7 text-xs text-muted-foreground">{r.label}</span>
          <div className="h-1 flex-1 overflow-hidden rounded-sm bg-border">
            <PercentBarFill value={r.value} />
          </div>
          <span className="w-6 text-right text-xs tabular-nums text-muted-foreground">
            {Math.round(r.value)}
          </span>
        </div>
      ))}
    </div>
  );
}

function PercentBarFill({ value }: { value: number }) {
  const width = Math.min(100, Math.max(0, value));
  const ref = useElementStyle<HTMLDivElement>({ "--bar-width": `${width}%` } as CSSProperties);
  return <div ref={ref} className="h-full w-[var(--bar-width)] bg-brand" />;
}

function PersonDrawer({
  userId,
  project,
  period,
  onClose,
}: {
  userId: string;
  project?: string;
  period: PeoplePeriod;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const { data, isLoading } = useAdminPersonDetail(userId, period, project);

  // v0.12.6 (A3) · reject/类别维度下钻:仅项目模式可下钻(tasks 查询需 project_id)。
  // 选中一个维度值 → 内联展开该项目内本人匹配任务列表。
  const [drill, setDrill] = useState<{ type: "reject" | "class"; value: string } | null>(null);
  const toggleDrill = (type: "reject" | "class", value: string) => {
    setDrill((cur) => (cur && cur.type === type && cur.value === value ? null : { type, value }));
  };

  // v0.12.5 · 项目维度下钻:跳到该项目 review 队列按本人 assignee 过滤(复用后端 assignee_id 过滤)。
  const drillToProject = (projectId: string) => {
    navigate(`/review?project=${projectId}&assignee=${userId}`);
    onClose();
  };
  const histogramValues = useMemo(
    () => (data?.duration_histogram ?? []).map((b) => b.count),
    [data],
  );
  const xLabels = useMemo(
    () => (data?.duration_histogram ?? []).map((b) => `${Math.round(b.upper_ms / 1000)}s`),
    [data],
  );

  return (
    <div onClick={onClose} className="fixed inset-0 z-modal flex justify-end bg-black/40">
      <div
        onClick={(e: MouseEvent<HTMLDivElement>) => e.stopPropagation()}
        className="flex w-[min(540px,100%)] flex-col border-l border-border bg-card"
      >
        <div className="flex items-center justify-between border-b border-border px-4 py-3.5">
          <div className="text-sm font-semibold">{data?.name ?? "成员详情"}</div>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭"
            className="cursor-pointer appearance-none border-0 bg-transparent text-muted-foreground"
          >
            <Icon name="x" size={16} />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-4">
          {isLoading || !data ? (
            <div className="p-8 text-center text-muted-foreground">加载中...</div>
          ) : (
            <div className="flex flex-col gap-4">
              <div className="grid grid-cols-[repeat(auto-fit,minmax(120px,1fr))] gap-2">
                <KpiCell label="产能" value={data.throughput} />
                <KpiCell label="质量" value={`${data.quality_score}%`} />
                <KpiCell
                  label="活跃"
                  value={data.active_minutes == null ? "—" : `${data.active_minutes}m`}
                />
                <KpiCell label="综合分" value={data.composite_score} />
                <KpiCell
                  label="首过率"
                  value={
                    data.first_pass_yield == null
                      ? "—"
                      : `${Math.round(data.first_pass_yield * 100)}%`
                  }
                />
              </div>

              <Card>
                <div className={SECTION_TITLE_CLASS}>4 周趋势</div>
                <div className="p-3.5">
                  <div className="mb-1.5 text-xs text-muted-foreground">产能</div>
                  <Sparkline
                    values={data.trend_throughput}
                    width={480}
                    height={48}
                    color="var(--sc-brand)"
                  />
                  <div className="mb-1.5 mt-3 text-xs text-muted-foreground">质量分</div>
                  <Sparkline
                    values={data.trend_quality}
                    width={480}
                    height={48}
                    color="var(--sc-positive)"
                  />
                </div>
              </Card>

              {data.duration_histogram.length > 0 && (
                <Card>
                  <div className={SECTION_TITLE_CLASS}>
                    任务耗时分布
                    {data.p50_duration_ms != null && (
                      <span className={SECTION_TITLE_META_CLASS}>
                        p50 {Math.round(data.p50_duration_ms / 1000)}s · p95{" "}
                        {Math.round((data.p95_duration_ms ?? 0) / 1000)}s
                      </span>
                    )}
                  </div>
                  <div className="p-3.5">
                    <Histogram values={histogramValues} xLabels={xLabels} />
                  </div>
                </Card>
              )}

              {data.project_distribution.length > 0 && (
                <Card>
                  <div className={SECTION_TITLE_CLASS}>
                    项目分布
                    <span className={SECTION_TITLE_META_CLASS}>点击进入该项目审核队列</span>
                  </div>
                  <div className="py-2">
                    {data.project_distribution.map((p) => (
                      <button
                        type="button"
                        key={p.project_id}
                        className={DISTRIBUTION_LINK_CLASS}
                        onClick={() => drillToProject(p.project_id)}
                        title="进入该项目审核队列(已按本人过滤)"
                      >
                        <span>{p.project_name}</span>
                        <span className="tabular-nums text-muted-foreground">{p.count}</span>
                      </button>
                    ))}
                  </div>
                </Card>
              )}

              {data.reject_reason_breakdown.length > 0 && (
                <Card>
                  <div className={SECTION_TITLE_CLASS}>
                    Reject 原因分布
                    {project && (
                      <span className={SECTION_TITLE_META_CLASS}>点击下钻该项目任务</span>
                    )}
                  </div>
                  <div className="py-2">
                    {data.reject_reason_breakdown.map((r) => {
                      const label =
                        REJECT_REASON_TYPE_LABELS[
                          r.reason_type as keyof typeof REJECT_REASON_TYPE_LABELS
                        ] ?? r.reason_type;
                      const active = drill?.type === "reject" && drill.value === r.reason_type;
                      const rowBody = (
                        <>
                          <span>{label}</span>
                          <span className="tabular-nums text-muted-foreground">
                            {r.count} · {r.pct}%
                          </span>
                        </>
                      );
                      return project ? (
                        <div key={r.reason_type}>
                          <button
                            type="button"
                            className={DISTRIBUTION_LINK_CLASS}
                            onClick={() => toggleDrill("reject", r.reason_type)}
                          >
                            {rowBody}
                          </button>
                          {active && (
                            <DrillTaskList
                              projectId={project}
                              assigneeId={userId}
                              rejectReasonType={r.reason_type}
                            />
                          )}
                        </div>
                      ) : (
                        <div key={r.reason_type} className={DISTRIBUTION_ROW_CLASS}>
                          {rowBody}
                        </div>
                      );
                    })}
                  </div>
                </Card>
              )}

              {data.class_distribution.length > 0 && (
                <Card>
                  <div className={SECTION_TITLE_CLASS}>
                    类别覆盖(top {data.class_distribution.length})
                    {project && (
                      <span className={SECTION_TITLE_META_CLASS}>点击下钻该项目任务</span>
                    )}
                  </div>
                  <div className="py-2">
                    {data.class_distribution.map((c) => {
                      const active = drill?.type === "class" && drill.value === c.class_name;
                      const rowBody = (
                        <>
                          <span>{c.class_name}</span>
                          <span className="tabular-nums text-muted-foreground">
                            {c.count} · {c.pct}%
                          </span>
                        </>
                      );
                      return project ? (
                        <div key={c.class_name}>
                          <button
                            type="button"
                            className={DISTRIBUTION_LINK_CLASS}
                            onClick={() => toggleDrill("class", c.class_name)}
                          >
                            {rowBody}
                          </button>
                          {active && (
                            <DrillTaskList
                              projectId={project}
                              assigneeId={userId}
                              classNameFilter={c.class_name}
                            />
                          )}
                        </div>
                      ) : (
                        <div key={c.class_name} className={DISTRIBUTION_ROW_CLASS}>
                          {rowBody}
                        </div>
                      );
                    })}
                    {(() => {
                      const sum = data.class_distribution.reduce((s, c) => s + c.pct, 0);
                      const other = Math.max(0, 100 - sum);
                      return other > 0 ? (
                        <div className={DISTRIBUTION_ROW_CLASS}>
                          <span>其他</span>
                          <span className="tabular-nums text-muted-foreground">{other}%</span>
                        </div>
                      ) : null;
                    })()}
                  </div>
                </Card>
              )}

              {data.timeline.length > 0 && (
                <Card>
                  <div className={SECTION_TITLE_CLASS}>最近 timeline ({data.timeline.length})</div>
                  <div className="max-h-80 overflow-y-auto">
                    {data.timeline.map((t, i) => (
                      <div
                        key={i}
                        className="flex items-center gap-2 border-t border-border px-3.5 py-2 text-xs [&:first-child]:border-t-0"
                      >
                        <Badge variant="outline">{t.action}</Badge>
                        {t.task_display_id && (
                          <span className="mono text-xs text-brand">{t.task_display_id}</span>
                        )}
                        <span className="ml-auto text-xs text-muted-foreground">
                          {t.at ? new Date(t.at).toLocaleString("zh-CN") : "—"}
                        </span>
                      </div>
                    ))}
                  </div>
                </Card>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// v0.12.6 (A3) · 下钻任务列表:项目内本人按 reject 原因 / 类别过滤的任务(只读,展示 display_id + 状态)。
function DrillTaskList({
  projectId,
  assigneeId,
  rejectReasonType,
  classNameFilter,
}: {
  projectId: string;
  assigneeId: string;
  rejectReasonType?: string;
  classNameFilter?: string;
}) {
  const ownerId = useAuthStore((state) => state.user?.id);
  const { data, isLoading } = useQuery({
    queryKey: ["drill-tasks", ownerId, projectId, assigneeId, rejectReasonType, classNameFilter],
    queryFn: ({ signal }) =>
      tasksApi.listByProject(
        projectId,
        {
          assignee_id: assigneeId,
          reject_reason_type: rejectReasonType,
          class_name: classNameFilter,
          limit: 20,
        },
        { signal },
      ),
    enabled: Boolean(ownerId),
  });
  const tasks = data?.items ?? [];
  return (
    <div className="flex flex-col gap-1 px-3.5 pb-2 pt-1">
      {isLoading ? (
        <div className="py-1 text-xs text-muted-foreground">加载中...</div>
      ) : tasks.length === 0 ? (
        <div className="py-1 text-xs text-muted-foreground">该项目内无匹配任务</div>
      ) : (
        tasks.map((t) => (
          <div
            key={t.id}
            className="flex items-center justify-between rounded-md bg-muted px-2 py-1 text-xs"
          >
            <span className="mono">{t.display_id}</span>
            <span className="text-muted-foreground">{t.status}</span>
          </div>
        ))
      )}
    </div>
  );
}

function KpiCell({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-md border border-border bg-muted px-3 py-2.5">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="text-lg font-semibold tabular-nums">{value}</div>
    </div>
  );
}

import { useMemo, type MouseEvent } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { Icon } from "@/components/ui/Icon";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";
import { StatCard } from "@/components/ui/StatCard";
import { Sparkline } from "@/components/ui/Sparkline";
import { Histogram } from "@/components/ui/Histogram";
import { SectionDivider } from "@/components/ui/SectionDivider";
import { useAnnotatorStats, useMyBatches } from "@/hooks/useDashboard";
import { useProjects } from "@/hooks/useProjects";
import { ApiError } from "@/api/client";
import type { ProjectResponse } from "@/api/projects";
import { MyBatchesCard } from "./MyBatchesCard";
import { StartChecklistCard } from "./StartChecklistCard";
import { WorkPriorityCard } from "./WorkPriorityCard";
import { buildWorkbenchUrl, currentWorkbenchReturnTo } from "@/utils/workbenchNavigation";
import { projectDisplayType } from "@/utils/projectDisplay";
import { PageContainer } from "@/components/layout/PageContainer";
import {
  isInitialQueryPaused,
  isRefreshQueryPaused,
  QueryPausedNotice,
  QueryPausedState,
} from "@/pages/shared/QueryState";

const STATS_GRID_FOUR = "grid grid-cols-[repeat(auto-fit,minmax(180px,1fr))] gap-3";
const STATS_GRID_THREE = "grid grid-cols-[repeat(auto-fit,minmax(180px,1fr))] gap-3";
const CARD_TITLE = "m-0 text-sm font-semibold";
const CARD_HEADER_PLAIN = "border-b border-border px-4 py-3.5";
const TABLE_CLASS =
  "w-full min-w-[760px] border-separate border-spacing-0 text-sm " +
  "[&_th]:border-b [&_th]:border-border [&_th]:bg-muted [&_th]:whitespace-nowrap [&_th]:px-3 [&_th]:py-2.5 [&_th]:text-left [&_th]:text-xs [&_th]:font-medium [&_th]:text-muted-foreground " +
  "[&_th:first-child]:pl-4 [&_th:last-child]:pr-4 " +
  "[&_td]:border-b [&_td]:border-border [&_td]:px-3 [&_td]:py-3 [&_td]:align-middle " +
  "[&_td:first-child]:py-2.5 [&_td:first-child]:pl-4 [&_td:last-child]:py-2.5 [&_td:last-child]:pr-4";

function formatMs(ms: number | null | undefined): string {
  if (ms == null) return "—";
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const m = Math.floor(ms / 60_000);
  const s = Math.round((ms % 60_000) / 1000);
  return `${m}m${s.toString().padStart(2, "0")}s`;
}

function queryErrorMessage(error: unknown, resource: string): string {
  if (error instanceof ApiError && error.status === 403) {
    return `当前账号没有权限查看${resource}，请联系项目管理员。`;
  }
  if (error instanceof ApiError && error.status === 404) {
    return `${resource}已不存在或已被移除，请刷新后重试。`;
  }
  if (error instanceof ApiError && error.status >= 500) {
    return `${resource}服务暂时不可用，请稍后重试。`;
  }
  return `${resource}加载失败，请检查网络后重试。`;
}

function QueryErrorState({
  resource,
  error,
  onRetry,
}: {
  resource: string;
  error: unknown;
  onRetry: () => void;
}) {
  return (
    <div role="alert" className="rounded-lg border border-border bg-card p-6">
      <h1 className="text-lg font-semibold">无法加载{resource}</h1>
      <p className="mb-4 mt-2 text-sm text-muted-foreground">
        {queryErrorMessage(error, resource)}
      </p>
      <Button onClick={onRetry}>重新加载</Button>
    </div>
  );
}

function RefreshNotice({
  resource,
  error,
  onRetry,
}: {
  resource: string;
  error: unknown;
  onRetry: () => void;
}) {
  return (
    <div
      role="alert"
      className="mb-3 flex flex-wrap items-center justify-between gap-3 rounded-md border border-border bg-status-caution-soft p-3"
    >
      <p className="m-0 text-sm">
        <strong className="mr-2 text-status-caution">{resource}更新失败</strong>
        <span className="text-muted-foreground">
          {queryErrorMessage(error, resource)} 当前内容已保留。
        </span>
      </p>
      <Button size="sm" onClick={onRetry}>
        重新加载
      </Button>
    </div>
  );
}

export function AnnotatorDashboard() {
  const statsQuery = useAnnotatorStats();
  const batchesQuery = useMyBatches();
  const stats = statsQuery.data;
  const hasStatsData = statsQuery.data !== undefined;
  const statsLoading = statsQuery.isLoading && !hasStatsData;
  const statsInitialPaused = isInitialQueryPaused(statsQuery, hasStatsData);
  const statsRefreshPaused = isRefreshQueryPaused(statsQuery, hasStatsData);
  const statsInitialError = statsQuery.isError && !hasStatsData;
  const projectsQuery = useProjects();
  const myProjects = useMemo(
    () => (projectsQuery.data ?? []).filter((project) => project.my_project_role === "annotator"),
    [projectsQuery.data],
  );
  const hasProjectData = projectsQuery.data !== undefined;
  const projectsLoading = projectsQuery.isLoading && !hasProjectData;
  const projectsInitialPaused = isInitialQueryPaused(projectsQuery, hasProjectData);
  const projectsRefreshPaused = isRefreshQueryPaused(projectsQuery, hasProjectData);
  const projectsInitialError = projectsQuery.isError && !hasProjectData;
  const navigate = useNavigate();
  const location = useLocation();
  const openWorkbench = (projectId: string) =>
    navigate(buildWorkbenchUrl(projectId, { returnTo: currentWorkbenchReturnTo(location) }));

  const sortedProjects = useMemo(
    () =>
      [...myProjects].sort((a: ProjectResponse, b: ProjectResponse) => {
        const ra = Math.max(0, (a.total_tasks ?? 0) - (a.completed_tasks ?? 0));
        const rb = Math.max(0, (b.total_tasks ?? 0) - (b.completed_tasks ?? 0));
        return rb - ra;
      }),
    [myProjects],
  );
  const myBatches = useMemo(() => batchesQuery.data ?? [], [batchesQuery.data]);
  const checklistProject = useMemo(
    () =>
      sortedProjects.find((project) =>
        myBatches.some((batch) => batch.project_id === project.id && batch.total_tasks > 0),
      ) ?? sortedProjects[0],
    [myBatches, sortedProjects],
  );

  if (statsInitialPaused) {
    return (
      <PageContainer>
        <QueryPausedState resource="标注统计" />
      </PageContainer>
    );
  }

  if (statsInitialError) {
    return (
      <PageContainer>
        <QueryErrorState
          resource="标注统计"
          error={statsQuery.error}
          onRetry={() => void statsQuery.refetch()}
        />
      </PageContainer>
    );
  }

  if (statsLoading || !stats) {
    return <div className="px-7 py-15 text-center text-muted-foreground">加载中...</div>;
  }

  const weeklyTarget = stats.weekly_target ?? 200;
  const weeklyPct = Math.min(Math.round((stats.weekly_completed / weeklyTarget) * 100), 100);
  const noProjects = myProjects.length === 0;
  const trendPct = stats.weekly_compare_pct ?? undefined;

  return (
    <PageContainer>
      {statsRefreshPaused && <QueryPausedNotice resource="标注统计" />}
      {statsQuery.isError && hasStatsData && (
        <RefreshNotice
          resource="标注统计"
          error={statsQuery.error}
          onRetry={() => void statsQuery.refetch()}
        />
      )}
      <div className="mb-3 flex items-end justify-between">
        <div>
          <h1 className="mb-1 text-xl font-semibold">标注工作台</h1>
          <p className="m-0 text-sm text-muted-foreground">查看任务进度，高效完成标注工作</p>
        </div>
        <Button variant="primary" onClick={() => navigate("/annotate")}>
          <Icon name="target" size={13} />
          进入标注页面
        </Button>
      </div>

      {/* M1 · 退回待重做提示 */}
      {(stats.rejected_tasks_count ?? 0) > 0 && (
        <div className="mb-4 flex items-center gap-3 rounded-md border border-rose-500/30 bg-status-danger-soft px-4 py-3">
          <Icon name="warning" size={16} className="shrink-0 text-status-danger" />
          <div className="flex-1">
            <span className="text-sm font-semibold text-status-danger">
              {stats.rejected_tasks_count} 个任务被退回，需重做
            </span>
            <span className="ml-2 text-xs text-muted-foreground">
              请进入工作台查看退回原因并重新提交
            </span>
          </div>
          <Button size="sm" variant="danger" onClick={() => navigate("/annotate?status=rejected")}>
            进入工作台
          </Button>
        </div>
      )}

      {checklistProject && batchesQuery.data !== undefined && (
        <div className="mb-4 grid gap-3">
          <StartChecklistCard project={checklistProject} batches={myBatches} />
          <WorkPriorityCard projects={sortedProjects} batches={myBatches} />
        </div>
      )}

      {/* 产能 */}
      <SectionDivider label="产能" hint="完成数 / 单题耗时" />
      <div className={STATS_GRID_FOUR}>
        <StatCard icon="flag" label="待标任务" value={stats.assigned_tasks} />
        <StatCard icon="check" label="今日完成" value={stats.today_completed} />
        <StatCard
          icon="activity"
          label="本周完成"
          value={stats.weekly_completed}
          trend={trendPct}
          hint={`目标 ${weeklyTarget}`}
          sparkValues={stats.daily_counts}
        />
        <StatCard
          icon="clock"
          label="平均单题耗时"
          value={formatMs(stats.median_duration_ms)}
          hint="中位 / 30 天"
        />
      </div>

      {/* 质量 */}
      <SectionDivider label="质量" hint="原创比例 / 退回率 / 重审次数" />
      <div className={STATS_GRID_THREE}>
        <StatCard icon="sparkles" label="原创比例" value={`${stats.personal_accuracy}%`} />
        <StatCard
          icon="alert-triangle"
          label="被退回率"
          value={stats.rejected_rate == null ? "—" : `${stats.rejected_rate}%`}
          hint="所有提交"
        />
        <StatCard
          icon="rotate-ccw"
          label="重审次数 avg"
          value={stats.reopened_avg == null ? "—" : stats.reopened_avg.toFixed(2)}
          hint="人均"
        />
      </div>

      {/* 投入：来自任务事件与当前账号的真实活动记录。 */}
      <SectionDivider label="投入" hint="活跃时长 / 连续天数" />
      <div className={STATS_GRID_THREE}>
        <StatCard
          icon="clock"
          label="今日活跃时长"
          value={stats.active_minutes_today == null ? "—" : `${stats.active_minutes_today}m`}
          hint="任务事件"
        />
        <StatCard
          icon="flame"
          label="连续标注天数"
          value={stats.streak_days == null ? "—" : `${stats.streak_days}天`}
          hint="任务事件"
        />
        <StatCard icon="layers" label="累计标注" value={stats.total_completed} />
      </div>

      {/* v0.8.5 · 24-bar 当日专注时段分布 */}
      <div className="mt-4">
        <Card>
          <div className={CARD_HEADER_PLAIN}>
            <h3 className={CARD_TITLE}>今日专注时段分布</h3>
            <p className="mt-1 text-xs text-muted-foreground">按小时聚合的标注分钟数（0-23 时）</p>
          </div>
          <div className="px-4 py-5">
            <Histogram
              values={stats.hour_buckets ?? Array(24).fill(0)}
              height={80}
              xLabels={["00:00", ...Array(22).fill(""), "23:00"]}
            />
          </div>
        </Card>
      </div>

      <div className="h-4" />
      <MyBatchesCard />

      <div className="mt-4 grid grid-cols-[repeat(auto-fit,minmax(320px,1fr))] gap-3">
        <Card>
          <div className={CARD_HEADER_PLAIN}>
            <h3 className={CARD_TITLE}>近 7 天标注趋势</h3>
          </div>
          <div className="px-4 py-5">
            <Sparkline
              values={stats.daily_counts}
              color="var(--sc-brand)"
              width={480}
              height={80}
            />
            <div className="mt-2 flex justify-between text-xs text-muted-foreground">
              <span>7 天前</span>
              <span>今天</span>
            </div>
          </div>
        </Card>

        <Card>
          <div className={CARD_HEADER_PLAIN}>
            <h3 className={CARD_TITLE}>本周目标进度</h3>
          </div>
          <div className="p-5 text-center">
            <div className="relative mx-auto mb-4 size-[120px]">
              <svg viewBox="0 0 120 120" width={120} height={120}>
                <circle
                  cx="60"
                  cy="60"
                  r="52"
                  fill="none"
                  stroke="var(--sc-border)"
                  strokeWidth="8"
                />
                <circle
                  cx="60"
                  cy="60"
                  r="52"
                  fill="none"
                  stroke="var(--sc-brand)"
                  strokeWidth="8"
                  strokeLinecap="round"
                  strokeDasharray={`${weeklyPct * 3.27} ${327 - weeklyPct * 3.27}`}
                  transform="rotate(-90 60 60)"
                />
              </svg>
              <div className="absolute inset-0 flex flex-col items-center justify-center">
                <span className="text-stat font-semibold">{weeklyPct}%</span>
                <span className="text-xs text-muted-foreground">完成率</span>
              </div>
            </div>
            <div className="text-sm text-muted-foreground">
              {stats.weekly_completed} / {weeklyTarget} 个标注
            </div>
          </div>
        </Card>
      </div>

      <div className="mt-4">
        <Card>
          <div className="flex items-center justify-between border-b border-border px-4 py-3.5">
            <h3 className={CARD_TITLE}>我的项目</h3>
            {!projectsInitialError && (
              <span className="text-xs text-muted-foreground">共 {sortedProjects.length} 个</span>
            )}
          </div>
          {projectsQuery.isError && hasProjectData && (
            <div className="px-4 pt-3">
              <RefreshNotice
                resource="项目列表"
                error={projectsQuery.error}
                onRetry={() => void projectsQuery.refetch()}
              />
            </div>
          )}
          {projectsRefreshPaused && (
            <div className="px-4 pt-3">
              <QueryPausedNotice resource="项目列表" />
            </div>
          )}
          {projectsLoading ? (
            <div className="px-4 py-8 text-center text-sm text-muted-foreground">加载中...</div>
          ) : projectsInitialPaused ? (
            <div className="p-4">
              <QueryPausedState resource="项目列表" compact />
            </div>
          ) : projectsInitialError ? (
            <div className="p-4">
              <QueryErrorState
                resource="项目列表"
                error={projectsQuery.error}
                onRetry={() => void projectsQuery.refetch()}
              />
            </div>
          ) : noProjects ? (
            <div className="px-4 py-8 text-center text-sm text-muted-foreground">
              <Icon name="folder" size={28} className="mb-2 opacity-25" />
              <div>暂无分配项目</div>
              <div className="mt-1 text-xs">请联系项目管理员将你加入项目成员</div>
            </div>
          ) : (
            <div className="w-full overflow-x-auto [overscroll-behavior-x:contain]">
              <table className={TABLE_CLASS}>
                <thead>
                  <tr>
                    {["项目", "类型", "进度", "待标", ""].map((h, i) => (
                      <th key={i}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {sortedProjects.map((p) => {
                    const remaining = Math.max(0, (p.total_tasks ?? 0) - (p.completed_tasks ?? 0));
                    const pct = p.total_tasks
                      ? Math.round(((p.completed_tasks ?? 0) / p.total_tasks) * 100)
                      : 0;
                    return (
                      <tr key={p.id} className="cursor-pointer" onClick={() => openWorkbench(p.id)}>
                        <td>
                          <div className="max-w-[260px] overflow-hidden text-ellipsis whitespace-nowrap text-sm font-medium">
                            {p.name}
                          </div>
                          <div className="max-w-[260px] overflow-hidden text-ellipsis whitespace-nowrap text-xs text-muted-foreground">
                            <span className="mono">{p.display_id}</span>
                          </div>
                        </td>
                        <td className="whitespace-nowrap text-xs text-muted-foreground">
                          {projectDisplayType(p)}
                        </td>
                        <td className="whitespace-nowrap text-xs text-muted-foreground">
                          {p.completed_tasks ?? 0} / {p.total_tasks ?? 0}{" "}
                          <span className="mono">({pct}%)</span>
                        </td>
                        <td>
                          <span className="[&_span]:text-xs">
                            <Badge variant={remaining > 0 ? "accent" : "outline"}>
                              {remaining}
                            </Badge>
                          </span>
                        </td>
                        <td className="whitespace-nowrap text-right">
                          <Button
                            size="sm"
                            variant="primary"
                            onClick={(e: MouseEvent<HTMLButtonElement>) => {
                              e.stopPropagation();
                              openWorkbench(p.id);
                            }}
                          >
                            <Icon name="target" size={11} />
                            打开
                          </Button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>
    </PageContainer>
  );
}

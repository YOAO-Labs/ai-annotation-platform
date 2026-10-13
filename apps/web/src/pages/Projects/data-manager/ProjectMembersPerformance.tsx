import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
import { useLocation, useNavigate } from "react-router-dom";
import { buildReviewWorkbenchUrl, buildWorkbenchUrl } from "@/utils/workbenchNavigation";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/Badge";
import { UserAvatar } from "@/components/ui/UserAvatar";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import {
  useProjectMemberPerformance,
  useProjectMemberPerformanceEvents,
  useProjectMembersPerformance,
} from "@/hooks/useProjectPerformance";
import {
  projectPerformanceApi,
  type ProjectMemberPerformance,
  type ProjectPerformanceEvidenceItem,
  type ProjectPerformanceMetric,
  type ProjectPerformanceQuery,
  type ProjectPerformanceTrendPoint,
} from "@/api/projectPerformance";
import { useUrlFilterState } from "@/hooks/useUrlFilterState";
import { useTheme } from "@/hooks/useTheme";
import { useToastStore } from "@/components/ui/Toast";
import { isCurrentAuthOwner, useAuthStore } from "@/stores/authStore";
import { Input } from "@/components/shadcn/ui/input";
import { Skeleton } from "@/components/shadcn/ui/skeleton";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/shadcn/ui/sheet";
import {
  PROJECT_MEMBERS_PERFORMANCE_URL_DEFAULTS,
  projectMembersPerformanceUrlCodec,
  type ProjectMembersAccountStatus,
  type ProjectMembersDatePreset,
  type ProjectMembersPerformanceUrlState,
  type ProjectMembersSort,
  type ProjectMembersSortDirection,
  type ProjectMembersWorkType,
  PROJECT_MEMBERS_MAX_CUSTOM_DAYS,
  PROJECT_MEMBERS_PERFORMANCE_URL_KEYS,
  validateProjectMembersDateRange,
} from "./projectMembersPerformanceUrlState";
import { REJECT_REASON_TYPE_LABELS } from "@/pages/Review/rejectReasonTypes";

const PAGE_SIZE = 50;
const MAX_CUSTOM_DAYS = PROJECT_MEMBERS_MAX_CUSTOM_DAYS;

const SORT_LABELS: Record<ProjectMembersSort, string> = {
  name: "成员名称",
  submitted_tasks: "提交任务",
  annotated_images: "已标注图片",
  retained_objects: "保留标注",
  approved_task_outcomes: "审核通过",
  first_review_pass_rate: "首审通过率",
  recorded_time_minutes: "记录时长",
  current_backlog: "当前待办",
  review_decisions: "审核决策",
};

const PRESET_LABELS: Record<ProjectMembersDatePreset, string> = {
  today: "今天",
  "7d": "最近 7 天",
  "30d": "最近 30 天",
  custom: "自定义",
};

const ACCOUNT_STATUS_LABELS: Record<ProjectMembersAccountStatus, string> = {
  all: "全部账号",
  active: "仅活跃",
  inactive: "仅停用",
};

function currentTimeZone() {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
}

function dateInTimeZone(date: Date, timezone: string) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(date);
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}

function dateOffset(dateText: string, days: number) {
  const date = new Date(`${dateText}T12:00:00Z`);
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

function resolvedDateRange(state: ProjectMembersPerformanceUrlState, now = new Date()) {
  const timezone = state.timezone || currentTimeZone();
  const today = dateInTimeZone(now, timezone);
  if (state.preset === "custom" && state.from && state.to) {
    return { from: state.from, to: state.to, timezone };
  }
  const days = state.preset === "today" ? 1 : state.preset === "30d" ? 30 : 7;
  return { from: dateOffset(today, -(days - 1)), to: now.toISOString(), timezone };
}

function queryFromState(
  state: ProjectMembersPerformanceUrlState,
  now = new Date(),
): ProjectPerformanceQuery {
  const range = resolvedDateRange(state, now);
  return {
    from: range.from,
    to: range.to,
    timezone: range.timezone,
    work_type: state.workType,
    account_status: state.accountStatus,
    include_historical: state.includeHistorical,
    q: state.q || undefined,
    sort: `${state.direction === "desc" ? "-" : "+"}${state.sort}`,
    cursor: state.cursor,
    limit: PAGE_SIZE,
  };
}

function previousQueryFromScope(
  query: ProjectPerformanceQuery,
  scope: { from: string; to: string },
) {
  const from = new Date(scope.from);
  const to = new Date(scope.to);
  const duration = to.getTime() - from.getTime();
  if (!Number.isFinite(duration) || duration <= 0) return null;
  return {
    ...query,
    from: new Date(from.getTime() - duration).toISOString(),
    to: from.toISOString(),
    cursor: null,
    limit: PAGE_SIZE,
  };
}

function metricValue(metric: ProjectPerformanceMetric | undefined) {
  return metric?.value ?? null;
}

function numberValue(value: number | null | undefined) {
  return value == null ? "—" : value.toLocaleString("zh-CN");
}

function metricText(metric: ProjectPerformanceMetric | undefined, unit?: string) {
  const value = metricValue(metric);
  if (value === null) return "不可用";
  const labels: Record<string, string> = {
    tasks: "个任务",
    objects: "条标注",
    images: "张图片",
    decisions: "次",
    minutes: "分钟",
    percent: "%",
  };
  const suffix = unit ?? (metric?.unit ? (labels[metric.unit] ?? metric.unit) : "");
  return suffix ? `${numberValue(value)} ${suffix}` : numberValue(value);
}

function rateText(metric: ProjectPerformanceMetric | undefined) {
  if (metric?.denominator === 0) return "暂无数据";
  const value = metricValue(metric);
  if (value === null) return "不可用";
  const denominator = metric?.denominator ?? null;
  const numerator = metric?.numerator ?? null;
  const rate = value;
  return numerator != null && denominator != null
    ? `${rate.toFixed(1)}% · ${numberValue(numerator)} / ${numberValue(denominator)}`
    : `${rate.toFixed(1)}%`;
}

function metricAvailability(metric: ProjectPerformanceMetric | undefined) {
  if (!metric || metric.value === null || metric.coverage === "unknown") return "unknown";
  if (metric.coverage === "partial") return "partial";
  return "available";
}

function initial(name: string) {
  return name.trim().slice(0, 1).toUpperCase() || "?";
}

function roleLabel(role: string | null) {
  if (!role) return "未记录角色";
  if (role === "owner") return "项目负责人";
  if (role === "project_admin") return "项目管理员";
  if (role === "reviewer") return "审核员";
  if (role === "annotator") return "标注员";
  if (role === "viewer") return "只读成员";
  return role;
}

export function formatPerformanceDate(value: string, timezone: string) {
  // Trend keys already represent a local calendar date, not a UTC instant.
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return `${value.slice(5, 7)}/${value.slice(8, 10)}`;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: timezone,
    month: "2-digit",
    day: "2-digit",
  }).format(date);
}

function dateTime(value: string, timezone: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: timezone,
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function boundaryLabel(value: string, timezone: string) {
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
  const date = new Date(value);
  return `${dateInTimeZone(date, timezone)} ${new Intl.DateTimeFormat("zh-CN", {
    timeZone: timezone,
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(date)}`;
}

function labelReason(value: string | undefined) {
  if (!value) return "未分类";
  return REJECT_REASON_TYPE_LABELS[value as keyof typeof REJECT_REASON_TYPE_LABELS] ?? value;
}

function labelSource(value: string | undefined) {
  if (!value) return "未分类";
  return (
    (
      {
        manual: "人工",
        prediction_based: "AI 辅助",
        imported: "导入",
        interpolated: "插值",
      } as Record<string, string>
    )[value] ?? value
  );
}

function isAbortError(error: unknown) {
  return Boolean(
    error && typeof error === "object" && "name" in error && error.name === "AbortError",
  );
}

export function ProjectMembersPerformance({ projectId }: { projectId: string }) {
  const {
    state,
    issues: urlIssues,
    patch,
    reset,
  } = useUrlFilterState({
    codec: projectMembersPerformanceUrlCodec,
    defaults: PROJECT_MEMBERS_PERFORMANCE_URL_DEFAULTS,
    ownedKeys: PROJECT_MEMBERS_PERFORMANCE_URL_KEYS,
  });
  const [queryDraft, setQueryDraft] = useState(state.q);
  const syncingQueryDraft = useRef(false);
  const lastUrlQuery = useRef(state.q);
  const [cursorStack, setCursorStack] = useState<string[]>([]);
  const debouncedQuery = useDebouncedValue(queryDraft, 250);
  const viewAsOf = useRef(new Date()).current;
  const range = useMemo(() => resolvedDateRange(state, viewAsOf), [state, viewAsOf]);
  const query = useMemo(() => queryFromState(state, viewAsOf), [state, viewAsOf]);
  const maximumDate = dateInTimeZone(viewAsOf, range.timezone);
  const rangeIssue =
    state.preset === "custom"
      ? validateProjectMembersDateRange(state.from, state.to, maximumDate)
      : null;
  const issues = useMemo(
    () =>
      rangeIssue &&
      !urlIssues.some((issue) =>
        ["members_range", "members_from", "members_to"].includes(issue.key),
      )
        ? [...urlIssues, { key: "members_range", message: rangeIssue }]
        : urlIssues,
    [rangeIssue, urlIssues],
  );
  const pushToast = useToastStore((store) => store.push);
  const authOwnerId = useAuthStore((store) => store.user?.id);
  const invalidState = issues.length > 0;
  const membersQ = useProjectMembersPerformance(projectId, query, !invalidState);
  const selectedMemberId = state.selected;
  const detailQ = useProjectMemberPerformance(
    projectId,
    selectedMemberId,
    {
      ...query,
      cursor: null,
      limit: PAGE_SIZE,
    },
    !invalidState,
  );
  const previousQuery = useMemo(
    () => (detailQ.data ? previousQueryFromScope(query, detailQ.data.scope) : null),
    [detailQ.data, query],
  );
  const previousDetailQ = useProjectMemberPerformance(
    projectId,
    selectedMemberId,
    previousQuery ?? query,
    Boolean(selectedMemberId && previousQuery && !invalidState),
  );
  const [eventsCursor, setEventsCursor] = useState<string | null>(null);
  const [eventItems, setEventItems] = useState<ProjectPerformanceEvidenceItem[]>([]);
  const eventsQ = useProjectMemberPerformanceEvents(
    projectId,
    selectedMemberId,
    { ...query, cursor: eventsCursor, limit: 20 },
    Boolean(selectedMemberId && !invalidState),
  );
  const [exporting, setExporting] = useState(false);
  const exportControllerRef = useRef<AbortController | null>(null);
  const lastEventPageRef = useRef("");

  useEffect(() => () => exportControllerRef.current?.abort(), []);

  useEffect(() => {
    if (lastUrlQuery.current === state.q) return;
    lastUrlQuery.current = state.q;
    if (queryDraft !== state.q) {
      syncingQueryDraft.current = true;
      setQueryDraft(state.q);
    }
  }, [queryDraft, state.q]);

  useEffect(() => {
    if (syncingQueryDraft.current) {
      if (debouncedQuery === state.q) syncingQueryDraft.current = false;
      return;
    }
    const nextQuery = debouncedQuery.trim();
    if (nextQuery !== queryDraft.trim() || nextQuery === state.q) return;
    setCursorStack([]);
    patch({ q: nextQuery, cursor: null, selected: null });
  }, [debouncedQuery, patch, queryDraft, state.q]);

  useEffect(() => {
    if (state.timezone || issues.some((issue) => issue.key === "members_timezone")) return;
    patch({ timezone: currentTimeZone() }, { replace: true });
  }, [issues, patch, state.timezone]);

  useEffect(() => {
    setCursorStack([]);
  }, [
    state.preset,
    state.from,
    state.to,
    state.timezone,
    state.workType,
    state.accountStatus,
    state.includeHistorical,
    state.q,
    state.sort,
    state.direction,
  ]);

  useEffect(() => {
    setEventsCursor(null);
    setEventItems([]);
    lastEventPageRef.current = "";
  }, [
    selectedMemberId,
    query.from,
    query.to,
    query.timezone,
    query.work_type,
    query.account_status,
    query.include_historical,
    query.q,
    query.sort,
  ]);

  useEffect(() => {
    const response = eventsQ.data;
    if (!response) return;
    const page = response.items;
    const signature = `${eventsCursor ?? "first"}:${response.next_cursor ?? "end"}:${page.map((item) => item.id).join(",")}`;
    if (lastEventPageRef.current === signature) return;
    lastEventPageRef.current = signature;
    setEventItems((previous) => (eventsCursor ? [...previous, ...page] : page));
  }, [eventsCursor, eventsQ.data]);

  const updateState = useCallback(
    (update: Partial<ProjectMembersPerformanceUrlState>) => {
      setCursorStack([]);
      patch({ ...update, cursor: null, selected: null }, { replace: false });
    },
    [patch],
  );

  const selectMember = useCallback(
    (memberId: string | null) => patch({ selected: memberId }, { replace: false }),
    [patch],
  );

  const exportCsv = useCallback(async () => {
    exportControllerRef.current?.abort();
    const controller = new AbortController();
    exportControllerRef.current = controller;
    setExporting(true);
    try {
      const result = await projectPerformanceApi.exportMembers(
        projectId,
        {
          ...query,
          cursor: null,
          limit: undefined,
        },
        controller.signal,
      );
      const url = URL.createObjectURL(result.blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = result.filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      if (isAbortError(error) || !authOwnerId || !isCurrentAuthOwner(authOwnerId)) return;
      pushToast({
        kind: "error",
        msg: "成员绩效导出失败",
        sub: error instanceof Error ? error.message : "请稍后重试",
      });
    } finally {
      if (exportControllerRef.current === controller) exportControllerRef.current = null;
      setExporting(false);
    }
  }, [authOwnerId, projectId, pushToast, query]);

  const goNext = () => {
    const next = membersQ.data?.next_cursor;
    if (!next) return;
    setCursorStack((previous) => [...previous, state.cursor ?? ""]);
    patch({ cursor: next });
  };

  const goPrevious = () => {
    const previous = cursorStack[cursorStack.length - 1];
    if (previous === undefined) return;
    setCursorStack((stack) => stack.slice(0, -1));
    patch({ cursor: previous || null });
  };

  const selectedMember = membersQ.data?.items.find((member) => member.user_id === selectedMemberId);

  return (
    <section aria-label="成员绩效" className="flex min-w-0 flex-col gap-3 p-3 md:p-4">
      <header className="flex flex-wrap items-start gap-3">
        <div className="min-w-0">
          <h2 className="text-base font-semibold text-foreground">成员绩效</h2>
          <p className="mt-1 text-xs text-muted-foreground">
            对比当前项目的成员贡献与瓶颈；每个指标显示自己的单位和数据范围。
          </p>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <Button
            type="button"
            size="sm"
            variant="default"
            onClick={exportCsv}
            disabled={exporting || invalidState}
          >
            <Icon name="download" size={14} />
            {exporting ? "导出中…" : "导出 CSV"}
          </Button>
        </div>
      </header>

      <MembersToolbar
        state={state}
        range={range}
        maximumDate={maximumDate}
        queryDraft={queryDraft}
        issues={issues}
        onQueryChange={(value) => {
          syncingQueryDraft.current = false;
          setQueryDraft(value);
        }}
        onQueryCommit={(value) => {
          syncingQueryDraft.current = false;
          lastUrlQuery.current = value.trim();
          setCursorStack([]);
          patch({ q: value.trim(), cursor: null, selected: null }, { replace: true });
        }}
        onUpdate={updateState}
        onReset={() => reset({ replace: false })}
      />

      {invalidState ? (
        <InvalidState issues={issues} />
      ) : membersQ.isError ? (
        <ErrorState onRetry={() => void membersQ.refetch()} />
      ) : (
        <>
          <MembersSummary
            workType={state.workType}
            totals={membersQ.data?.project_totals}
            loading={membersQ.isLoading}
            scope={membersQ.data?.scope}
            coverage={membersQ.data?.coverage}
          />
          <MembersTable
            workType={state.workType}
            members={membersQ.data?.items ?? []}
            loading={membersQ.isLoading}
            onSelect={selectMember}
          />
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
            <span>
              {membersQ.data ? `${membersQ.data.items.length} 位成员` : "加载成员中…"} ·
              含无活动成员
            </span>
            <div className="flex items-center gap-1.5">
              <Button
                type="button"
                size="xs"
                variant="default"
                onClick={goPrevious}
                disabled={!cursorStack.length || membersQ.isFetching}
                aria-label="上一页成员"
              >
                <Icon name="chevLeft" size={12} />
                上一页
              </Button>
              <Button
                type="button"
                size="xs"
                variant="default"
                onClick={goNext}
                disabled={!membersQ.data?.next_cursor || membersQ.isFetching}
                aria-label="下一页成员"
              >
                下一页
                <Icon name="chevRight" size={12} />
              </Button>
            </div>
          </div>
        </>
      )}

      <Sheet open={Boolean(selectedMemberId)} onOpenChange={(open) => !open && selectMember(null)}>
        <SheetContent side="right" className="w-full overflow-y-auto sm:max-w-xl">
          {selectedMemberId && (
            <MemberDetail
              member={selectedMember}
              detail={detailQ.data}
              events={eventItems.length ? eventItems : (detailQ.data?.evidence ?? [])}
              workType={state.workType}
              loading={detailQ.isLoading}
              error={detailQ.isError}
              onRetry={() => void detailQ.refetch()}
              previousDetail={previousDetailQ.data}
              previousLoading={previousDetailQ.isLoading}
              previousError={previousDetailQ.isError}
              onRetryPrevious={() => void previousDetailQ.refetch()}
              eventsLoading={eventsQ.isLoading && !eventItems.length}
              eventsError={eventsQ.isError}
              onRetryEvents={() => void eventsQ.refetch()}
              hasMoreEvents={Boolean(
                eventsQ.data?.next_cursor || (eventsCursor && eventsQ.isFetching),
              )}
              loadingMoreEvents={eventsQ.isFetching && eventItems.length > 0}
              onLoadMoreEvents={() => setEventsCursor(eventsQ.data?.next_cursor ?? null)}
              timezone={membersQ.data?.scope.timezone ?? range.timezone}
              projectId={projectId}
            />
          )}
        </SheetContent>
      </Sheet>
    </section>
  );
}

function MembersToolbar({
  state,
  range,
  maximumDate,
  queryDraft,
  issues,
  onQueryChange,
  onQueryCommit,
  onUpdate,
  onReset,
}: {
  state: ProjectMembersPerformanceUrlState;
  range: { from: string; to: string; timezone: string };
  maximumDate: string;
  queryDraft: string;
  issues: Array<{ key: string; message: string }>;
  onQueryChange: (value: string) => void;
  onQueryCommit: (value: string) => void;
  onUpdate: (update: Partial<ProjectMembersPerformanceUrlState>) => void;
  onReset: () => void;
}) {
  const [customOpen, setCustomOpen] = useState(state.preset === "custom");
  const [customFrom, setCustomFrom] = useState(state.from);
  const [customTo, setCustomTo] = useState(state.to);
  const customRangeIssue = validateProjectMembersDateRange(customFrom, customTo, maximumDate);

  useEffect(() => {
    setCustomFrom(state.from);
    setCustomTo(state.to);
    setCustomOpen(state.preset === "custom");
  }, [state.from, state.preset, state.to]);

  const applyCustom = () => {
    if (customRangeIssue) return;
    onUpdate({ preset: "custom", from: customFrom, to: customTo });
  };

  return (
    <div className="rounded-md border border-border bg-card p-2.5">
      <div className="flex flex-wrap items-end gap-2">
        <label className="flex min-w-32 flex-1 flex-col gap-1 text-xs font-medium text-muted-foreground sm:max-w-56">
          搜索成员
          <div className="relative">
            <Icon
              name="search"
              size={14}
              className="pointer-events-none absolute top-2 left-2 text-muted-foreground"
            />
            <Input
              type="search"
              value={queryDraft}
              onChange={(event) => onQueryChange(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") onQueryCommit(event.currentTarget.value);
              }}
              placeholder="姓名或邮箱"
              aria-label="搜索成员姓名或邮箱"
              className="h-8 pl-7 text-sm"
            />
          </div>
        </label>
        <label className="flex min-w-28 flex-col gap-1 text-xs font-medium text-muted-foreground">
          工作类型
          <select
            value={state.workType}
            onChange={(event) =>
              onUpdate({ workType: event.target.value as ProjectMembersWorkType })
            }
            className="h-8 rounded-md border border-input bg-background px-2 text-sm text-foreground"
            aria-label="工作类型"
          >
            <option value="annotation">标注</option>
            <option value="review">审核</option>
          </select>
        </label>
        <label className="flex min-w-28 flex-col gap-1 text-xs font-medium text-muted-foreground">
          账号状态
          <select
            value={state.accountStatus}
            onChange={(event) =>
              onUpdate({ accountStatus: event.target.value as ProjectMembersAccountStatus })
            }
            className="h-8 rounded-md border border-input bg-background px-2 text-sm text-foreground"
            aria-label="账号状态"
          >
            {Object.entries(ACCOUNT_STATUS_LABELS).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label className="flex min-w-28 flex-col gap-1 text-xs font-medium text-muted-foreground">
          时间范围
          <select
            value={state.preset}
            onChange={(event) => {
              const preset = event.target.value as ProjectMembersDatePreset;
              setCustomOpen(preset === "custom");
              onUpdate(preset === "custom" ? { preset } : { preset, from: "", to: "" });
            }}
            className="h-8 rounded-md border border-input bg-background px-2 text-sm text-foreground"
            aria-label="绩效时间范围"
          >
            {Object.entries(PRESET_LABELS).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label className="flex min-w-36 flex-1 flex-col gap-1 text-xs font-medium text-muted-foreground sm:max-w-52">
          排序
          <select
            value={`${state.sort}:${state.direction}`}
            onChange={(event) => {
              const [sort, direction] = event.target.value.split(":") as [
                ProjectMembersSort,
                ProjectMembersSortDirection,
              ];
              onUpdate({ sort, direction });
            }}
            className="h-8 rounded-md border border-input bg-background px-2 text-sm text-foreground"
            aria-label="成员绩效排序"
          >
            {Object.entries(SORT_LABELS).flatMap(([sort, label]) => [
              <option key={`${sort}:desc`} value={`${sort}:desc`}>
                {label} ↓
              </option>,
              <option key={`${sort}:asc`} value={`${sort}:asc`}>
                {label} ↑
              </option>,
            ])}
          </select>
        </label>
        <label className="flex h-8 items-center gap-2 whitespace-nowrap rounded-md border border-border px-2 text-xs text-foreground">
          <input
            type="checkbox"
            checked={state.includeHistorical}
            onChange={(event) => onUpdate({ includeHistorical: event.target.checked })}
          />
          包含历史贡献者
        </label>
        <Button type="button" size="sm" variant="ghost" onClick={onReset}>
          重置
        </Button>
      </div>
      {customOpen && (
        <div className="mt-2 flex flex-wrap items-end gap-2 border-t border-border pt-2">
          <label className="flex flex-col gap-1 text-xs text-muted-foreground">
            开始日期
            <Input
              type="date"
              value={customFrom}
              max={maximumDate}
              onChange={(event) => setCustomFrom(event.target.value)}
              className="h-8 w-36 text-sm"
              aria-label="自定义开始日期"
            />
          </label>
          <label className="flex flex-col gap-1 text-xs text-muted-foreground">
            结束日期（不含）
            <Input
              type="date"
              value={customTo}
              max={maximumDate}
              onChange={(event) => setCustomTo(event.target.value)}
              className="h-8 w-36 text-sm"
              aria-label="自定义结束日期"
            />
          </label>
          <Button
            type="button"
            size="sm"
            variant="default"
            onClick={applyCustom}
            disabled={Boolean(customRangeIssue)}
          >
            应用范围
          </Button>
          <span className="text-xs text-muted-foreground">
            最多 {MAX_CUSTOM_DAYS} 天 · 使用 {range.timezone}
          </span>
          {customRangeIssue && (
            <span className="text-xs text-status-danger">{customRangeIssue}</span>
          )}
        </div>
      )}
      <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <Badge variant="outline">
          {boundaryLabel(range.from, range.timezone)} — {boundaryLabel(range.to, range.timezone)}
        </Badge>
        <Badge variant="outline">{range.timezone}</Badge>
        <span>范围按本地日期解析，结束边界不含。</span>
        {state.includeHistorical && <Badge variant="warning">含历史贡献者</Badge>}
      </div>
      {issues
        .filter((issue) => issue.key !== "members_range")
        .map((issue) => (
          <p key={`${issue.key}:${issue.message}`} className="mt-1 text-xs text-status-danger">
            {issue.message}
          </p>
        ))}
    </div>
  );
}

function MembersSummary({
  workType,
  totals,
  loading,
  scope,
  coverage,
}: {
  workType: ProjectMembersWorkType;
  totals:
    | {
        submitted_tasks: ProjectPerformanceMetric;
        approved_task_outcomes: ProjectPerformanceMetric;
        first_review_pass_rate: ProjectPerformanceMetric;
        recorded_time_minutes: ProjectPerformanceMetric;
        current_backlog: ProjectPerformanceMetric;
        review_decisions: ProjectPerformanceMetric;
        approvals: ProjectPerformanceMetric;
        rejections: ProjectPerformanceMetric;
        review_backlog: ProjectPerformanceMetric;
        annotated_images: ProjectPerformanceMetric;
        retained_objects: ProjectPerformanceMetric;
      }
    | undefined;
  loading: boolean;
  scope?: { from: string; to: string; timezone: string; as_of: string };
  coverage?: { state: string; source: string; detail?: string | null };
}) {
  const items =
    workType === "annotation"
      ? ([
          ["已标注图片", totals?.annotated_images, "张图片"],
          ["保留标注", totals?.retained_objects, "条标注"],
          ["周期内提交", totals?.submitted_tasks, "个任务"],
          ["审核通过结果", totals?.approved_task_outcomes, "个任务"],
          ["首审通过率", totals?.first_review_pass_rate, ""],
          ["当前待办", totals?.current_backlog, "个任务"],
        ] as const)
      : ([
          ["审核决策", totals?.review_decisions, "次"],
          ["通过", totals?.approvals, "次"],
          ["退回", totals?.rejections, "次"],
          ["当前待审", totals?.review_backlog, "个任务"],
        ] as const);
  return (
    <section
      aria-label="项目成员绩效汇总"
      className="overflow-hidden rounded-md border border-border bg-card"
    >
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-3 py-2">
        <span className="text-xs text-muted-foreground">
          {scope
            ? `${formatPerformanceDate(scope.from, scope.timezone)} — ${formatPerformanceDate(scope.to, scope.timezone)}`
            : "应用范围"}
        </span>
        {coverage && <CoverageBadge state={coverage.state} />}
      </div>
      <div
        className={cn(
          "grid grid-cols-2 gap-px bg-border",
          workType === "annotation" ? "md:grid-cols-3 xl:grid-cols-6" : "md:grid-cols-4",
        )}
      >
        {items.map(([label, metric, unit]) => (
          <div key={label} className="min-w-0 bg-card px-3 py-2.5">
            <div className="text-xs text-muted-foreground">{label}</div>
            {loading ? (
              <Skeleton className="mt-1 h-6 w-16" />
            ) : (
              <strong className="mt-1 block truncate font-mono text-lg font-semibold text-foreground">
                {label === "首审通过率" ? rateText(metric) : metricText(metric, unit)}
              </strong>
            )}
            <div className="mt-1 text-2xs text-muted-foreground">
              {label === "当前待办" || label === "当前待审"
                ? "当前快照"
                : label === "已标注图片" || label === "保留标注"
                  ? "区间内创建且仍保留"
                  : metric?.coverage === "partial"
                    ? "仅统计有完整记录的部分"
                    : "按成员与任务范围统计"}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

function MembersTable({
  workType,
  members,
  loading,
  onSelect,
}: {
  workType: ProjectMembersWorkType;
  members: ProjectMemberPerformance[];
  loading: boolean;
  onSelect: (memberId: string) => void;
}) {
  return (
    <div className="overflow-hidden rounded-md border border-border bg-card">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-3 py-2.5">
        <h3 className="text-sm font-semibold text-foreground">成员贡献明细</h3>
        <span className="text-xs text-muted-foreground">
          点击姓名查看趋势、原因和依据 · 没有活动的成员仍保留
        </span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[980px] border-collapse text-sm">
          <caption className="sr-only">项目成员绩效表</caption>
          <thead className="bg-muted text-xs text-muted-foreground">
            <tr>
              <th scope="col" className="px-3 py-2 text-left font-medium">
                成员
              </th>
              {workType === "annotation" ? (
                <>
                  <th
                    scope="col"
                    className="px-3 py-2 text-right font-medium"
                    title="区间内创建且仍保留标注的不同图片任务数（按任务实际媒体类型统计，不含视频/点云）。多人标注同一图片时项目汇总按图片去重。"
                  >
                    已标注图片
                  </th>
                  <th
                    scope="col"
                    className="px-3 py-2 text-right font-medium"
                    title="区间内创建且仍保留的标注记录数，无需提交或审核。视频轨迹计一条，Scene 跨帧实例分别计数；删除或取消会减少计数。"
                  >
                    保留标注
                  </th>
                  <th scope="col" className="px-3 py-2 text-right font-medium">
                    提交任务
                  </th>
                  <th scope="col" className="px-3 py-2 text-right font-medium">
                    审核通过
                  </th>
                  <th scope="col" className="px-3 py-2 text-right font-medium">
                    首审通过率
                  </th>
                  <th scope="col" className="px-3 py-2 text-right font-medium">
                    记录时长
                  </th>
                  <th scope="col" className="px-3 py-2 text-right font-medium">
                    当前待办
                  </th>
                </>
              ) : (
                <>
                  <th scope="col" className="px-3 py-2 text-right font-medium">
                    审核决策
                  </th>
                  <th scope="col" className="px-3 py-2 text-right font-medium">
                    通过
                  </th>
                  <th scope="col" className="px-3 py-2 text-right font-medium">
                    退回
                  </th>
                  <th scope="col" className="px-3 py-2 text-right font-medium">
                    记录时长
                  </th>
                  <th scope="col" className="px-3 py-2 text-right font-medium">
                    当前待审
                  </th>
                </>
              )}
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {loading ? (
              Array.from({ length: 4 }, (_, index) => (
                <MemberSkeletonRow key={index} workType={workType} />
              ))
            ) : members.length ? (
              members.map((member) => (
                <MemberRow
                  key={member.user_id}
                  member={member}
                  workType={workType}
                  onSelect={onSelect}
                />
              ))
            ) : (
              <tr>
                <td
                  colSpan={workType === "annotation" ? 8 : 6}
                  className="px-3 py-10 text-center text-sm text-muted-foreground"
                >
                  没有符合条件的成员
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function MemberSkeletonRow({ workType }: { workType: ProjectMembersWorkType }) {
  return (
    <tr>
      <td className="px-3 py-3">
        <Skeleton className="h-5 w-36" />
      </td>
      {Array.from({ length: workType === "annotation" ? 7 : 5 }, (_, index) => (
        <td key={index} className="px-3 py-3 text-right">
          <Skeleton className="ml-auto h-5 w-16" />
        </td>
      ))}
    </tr>
  );
}

function MemberRow({
  member,
  workType,
  onSelect,
}: {
  member: ProjectMemberPerformance;
  workType: ProjectMembersWorkType;
  onSelect: (memberId: string) => void;
}) {
  const metrics = member.metrics;
  return (
    <tr className="transition-colors hover:bg-muted/50">
      <td className="px-3 py-2.5">
        <button
          type="button"
          onClick={() => onSelect(member.user_id)}
          className="flex min-w-48 items-center gap-2 rounded-md text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <UserAvatar
            size="sm"
            user={{ name: member.name, email: member.email, avatar_ref: member.avatar_ref }}
          />
          <span className="min-w-0">
            <span className="flex items-center gap-1.5 truncate font-medium text-foreground">
              {member.name}
              {member.is_owner && <Badge variant="accent">负责人</Badge>}
              {!member.is_current_member && <Badge variant="outline">历史贡献者</Badge>}
            </span>
            <span className="mt-0.5 block truncate text-xs text-muted-foreground">
              {roleLabel(member.project_role)} · {member.email}
            </span>
          </span>
          {member.account_status === "inactive" && <Badge variant="warning">停用</Badge>}
        </button>
      </td>
      {workType === "annotation" ? (
        <>
          <MetricCell metric={metrics.annotated_images} />
          <MetricCell metric={metrics.retained_objects} />
          <MetricCell metric={metrics.submitted_tasks} />
          <MetricCell metric={metrics.approved_task_outcomes} />
          <MetricCell metric={metrics.first_review_pass_rate} rate />
          <MetricCell metric={metrics.recorded_time_minutes} unit="分钟" />
          <MetricCell metric={metrics.current_backlog} />
        </>
      ) : (
        <>
          <MetricCell metric={metrics.review_decisions} />
          <MetricCell metric={metrics.approvals} />
          <MetricCell metric={metrics.rejections} />
          <MetricCell
            metric={metrics.recorded_review_minutes ?? metrics.recorded_time_minutes}
            unit="分钟"
          />
          <MetricCell metric={metrics.review_backlog} />
        </>
      )}
    </tr>
  );
}

function MetricCell({
  metric,
  unit,
  rate,
}: {
  metric: ProjectPerformanceMetric | undefined;
  unit?: string;
  rate?: boolean;
}) {
  const availability = metricAvailability(metric);
  return (
    <td className="px-3 py-2.5 text-right align-middle font-mono tabular-nums">
      <span
        className={
          availability === "unknown"
            ? "text-muted-foreground"
            : availability === "partial"
              ? "text-status-caution"
              : "text-foreground"
        }
      >
        {rate ? rateText(metric) : metricText(metric, unit)}
      </span>
      {availability === "partial" && (
        <span className="block text-2xs text-status-caution">覆盖不完整</span>
      )}
    </td>
  );
}

function MemberDetail({
  member,
  detail,
  events,
  workType,
  loading,
  error,
  onRetry,
  previousDetail,
  previousLoading,
  previousError,
  onRetryPrevious,
  eventsLoading,
  eventsError,
  onRetryEvents,
  hasMoreEvents,
  loadingMoreEvents,
  onLoadMoreEvents,
  timezone,
  projectId,
}: {
  member: ProjectMemberPerformance | undefined;
  detail:
    | {
        scope: { from: string; to: string; timezone: string; as_of: string };
        coverage: { state: string; source: string; detail?: string | null };
        member: ProjectMemberPerformance;
        trend: ProjectPerformanceTrendPoint[];
        reject_reasons: Array<{ reason_type?: string; count: number; pct?: number | null }>;
        class_distribution: Array<{ class_name?: string; count: number; pct?: number | null }>;
        evidence: Array<{
          id: string;
          at: string;
          action: string;
          task_id?: string | null;
          task_display_id?: string | null;
          detail?: string | null;
          contributor_name?: string | null;
        }>;
        source_distribution: Array<{ source: string; count: number; pct?: number | null }>;
        geometry_distribution: Array<{
          annotation_type: string;
          count: number;
          pct?: number | null;
        }>;
      }
    | undefined;
  events: Array<{
    id: string;
    at: string;
    action: string;
    task_id?: string | null;
    task_display_id?: string | null;
    detail?: string | null;
    contributor_name?: string | null;
  }>;
  workType: ProjectMembersWorkType;
  loading: boolean;
  error: boolean;
  onRetry: () => void;
  previousDetail:
    | {
        scope: { from: string; to: string; timezone: string; as_of: string };
        member: ProjectMemberPerformance;
      }
    | undefined;
  previousLoading: boolean;
  previousError: boolean;
  onRetryPrevious: () => void;
  eventsLoading: boolean;
  eventsError: boolean;
  onRetryEvents: () => void;
  hasMoreEvents: boolean;
  loadingMoreEvents: boolean;
  onLoadMoreEvents: () => void;
  timezone: string;
  projectId: string;
}) {
  const activeMember = detail?.member ?? member;
  const trend = detail?.trend ?? [];
  const { resolved } = useTheme();
  const chartColor = resolved === "dark" ? "#60a5fa" : "#2563eb";
  const gridColor = resolved === "dark" ? "#2f2f36" : "#e4e4e7";
  const trendData = trend.map((point) => ({
    date: formatPerformanceDate(point.date, timezone),
    提交任务: point.submitted_tasks,
    审核通过: point.approved_task_outcomes,
    审核决策: point.review_decisions,
  }));
  const reasonData = (detail?.reject_reasons ?? []).map((item) => ({
    name: labelReason(item.reason_type),
    value: item.count,
  }));
  const classData = (detail?.class_distribution ?? []).map((item) => ({
    name: item.class_name || "未分类",
    value: item.count,
  }));

  return (
    <>
      <SheetHeader className="border-b border-border px-5 py-4">
        <SheetTitle>{activeMember?.name ?? "成员详情"}</SheetTitle>
        <SheetDescription>
          {activeMember
            ? `${roleLabel(activeMember.project_role)} · ${activeMember.email}`
            : "成员绩效详情"}
          {activeMember?.account_status === "inactive" ? " · 账号已停用" : ""}
        </SheetDescription>
      </SheetHeader>
      <div className="flex flex-col gap-4 px-5 pb-6">
        {error ? (
          <DetailError onRetry={onRetry} />
        ) : loading && !activeMember ? (
          <DetailSkeleton />
        ) : activeMember ? (
          <>
            <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <Badge variant={activeMember.is_current_member ? "outline" : "warning"}>
                {activeMember.is_current_member ? "当前项目成员" : "历史贡献者"}
              </Badge>
              {activeMember.is_owner && <Badge variant="accent">负责人</Badge>}
              {detail?.scope && (
                <span>
                  {boundaryLabel(detail.scope.from, detail.scope.timezone)} —{" "}
                  {boundaryLabel(detail.scope.to, detail.scope.timezone)} · {detail.scope.timezone}
                </span>
              )}
            </div>
            <DetailMetrics member={activeMember} workType={workType} />
            <ComparisonCard
              workType={workType}
              current={activeMember}
              previous={previousDetail?.member}
              currentScope={detail?.scope}
              previousScope={previousDetail?.scope}
              loading={previousLoading}
              error={previousError}
              onRetry={onRetryPrevious}
            />
            {workType === "annotation" && (
              <RetainedContent
                member={activeMember}
                sourceDistribution={detail?.source_distribution ?? []}
                geometryDistribution={detail?.geometry_distribution ?? []}
              />
            )}
            <section
              aria-labelledby="member-trend-title"
              className="rounded-md border border-border p-3"
            >
              <div className="mb-2 flex items-start justify-between gap-2">
                <div>
                  <h3 id="member-trend-title" className="text-sm font-semibold text-foreground">
                    产出趋势
                  </h3>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    沿用当前成员、时间范围和工作类型。
                  </p>
                </div>
                {detail?.coverage && <CoverageBadge state={detail.coverage.state} />}
              </div>
              {trendData.length ? (
                <div className="h-52 w-full">
                  <ResponsiveContainer width="100%" height="100%">
                    <LineChart data={trendData} margin={{ top: 8, right: 8, bottom: 4, left: -18 }}>
                      <CartesianGrid strokeDasharray="3 3" stroke={gridColor} />
                      <XAxis
                        dataKey="date"
                        tick={{ fontSize: 10, fill: "var(--sc-muted-foreground)" }}
                        axisLine={{ stroke: gridColor }}
                        tickLine={{ stroke: gridColor }}
                      />
                      <YAxis
                        allowDecimals={false}
                        tick={{ fontSize: 10, fill: "var(--sc-muted-foreground)" }}
                        axisLine={{ stroke: gridColor }}
                        tickLine={{ stroke: gridColor }}
                      />
                      <Tooltip
                        contentStyle={{
                          background: "var(--sc-card)",
                          border: "1px solid var(--sc-border)",
                          color: "var(--sc-foreground)",
                        }}
                      />
                      <Line
                        type="monotone"
                        dataKey="提交任务"
                        stroke={chartColor}
                        strokeWidth={2}
                        dot={false}
                      />
                      <Line
                        type="monotone"
                        dataKey="审核通过"
                        stroke="var(--sc-status-positive)"
                        strokeWidth={2}
                        dot={false}
                      />
                      <Line
                        type="monotone"
                        dataKey="审核决策"
                        stroke="var(--sc-status-caution)"
                        strokeWidth={2}
                        dot={false}
                      />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              ) : (
                <EmptyDetail text="当前范围暂无趋势数据" />
              )}
            </section>
            <div className="grid gap-3 md:grid-cols-2">
              <BreakdownCard
                title="退回原因"
                data={reasonData}
                color="var(--sc-status-danger)"
                empty="当前范围无退回记录"
              />
              <BreakdownCard
                title="类别分布"
                data={classData}
                color="var(--sc-brand)"
                empty="当前范围暂无标注"
              />
            </div>
            <EvidenceCard
              events={events}
              loading={eventsLoading}
              error={eventsError}
              onRetry={onRetryEvents}
              hasMore={hasMoreEvents}
              loadingMore={loadingMoreEvents}
              onLoadMore={onLoadMoreEvents}
              timezone={timezone}
              projectId={projectId}
              workType={workType}
            />
          </>
        ) : (
          <EmptyDetail text="成员不存在或已离开当前范围" />
        )}
      </div>
    </>
  );
}

function DetailMetrics({
  member,
  workType,
}: {
  member: ProjectMemberPerformance;
  workType: ProjectMembersWorkType;
}) {
  const items =
    workType === "annotation"
      ? [
          ["提交任务", metricText(member.metrics.submitted_tasks, "个")],
          ["审核通过", metricText(member.metrics.approved_task_outcomes, "个")],
          ["首审通过率", rateText(member.metrics.first_review_pass_rate)],
          ["当前待办", metricText(member.metrics.current_backlog, "个")],
        ]
      : [
          ["审核决策", metricText(member.metrics.review_decisions, "次")],
          ["通过", metricText(member.metrics.approvals, "次")],
          ["退回", metricText(member.metrics.rejections, "次")],
          ["当前待审", metricText(member.metrics.review_backlog, "个")],
        ];
  return (
    <div className="grid grid-cols-2 gap-px overflow-hidden rounded-md border border-border bg-border">
      {items.map(([label, value]) => (
        <div key={label} className="bg-card px-2.5 py-2">
          <div className="text-2xs text-muted-foreground">{label}</div>
          <strong className="mt-1 block font-mono text-sm text-foreground">{value}</strong>
        </div>
      ))}
    </div>
  );
}

function ComparisonCard({
  workType,
  current,
  previous,
  currentScope,
  previousScope,
  loading,
  error,
  onRetry,
}: {
  workType: ProjectMembersWorkType;
  current: ProjectMemberPerformance;
  previous: ProjectMemberPerformance | undefined;
  currentScope: { from: string; to: string; timezone: string; as_of: string } | undefined;
  previousScope: { from: string; to: string; timezone: string; as_of: string } | undefined;
  loading: boolean;
  error: boolean;
  onRetry: () => void;
}) {
  const currentMetric =
    workType === "annotation" ? current.metrics.submitted_tasks : current.metrics.review_decisions;
  const previousMetric = previous
    ? workType === "annotation"
      ? previous.metrics.submitted_tasks
      : previous.metrics.review_decisions
    : undefined;
  const currentValue = metricValue(currentMetric);
  const previousValue = metricValue(previousMetric);
  let value = "不可用";
  let tone = "text-muted-foreground";
  if (previousValue === 0) value = "无法比较";
  else if (currentValue !== null && previousValue !== null) {
    const delta = currentValue - previousValue;
    const pct = (delta / previousValue) * 100;
    value = `${delta > 0 ? "+" : ""}${numberValue(delta)} · ${pct > 0 ? "+" : ""}${pct.toFixed(1)}%`;
    tone = delta >= 0 ? "text-status-positive" : "text-status-danger";
  }
  return (
    <section
      aria-label="上期对比"
      className="rounded-md border border-border bg-muted/30 px-3 py-2.5"
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold text-foreground">上期对比</h3>
          <p className="mt-0.5 text-xs text-muted-foreground">
            按相同项目、工作类型和等长时间范围比较
            {workType === "annotation" ? "提交任务" : "审核决策"}。
          </p>
        </div>
        {loading ? (
          <Skeleton className="h-5 w-20" />
        ) : error ? (
          <Button type="button" size="xs" variant="ghost" onClick={onRetry}>
            重试
          </Button>
        ) : (
          <strong className={`font-mono text-sm ${tone}`}>{value}</strong>
        )}
      </div>
      <div className="mt-1 text-2xs text-muted-foreground">
        {currentScope && previousScope
          ? `${boundaryLabel(previousScope.from, previousScope.timezone)} — ${boundaryLabel(previousScope.to, previousScope.timezone)} → ${boundaryLabel(currentScope.from, currentScope.timezone)} — ${boundaryLabel(currentScope.to, currentScope.timezone)}`
          : "等待当前范围确认后加载上期"}
      </div>
    </section>
  );
}

function RetainedContent({
  member,
  sourceDistribution,
  geometryDistribution,
}: {
  member: ProjectMemberPerformance;
  sourceDistribution: Array<{ source: string; count: number; pct?: number | null }>;
  geometryDistribution: Array<{ annotation_type: string; count: number; pct?: number | null }>;
}) {
  return (
    <section aria-label="保留内容" className="rounded-md border border-border p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold text-foreground">保留内容</h3>
          <p className="mt-0.5 text-xs text-muted-foreground">
            区间内创建且仍有效的标注记录，无需提交或审核。视频轨迹计一条，Scene
            跨帧实例分别计数；删除或取消会减少计数。已标注图片按任务实际媒体类型统计，不含视频与点云。
          </p>
        </div>
        <div className="flex gap-3 text-right">
          <div>
            <div className="text-2xs text-muted-foreground">已标注图片</div>
            <strong className="font-mono text-sm text-foreground">
              {metricText(member.metrics.annotated_images, "张")}
            </strong>
          </div>
          <div>
            <div className="text-2xs text-muted-foreground">贡献任务</div>
            <strong className="font-mono text-sm text-foreground">
              {metricText(member.metrics.contributed_tasks, "个")}
            </strong>
          </div>
          <div>
            <div className="text-2xs text-muted-foreground">保留标注记录</div>
            <strong className="font-mono text-sm text-foreground">
              {metricText(member.metrics.retained_objects, "条")}
            </strong>
          </div>
        </div>
      </div>
      <div className="mt-3 grid gap-3 text-xs sm:grid-cols-2">
        <DistributionList
          title="来源"
          rows={sourceDistribution.map((row) => ({
            label: labelSource(row.source),
            count: row.count,
            pct: row.pct,
          }))}
        />
        <DistributionList
          title="几何类型"
          rows={geometryDistribution.map((row) => ({
            label: row.annotation_type || "未分类",
            count: row.count,
            pct: row.pct,
          }))}
        />
      </div>
    </section>
  );
}

function DistributionList({
  title,
  rows,
}: {
  title: string;
  rows: Array<{ label: string; count: number; pct?: number | null }>;
}) {
  return (
    <div>
      <div className="mb-1.5 font-medium text-muted-foreground">{title}</div>
      {rows.length ? (
        <ul className="space-y-1">
          {rows.map((row) => (
            <li key={row.label} className="flex items-center justify-between gap-2">
              <span className="truncate text-foreground">{row.label}</span>
              <span className="font-mono text-muted-foreground">
                {row.count}
                {row.pct == null ? "" : ` · ${row.pct}%`}
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <span className="text-muted-foreground">暂无数据</span>
      )}
    </div>
  );
}

function BreakdownCard({
  title,
  data,
  color,
  empty,
}: {
  title: string;
  data: Array<{ name: string; value: number }>;
  color: string;
  empty: string;
}) {
  return (
    <section aria-label={title} className="rounded-md border border-border p-3">
      <h3 className="text-sm font-semibold text-foreground">{title}</h3>
      {data.length ? (
        <div className="mt-3 h-36 w-full">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart
              layout="vertical"
              data={data}
              margin={{ top: 0, right: 8, bottom: 0, left: 0 }}
            >
              <XAxis type="number" allowDecimals={false} hide />
              <YAxis
                type="category"
                dataKey="name"
                width={72}
                tick={{ fontSize: 10, fill: "var(--sc-muted-foreground)" }}
                axisLine={false}
                tickLine={false}
              />
              <Tooltip
                contentStyle={{
                  background: "var(--sc-card)",
                  border: "1px solid var(--sc-border)",
                  color: "var(--sc-foreground)",
                }}
              />
              <Bar dataKey="value" name="次数" fill={color} radius={[0, 3, 3, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      ) : (
        <EmptyDetail text={empty} />
      )}
    </section>
  );
}

function EvidenceCard({
  events,
  loading,
  error,
  onRetry,
  hasMore,
  loadingMore,
  onLoadMore,
  timezone,
  projectId,
  workType,
}: {
  events: Array<{
    id: string;
    at: string;
    action: string;
    task_id?: string | null;
    task_display_id?: string | null;
    detail?: string | null;
    contributor_name?: string | null;
  }>;
  loading: boolean;
  error: boolean;
  onRetry: () => void;
  hasMore: boolean;
  loadingMore: boolean;
  onLoadMore: () => void;
  timezone: string;
  projectId: string;
  workType: ProjectMembersWorkType;
}) {
  const navigate = useNavigate();
  const location = useLocation();
  return (
    <section aria-label="成员活动依据" className="rounded-md border border-border">
      <div className="border-b border-border px-3 py-2.5">
        <h3 className="text-sm font-semibold text-foreground">活动依据</h3>
        <p className="mt-0.5 text-xs text-muted-foreground">事件属于当前项目和当前时间范围。</p>
      </div>
      {loading ? (
        <div className="space-y-2 p-3">
          <Skeleton className="h-8 w-full" />
          <Skeleton className="h-8 w-full" />
          <Skeleton className="h-8 w-full" />
        </div>
      ) : error ? (
        <div className="px-3 py-6 text-center">
          <p className="text-xs text-status-danger">活动依据加载失败</p>
          <Button type="button" size="xs" variant="default" className="mt-2" onClick={onRetry}>
            重试
          </Button>
        </div>
      ) : events.length ? (
        <>
          <ul className="divide-y divide-border">
            {events.map((event) => (
              <li key={event.id} className="flex items-start gap-2 px-3 py-2.5 text-xs">
                <Icon name="activity" size={14} className="mt-0.5 shrink-0 text-muted-foreground" />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap gap-x-2 gap-y-0.5">
                    <span className="font-medium text-foreground">{event.action}</span>
                    <time className="text-muted-foreground">{dateTime(event.at, timezone)}</time>
                  </div>
                  <div className="mt-0.5 truncate text-muted-foreground">
                    {event.detail || event.contributor_name || "已记录活动"}
                  </div>
                </div>
                {event.task_id && (
                  <Button
                    type="button"
                    size="xs"
                    variant="ghost"
                    onClick={() =>
                      navigate(
                        workType === "review"
                          ? buildReviewWorkbenchUrl(projectId, {
                              taskId: event.task_id,
                              returnTo: `${location.pathname}${location.search}`,
                            })
                          : buildWorkbenchUrl(projectId, {
                              taskId: event.task_id,
                              returnTo: `${location.pathname}${location.search}`,
                            }),
                      )
                    }
                  >
                    查看任务
                  </Button>
                )}
              </li>
            ))}
          </ul>
          {hasMore && (
            <div className="border-t border-border px-3 py-2 text-center">
              <Button
                type="button"
                size="xs"
                variant="ghost"
                onClick={onLoadMore}
                disabled={loadingMore}
              >
                {loadingMore ? "加载中…" : "加载更多依据"}
              </Button>
            </div>
          )}
        </>
      ) : (
        <EmptyDetail text="当前范围暂无可见活动依据" />
      )}
    </section>
  );
}

function CoverageBadge({ state }: { state: string }) {
  const label = state === "complete" ? "覆盖完整" : state === "partial" ? "覆盖不完整" : "覆盖未知";
  return (
    <Badge variant={state === "complete" ? "success" : state === "partial" ? "warning" : "outline"}>
      {label}
    </Badge>
  );
}

function DetailSkeleton() {
  return (
    <div className="space-y-3">
      <Skeleton className="h-6 w-32" />
      <Skeleton className="h-24 w-full" />
      <Skeleton className="h-52 w-full" />
    </div>
  );
}

function EmptyDetail({ text }: { text: string }) {
  return <div className="px-3 py-8 text-center text-xs text-muted-foreground">{text}</div>;
}

function ErrorState({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="rounded-md border border-status-danger/30 bg-status-danger-soft px-3 py-8 text-center">
      <p className="text-sm font-medium text-status-danger">成员绩效暂时无法加载</p>
      <p className="mt-1 text-xs text-muted-foreground">请检查权限或稍后重试。</p>
      <Button type="button" size="sm" variant="default" className="mt-3" onClick={onRetry}>
        重试
      </Button>
    </div>
  );
}

function InvalidState({ issues }: { issues: Array<{ key: string; message: string }> }) {
  return (
    <div
      role="alert"
      className="rounded-md border border-status-caution/30 bg-status-caution-soft px-3 py-8 text-center"
    >
      <p className="text-sm font-medium text-status-caution">无法应用成员绩效筛选</p>
      <p className="mt-1 text-xs text-muted-foreground">请修正时间范围或 URL 参数后重试。</p>
      <ul className="mx-auto mt-3 max-w-md space-y-1 text-left text-xs text-status-caution">
        {issues.map((issue) => (
          <li key={`${issue.key}:${issue.message}`}>{issue.message}</li>
        ))}
      </ul>
    </div>
  );
}

function DetailError({ onRetry }: { onRetry: () => void }) {
  return (
    <div
      role="alert"
      className="rounded-md border border-status-danger/30 bg-status-danger-soft px-3 py-8 text-center"
    >
      <p className="text-sm font-medium text-status-danger">成员详情加载失败</p>
      <Button type="button" size="sm" variant="default" className="mt-3" onClick={onRetry}>
        重试
      </Button>
    </div>
  );
}

export function ProjectPerformanceSummary({
  projectId,
  onOpenMembers,
}: {
  projectId: string;
  onOpenMembers?: () => void;
}) {
  const asOf = useRef(new Date()).current;
  const timezone = currentTimeZone();
  const today = dateInTimeZone(asOf, timezone);
  const query: ProjectPerformanceQuery = {
    from: dateOffset(today, -6),
    to: asOf.toISOString(),
    timezone,
    work_type: "annotation",
    account_status: "all",
    include_historical: false,
    limit: 1,
  };
  const summaryQ = useProjectMembersPerformance(projectId, query);
  const totals = summaryQ.data?.project_totals;
  if (summaryQ.isError) {
    return <ErrorState onRetry={() => void summaryQ.refetch()} />;
  }
  return (
    <section aria-label="成员产出摘要" className="rounded-md border border-border bg-card p-3">
      <div className="flex items-center justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold text-foreground">成员产出</h3>
          <p className="mt-0.5 text-xs text-muted-foreground">最近 7 天 · {timezone}</p>
        </div>
        {onOpenMembers ? (
          <Button variant="ghost" size="sm" onClick={onOpenMembers}>
            成员看板 <Icon name="chevRight" size={12} />
          </Button>
        ) : (
          <Badge variant="outline">{summaryQ.isLoading ? "加载中" : "项目范围"}</Badge>
        )}
      </div>
      <div className="mt-3 grid grid-cols-3 gap-2 text-xs">
        <div>
          <div className="text-muted-foreground">提交任务</div>
          <strong className="mt-1 block font-mono text-base">
            {metricText(totals?.submitted_tasks, "个")}
          </strong>
        </div>
        <div>
          <div className="text-muted-foreground">审核通过</div>
          <strong className="mt-1 block font-mono text-base">
            {metricText(totals?.approved_task_outcomes, "个")}
          </strong>
        </div>
        <div>
          <div className="text-muted-foreground">当前待办</div>
          <strong className="mt-1 block font-mono text-base">
            {metricText(totals?.current_backlog, "个")}
          </strong>
        </div>
      </div>
    </section>
  );
}

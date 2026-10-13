import type { UrlStateCodec, UrlStateIssue } from "@/hooks/useUrlFilterState";

export type ProjectMembersDatePreset = "today" | "7d" | "30d" | "custom";
export type ProjectMembersWorkType = "annotation" | "review";
export type ProjectMembersAccountStatus = "all" | "active" | "inactive";
export type ProjectMembersSort =
  | "name"
  | "submitted_tasks"
  | "annotated_images"
  | "retained_objects"
  | "approved_task_outcomes"
  | "first_review_pass_rate"
  | "recorded_time_minutes"
  | "current_backlog"
  | "review_decisions";
export type ProjectMembersSortDirection = "asc" | "desc";

export interface ProjectMembersPerformanceUrlState {
  preset: ProjectMembersDatePreset;
  from: string;
  to: string;
  timezone: string;
  workType: ProjectMembersWorkType;
  accountStatus: ProjectMembersAccountStatus;
  includeHistorical: boolean;
  q: string;
  sort: ProjectMembersSort;
  direction: ProjectMembersSortDirection;
  cursor: string | null;
  selected: string | null;
}

export const PROJECT_MEMBERS_PERFORMANCE_URL_DEFAULTS: ProjectMembersPerformanceUrlState = {
  preset: "7d",
  from: "",
  to: "",
  timezone: "",
  workType: "annotation",
  accountStatus: "all",
  includeHistorical: false,
  q: "",
  sort: "name",
  direction: "asc",
  cursor: null,
  selected: null,
};

export const PROJECT_MEMBERS_PERFORMANCE_URL_KEYS = [
  "members_preset",
  "members_from",
  "members_to",
  "members_timezone",
  "members_work_type",
  "members_account_status",
  "members_historical",
  "members_q",
  "members_sort",
  "members_direction",
  "members_cursor",
  "members_selected",
] as const;

export const PROJECT_MEMBERS_MAX_CUSTOM_DAYS = 90;

const PRESETS = new Set<ProjectMembersDatePreset>(["today", "7d", "30d", "custom"]);
const WORK_TYPES = new Set<ProjectMembersWorkType>(["annotation", "review"]);
const ACCOUNT_STATUSES = new Set<ProjectMembersAccountStatus>(["all", "active", "inactive"]);
const SORTS = new Set<ProjectMembersSort>([
  "name",
  "submitted_tasks",
  "annotated_images",
  "retained_objects",
  "approved_task_outcomes",
  "first_review_pass_rate",
  "recorded_time_minutes",
  "current_backlog",
  "review_decisions",
]);
const DIRECTIONS = new Set<ProjectMembersSortDirection>(["asc", "desc"]);

function value(search: string | URLSearchParams, key: string) {
  return new URLSearchParams(search).get(key)?.trim() ?? "";
}

function parseEnum<T extends string>(
  search: string | URLSearchParams,
  key: string,
  values: Set<T>,
  fallback: T,
  issues: UrlStateIssue[],
): T {
  const raw = value(search, key);
  if (!raw) return fallback;
  if (values.has(raw as T)) return raw as T;
  issues.push({ key, message: `未知的成员绩效参数：${raw}` });
  return fallback;
}

function validDate(valueToCheck: string) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(valueToCheck)) return false;
  const parsed = new Date(`${valueToCheck}T00:00:00Z`);
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === valueToCheck;
}

export function validateProjectMembersDateRange(from: string, to: string, maximumDate?: string) {
  if (!from || !to) return "自定义范围需要开始和结束日期";
  if (!validDate(from)) return "开始日期格式无效";
  if (!validDate(to)) return "结束日期格式无效";
  if (from >= to) return "开始日期必须早于结束日期";
  if (maximumDate && to > maximumDate) return "结束日期不能晚于今天；查看今天请使用“今天”范围";
  const days = (Date.parse(`${to}T00:00:00Z`) - Date.parse(`${from}T00:00:00Z`)) / 86_400_000;
  return days > PROJECT_MEMBERS_MAX_CUSTOM_DAYS
    ? `自定义范围最多 ${PROJECT_MEMBERS_MAX_CUSTOM_DAYS} 天`
    : null;
}

export function parseProjectMembersPerformanceUrl(search: string | URLSearchParams): {
  state: ProjectMembersPerformanceUrlState;
  issues: UrlStateIssue[];
} {
  const params = new URLSearchParams(search);
  const issues: UrlStateIssue[] = [];
  const preset = parseEnum(params, "members_preset", PRESETS, "7d", issues);
  const workType = parseEnum(params, "members_work_type", WORK_TYPES, "annotation", issues);
  const accountStatus = parseEnum(
    params,
    "members_account_status",
    ACCOUNT_STATUSES,
    "all",
    issues,
  );
  const sort = parseEnum(params, "members_sort", SORTS, "name", issues);
  const direction = parseEnum(params, "members_direction", DIRECTIONS, "asc", issues);
  const from = value(params, "members_from");
  const to = value(params, "members_to");
  if (preset === "custom" || from || to) {
    const message = validateProjectMembersDateRange(from, to);
    if (message)
      issues.push({
        key:
          from && !validDate(from)
            ? "members_from"
            : to && !validDate(to)
              ? "members_to"
              : "members_range",
        message,
      });
  }
  const historical = value(params, "members_historical");
  if (historical && historical !== "1" && historical !== "0") {
    issues.push({ key: "members_historical", message: "历史成员参数无效" });
  }
  const timezone = value(params, "members_timezone");
  if (timezone && !isTimeZone(timezone)) {
    issues.push({ key: "members_timezone", message: "时区无效" });
  }
  return {
    state: {
      preset,
      from: validDate(from) ? from : "",
      to: validDate(to) ? to : "",
      timezone: isTimeZone(timezone) ? timezone : "",
      workType,
      accountStatus,
      includeHistorical: historical === "1",
      q: value(params, "members_q"),
      sort,
      direction,
      cursor: value(params, "members_cursor") || null,
      selected: value(params, "members_selected") || null,
    },
    issues,
  };
}

function isTimeZone(timezone: string) {
  if (!timezone) return true;
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: timezone }).format();
    return true;
  } catch {
    return false;
  }
}

export const projectMembersPerformanceUrlCodec: UrlStateCodec<ProjectMembersPerformanceUrlState> = {
  parse: parseProjectMembersPerformanceUrl,
  encode: (current, state) => {
    const params = new URLSearchParams(current);
    const setOrDelete = (key: string, next: string) => {
      if (next) params.set(key, next);
      else params.delete(key);
    };
    setOrDelete("members_preset", state.preset === "7d" ? "" : state.preset);
    setOrDelete("members_from", state.from);
    setOrDelete("members_to", state.to);
    setOrDelete("members_timezone", state.timezone);
    setOrDelete("members_work_type", state.workType === "annotation" ? "" : state.workType);
    setOrDelete("members_account_status", state.accountStatus === "all" ? "" : state.accountStatus);
    setOrDelete("members_historical", state.includeHistorical ? "1" : "");
    setOrDelete("members_q", state.q);
    setOrDelete("members_sort", state.sort === "name" ? "" : state.sort);
    setOrDelete("members_direction", state.direction === "asc" ? "" : state.direction);
    setOrDelete("members_cursor", state.cursor ?? "");
    setOrDelete("members_selected", state.selected ?? "");
    return params;
  },
  clear: (current) => {
    const params = new URLSearchParams(current);
    for (const key of PROJECT_MEMBERS_PERFORMANCE_URL_KEYS) params.delete(key);
    return params;
  },
};

export function updateProjectMembersPerformanceUrl(
  current: string | URLSearchParams,
  update: Partial<ProjectMembersPerformanceUrlState>,
) {
  const previous = parseProjectMembersPerformanceUrl(current).state;
  return projectMembersPerformanceUrlCodec.encode(new URLSearchParams(current), {
    ...previous,
    ...update,
  });
}

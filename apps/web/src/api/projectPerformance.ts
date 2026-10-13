import { isCurrentAuthOwner, useAuthStore } from "@/stores/authStore";
import { apiClient } from "./client";

export type ProjectPerformanceWorkType = "annotation" | "review";
export type ProjectPerformanceAccountStatus = "all" | "active" | "inactive";
export type ProjectPerformanceCoverageState = "complete" | "partial" | "unknown";
export type ProjectPerformanceMetricUnit =
  | "tasks"
  | "objects"
  | "images"
  | "decisions"
  | "minutes"
  | "percent";

export interface ProjectPerformanceQuery {
  from?: string;
  to?: string;
  timezone?: string;
  work_type: ProjectPerformanceWorkType;
  account_status: ProjectPerformanceAccountStatus;
  include_historical: boolean;
  q?: string;
  sort?: string;
  cursor?: string | null;
  limit?: number;
}

export interface ProjectPerformanceScope {
  from: string;
  to: string;
  timezone: string;
  as_of: string;
}

export interface ProjectPerformanceCoverage {
  state: ProjectPerformanceCoverageState;
  source: string;
  detail?: string | null;
}

export interface ProjectPerformanceMetric {
  value: number | null;
  unit: ProjectPerformanceMetricUnit;
  numerator?: number | null;
  denominator?: number | null;
  coverage?: ProjectPerformanceCoverageState;
}

export interface ProjectMemberPerformanceMetrics {
  submitted_tasks: ProjectPerformanceMetric;
  resubmissions: ProjectPerformanceMetric;
  approved_task_outcomes: ProjectPerformanceMetric;
  first_review_pass_rate: ProjectPerformanceMetric;
  recorded_time_minutes: ProjectPerformanceMetric;
  current_backlog: ProjectPerformanceMetric;
  review_decisions: ProjectPerformanceMetric;
  approvals: ProjectPerformanceMetric;
  rejections: ProjectPerformanceMetric;
  reviewed_tasks: ProjectPerformanceMetric;
  recorded_review_minutes: ProjectPerformanceMetric;
  review_backlog: ProjectPerformanceMetric;
  contributed_tasks: ProjectPerformanceMetric;
  annotated_images: ProjectPerformanceMetric;
  retained_objects: ProjectPerformanceMetric;
}

export interface ProjectMemberPerformance {
  user_id: string;
  name: string;
  email: string;
  project_role: string | null;
  account_status: "active" | "inactive";
  is_owner: boolean;
  is_current_member: boolean;
  member_since: string | null;
  /** 头像引用(`preset:<slug>` / `upload:<token>`);空 = 回退首字母。 */
  avatar_ref?: string | null;
  metrics: ProjectMemberPerformanceMetrics;
}

export interface ProjectPerformanceTotals {
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

export interface ProjectMembersPerformanceResponse {
  scope: ProjectPerformanceScope;
  coverage: ProjectPerformanceCoverage;
  project_totals: ProjectPerformanceTotals;
  items: ProjectMemberPerformance[];
  next_cursor: string | null;
}

export interface ProjectPerformanceTrendPoint {
  date: string;
  submitted_tasks: number | null;
  approved_task_outcomes: number | null;
  review_decisions: number | null;
}

export interface ProjectPerformanceBreakdown {
  reason_type?: string;
  class_name?: string;
  count: number;
  pct?: number | null;
}

export interface ProjectPerformanceSourceBreakdown {
  source: string;
  count: number;
  pct?: number | null;
}

export interface ProjectPerformanceGeometryBreakdown {
  annotation_type: string;
  count: number;
  pct?: number | null;
}

export interface ProjectPerformanceEvidenceItem {
  id: string;
  at: string;
  action: string;
  task_id?: string | null;
  task_display_id?: string | null;
  detail?: string | null;
  contributor_name?: string | null;
}

export interface ProjectMemberPerformanceDetail {
  scope: ProjectPerformanceScope;
  coverage: ProjectPerformanceCoverage;
  member: ProjectMemberPerformance;
  trend: ProjectPerformanceTrendPoint[];
  reject_reasons: ProjectPerformanceBreakdown[];
  class_distribution: ProjectPerformanceBreakdown[];
  source_distribution: ProjectPerformanceSourceBreakdown[];
  geometry_distribution: ProjectPerformanceGeometryBreakdown[];
  evidence: ProjectPerformanceEvidenceItem[];
  evidence_next_cursor?: string | null;
}

export interface ProjectMemberPerformanceEventsResponse {
  scope: ProjectPerformanceScope;
  items: ProjectPerformanceEvidenceItem[];
  next_cursor: string | null;
}

function queryString(query: ProjectPerformanceQuery) {
  const params = new URLSearchParams();
  if (query.from) params.set("from", query.from);
  if (query.to) params.set("to", query.to);
  if (query.timezone) params.set("timezone", query.timezone);
  params.set("work_type", query.work_type);
  params.set("account_status", query.account_status);
  params.set("include_historical", String(query.include_historical));
  if (query.q) params.set("q", query.q);
  if (query.sort) params.set("sort", query.sort);
  if (query.cursor) params.set("cursor", query.cursor);
  if (query.limit) params.set("limit", String(query.limit));
  return params.toString();
}

function pathWithQuery(path: string, query: ProjectPerformanceQuery) {
  const queryText = queryString(query);
  return queryText ? `${path}?${queryText}` : path;
}

function getWithSignal<T>(path: string, signal?: AbortSignal) {
  return signal ? apiClient.get<T>(path, { signal }) : apiClient.get<T>(path);
}

export const projectPerformanceApi = {
  getMembers: (projectId: string, query: ProjectPerformanceQuery, signal?: AbortSignal) =>
    getWithSignal<ProjectMembersPerformanceResponse>(
      pathWithQuery(`/projects/${encodeURIComponent(projectId)}/performance/members`, query),
      signal,
    ),

  getMember: (
    projectId: string,
    userId: string,
    query: ProjectPerformanceQuery,
    signal?: AbortSignal,
  ) =>
    getWithSignal<ProjectMemberPerformanceDetail>(
      pathWithQuery(
        `/projects/${encodeURIComponent(projectId)}/performance/members/${encodeURIComponent(userId)}`,
        query,
      ),
      signal,
    ),

  getMemberEvents: (
    projectId: string,
    userId: string,
    query: ProjectPerformanceQuery,
    signal?: AbortSignal,
  ) =>
    getWithSignal<ProjectMemberPerformanceEventsResponse>(
      pathWithQuery(
        `/projects/${encodeURIComponent(projectId)}/performance/members/${encodeURIComponent(userId)}/events`,
        query,
      ),
      signal,
    ),

  exportMembers: async (
    projectId: string,
    query: ProjectPerformanceQuery,
    signal?: AbortSignal,
  ): Promise<{ blob: Blob; filename: string }> => {
    const auth = useAuthStore.getState();
    const ownerId = auth.user?.id;
    const token = auth.token;
    if (!ownerId || !token || !isCurrentAuthOwner(ownerId)) {
      throw new Error("当前登录状态已改变，请重新导出");
    }
    const path = pathWithQuery(
      `/projects/${encodeURIComponent(projectId)}/performance/export`,
      query,
    );
    const response = await fetch(`/api/v1${path}`, {
      signal,
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error((body as { detail?: string }).detail || `导出失败 (HTTP ${response.status})`);
    }
    const blob = await response.blob();
    if (
      !ownerId ||
      !token ||
      token !== useAuthStore.getState().token ||
      !isCurrentAuthOwner(ownerId)
    ) {
      throw new Error("当前登录状态已改变，请重新导出");
    }
    const disposition = response.headers.get("Content-Disposition") || "";
    const match = /filename\*?=(?:UTF-8''|")?([^";]+)/i.exec(disposition);
    return {
      blob,
      filename: match?.[1] ? decodeURIComponent(match[1]) : "project_members_performance.csv",
    };
  },
};

export { queryString as projectPerformanceQueryString };

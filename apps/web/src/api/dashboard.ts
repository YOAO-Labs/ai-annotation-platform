import { apiClient } from "./client";
import type { UserBrief } from "@/types";
import { isCurrentAuthOwner, useAuthStore } from "@/stores/authStore";

export interface RegistrationDayPoint {
  date: string;
  invite_count: number;
  open_count: number;
}

export interface AdminDashboardStats {
  total_users: number;
  active_users: number;
  total_projects: number;
  projects_in_progress: number;
  projects_completed: number;
  projects_pending_review: number;
  projects_archived: number;
  total_tasks: number;
  total_annotations: number;
  ml_backends_total: number;
  ml_backends_connected: number;
  role_distribution: Record<string, number>;
  registration_by_day?: RegistrationDayPoint[];
  /** v0.9.5 · pre_annotated 批次数（Sidebar 徽章 + AdminDashboard 卡片共用） */
  pre_annotated_batches?: number;
}

export interface ReviewTaskItem {
  task_id: string;
  task_display_id: string;
  file_name: string;
  project_id: string;
  project_name: string;
  total_annotations: number;
  total_predictions: number;
  updated_at: string | null;
  is_rework?: boolean;
}

export interface ReviewingBatchItem {
  batch_id: string;
  batch_display_id: string;
  batch_name: string;
  project_id: string;
  project_name: string;
  total_tasks: number;
  review_tasks: number;
  completed_tasks: number;
  /** 批次封面缩略图（presigned URL）+ blurhash 占位，用于卡片网格。 */
  thumbnail_url?: string | null;
  cover_blurhash?: string | null;
  /** v0.7.2 · 这批的标注员（单值；让 reviewer 知道是谁标的） */
  annotator: UserBrief | null;
}

export interface ReviewerDashboardStats {
  pending_review_count: number;
  today_reviewed: number;
  approval_rate: number;
  approval_rate_24h: number;
  total_reviewed: number;
  pending_tasks: ReviewTaskItem[];
  reviewing_batches?: ReviewingBatchItem[];
  // v0.8.4
  median_review_duration_ms?: number | null;
  reopen_after_approve_rate?: number | null;
  weekly_compare_pct?: number | null;
  daily_review_counts?: number[];
}

export interface RecentReviewItem {
  task_id: string;
  task_display_id: string;
  file_name: string;
  project_id: string;
  project_name: string;
  status: string;
  reviewed_at: string | null;
}

export interface AnnotatorDashboardStats {
  assigned_tasks: number;
  today_completed: number;
  weekly_completed: number;
  total_completed: number;
  personal_accuracy: number;
  daily_counts: number[];
  // v0.8.4
  median_duration_ms?: number | null;
  rejected_rate?: number | null;
  reopened_avg?: number | null;
  weekly_compare_pct?: number | null;
  weekly_target?: number;
  active_minutes_today?: number | null;
  streak_days?: number | null;
  // v0.8.5 · 当日 0-23 时分钟数
  hour_buckets?: number[];
  // M1 · 当前待重做退回任务数
  rejected_tasks_count?: number;
}

export interface OnboardingProjectSummary {
  project_id: string;
  assigned_task_count: number;
  opened_task_count: number;
  saved_annotation_count: number;
  reviewed_task_count: number;
  reviewed_task_id: string | null;
  reviewed_task_display_id: string | null;
  reviewed_task_status: "completed" | "rejected" | null;
  reviewed_task_reason: string | null;
}

// v0.8.4 · 管理员人员看板
export interface AdminPersonItem {
  user_id: string;
  name: string;
  email: string;
  role: string;
  status: string;
  project_count: number;
  /** 头像引用(`preset:<slug>` / `upload:<token>`);空 = 回退首字母。 */
  avatar_ref?: string | null;
  main_metric: number;
  main_metric_label: string;
  weekly_compare_pct?: number | null;
  throughput_score: number;
  quality_score: number;
  activity_score: number;
  sparkline_7d: number[];
  rejected_rate?: number | null;
  alerts: string[];
}

export interface AdminPeopleList {
  items: AdminPersonItem[];
  total: number;
  period: string;
}

export interface AdminPersonDetail {
  user_id: string;
  name: string;
  email: string;
  role: string;
  project_count: number;
  throughput: number;
  quality_score: number;
  active_minutes: number | null;
  composite_score: number;
  weekly_compare_pct: number | null;
  trend_throughput: number[];
  trend_quality: number[];
  project_distribution: Array<{ project_id: string; project_name: string; count: number }>;
  duration_histogram: Array<{ upper_ms: number; count: number }>;
  p50_duration_ms: number | null;
  p95_duration_ms: number | null;
  timeline: Array<{
    at: string;
    action: string;
    task_id?: string;
    task_display_id?: string;
    detail?: string;
  }>;
  // v0.12.4 · 质量归因(A1)
  reject_reason_breakdown: Array<{ reason_type: string; count: number; pct: number }>;
  class_distribution: Array<{ class_name: string; count: number; pct: number }>;
  first_pass_yield: number | null;
}

// v0.12.3 · 标注员自助绩效（取经合集 §4.1 个人页）
export interface MyPerformance {
  user_id: string;
  name: string;
  period: string;
  throughput: number;
  quality_score: number;
  weekly_compare_pct: number | null;
  trend_throughput: number[];
  trend_quality: number[];
  team_trend_throughput: number[];
  duration_histogram: Array<{ upper_ms: number; count: number }>;
  p50_duration_ms: number | null;
  p95_duration_ms: number | null;
  // v0.12.4 · 质量归因(A1)
  reject_reason_breakdown: Array<{ reason_type: string; count: number; pct: number }>;
  class_distribution: Array<{ class_name: string; count: number; pct: number }>;
  first_pass_yield: number | null;
}

export interface MyBatchItem {
  batch_id: string;
  batch_display_id: string;
  batch_name: string;
  project_id: string;
  project_name: string;
  status: string;
  total_tasks: number;
  completed_tasks: number;
  review_tasks: number;
  /** B-20：标注员已动工 (status='in_progress') 的任务数 */
  in_progress_tasks?: number;
  approved_tasks: number;
  rejected_tasks: number;
  progress_pct: number;
  /** 批次封面缩略图（presigned URL）+ blurhash 占位，用于卡片网格。 */
  thumbnail_url?: string | null;
  cover_blurhash?: string | null;
  review_feedback: string | null;
  reviewed_at: string | null;
  /** v0.7.2 · 这批的审核员（单值；让标注员知道谁会审） */
  reviewer: UserBrief | null;
}

// v0.8.6 F4 · 预测成本卡片
export interface BackendCostBreakdown {
  backend_id: string | null;
  backend_name: string | null;
  predictions: number;
  failures: number;
  total_cost: number;
  avg_inference_time_ms: number | null;
}

export interface PredictionCostStats {
  range: "7d" | "30d";
  total_predictions: number;
  failed_predictions: number;
  failure_rate: number;
  avg_inference_time_ms: number | null;
  // v0.8.7 F2 · 延迟分位数
  p50_inference_time_ms: number | null;
  p95_inference_time_ms: number | null;
  p99_inference_time_ms: number | null;
  total_cost: number;
  total_tokens: number;
  by_backend: BackendCostBreakdown[];
}

// v0.8.7 F5.3 · Reviewer 实时 mini 仪表（在 ReviewWorkbench 右侧栏渲染）
export interface ReviewerMiniStats {
  approved_today: number;
  rejected_today: number;
  avg_review_seconds: number | null;
}

export const dashboardApi = {
  getAdminStats: () => apiClient.get<AdminDashboardStats>("/dashboard/admin"),
  getReviewerStats: () => apiClient.get<ReviewerDashboardStats>("/dashboard/reviewer"),
  getAnnotatorStats: () => apiClient.get<AnnotatorDashboardStats>("/dashboard/annotator"),
  // v0.8.6 F4
  getPredictionCostStats: (range: "7d" | "30d" = "30d") =>
    apiClient.get<PredictionCostStats>(`/dashboard/admin/prediction-cost-stats?range=${range}`),
  getMyBatches: () => apiClient.get<MyBatchItem[]>("/dashboard/annotator/batches"),
  getOnboardingProjectSummary: (projectId: string) =>
    apiClient.get<OnboardingProjectSummary>(
      `/dashboard/annotator/projects/${projectId}/onboarding`,
    ),
  getMyRecentReviews: (limit = 20) =>
    apiClient.get<RecentReviewItem[]>(`/dashboard/me/recent-reviews?limit=${limit}`),
  // v0.8.7 F5.3
  getReviewerTodayMini: () => apiClient.get<ReviewerMiniStats>("/dashboard/reviewer/today-mini"),
  // v0.8.4 · 管理员人员看板
  getAdminPeople: (
    params: {
      role?: string;
      project?: string;
      period?: string;
      sort?: string;
      q?: string;
    } = {},
    signal?: AbortSignal,
  ) => {
    const sp = new URLSearchParams();
    if (params.role) sp.set("role", params.role);
    if (params.project) sp.set("project", params.project);
    if (params.period) sp.set("period", params.period);
    if (params.sort) sp.set("sort", params.sort);
    if (params.q) sp.set("q", params.q);
    const qs = sp.toString();
    const path = `/dashboard/admin/people${qs ? `?${qs}` : ""}`;
    return signal
      ? apiClient.get<AdminPeopleList>(path, { signal })
      : apiClient.get<AdminPeopleList>(path);
  },
  getAdminPersonDetail: (
    userId: string,
    period: string = "4w",
    project?: string,
    signal?: AbortSignal,
  ) => {
    const path = `/dashboard/admin/people/${userId}?period=${period}${project ? `&project=${project}` : ""}`;
    return signal
      ? apiClient.get<AdminPersonDetail>(path, { signal })
      : apiClient.get<AdminPersonDetail>(path);
  },
  // v0.12.5 · 成员绩效 CSV 导出（A2）。带 Bearer 拉 blob 触发下载，镜像 usersApi.exportUsers。
  exportPeople: async (
    params: { role?: string; project?: string; period?: string; sort?: string; q?: string } = {},
  ): Promise<void> => {
    const ownerId = useAuthStore.getState().user?.id;
    const sp = new URLSearchParams();
    if (params.role) sp.set("role", params.role);
    if (params.project) sp.set("project", params.project);
    if (params.period) sp.set("period", params.period);
    if (params.sort) sp.set("sort", params.sort);
    if (params.q) sp.set("q", params.q);
    const qs = sp.toString();
    const token = localStorage.getItem("token");
    const res = await fetch(`/api/v1/dashboard/admin/people/export${qs ? `?${qs}` : ""}`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error((body as { detail?: string }).detail || `导出失败 (HTTP ${res.status})`);
    }
    const blob = await res.blob();
    if (
      !ownerId ||
      !token ||
      token !== useAuthStore.getState().token ||
      token !== localStorage.getItem("token") ||
      !isCurrentAuthOwner(ownerId)
    ) {
      throw new Error("当前登录状态已改变，请重新导出");
    }
    const dispo = res.headers.get("Content-Disposition") || "";
    const match = /filename="?([^"]+)"?/.exec(dispo);
    const filename = match ? match[1] : "people_performance.csv";
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  },
  // v0.12.3 · 标注员自助绩效（取经合集 §4.1 个人页）
  getMyPerformance: (period: string = "4w") =>
    apiClient.get<MyPerformance>(`/dashboard/me/performance?period=${period}`),
};

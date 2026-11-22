import { apiClient } from "./client";

export type AnalyticsPanelName =
  | "throughput_daily"
  | "reject_rate_by_type"
  | "duration_dist"
  | "activity_heatmap";

export interface ThroughputRow {
  day: string;
  user_id: string;
  event_count: number;
}

export interface RejectRateRow {
  reason_type: "missing" | "extra" | "wrong_label" | "wrong_geometry" | string;
  count: number;
  pct: number;
}

export interface DurationDist {
  n: number;
  p50: number;
  p95: number;
  mean: number;
}

export interface HeatmapCell {
  weekday: number;
  hour: number;
  count: number;
}

export interface AnalyticsPanelResponse<T> {
  panel: AnalyticsPanelName;
  data: T;
}

export const adminAnalyticsApi = {
  throughputDaily: (days = 30) =>
    apiClient.get<AnalyticsPanelResponse<ThroughputRow[]>>(
      `/admin/analytics/throughput_daily?days=${days}`,
    ),
  rejectRateByType: (days = 30) =>
    apiClient.get<AnalyticsPanelResponse<RejectRateRow[]>>(
      `/admin/analytics/reject_rate_by_type?days=${days}`,
    ),
  durationDist: (days = 30) =>
    apiClient.get<AnalyticsPanelResponse<DurationDist>>(
      `/admin/analytics/duration_dist?days=${days}`,
    ),
  activityHeatmap: (days = 30) =>
    apiClient.get<AnalyticsPanelResponse<HeatmapCell[]>>(
      `/admin/analytics/activity_heatmap?days=${days}`,
    ),
};

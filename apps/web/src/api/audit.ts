import { apiClient } from "./client";
import type { AuditLogOut, AuditLogList as AuditLogListGen } from "./generated/types.gen";
import { isCurrentAuthOwner, useAuthStore } from "@/stores/authStore";

export type AuditLogResponse = AuditLogOut;
export type AuditLogList = AuditLogListGen;

export interface AuditQuery {
  page?: number;
  page_size?: number;
  action?: string;
  target_type?: string;
  target_id?: string;
  actor_id?: string;
  from?: string;
  to?: string;
  business_only?: boolean;
  detail_key?: string;
  detail_value?: string;
}

export interface AuditSummaryTotals {
  event_count: number;
  error_count: number;
  action_kind_count: number;
}

export interface AuditSummaryDailyPoint {
  day: string;
  event_count: number;
  error_count: number;
}

export interface AuditSummaryBucket {
  key: string;
  event_count: number;
}

export interface AuditMonthlySummary {
  month: string;
  timezone: "UTC";
  materialized_through: string | null;
  totals: AuditSummaryTotals;
  daily: AuditSummaryDailyPoint[];
  top_actions: AuditSummaryBucket[];
  target_types: AuditSummaryBucket[];
  actor_roles: AuditSummaryBucket[];
}

function toQuery(params?: AuditQuery): string {
  if (!params) return "";
  const entries = Object.entries(params).filter(
    ([key, value]) =>
      value !== undefined &&
      (value !== "" ||
        (key === "detail_value" &&
          typeof params.detail_key === "string" &&
          params.detail_key.trim() !== "")),
  );
  if (entries.length === 0) return "";
  const sp = new URLSearchParams(entries.map(([k, v]) => [k, String(v)]));
  return `?${sp.toString()}`;
}

export const auditApi = {
  list: (params?: AuditQuery, signal?: AbortSignal) => {
    const path = `/audit-logs${toQuery(params)}`;
    return signal
      ? apiClient.get<AuditLogList>(path, { signal })
      : apiClient.get<AuditLogList>(path);
  },

  monthlySummary: (month: string, businessOnly = true, signal?: AbortSignal) => {
    const path = `/audit-logs/monthly-summary${toQuery({
      month,
      business_only: businessOnly,
    } as AuditQuery & {
      month: string;
    })}`;
    return signal
      ? apiClient.get<AuditMonthlySummary>(path, { signal })
      : apiClient.get<AuditMonthlySummary>(path);
  },

  export: async (params?: AuditQuery, format: "csv" | "json" = "csv"): Promise<void> => {
    const ownerId = useAuthStore.getState().user?.id;
    const token = localStorage.getItem("token");
    const q = toQuery({ ...params, format } as AuditQuery & { format: string });
    const res = await fetch(`/api/v1/audit-logs/export${q}`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body?.detail ?? `导出失败 (HTTP ${res.status})`);
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
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `audit_logs.${format}`;
    a.click();
    URL.revokeObjectURL(url);
  },
};

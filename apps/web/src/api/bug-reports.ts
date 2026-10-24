import { apiClient } from "./client";

export interface BugReportPayload {
  title: string;
  description: string;
  severity?: "low" | "medium" | "high" | "critical";
  route?: string;
  browser_ua?: string;
  viewport?: string;
  project_id?: string;
  task_id?: string;
  recent_api_calls?: Array<{ method: string; url: string; status: number; ms: number }>;
  recent_console_errors?: Array<{ msg: string; stack?: string }>;
  screenshot_url?: string | null;
  attachments?: BugAttachment[];
}

export interface BugAttachment {
  storageKey: string;
  fileName: string;
  mimeType: string;
  size: number;
}

export interface BugReportResponse {
  id: string;
  display_id: string;
  reporter_id: string;
  route: string;
  user_role: string;
  project_id: string | null;
  task_id: string | null;
  title: string;
  description: string;
  severity: string;
  status: string;
  duplicate_of_id: string | null;
  browser_ua: string | null;
  viewport: string | null;
  recent_api_calls: unknown;
  recent_console_errors: unknown;
  screenshot_url: string | null;
  attachments: BugAttachment[];
  resolution: string | null;
  fixed_in_version: string | null;
  assigned_to_id: string | null;
  created_at: string;
  triaged_at: string | null;
  fixed_at: string | null;
  reopen_count: number;
  last_reopened_at: string | null;
}

export interface BugReportDetail extends BugReportResponse {
  comments: BugCommentResponse[];
}

export interface BugCommentResponse {
  id: string;
  bug_report_id: string;
  author_id: string;
  author_name: string;
  author_role: string;
  body: string;
  created_at: string;
}

export interface BugReportListResponse {
  items: BugReportResponse[];
  total: number;
}

export interface BugReportListParams {
  status?: string;
  severity?: string;
  route?: string;
  limit?: number;
  offset?: number;
}

export interface BugReportUpdatePayload {
  status?: string;
  severity?: string;
  title?: string;
  description?: string;
  duplicate_of_id?: string;
  assigned_to_id?: string;
  fixed_in_version?: string;
  resolution?: string;
}

export const bugReportsApi = {
  create: (payload: BugReportPayload) =>
    apiClient.silentPost<BugReportResponse>("/bug_reports", payload),

  list: (params?: BugReportListParams, signal?: AbortSignal) => {
    const q = new URLSearchParams();
    if (params?.status) q.set("status", params.status);
    if (params?.severity) q.set("severity", params.severity);
    if (params?.route) q.set("route", params.route);
    if (params?.limit) q.set("limit", String(params.limit));
    if (params?.offset) q.set("offset", String(params.offset));
    return signal
      ? apiClient.get<BugReportListResponse>(`/bug_reports?${q}`, { signal })
      : apiClient.get<BugReportListResponse>(`/bug_reports?${q}`);
  },

  listMine: (limit = 50, offset = 0) =>
    apiClient.get<BugReportListResponse>(`/bug_reports/mine?limit=${limit}&offset=${offset}`),

  get: (id: string) => apiClient.get<BugReportDetail>(`/bug_reports/${id}`),

  update: (id: string, payload: BugReportUpdatePayload) =>
    apiClient.patch<BugReportResponse>(`/bug_reports/${id}`, payload),

  delete: (id: string) => apiClient.delete<void>(`/bug_reports/${id}`),

  addComment: (id: string, body: string) =>
    apiClient.post<BugCommentResponse>(`/bug_reports/${id}/comments`, { body }),

  downloadAttachment: (reportId: string, key: string, signal?: AbortSignal) =>
    apiClient.silentGetBlob(
      `/bug_reports/${reportId}/attachments/download?key=${encodeURIComponent(key)}`,
      { signal },
    ),
};

export async function uploadBugScreenshot(blob: Blob): Promise<string> {
  return (await uploadBugAttachment(blob)).storageKey;
}

export function uploadBugAttachment(
  file: Blob,
  fileName = "screenshot.png",
): Promise<BugAttachment> {
  return apiClient.silentPost<BugAttachment>(
    `/bug_reports/screenshot/upload?file_name=${encodeURIComponent(fileName)}`,
    undefined,
    { body: file, headers: { "Content-Type": file.type || "image/png" } },
  );
}

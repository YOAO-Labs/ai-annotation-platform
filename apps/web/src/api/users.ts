import { apiClient } from "./client";
import type { UserOut } from "./generated/types.gen";

/** The lifecycle fields are served by the API but may lag the generated client snapshot. */
export type UserResponse = UserOut & {
  is_active?: boolean;
  disabled_kind?: string | null;
  disabled_at?: string | null;
  disabled_by?: string | null;
  disabled_reason?: string | null;
  /** Whether the current actor may run account-management actions on this row. */
  is_managed?: boolean;
  /**
   * Whether the current actor may run lifecycle writes (offboarding,
   * deactivate, delete, reactivate). False for cross-project rows whose
   * work-handover the API rejects with 403.
   */
  is_lifecycle_managed?: boolean;
};

export type UserStatusFilter = "active" | "inactive" | "all";

export interface UserPageParams {
  role?: string;
  project_id?: string;
  group_id?: string;
  status?: UserStatusFilter;
  search?: string;
  page?: number;
  page_size?: number;
}

export interface UserPageResponse {
  items: UserResponse[];
  total: number;
  page: number;
  page_size: number;
  pages: number;
}

export interface UserStatsParams {
  role?: string;
  project_id?: string;
  group_id?: string;
  status?: UserStatusFilter;
  search?: string;
}

export type OffboardingMode = "handoff" | "emergency_suspend";
export type OffboardingRole = "owner" | "annotator" | "reviewer";

export interface OffboardingReceiverOption {
  id: string;
  name: string;
  email: string;
  role: string;
  project_member_role: string | null;
}

export interface OffboardingBlocker {
  code: string;
  message: string;
}

export interface OffboardingBatchRef {
  batch_id: string;
  batch_name: string;
}

export interface OffboardingRolePreview {
  present: boolean;
  batches: OffboardingBatchRef[];
  receiver_options: OffboardingReceiverOption[];
}

export interface OffboardingProjectPreview {
  project_id: string;
  project_name: string;
  roles: Record<OffboardingRole, OffboardingRolePreview>;
  tasks: Record<string, Record<string, number>>;
  locked_task_count?: number;
  blockers: OffboardingBlocker[];
}

export interface OffboardingApiKeyPreview {
  id: string;
  name: string;
  key_prefix: string;
  last_used_at: string | null;
  revoked_at: string | null;
}

export interface OffboardingPreview {
  user: UserResponse;
  preview_version: string;
  generated_at: string;
  projects: OffboardingProjectPreview[];
  api_keys: OffboardingApiKeyPreview[];
  blockers: OffboardingBlocker[];
  can_commit: boolean;
}

export interface OffboardingProjectRequest {
  project_id: string;
  owner_receiver_id?: string;
  annotator_receiver_id?: string;
  reviewer_receiver_id?: string;
}

export interface OffboardingCommitRequest {
  preview_version: string;
  reason: string;
  mode: OffboardingMode;
  projects: OffboardingProjectRequest[];
}

export interface OffboardingTransferResult {
  project_id: string;
  role: string;
  receiver_id: string | null;
  batch_ids: string[];
  task_count: number;
  lock_count: number;
}

export interface OffboardingUnresolvedResult {
  project_id: string | null;
  role: string | null;
  reason: string;
  batch_ids: string[];
  task_count: number;
  lock_count: number;
}

export interface OffboardingResult {
  user: UserResponse;
  status: string;
  mode: OffboardingMode;
  transfers: OffboardingTransferResult[];
  unresolved: OffboardingUnresolvedResult[];
  revoked_api_key_ids: string[];
  audit_id: number | null;
}

export interface ReactivateRequest {
  reason?: string;
}

export interface InvitePayload {
  email: string;
  /** Platform (account) role. */
  role: string;
  /** Project responsibility, only when a target project is selected. */
  project_member_role?: string | undefined;
  group_name?: string;
  project_id?: string;
}

export interface InvitationCreated {
  id?: string;
  invite_url: string;
  token: string;
  expires_at: string;
  project_id?: string | null;
  project_name?: string | null;
  project_member_role?: string | null;
}

export type UserExportFormat = "csv" | "json";

export interface AdminResetPasswordResult {
  temp_password: string;
  message: string;
  target_email: string;
}

export interface UsersStats {
  total: number;
  online: number;
  weekly_active: number;
}

export interface BulkInviteItemPayload {
  email: string;
  /** Platform (account) role. */
  role: string;
  /** Project responsibility, only when a target project is selected. */
  project_member_role?: string;
  group_name?: string;
  project_id?: string;
}

export interface BulkInviteResultItem {
  index: number;
  email: string;
  ok: boolean;
  retryable: boolean;
  invitation_id?: string | null;
  token?: string | null;
  invite_url?: string | null;
  project_id?: string | null;
  project_name?: string | null;
  error?: string | null;
}

export interface BulkInviteResponse {
  items: BulkInviteResultItem[];
  succeeded: number;
  failed: number;
  preview?: boolean;
}

export interface BulkGroupAssignmentPayload {
  user_ids: string[];
  group_id: string | null;
}

export interface GroupAssignmentPreviewItem {
  user_id: string;
  email?: string | null;
  name?: string | null;
  ok: boolean;
  current_group_id?: string | null;
  current_group_name?: string | null;
  next_group_id?: string | null;
  next_group_name?: string | null;
  error?: string | null;
}

export interface BulkGroupAssignmentPreview {
  group_id: string | null;
  group_name?: string | null;
  items: GroupAssignmentPreviewItem[];
  applicable: number;
  blocked: number;
}

export interface BulkGroupAssignmentResultItem {
  user_id: string;
  ok: boolean;
  retryable: boolean;
  error?: string | null;
}

export interface BulkGroupAssignmentResponse {
  items: BulkGroupAssignmentResultItem[];
  succeeded: number;
  failed: number;
}

export interface RoleImpactProject {
  project_id: string;
  project_name: string;
  membership_role?: string | null;
  annotator_batch_count: number;
  reviewer_batch_count: number;
  assigned_task_count: number;
  review_task_count: number;
}

export interface RoleImpactPreview {
  user_id: string;
  email: string;
  current_role: string;
  requested_role: string;
  can_change: boolean;
  blockers: string[];
  projects: RoleImpactProject[];
  other_project_count: number;
  warnings: string[];
  assigned_batch_count: number;
  assigned_task_count: number;
  review_task_count: number;
}

function queryString(params: object) {
  return new URLSearchParams(
    Object.fromEntries(Object.entries(params).filter(([, value]) => value !== undefined)) as Record<
      string,
      string
    >,
  ).toString();
}

export const usersApi = {
  list: (
    params?: { role?: string; project_id?: string; status?: UserStatusFilter },
    signal?: AbortSignal,
  ) => {
    const q = new URLSearchParams(
      Object.fromEntries(Object.entries(params ?? {}).filter(([, v]) => v !== undefined)) as Record<
        string,
        string
      >,
    ).toString();
    const path = `/users${q ? `?${q}` : ""}`;
    return signal
      ? apiClient.get<UserResponse[]>(path, { signal })
      : apiClient.get<UserResponse[]>(path);
  },

  // v0.8.3 · UsersPage 顶部 4 卡之「本周活跃」聚合（last_seen_at >= now-7d）
  page: (params: UserPageParams = {}, signal?: AbortSignal) => {
    const q = queryString(params);
    const path = `/users/query${q ? `?${q}` : ""}`;
    return signal
      ? apiClient.get<UserPageResponse>(path, { signal })
      : apiClient.get<UserPageResponse>(path);
  },

  stats: (params: UserStatsParams = {}, signal?: AbortSignal) => {
    const q = queryString(params);
    const path = `/users/stats${q ? `?${q}` : ""}`;
    return signal ? apiClient.get<UsersStats>(path, { signal }) : apiClient.get<UsersStats>(path);
  },

  invite: (payload: InvitePayload) => apiClient.post<InvitationCreated>("/users/invite", payload),

  changeRole: (userId: string, role: string) =>
    apiClient.patch<UserResponse>(`/users/${userId}/role`, { role }),

  deactivate: (userId: string) => apiClient.post<UserResponse>(`/users/${userId}/deactivate`, {}),

  offboardingPreview: (userId: string) =>
    apiClient.get<OffboardingPreview>(`/users/${userId}/offboarding-preview`),

  offboard: (userId: string, payload: OffboardingCommitRequest) =>
    apiClient.post<OffboardingResult>(`/users/${userId}/offboarding`, payload),

  reactivate: (userId: string, payload?: ReactivateRequest) =>
    apiClient.post<UserResponse>(`/users/${userId}/reactivate`, payload ?? {}),

  remove: (userId: string, opts?: { transfer_to_user_id?: string }) =>
    apiClient.delete<UserResponse>(
      `/users/${userId}`,
      opts?.transfer_to_user_id ? { transfer_to_user_id: opts.transfer_to_user_id } : undefined,
    ),

  assignGroup: (userId: string, groupId: string | null) =>
    apiClient.patch<UserResponse>(`/users/${userId}/group`, { group_id: groupId }),

  adminResetPassword: (userId: string) =>
    apiClient.post<AdminResetPasswordResult>(`/users/${userId}/admin-reset-password`, {}),

  exportUsers: async (
    format: UserExportFormat = "csv",
    params: UserStatsParams = {},
  ): Promise<void> => {
    const token = localStorage.getItem("token");
    const q = queryString({ format, ...params });
    const res = await fetch(`/api/v1/users/export?${q}`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error((body as { detail?: string }).detail || `导出失败 (HTTP ${res.status})`);
    }
    const blob = await res.blob();
    if (!token || token !== localStorage.getItem("token"))
      throw new Error("当前登录状态已改变，请重新导出");
    const dispo = res.headers.get("Content-Disposition") || "";
    const match = /filename="?([^"]+)"?/.exec(dispo);
    const filename = match ? match[1] : `users.${format}`;
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  },

  previewBulkInvite: (items: BulkInviteItemPayload[]) =>
    apiClient.post<BulkInviteResponse>("/users/bulk-invite/preview", { items }),

  bulkInvite: (items: BulkInviteItemPayload[]) =>
    apiClient.post<BulkInviteResponse>("/users/bulk-invite", { items }),

  previewBulkGroup: (payload: BulkGroupAssignmentPayload) =>
    apiClient.post<BulkGroupAssignmentPreview>("/users/groups/bulk/preview", payload),

  bulkGroup: (payload: BulkGroupAssignmentPayload) =>
    apiClient.post<BulkGroupAssignmentResponse>("/users/groups/bulk", payload),

  previewRoleChange: (userId: string, role: string) =>
    apiClient.get<RoleImpactPreview>(
      `/users/${encodeURIComponent(userId)}/role/preview?role=${encodeURIComponent(role)}`,
    ),
};

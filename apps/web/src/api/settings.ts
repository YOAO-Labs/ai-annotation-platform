import { apiClient } from "./client";

export type SystemSettingSource = "deployment" | "override";

export type SystemSettingValue = string | number | boolean | null;

export type SystemSettingKey =
  | "allow_open_registration"
  | "invitation_ttl_days"
  | "max_invitations_per_day"
  | "offline_threshold_minutes"
  | "frontend_base_url"
  | "dataset_import_max_files"
  | "dataset_import_max_total_bytes"
  | "task_create_sync_threshold"
  | "video_chunk_warmup_lookahead"
  | "smtp_host"
  | "smtp_port"
  | "smtp_user"
  | "smtp_password"
  | "smtp_from";

export interface SystemSettingMetadata {
  source: SystemSettingSource;
  deployment_default: SystemSettingValue;
  updated_at: string | null;
  updated_by: string | null;
  value_type: string;
  unit: string | null;
  effect: string;
  min_value: number | null;
  max_value: number | null;
  in_range: boolean;
}

export interface SmtpStatusResponse {
  host: string | null;
  port: number | null;
  user: string | null;
  from_address: string | null;
  password_set: boolean;
  configured: boolean;
}

export interface SystemSettingsResponse {
  version?: string;
  metadata?: Partial<Record<SystemSettingKey, SystemSettingMetadata>>;
  environment: string;
  invitation_ttl_days: number;
  max_invitations_per_day?: number;
  offline_threshold_minutes?: number;
  frontend_base_url: string;
  dataset_import_max_files?: number;
  dataset_import_max_total_bytes?: number;
  task_create_sync_threshold?: number;
  video_chunk_warmup_lookahead?: number;
  smtp: SmtpStatusResponse;
  allow_open_registration: boolean;
}

export interface SystemSettingsPatch {
  expected_version?: string;
  allow_open_registration?: boolean;
  invitation_ttl_days?: number;
  max_invitations_per_day?: number;
  offline_threshold_minutes?: number;
  frontend_base_url?: string;
  dataset_import_max_files?: number;
  dataset_import_max_total_bytes?: number;
  task_create_sync_threshold?: number;
  video_chunk_warmup_lookahead?: number;
  smtp_host?: string;
  smtp_port?: number | null;
  smtp_user?: string;
  smtp_password?: string;
  smtp_from?: string;
}

export interface SystemSettingsReset {
  keys: SystemSettingKey[];
  expected_version?: string;
}

export interface SmtpTestResponse {
  ok: boolean;
  to?: string;
  from?: string;
  host?: string;
  port?: number;
}

export const settingsApi = {
  getSystem: () => apiClient.get<SystemSettingsResponse>("/settings/system"),
  updateSystem: (patch: SystemSettingsPatch) =>
    apiClient.patch<SystemSettingsResponse>("/settings/system", patch),
  resetSystem: (payload: SystemSettingsReset) =>
    apiClient.post<SystemSettingsResponse>("/settings/system/reset", payload),
  testSmtp: () => apiClient.post<SmtpTestResponse>("/settings/system/test-smtp", {}),
};

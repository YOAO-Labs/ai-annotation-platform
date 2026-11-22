import { apiClient } from "./client";
import type { MeResponse } from "./auth";

export interface ProfileUpdatePayload {
  name: string;
}

export interface AvatarRefPayload {
  avatar_ref: string | null;
}

export interface PasswordChangePayload {
  old_password: string;
  new_password: string;
}

export interface TaskEventIn {
  client_id?: string;
  task_id: string;
  project_id: string;
  kind: "annotate" | "review";
  started_at: string;
  ended_at: string;
  duration_ms: number;
  annotation_count?: number;
  was_rejected?: boolean;
  collector_version?: "session-v2";
  collection_coverage?: "qualified" | "partial";
}

export interface TaskEventDiscarded {
  index: number;
  client_id?: string | null;
  reason: string;
}

export interface TaskEventBatchOut {
  accepted: number;
  queued_async: boolean;
  discarded?: TaskEventDiscarded[];
}

export const meApi = {
  updateProfile: (payload: ProfileUpdatePayload) =>
    apiClient.patch<MeResponse>("/auth/me", payload),
  changePassword: (payload: PasswordChangePayload) =>
    apiClient.post<void>("/auth/me/password", payload),
  setAvatarRef: (avatar_ref: string | null) =>
    apiClient.patch<MeResponse>("/auth/me/avatar", { avatar_ref } satisfies AvatarRefPayload),
  clearAvatar: () => apiClient.delete<MeResponse>("/auth/me/avatar"),
  uploadAvatar: (file: File, onProgress?: (percent: number) => void): Promise<MeResponse> =>
    new Promise((resolve, reject) => {
      const token = localStorage.getItem("token");
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/v1/auth/me/avatar");
      if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable && onProgress) {
          onProgress((event.loaded / event.total) * 100);
        }
      };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          try {
            resolve(JSON.parse(xhr.responseText) as MeResponse);
          } catch {
            reject(new Error("响应解析失败"));
          }
          return;
        }
        let detail = `上传失败 (HTTP ${xhr.status})`;
        try {
          const body = JSON.parse(xhr.responseText) as { detail?: unknown };
          if (typeof body.detail === "string") detail = body.detail;
        } catch {
          // 非 JSON 响应保留状态码文案
        }
        reject(new Error(detail));
      };
      xhr.onerror = () => reject(new Error("网络错误"));
      const form = new FormData();
      form.append("file", file);
      xhr.send(form);
    }),
  requestDeactivation: (reason: string) =>
    apiClient.post<MeResponse>("/auth/me/deactivation-request", { reason }),
  cancelDeactivation: () => apiClient.delete<MeResponse>("/auth/me/deactivation-request"),
  submitTaskEvents: (events: TaskEventIn[], init?: RequestInit) =>
    apiClient.post<TaskEventBatchOut>("/auth/me/task-events:batch", { events }, init),
  heartbeat: () => apiClient.post<void>("/auth/me/heartbeat"),
};

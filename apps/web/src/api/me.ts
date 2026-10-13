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

// v0.8.4 · 工作台耗时事件批量上报（效率看板源数据）
export interface TaskEventIn {
  client_id?: string;
  task_id: string;
  project_id: string;
  kind: "annotate" | "review";
  started_at: string; // ISO
  ended_at: string; // ISO
  duration_ms: number;
  annotation_count?: number;
  was_rejected?: boolean;
  collector_version?: "session-v2";
  /** Set by the bounded client queue after local events were evicted. */
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
  /**
   * 头像：选择内置像素头像（`preset:<slug>`）或恢复默认（`avatar_ref: null`）。
   * 上传头像不能走这里——`upload:` 前缀由后端上传端点独占写入。
   */
  setAvatarRef: (avatar_ref: string | null) =>
    apiClient.patch<MeResponse>("/auth/me/avatar", { avatar_ref } satisfies AvatarRefPayload),
  clearAvatar: () => apiClient.delete<MeResponse>("/auth/me/avatar"),
  /**
   * 上传自定义头像（≤2 MB，PNG/JPEG/WebP）。
   *
   * 走 XHR 而不是 `apiClient`：后者固定 `Content-Type: application/json`，multipart 必须
   * 交给浏览器自己带 boundary。顺带拿到上传进度（与 datasets.uploadZip 同一套写法）。
   */
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
  // v0.8.1 · 自助注销冷静期
  requestDeactivation: (reason: string) =>
    apiClient.post<MeResponse>("/auth/me/deactivation-request", { reason }),
  cancelDeactivation: () => apiClient.delete<MeResponse>("/auth/me/deactivation-request"),
  // v0.8.4
  submitTaskEvents: (events: TaskEventIn[], init?: RequestInit) =>
    apiClient.post<TaskEventBatchOut>("/auth/me/task-events:batch", { events }, init),
  // v0.8.3 · 在线状态心跳：前端 30s 周期触发，刷新 last_seen_at + status='online'。
  heartbeat: () => apiClient.post<void>("/auth/me/heartbeat"),
};

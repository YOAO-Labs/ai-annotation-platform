import { apiClient } from "./client";

export interface NotificationItem {
  id: string;
  type: string;
  target_type: string;
  target_id: string;
  payload: Record<string, unknown>;
  read_at: string | null;
  created_at: string;
}

export interface NotificationsResponse {
  items: NotificationItem[];
  total: number;
  unread: number;
}

export interface UnreadCountResponse {
  unread: number;
}

export interface NotificationPreferenceItem {
  type: string;
  in_app: boolean;
  email: boolean;
  /** 有效弹出选择：显式存储值优先，否则按类型默认重要程度。 */
  toast: boolean;
}

export interface NotificationPreferencesResponse {
  items: NotificationPreferenceItem[];
}

export interface NotificationPreferencePatch {
  in_app?: boolean;
  toast?: boolean;
}

export const notificationsApi = {
  list: (params?: { limit?: number; offset?: number; unreadOnly?: boolean }) => {
    const q = new URLSearchParams();
    if (params?.limit) q.set("limit", String(params.limit));
    if (params?.offset) q.set("offset", String(params.offset));
    if (params?.unreadOnly) q.set("unread_only", "true");
    const qs = q.toString();
    return apiClient.get<NotificationsResponse>(`/notifications${qs ? `?${qs}` : ""}`);
  },

  unreadCount: () => apiClient.get<UnreadCountResponse>("/notifications/unread-count"),

  markRead: (id: string) => apiClient.post<{ ok: boolean }>(`/notifications/${id}/read`, {}),

  markAllRead: () => apiClient.post<{ updated: number }>("/notifications/mark-all-read", {}),

  delete: (id: string) => apiClient.delete<{ ok: boolean }>(`/notifications/${id}`),

  clearRead: () => apiClient.post<{ deleted: number }>("/notifications/clear-read", {}),

  getPreferences: () => apiClient.get<NotificationPreferencesResponse>("/notification-preferences"),

  updatePreference: (type: string, patch: NotificationPreferencePatch) =>
    apiClient.put<{ ok: boolean }>("/notification-preferences", { type, ...patch }),
};

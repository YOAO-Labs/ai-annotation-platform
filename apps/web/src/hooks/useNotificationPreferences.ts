import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  notificationsApi,
  type NotificationPreferencePatch,
  type NotificationPreferencesResponse,
} from "@/api/notifications";
import { isCurrentAuthOwner, useAuthStore } from "@/stores/authStore";

export function notificationPreferencesKey(userId: string | null | undefined) {
  return ["notification-preferences", userId ?? null] as const;
}

export function useNotificationPreferences(enabled = true) {
  const userId = useAuthStore((s) => s.user?.id);
  return useQuery<NotificationPreferencesResponse>({
    queryKey: notificationPreferencesKey(userId),
    queryFn: () => notificationsApi.getPreferences(),
    enabled: enabled && !!userId,
    refetchInterval: 30_000,
    refetchOnWindowFocus: true,
    retry: false,
  });
}

export function useUpdateNotificationPreference() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: { type: string; owner?: string } & NotificationPreferencePatch) => {
      const owner = input.owner ?? useAuthStore.getState().user?.id;
      const patch: NotificationPreferencePatch = {};
      if (input.in_app !== undefined) patch.in_app = input.in_app;
      if (input.toast !== undefined) patch.toast = input.toast;
      return notificationsApi.updatePreference(input.type, patch).then((result) => ({
        result,
        owner,
      }));
    },
    onSuccess: ({ owner }, variables) => {
      if (!owner || !isCurrentAuthOwner(owner)) return;
      qc.setQueryData<NotificationPreferencesResponse>(
        notificationPreferencesKey(owner),
        (previous) => {
          if (!previous) return previous;
          return {
            ...previous,
            items: previous.items.map((item) =>
              item.type === variables.type
                ? {
                    ...item,
                    ...(variables.in_app !== undefined ? { in_app: variables.in_app } : {}),
                    ...(variables.toast !== undefined ? { toast: variables.toast } : {}),
                  }
                : item,
            ),
          };
        },
      );
      void qc.invalidateQueries({ queryKey: notificationPreferencesKey(owner) });
    },
  });
}

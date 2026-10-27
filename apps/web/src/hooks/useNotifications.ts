import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { notificationsApi } from "../api/notifications";
import { useAuthStore } from "@/stores/authStore";
import { useSessionRevision } from "@/hooks/useSessionGuard";

function useNotificationOwner() {
  const userId = useAuthStore((state) => state.user?.id ?? null);
  const superAdmin = useAuthStore((state) => state.user?.role === "super_admin");
  const revision = useSessionRevision();
  return { userId, superAdmin, revision };
}

export function useNotifications(enabled = true, limit = 30) {
  const owner = useNotificationOwner();
  return useInfiniteQuery({
    queryKey: ["notifications", "list", { limit }, owner],
    queryFn: ({ pageParam }: { pageParam: number }) =>
      notificationsApi.list({ limit, offset: pageParam }),
    initialPageParam: 0,
    getNextPageParam: (lastPage, _allPages, lastPageParam: number) => {
      const nextOffset = lastPageParam + limit;
      return nextOffset < lastPage.total ? nextOffset : undefined;
    },
    refetchInterval: enabled ? 30_000 : false,
    refetchOnWindowFocus: enabled,
    refetchOnReconnect: enabled,
    enabled,
    retry: false,
  });
}

export function useUnreadCount() {
  const owner = useNotificationOwner();
  return useQuery({
    queryKey: ["notifications", "unread-count", owner],
    queryFn: () => notificationsApi.unreadCount(),
    refetchInterval: 30_000,
    // 断线期间错过的事件在回到前台时立即补偿；后台节流由浏览器决定。
    refetchOnWindowFocus: true,
    refetchOnReconnect: true,
    retry: false,
  });
}

export function useMarkRead() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => notificationsApi.markRead(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["notifications"] });
    },
  });
}

export function useMarkAllRead() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => notificationsApi.markAllRead(),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["notifications"] });
    },
  });
}

export function useDeleteNotification() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => notificationsApi.delete(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["notifications"] });
    },
  });
}

export function useClearReadNotifications() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => notificationsApi.clearRead(),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["notifications"] });
    },
  });
}

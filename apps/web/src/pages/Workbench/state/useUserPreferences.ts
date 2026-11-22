import { useQuery } from "@tanstack/react-query";
import { authApi } from "@/api/auth";
import { useAuthStore } from "@/stores/authStore";

export function useUserPreferences() {
  const userId = useAuthStore((s) => s.user?.id);
  const query = useQuery({
    queryKey: ["me", "preferences", userId],
    queryFn: () => authApi.getPreferences(),
    enabled: !!userId,
    staleTime: 5 * 60 * 1000,
  });
  return {
    prefs: query.data,
    loaded: !query.isPending,
    error: query.error,
    refetch: query.refetch,
    userId,
  };
}

export function userPreferencesQueryKey(userId: string | null | undefined) {
  return ["me", "preferences", userId] as const;
}

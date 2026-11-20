import { useQuery } from "@tanstack/react-query";
import { authApi } from "@/api/auth";
import { useAuthStore } from "@/stores/authStore";

/**
 * 用户偏好 `GET /me/preferences` 的共享 react-query 单一来源。
 *
 * 此前 `ai.*` 四个偏好 hook (interactive_backend / model / secondary / params) 各自在挂载时裸调
 * `authApi.getPreferences()`, 进工作台首屏并发多次相同 GET。收敛到一个 react-query 后, 同 key
 * 自动去重 —— 多个消费者共享一次请求 / 一份缓存。
 *
 * query key 带 `userId`: 切账号即换 key → 新 query (data 先 undefined), 天然隔离上一个用户的
 * 偏好, 消费方据此把本地态清回空。
 *
 * 注: `workbench.layout` 等其它子树各有独立管道 (useWorkbenchConfig 等), 不走本 hook。
 */
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

/** 供 writer 成功后 invalidate 用: 与 useUserPreferences 同 key。 */
export function userPreferencesQueryKey(userId: string | null | undefined) {
  return ["me", "preferences", userId] as const;
}

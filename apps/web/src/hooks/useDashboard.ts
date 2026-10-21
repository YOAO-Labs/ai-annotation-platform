import { useQuery } from "@tanstack/react-query";
import { dashboardApi, type MyBatchItem, type ReviewingBatchItem } from "../api/dashboard";
import { useAuthStore } from "@/stores/authStore";

/** Fetch every batch page before consumers filter and sort the collection. */
async function allBatches<T extends { batch_id: string }>(
  fetchPage: (limit: number, offset: number, signal?: AbortSignal) => Promise<T[]>,
  signal: AbortSignal,
): Promise<T[]> {
  const items = new Map<string, T>();
  const limit = 200;
  for (let offset = 0; ; offset += limit) {
    signal.throwIfAborted();
    const page = await fetchPage(limit, offset, signal);
    for (const item of page) items.set(item.batch_id, item);
    if (page.length < limit) return [...items.values()];
  }
}

/** Account identity for cache binding; `bindAuthQueryCache` also clears on change. */
function useAccountScope() {
  const userId = useAuthStore((state) => state.user?.id ?? null);
  return { userId };
}

export function useAdminStats(enabled: boolean = true) {
  const { userId } = useAccountScope();
  return useQuery({
    queryKey: ["dashboard", "admin", userId],
    queryFn: dashboardApi.getAdminStats,
    enabled,
  });
}

export function useReviewerStats() {
  const { userId } = useAccountScope();
  return useQuery({
    queryKey: ["dashboard", "reviewer", userId],
    queryFn: async ({ signal }) => {
      const [stats, reviewing_batches] = await Promise.all([
        dashboardApi.getReviewerStats(signal),
        allBatches<ReviewingBatchItem>(dashboardApi.getReviewerBatches, signal),
      ]);
      return { ...stats, reviewing_batches };
    },
  });
}

export function useAnnotatorStats() {
  const { userId } = useAccountScope();
  return useQuery({
    queryKey: ["dashboard", "annotator", userId],
    queryFn: dashboardApi.getAnnotatorStats,
  });
}

export function useMyBatches() {
  const { userId } = useAccountScope();
  return useQuery({
    queryKey: ["dashboard", "annotator", "batches", userId],
    queryFn: ({ signal }) => allBatches<MyBatchItem>(dashboardApi.getMyBatches, signal),
    // B-20：标注员看进度需实时性，10s 轻量轮询；窗口可见时才轮询，避免后台 tab 浪费
    refetchInterval: 10_000,
    refetchIntervalInBackground: false,
  });
}

export function useOnboardingProjectSummary(projectId: string | undefined) {
  const userId = useAuthStore((state) => state.user?.id);
  return useQuery({
    queryKey: ["dashboard", "annotator", "onboarding", projectId, userId],
    queryFn: () => dashboardApi.getOnboardingProjectSummary(projectId!),
    enabled: Boolean(projectId && userId),
  });
}

export function useMyRecentReviews(limit = 20) {
  const { userId } = useAccountScope();
  return useQuery({
    queryKey: ["dashboard", "me-recent-reviews", limit, userId],
    queryFn: () => dashboardApi.getMyRecentReviews(limit),
  });
}

// v0.8.4 · 管理员人员看板
export function useAdminPeople(params: {
  role?: string;
  project?: string;
  period?: string;
  sort?: string;
  q?: string;
  enabled?: boolean;
}) {
  const { enabled = true, ...query } = params;
  const ownerId = useAuthStore((state) => state.user?.id);
  return useQuery({
    queryKey: ["dashboard", "admin", "people", ownerId, query],
    queryFn: ({ signal }) => dashboardApi.getAdminPeople(query, signal),
    enabled,
  });
}

export function useAdminPersonDetail(
  userId: string | null,
  period: string = "4w",
  project?: string,
) {
  const ownerId = useAuthStore((state) => state.user?.id);
  return useQuery({
    queryKey: ["dashboard", "admin", "people", "detail", ownerId, userId, period, project],
    queryFn: ({ signal }) => dashboardApi.getAdminPersonDetail(userId!, period, project, signal),
    enabled: Boolean(userId),
  });
}

// v0.8.6 F4 · 预测成本卡片
export function usePredictionCostStats(range: "7d" | "30d" = "30d") {
  return useQuery({
    queryKey: ["dashboard", "admin", "prediction-cost-stats", range],
    queryFn: () => dashboardApi.getPredictionCostStats(range),
  });
}

// v0.8.7 F5.3 · ReviewWorkbench mini 仪表（20s 自动 refetch）
export function useReviewerTodayMini() {
  const { userId } = useAccountScope();
  return useQuery({
    queryKey: ["dashboard", "reviewer", "today-mini", userId],
    queryFn: dashboardApi.getReviewerTodayMini,
    refetchInterval: 20_000,
  });
}

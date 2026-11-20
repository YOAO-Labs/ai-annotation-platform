/**
 * 失败预测列表 + 重试 mutation。
 * 加 dismiss / restore mutation + include_dismissed filter。
 *
 * ws 推送 `failed_prediction.retry.started` 与 async_jobs `job.*` 终态事件由
 * useNotificationSocket 监听并 invalidate 此 query；本 hook 只负责拉取与触发。
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { failedPredictionsApi } from "@/api/failed-predictions";

export function useFailedPredictions(
  page = 1,
  pageSize = 50,
  includeDismissed = false,
  enabled = true,
) {
  return useQuery({
    queryKey: ["admin", "failed-predictions", page, pageSize, includeDismissed],
    queryFn: () => failedPredictionsApi.list(page, pageSize, includeDismissed),
    enabled,
  });
}

export function useRetryFailedPrediction() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (failedId: string) => failedPredictionsApi.retry(failedId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin", "failed-predictions"] });
    },
  });
}

export function useDismissFailedPrediction() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (failedId: string) => failedPredictionsApi.dismiss(failedId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin", "failed-predictions"] });
    },
  });
}

export function useRestoreFailedPrediction() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (failedId: string) => failedPredictionsApi.restore(failedId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin", "failed-predictions"] });
    },
  });
}

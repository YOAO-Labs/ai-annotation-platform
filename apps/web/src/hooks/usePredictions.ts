import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { predictionsApi } from "@/api/predictions";
import type { PredictionResponse } from "@/types";

const PAGE_SIZE = 100;

/**
 * 跨 Prediction 按 shape 置信度 desc 分页加载。
 * 单个 Prediction 可能在多页中各出现一部分 shape，前端 flatMap 后无差别；
 * pageParam = 已加载的 shape 总数（offset）。
 */
export function usePredictions(
  taskId: string | undefined,
  modelVersion?: string,
  minConfidence?: number,
  pageSize: number = PAGE_SIZE,
) {
  return useInfiniteQuery({
    queryKey: ["predictions", taskId, modelVersion, minConfidence, pageSize],
    initialPageParam: 0,
    queryFn: ({ pageParam = 0, signal }) =>
      predictionsApi.listByTask(
        taskId!,
        modelVersion,
        minConfidence,
        pageSize,
        pageParam as number,
        { signal },
      ),
    getNextPageParam: (lastPage: PredictionResponse[], allPages) => {
      const lastShapes = lastPage.reduce((sum, p) => sum + (p.result?.length ?? 0), 0);
      if (lastShapes < pageSize) return undefined; // 这页就没填满，没下一页了
      return allPages.reduce(
        (sum, page) => sum + page.reduce((s, p) => s + (p.result?.length ?? 0), 0),
        0,
      );
    },
    enabled: !!taskId,
    // 阈值 / 模型版本变化会产生新 query key；旧 key 用默认 5min GC 会在长时间调阈值场景下堆积内存。
    gcTime: 30_000,
  });
}

export function useAcceptPrediction(taskId: string, videoSegmentId?: string | null) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: {
      predictionId: string;
      shapeIndex?: number;
      overrideClassName?: string;
      attributeOverrides?: Record<string, unknown>;
      idempotencyKey: string;
    }) => {
      return predictionsApi.accept(
        taskId,
        vars.predictionId,
        vars.shapeIndex,
        // 采纳时选类: 预测类名不在项目标签集 (会 422) 时带人选的项目标签.
        vars.overrideClassName,
        // 采纳前审阅改过的候选属性值.
        vars.attributeOverrides,
        videoSegmentId,
        vars.idempotencyKey,
      );
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["predictions", taskId] });
      qc.invalidateQueries({
        queryKey: videoSegmentId
          ? ["annotations", taskId, videoSegmentId]
          : ["annotations", taskId],
      });
    },
  });
}

/**
 * B-37 · 驳回 AI 预测 shape, 持久化到后端.
 * 成功后 invalidate predictions query, 让该 shape 不再出现在 AI 待审区.
 */
export function useRejectPrediction(taskId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { predictionId: string; shapeIndex?: number }) =>
      predictionsApi.reject(taskId, vars.predictionId, vars.shapeIndex),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["predictions", taskId] });
    },
  });
}

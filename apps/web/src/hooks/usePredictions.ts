import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { predictionsApi } from "@/api/predictions";
import type { PredictionResponse } from "@/types";

const PAGE_SIZE = 100;

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
      if (lastShapes < pageSize) return undefined;
      return allPages.reduce(
        (sum, page) => sum + page.reduce((s, p) => s + (p.result?.length ?? 0), 0),
        0,
      );
    },
    enabled: !!taskId,
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
        vars.overrideClassName,
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

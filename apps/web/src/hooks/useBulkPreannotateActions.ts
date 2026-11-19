/**
 * BUG B-16 / B-17
 *
 * `/ai-pre` 已就绪卡片多选批量清理 + 项目卡片聚合.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { adminPreannotateApi, type BulkClearRequest } from "@/api/adminPreannotate";

const SUMMARY_KEY = ["admin", "preannotate-summary"] as const;
const QUEUE_KEY = ["admin", "preannotate-queue"] as const;

export function useBulkPreannotateClear() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: BulkClearRequest) => adminPreannotateApi.bulkClear(payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QUEUE_KEY });
      qc.invalidateQueries({ queryKey: SUMMARY_KEY });
      qc.invalidateQueries({ queryKey: ["batches"] });
      qc.invalidateQueries({ queryKey: ["admin", "preannotate-jobs"] });
    },
  });
}

export function useAIPreProjectSummary() {
  return useQuery({
    queryKey: SUMMARY_KEY,
    queryFn: () => adminPreannotateApi.summary(),
    staleTime: 1000 * 30,
  });
}

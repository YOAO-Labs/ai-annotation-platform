/**
 * 批量编辑 React Query mutation hook。
 *
 * 标注编组(Ctrl+G)持久化已删除,group / ungroup mutation 一并移除;
 * 仅保留 bulkUpdate(选中多框一次改 class/属性/状态位),invalidate
 * `["annotations", taskId]` 让 useAnnotations 重拉反映字段变更。
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  annotationGroupApi,
  type AnnotationBulkPatch,
  type BulkUpdateResponse,
} from "@/api/annotationGroup";

function invalidateTaskAnnotations(qc: ReturnType<typeof useQueryClient>, taskId: string) {
  qc.invalidateQueries({ queryKey: ["annotations", taskId] });
  // useAnnotationCommentsInfinite / useAnnotationAuditHistory 不受影响, 不动.
}

export function useAnnotationBulkUpdate(taskId: string) {
  const qc = useQueryClient();
  return useMutation<BulkUpdateResponse, Error, { ids: string[]; patch: AnnotationBulkPatch }>({
    mutationKey: ["annotation-write", taskId],
    mutationFn: ({ ids, patch }) => annotationGroupApi.bulkUpdate({ ids, patch }),
    onSuccess: () => invalidateTaskAnnotations(qc, taskId),
  });
}

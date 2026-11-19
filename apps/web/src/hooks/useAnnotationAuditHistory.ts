import { useQuery } from "@tanstack/react-query";
import { annotationHistoryApi } from "@/api/annotationHistory";

/**
 * 拉取单个 annotation 的完整时间线（audit + comments + 关联 task 审核事件）。
 * 注意：本地 undo/redo 栈也叫 useAnnotationHistory（state/useAnnotationHistory.ts），
 * 这里命名为 useAnnotationAuditHistory 避免冲突。
 */
export function useAnnotationAuditHistory(annotationId: string | null) {
  return useQuery({
    queryKey: ["annotation-history", annotationId],
    queryFn: () => annotationHistoryApi.get(annotationId as string),
    enabled: !!annotationId,
    staleTime: 30 * 1000,
  });
}

/**
 * I4 · DiscussionPanel 未选中标注时降级到 task 级时间线.
 */
export function useTaskAuditHistory(taskId: string | null, enabled = true) {
  return useQuery({
    queryKey: ["task-audit-history", taskId],
    queryFn: () => annotationHistoryApi.getByTask(taskId as string),
    enabled: enabled && !!taskId,
    staleTime: 30 * 1000,
  });
}

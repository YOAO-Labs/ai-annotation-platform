import { useQuery } from "@tanstack/react-query";
import { annotationHistoryApi } from "@/api/annotationHistory";

export function useAnnotationAuditHistory(annotationId: string | null) {
  return useQuery({
    queryKey: ["annotation-history", annotationId],
    queryFn: () => annotationHistoryApi.get(annotationId as string),
    enabled: !!annotationId,
    staleTime: 30 * 1000,
  });
}

export function useTaskAuditHistory(taskId: string | null, enabled = true) {
  return useQuery({
    queryKey: ["task-audit-history", taskId],
    queryFn: () => annotationHistoryApi.getByTask(taskId as string),
    enabled: enabled && !!taskId,
    staleTime: 30 * 1000,
  });
}

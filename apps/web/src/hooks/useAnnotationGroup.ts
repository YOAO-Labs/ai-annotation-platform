import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  annotationGroupApi,
  type AnnotationBulkPatch,
  type BulkUpdateResponse,
} from "@/api/annotationGroup";

function invalidateTaskAnnotations(qc: ReturnType<typeof useQueryClient>, taskId: string) {
  qc.invalidateQueries({ queryKey: ["annotations", taskId] });
}

export function useAnnotationBulkUpdate(taskId: string) {
  const qc = useQueryClient();
  return useMutation<BulkUpdateResponse, Error, { ids: string[]; patch: AnnotationBulkPatch }>({
    mutationKey: ["annotation-write", taskId],
    mutationFn: ({ ids, patch }) => annotationGroupApi.bulkUpdate({ ids, patch }),
    onSuccess: () => invalidateTaskAnnotations(qc, taskId),
  });
}

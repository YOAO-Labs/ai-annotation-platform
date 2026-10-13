import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  batchesApi,
  type BatchCreatePayload,
  type BatchUpdatePayload,
  type BatchSplitPayload,
  type BulkBatchReassignPayload,
  type ProjectDistributeBatchesPayload,
} from "../api/batches";

export function useBatches(projectId: string, status?: string) {
  return useQuery({
    queryKey: ["batches", projectId, status],
    queryFn: () => batchesApi.list(projectId, status),
    enabled: !!projectId,
  });
}

export function useBatch(projectId: string, batchId: string) {
  return useQuery({
    queryKey: ["batch", projectId, batchId],
    queryFn: () => batchesApi.get(projectId, batchId),
    enabled: !!projectId && !!batchId,
  });
}

export function useCreateBatch(projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: BatchCreatePayload) => batchesApi.create(projectId, payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["batches", projectId] });
    },
  });
}

export function useUpdateBatch(projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ batchId, payload }: { batchId: string; payload: BatchUpdatePayload }) =>
      batchesApi.update(projectId, batchId, payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["batches", projectId] });
    },
  });
}

export function useDeleteBatch(projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ batchId, force }: { batchId: string; force?: boolean }) =>
      batchesApi.remove(projectId, batchId, force ?? false),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["batches", projectId] });
      qc.invalidateQueries({ queryKey: ["unclassified-count", projectId] });
    },
  });
}

export function useTransitionBatch(projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      batchId,
      targetStatus,
      reason,
    }: {
      batchId: string;
      targetStatus: string;
      reason?: string;
    }) => batchesApi.transition(projectId, batchId, targetStatus, reason),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["batches", projectId] });
      qc.invalidateQueries({ queryKey: ["projects"] });
      qc.invalidateQueries({ queryKey: ["batch-audit-logs", projectId] });
      qc.invalidateQueries({ queryKey: ["notifications"] });
    },
  });
}

// v0.x · 整批送审：提交批次内所有 pending / in_progress 任务并推进到 reviewing。
export function useSubmitBatch(projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (batchId: string) => batchesApi.submitReview(projectId, batchId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["batches", projectId] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["task"] });
      qc.invalidateQueries({ queryKey: ["annotations"] });
      qc.invalidateQueries({ queryKey: ["projects"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
}

// v0.7.3 · 多选批量操作
export function useBulkArchiveBatches(projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (batchIds: string[]) => batchesApi.bulkArchive(projectId, batchIds),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["batches", projectId] });
      qc.invalidateQueries({ queryKey: ["projects"] });
    },
  });
}

export function useBulkDeleteBatches(projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (batchIds: string[]) => batchesApi.bulkDelete(projectId, batchIds),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["batches", projectId] });
      qc.invalidateQueries({ queryKey: ["projects"] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["unclassified-count", projectId] });
    },
  });
}

export function useBulkReassignBatches(projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: BulkBatchReassignPayload) => batchesApi.bulkReassign(projectId, payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["batches", projectId] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
    },
  });
}

export function useBulkActivateBatches(projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (batchIds: string[]) => batchesApi.bulkActivate(projectId, batchIds),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["batches", projectId] });
      qc.invalidateQueries({ queryKey: ["projects"] });
    },
  });
}

// v0.7.3 · 批次操作历史
export function useBatchAuditLogs(projectId: string, batchId: string | null, enabled: boolean) {
  return useQuery({
    queryKey: ["batch-audit-logs", projectId, batchId],
    queryFn: () => batchesApi.auditLogs(projectId, batchId!),
    enabled: enabled && !!projectId && !!batchId,
  });
}

// v0.7.3 · 未归类任务数
export function useUnclassifiedTaskCount(projectId: string) {
  return useQuery({
    queryKey: ["unclassified-count", projectId],
    queryFn: () => batchesApi.unclassifiedCount(projectId),
    enabled: !!projectId,
  });
}

export function useSplitBatches(projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: BatchSplitPayload) => batchesApi.split(projectId, payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["batches", projectId] });
      qc.invalidateQueries({ queryKey: ["unclassified-count", projectId] });
    },
  });
}

export function useDistributeBatches(projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      annotatorIds,
      reviewerIds,
      onlyUnassigned = true,
      previewVersion,
    }: {
      annotatorIds: string[];
      reviewerIds: string[];
      onlyUnassigned?: boolean;
      previewVersion?: string;
    }) =>
      batchesApi.distributeBatches(projectId, {
        annotator_ids: annotatorIds,
        reviewer_ids: reviewerIds,
        only_unassigned: onlyUnassigned,
        ...(previewVersion ? { preview_version: previewVersion } : {}),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["batches", projectId] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
}

export function useApplyDistributeBatches(projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      annotatorIds,
      reviewerIds,
      onlyUnassigned = true,
      previewVersion,
      batchIds,
    }: {
      annotatorIds: string[];
      reviewerIds: string[];
      onlyUnassigned?: boolean;
      previewVersion: string;
      batchIds?: string[];
    }) =>
      batchesApi.applyDistribution(projectId, {
        annotator_ids: annotatorIds,
        reviewer_ids: reviewerIds,
        only_unassigned: onlyUnassigned,
        preview_version: previewVersion,
        ...(batchIds ? { batch_ids: batchIds } : {}),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["batches", projectId] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
}

export function usePreviewDistributeBatches(projectId: string) {
  return useMutation({
    mutationFn: (payload: ProjectDistributeBatchesPayload) =>
      batchesApi.previewDistribution(projectId, payload),
  });
}

export function useRejectBatch(projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ batchId, feedback }: { batchId: string; feedback: string }) =>
      batchesApi.reject(projectId, batchId, feedback),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["batches", projectId] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["projects"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
      qc.invalidateQueries({ queryKey: ["notifications"] });
    },
  });
}

// v0.7.6 · 终极重置到 draft
export function useResetBatch(projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ batchId, reason }: { batchId: string; reason: string }) =>
      batchesApi.reset(projectId, batchId, reason),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["batches", projectId] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["projects"] });
      qc.invalidateQueries({ queryKey: ["batch-audit-logs", projectId] });
    },
  });
}

// v0.9.15 · ADR-0008 admin-lock
export function useAdminLockBatch(projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ batchId, reason }: { batchId: string; reason: string }) =>
      batchesApi.adminLock(projectId, batchId, reason),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["batches", projectId] });
      qc.invalidateQueries({ queryKey: ["batch-audit-logs", projectId] });
    },
  });
}

export function useAdminUnlockBatch(projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (batchId: string) => batchesApi.adminUnlock(projectId, batchId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["batches", projectId] });
      qc.invalidateQueries({ queryKey: ["batch-audit-logs", projectId] });
    },
  });
}

// v0.9.15 · Bulk approve/reject
export function useBulkApproveBatches(projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (batchIds: string[]) => batchesApi.bulkApprove(projectId, batchIds),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["batches", projectId] });
      qc.invalidateQueries({ queryKey: ["projects"] });
      qc.invalidateQueries({ queryKey: ["notifications"] });
    },
  });
}

export function useBulkRejectBatches(projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ batchIds, feedback }: { batchIds: string[]; feedback: string }) =>
      batchesApi.bulkReject(projectId, batchIds, feedback),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["batches", projectId] });
      qc.invalidateQueries({ queryKey: ["projects"] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["notifications"] });
    },
  });
}

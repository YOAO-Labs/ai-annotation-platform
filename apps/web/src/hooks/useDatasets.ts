import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useRef } from "react";
import { datasetsApi, type DatasetCreatePayload, type DatasetUpdatePayload } from "../api/datasets";
import {
  storageConnectionsApi,
  type DatasetImportFromConnectionPayload,
} from "@/api/storageConnections";
import { useAuthStore } from "@/stores/authStore";

export function useDatasets(params?: {
  search?: string;
  data_type?: string;
  has_scenes?: boolean;
}) {
  const userId = useAuthStore((state) => state.user?.id ?? null);
  const tokenEpoch = useAuthStore((state) => state.token);
  return useQuery({
    queryKey: ["datasets", params, userId, tokenEpoch],
    queryFn: ({ signal }) => datasetsApi.list(params, { signal }),
    enabled: Boolean(userId),
  });
}

export function useDataset(id: string | undefined) {
  return useQuery({
    queryKey: ["dataset", id],
    queryFn: () => datasetsApi.get(id!),
    enabled: !!id,
  });
}

export function useCreateDataset() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      payload,
      idempotencyKey,
    }: {
      payload: DatasetCreatePayload;
      idempotencyKey: string;
    }) => datasetsApi.create(payload, { idempotencyKey }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["datasets"] });
    },
  });
}

export function useUpdateDataset() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      payload,
      idempotencyKey,
    }: {
      id: string;
      payload: DatasetUpdatePayload;
      idempotencyKey: string;
    }) => datasetsApi.update(id, payload, { idempotencyKey }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["datasets"] });
    },
  });
}

export function useDeleteDataset() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => datasetsApi.delete(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["datasets"] });
    },
  });
}

export function useDatasetItems(
  datasetId: string | undefined,
  params?: { limit?: number; offset?: number },
) {
  return useQuery({
    queryKey: ["dataset-items", datasetId, params],
    queryFn: () => datasetsApi.listItems(datasetId!, params),
    enabled: !!datasetId,
  });
}

export function useScanDatasetItems(datasetId: string) {
  const qc = useQueryClient();
  const attemptRef = useRef<{ datasetId: string; key: string } | null>(null);
  return useMutation({
    mutationFn: (request?: { idempotencyKey?: string }) => {
      const key =
        request?.idempotencyKey ??
        (attemptRef.current?.datasetId === datasetId
          ? attemptRef.current.key
          : crypto.randomUUID());
      attemptRef.current = { datasetId, key };
      return datasetsApi.scanItems(datasetId, { idempotencyKey: key });
    },
    retry: false,
    onSuccess: (_result, request) => {
      if (
        attemptRef.current?.datasetId === datasetId &&
        (!request?.idempotencyKey || attemptRef.current.key === request.idempotencyKey)
      ) {
        attemptRef.current = null;
      }
      qc.invalidateQueries({ queryKey: ["datasets"] });
      qc.invalidateQueries({ queryKey: ["dataset-items", datasetId] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["projects"] });
      qc.invalidateQueries({ queryKey: ["project-stats"] });
    },
  });
}

export function useBackfillDimensions(datasetId: string) {
  const qc = useQueryClient();
  const actorId = useAuthStore((state) => state.user?.id ?? null);
  const attemptRef = useRef<{ scope: string; key: string } | null>(null);
  return useMutation({
    mutationFn: (batch?: number) => {
      const scope = JSON.stringify([actorId, datasetId]);
      const key =
        attemptRef.current?.scope === scope ? attemptRef.current.key : crypto.randomUUID();
      attemptRef.current = { scope, key };
      return datasetsApi.backfillDimensions(datasetId, batch ?? 50, { idempotencyKey: key });
    },
    retry: false,
    onSuccess: (result) => {
      if (attemptRef.current?.key === result.operation_id) attemptRef.current = null;
      qc.invalidateQueries({ queryKey: ["dataset-items", datasetId] });
    },
  });
}

export function useBackfillMedia(datasetId: string) {
  const qc = useQueryClient();
  const actorId = useAuthStore((state) => state.user?.id ?? null);
  const attemptRef = useRef<{ scope: string; key: string } | null>(null);
  return useMutation({
    mutationFn: () => {
      const scope = JSON.stringify([actorId, datasetId]);
      const key =
        attemptRef.current?.scope === scope ? attemptRef.current.key : crypto.randomUUID();
      attemptRef.current = { scope, key };
      return datasetsApi.backfillMedia(datasetId, { idempotencyKey: key });
    },
    retry: false,
    onSuccess: (result) => {
      if (attemptRef.current?.key === result.operation_id) attemptRef.current = null;
      qc.invalidateQueries({ queryKey: ["dataset-items", datasetId] });
    },
  });
}

export function useImportFromConnection() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      datasetId,
      payload,
      idempotencyKey,
    }: {
      datasetId: string;
      payload: DatasetImportFromConnectionPayload;
      idempotencyKey: string;
    }) => storageConnectionsApi.importFromConnection(datasetId, payload, idempotencyKey),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["async-job", res.job_id] });
    },
  });
}

export function useLinkProject(datasetId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (projectId: string) => datasetsApi.linkProject(datasetId, projectId),
    onSuccess: (_data, projectId) => {
      qc.invalidateQueries({ queryKey: ["datasets"] });
      qc.invalidateQueries({ queryKey: ["dataset", datasetId] });
      qc.invalidateQueries({ queryKey: ["dataset-projects", datasetId] });
      qc.invalidateQueries({ queryKey: ["projects"] });
      qc.invalidateQueries({ queryKey: ["project", projectId] });
      qc.invalidateQueries({ queryKey: ["project-stats"] });
      qc.invalidateQueries({ queryKey: ["batches", projectId] });
      qc.invalidateQueries({ queryKey: ["project-datasets", projectId] });
      qc.invalidateQueries({ queryKey: ["unclassified-count", projectId] });
    },
  });
}

export function useUnlinkProject() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      datasetId,
      projectId,
      idempotencyKey,
    }: {
      datasetId: string;
      projectId: string;
      idempotencyKey: string;
    }) => datasetsApi.unlinkProject(datasetId, projectId, { idempotencyKey }),
    onSuccess: (_data, { datasetId, projectId }) => {
      qc.invalidateQueries({ queryKey: ["datasets"] });
      qc.invalidateQueries({ queryKey: ["dataset", datasetId] });
      qc.invalidateQueries({ queryKey: ["dataset-projects", datasetId] });
      qc.invalidateQueries({ queryKey: ["projects"] });
      qc.invalidateQueries({ queryKey: ["project", projectId] });
      qc.invalidateQueries({ queryKey: ["project-stats"] });
      qc.invalidateQueries({ queryKey: ["batches", projectId] });
      qc.invalidateQueries({ queryKey: ["project-datasets", projectId] });
      qc.invalidateQueries({ queryKey: ["unclassified-count", projectId] });
    },
  });
}

export function useDatasetProjects(datasetId: string | undefined) {
  return useQuery({
    queryKey: ["dataset-projects", datasetId],
    queryFn: () => datasetsApi.getLinkedProjects(datasetId!),
    enabled: !!datasetId,
  });
}

// v0.7.3 · 项目侧反查：本项目关联的所有数据集
export function useProjectDatasets(projectId: string) {
  return useQuery({
    queryKey: ["project-datasets", projectId],
    queryFn: () => datasetsApi.listForProject(projectId),
    enabled: !!projectId,
  });
}

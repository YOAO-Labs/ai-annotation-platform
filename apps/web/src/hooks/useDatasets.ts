import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
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
  limit?: number;
  offset?: number;
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
    mutationFn: (payload: DatasetCreatePayload) => datasetsApi.create(payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["datasets"] });
    },
  });
}

export function useUpdateDataset() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: DatasetUpdatePayload }) =>
      datasetsApi.update(id, payload),
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
  return useMutation({
    mutationFn: () => datasetsApi.scanItems(datasetId),
    onSuccess: () => {
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
  return useMutation({
    mutationFn: (batch?: number) => datasetsApi.backfillDimensions(datasetId, batch ?? 50),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dataset-items", datasetId] });
    },
  });
}

export function useBackfillMedia(datasetId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => datasetsApi.backfillMedia(datasetId),
    onSuccess: () => {
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
    }: {
      datasetId: string;
      payload: DatasetImportFromConnectionPayload;
    }) => storageConnectionsApi.importFromConnection(datasetId, payload),
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

export function useUnlinkProject(datasetId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (projectId: string) => datasetsApi.unlinkProject(datasetId, projectId),
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

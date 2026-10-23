import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { datasetsApi, type DatasetCreatePayload, type DatasetUpdatePayload } from "../api/datasets";
import {
  storageConnectionsApi,
  type DatasetImportFromConnectionPayload,
} from "@/api/storageConnections";
import { useAuthStore } from "@/stores/authStore";
import { useSessionGuard, useSessionRevision } from "@/hooks/useSessionGuard";
import { getAuthSessionRevision } from "@/stores/authSession";
import type {
  DatasetItemListParams,
  DatasetLifecycleState,
  DatasetSavedViewPayload,
  DatasetSavedView,
} from "@/api/datasets";

function ownedMutation<T>(
  owner: { assert: (signal?: AbortSignal) => void },
  work: () => Promise<T>,
) {
  owner.assert();
  return work();
}
export function useDatasetOwner() {
  const revision = useSessionRevision();
  const userId = useAuthStore((s) => s.user?.id);
  const capture = useSessionGuard();
  return {
    key: [userId, revision],
    revision,
    enabled: !!userId,
    capture,
    assert: (signal?: AbortSignal) => {
      if (!userId || signal?.aborted || getAuthSessionRevision() !== revision || !capture()())
        throw new DOMException("数据集请求已失效", "AbortError");
    },
  };
}
export function useDatasetWorkbench(id: string) {
  const owner = useDatasetOwner();
  return useQuery({
    queryKey: ["dataset-workbench", id, ...owner.key],
    queryFn: async ({ signal }) => {
      owner.assert(signal);
      const value = await datasetsApi.workbench(id, { signal });
      owner.assert(signal);
      if (value.dataset_id !== id) throw new Error("数据集归属已变化，请刷新");
      return value;
    },
    enabled: !!id && owner.enabled,
  });
}
export function useDatasetItem(id: string, itemId: string | undefined) {
  const owner = useDatasetOwner();
  return useQuery({
    queryKey: ["dataset-item", id, itemId, ...owner.key],
    queryFn: async ({ signal }) => {
      owner.assert(signal);
      const value = await datasetsApi.item(id, itemId!, { signal });
      owner.assert(signal);
      if (value.dataset_id !== id || value.id !== itemId) throw new Error("样本归属已变化");
      return value;
    },
    enabled: !!id && !!itemId && owner.enabled,
  });
}
export function useDatasetTrainingVersions(
  id: string,
  params: { limit: number; offset: number },
  enabled: boolean,
) {
  const owner = useDatasetOwner();
  return useQuery({
    queryKey: ["dataset-training-versions", id, params, ...owner.key],
    queryFn: async ({ signal }) => {
      owner.assert(signal);
      const value = await datasetsApi.trainingVersions(id, params, { signal });
      owner.assert(signal);
      return value;
    },
    enabled: !!id && enabled && owner.enabled,
    refetchInterval: (q) =>
      q.state.data?.items.some((v) => ["pending", "running"].includes(v.status)) ? 3000 : false,
  });
}

export function useDatasets(params?: {
  search?: string;
  state?: DatasetLifecycleState;
  data_type?: string;
  has_scenes?: boolean;
  location_id?: string;
  limit?: number;
  offset?: number;
}) {
  const owner = useDatasetOwner();
  const userId = useAuthStore((state) => state.user?.id ?? null);
  const tokenEpoch = useAuthStore((state) => state.token);
  return useQuery({
    queryKey: ["datasets", params, userId, tokenEpoch, owner.revision],
    queryFn: async ({ signal }) => {
      owner.assert(signal);
      const value = await datasetsApi.list(params, { signal });
      owner.assert(signal);
      return value;
    },
    enabled: Boolean(userId),
  });
}

export function useDataset(id: string | undefined) {
  const owner = useDatasetOwner();
  return useQuery({
    queryKey: ["dataset", id, ...owner.key],
    queryFn: async ({ signal }) => {
      owner.assert(signal);
      const value = await datasetsApi.get(id!, { signal });
      owner.assert(signal);
      if (value.id !== id) throw new Error("数据集归属已变化");
      return value;
    },
    enabled: !!id && owner.enabled,
  });
}

export function useCreateDataset() {
  const qc = useQueryClient();
  const owner = useDatasetOwner();
  return useMutation({
    onMutate: () => {
      owner.assert();
      return owner.capture();
    },
    mutationFn: (payload: DatasetCreatePayload) =>
      ownedMutation(owner, () => datasetsApi.create(payload)),
    onSuccess: (_value, _variables, current) => {
      if (!current?.()) return;
      qc.invalidateQueries({ queryKey: ["datasets"] });
    },
  });
}

export function useUpdateDataset() {
  const qc = useQueryClient();
  const owner = useDatasetOwner();
  return useMutation({
    onMutate: () => {
      owner.assert();
      return owner.capture();
    },
    mutationFn: ({ id, payload }: { id: string; payload: DatasetUpdatePayload }) =>
      ownedMutation(owner, () => datasetsApi.update(id, payload)),
    onSuccess: (_value, _variables, current) => {
      if (!current?.()) return;
      qc.invalidateQueries({ queryKey: ["datasets"] });
    },
  });
}

export function useDeleteDataset() {
  const qc = useQueryClient();
  const owner = useDatasetOwner();
  return useMutation({
    onMutate: () => {
      owner.assert();
      return owner.capture();
    },
    mutationFn: (id: string) => ownedMutation(owner, () => datasetsApi.delete(id)),
    onSuccess: (_value, _variables, current) => {
      if (!current?.()) return;
      qc.invalidateQueries({ queryKey: ["datasets"] });
    },
  });
}

export function useDatasetItems(
  datasetId: string | undefined,
  params?: DatasetItemListParams,
  enabled = true,
) {
  const owner = useDatasetOwner();
  return useQuery({
    queryKey: ["dataset-items", datasetId, params, ...owner.key],
    queryFn: async ({ signal }) => {
      owner.assert(signal);
      const value = await datasetsApi.listItems(datasetId!, params, { signal });
      owner.assert(signal);
      if (value.items.some((item) => item.dataset_id !== datasetId))
        throw new Error("样本归属已变化");
      return value;
    },
    enabled: !!datasetId && owner.enabled && enabled,
  });
}

export function useScanDatasetItems(datasetId: string) {
  const qc = useQueryClient();
  const owner = useDatasetOwner();
  return useMutation({
    onMutate: () => {
      owner.assert();
      return owner.capture();
    },
    mutationFn: () => ownedMutation(owner, () => datasetsApi.scanItems(datasetId)),
    onSuccess: (_value, _variables, current) => {
      if (!current?.()) return;
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
  const owner = useDatasetOwner();
  return useMutation({
    onMutate: () => {
      owner.assert();
      return owner.capture();
    },
    mutationFn: (batch?: number) =>
      ownedMutation(owner, () => datasetsApi.backfillDimensions(datasetId, batch ?? 50)),
    onSuccess: (_value, _variables, current) => {
      if (!current?.()) return;
      qc.invalidateQueries({ queryKey: ["dataset-items", datasetId] });
    },
  });
}

export function useBackfillMedia(datasetId: string) {
  const qc = useQueryClient();
  const owner = useDatasetOwner();
  return useMutation({
    onMutate: () => {
      owner.assert();
      return owner.capture();
    },
    mutationFn: () => ownedMutation(owner, () => datasetsApi.backfillMedia(datasetId)),
    onSuccess: (_value, _variables, current) => {
      if (!current?.()) return;
      qc.invalidateQueries({ queryKey: ["dataset-items", datasetId] });
    },
  });
}

export function useImportFromConnection() {
  const qc = useQueryClient();
  const owner = useDatasetOwner();
  return useMutation({
    onMutate: () => {
      owner.assert();
      return owner.capture();
    },
    mutationFn: ({
      datasetId,
      payload,
    }: {
      datasetId: string;
      payload: DatasetImportFromConnectionPayload;
    }) =>
      ownedMutation(owner, () => storageConnectionsApi.importFromConnection(datasetId, payload)),
    onSuccess: (res, _variables, current) => {
      if (!current?.()) return;
      qc.invalidateQueries({ queryKey: ["async-job", res.job_id] });
    },
  });
}

export function useLinkProject(datasetId: string) {
  const qc = useQueryClient();
  const owner = useDatasetOwner();
  return useMutation({
    onMutate: () => {
      owner.assert();
      return owner.capture();
    },
    mutationFn: (projectId: string) =>
      ownedMutation(owner, () => datasetsApi.linkProject(datasetId, projectId)),
    onSuccess: (_data, projectId, current) => {
      if (!current?.()) return;
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
  const owner = useDatasetOwner();
  return useMutation({
    onMutate: () => {
      owner.assert();
      return owner.capture();
    },
    mutationFn: (projectId: string) =>
      ownedMutation(owner, () => datasetsApi.unlinkProject(datasetId, projectId)),
    onSuccess: (_data, projectId, current) => {
      if (!current?.()) return;
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
  const owner = useDatasetOwner();
  return useQuery({
    queryKey: ["dataset-projects", datasetId, ...owner.key],
    queryFn: async ({ signal }) => {
      owner.assert(signal);
      const value = await datasetsApi.getLinkedProjects(datasetId!, { signal });
      owner.assert(signal);
      return value;
    },
    enabled: !!datasetId && owner.enabled,
  });
}
export function useProjectDatasets(projectId: string) {
  const owner = useDatasetOwner();
  return useQuery({
    queryKey: ["project-datasets", projectId, ...owner.key],
    queryFn: async ({ signal }) => {
      owner.assert(signal);
      const value = await datasetsApi.listForProject(projectId, { signal });
      owner.assert(signal);
      return value;
    },
    enabled: !!projectId && owner.enabled,
  });
}

export function useDatasetSavedViews(id: string) {
  const owner = useDatasetOwner();
  return useQuery({
    queryKey: ["dataset-saved-views", id, ...owner.key],
    queryFn: async ({ signal }) => {
      owner.assert(signal);
      const value = await datasetsApi.savedViews(id, { signal });
      owner.assert(signal);
      if (value.items.some((view) => view.dataset_id !== id)) throw new Error("保存视图归属已变化");
      return value;
    },
    enabled: !!id && owner.enabled,
  });
}
export function useSaveDatasetView(id: string) {
  const owner = useDatasetOwner();
  const client = useQueryClient();
  return useMutation({
    onMutate: () => {
      owner.assert();
      return owner.capture();
    },
    mutationFn: async ({
      viewId,
      payload,
    }: {
      viewId?: string;
      payload: DatasetSavedViewPayload;
    }) => {
      owner.assert();
      const value = await datasetsApi.saveView(id, payload, viewId);
      owner.assert();
      if (value.dataset_id !== id) throw new Error("保存视图归属已变化");
      return value;
    },
    onSuccess: (value, _variables, current) => {
      if (!current?.()) return;
      client.setQueryData<{ items: DatasetSavedView[] }>(
        ["dataset-saved-views", id, ...owner.key],
        (old) => ({
          items: [...(old?.items ?? []).filter((view) => view.id !== value.id), value].sort(
            (a, b) => a.name.localeCompare(b.name),
          ),
        }),
      );
      void client.invalidateQueries({ queryKey: ["dataset-saved-views", id] });
    },
  });
}
export function useDeleteDatasetView(id: string) {
  const owner = useDatasetOwner();
  const client = useQueryClient();
  return useMutation({
    onMutate: () => {
      owner.assert();
      return owner.capture();
    },
    mutationFn: async (viewId: string) => {
      owner.assert();
      await datasetsApi.deleteView(id, viewId);
      owner.assert();
    },
    onSuccess: (_value, _variables, current) => {
      if (current?.()) void client.invalidateQueries({ queryKey: ["dataset-saved-views", id] });
    },
  });
}

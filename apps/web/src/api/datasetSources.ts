import { apiClient } from "./client";
import type {
  DatasetSourceList,
  DatasetSourceCreate,
  DatasetScanList,
  DatasetScanEntryList,
  StorageLocationList,
  StorageDirectoryPage,
  StorageProbeOut,
  DatasetSourceOut,
  DatasetScanOut,
  RegisteredReferenceOut,
} from "./generated/types.gen";

export const datasetSourcesApi = {
  locations: (signal?: AbortSignal) =>
    apiClient.get<StorageLocationList>("/storage-locations?limit=200", { signal }),
  browse: (id: string, path: string, cursor?: string, signal?: AbortSignal) =>
    apiClient.get<StorageDirectoryPage>(
      `/storage-locations/${id}/directories?${new URLSearchParams({ path, limit: "50", ...(cursor ? { cursor } : {}) })}`,
      { signal },
    ),
  probe: (id: string, node_id: string, sample_file?: string) =>
    apiClient.post<StorageProbeOut>(`/storage-locations/${id}/probe`, {
      node_id,
      ...(sample_file ? { sample_file } : {}),
    }),
  sources: (datasetId: string, signal?: AbortSignal) =>
    apiClient.get<DatasetSourceList>(`/dataset-sources?dataset_id=${datasetId}&limit=200`, {
      signal,
    }),
  create: (payload: DatasetSourceCreate) =>
    apiClient.post<DatasetSourceOut>("/dataset-sources", payload),
  scans: (sourceId: string, signal?: AbortSignal) =>
    apiClient.get<DatasetScanList>(`/dataset-sources/${sourceId}/scans?limit=20`, { signal }),
  start: (sourceId: string) => apiClient.post<DatasetScanOut>(`/dataset-sources/${sourceId}/scans`),
  resume: (runId: string) => apiClient.post<DatasetScanOut>(`/dataset-scans/${runId}/resume`),
  entries: (runId: string, offset: number, signal?: AbortSignal) =>
    apiClient.get<DatasetScanEntryList>(
      `/dataset-scans/${runId}/entries?limit=20&offset=${offset}`,
      { signal },
    ),
  register: (runId: string, path: string) =>
    apiClient.post<RegisteredReferenceOut>(`/dataset-scans/${runId}/entries/${path}/register`),
};

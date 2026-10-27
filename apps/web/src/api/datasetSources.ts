import { apiClient } from "./client";
import type {
  DatasetSourceList,
  DatasetSourceCreate,
  DatasetSourceUpdate,
  SourceExternalProtectionUpdate,
  SourceLifecycleImpact,
  SourceLifecycleChange,
  DatasetScanList,
  DatasetScanEntryList,
  StorageLocationList,
  StorageLocationOut,
  StorageSourceSummary,
  StorageDirectoryPage,
  StorageProbeOut,
  DatasetSourceOut,
  DatasetScanOut,
  RegisteredReferenceOut,
  DatasetRegistrationOut,
  DatasetRegistrationEntryList,
  DatasetSourceChangeList,
  SourceRevisionAccept,
  SourceRevisionResult,
  SourceMoveCandidateList,
  DatasetRegistrationConfirm,
  SourceSyncScheduleOut,
  SourceSyncScheduleUpdate,
  SourceSyncScheduleContinue,
  SourceRulesPreview,
  SourceRulesApply,
  SourceRulesImpact,
} from "./generated/types.gen";

export const datasetSourcesApi = {
  location: (id: string, signal?: AbortSignal) =>
    apiClient.get<StorageLocationOut>(`/storage-locations/${id}`, { signal }),
  previewRules: (id: string, payload: SourceRulesPreview, offset: number) =>
    apiClient.post<SourceRulesImpact>(
      `/dataset-sources/${id}/rules/preview?limit=20&offset=${offset}`,
      payload,
    ),
  applyRules: (id: string, payload: SourceRulesApply) =>
    apiClient.post<DatasetSourceOut>(`/dataset-sources/${id}/rules`, payload),
  syncSchedule: (id: string, signal?: AbortSignal) =>
    apiClient.get<SourceSyncScheduleOut | null>(`/dataset-sources/${id}/sync-schedule`, { signal }),
  configureSync: (id: string, payload: SourceSyncScheduleUpdate) =>
    apiClient.put<SourceSyncScheduleOut>(`/dataset-sources/${id}/sync-schedule`, payload),
  continueSync: (id: string, payload: SourceSyncScheduleContinue) =>
    apiClient.post<SourceSyncScheduleOut>(`/dataset-sources/${id}/sync-schedule/continue`, payload),
  locations: (signal?: AbortSignal) =>
    apiClient.get<StorageLocationList>("/storage-locations?limit=200", { signal }),
  locationPage: (offset: number, signal?: AbortSignal, limit = 20) =>
    apiClient.get<StorageLocationList>(`/storage-locations?limit=${limit}&offset=${offset}`, {
      signal,
    }),
  sourceSummary: (id: string, offset: number, jobsOffset: number, signal?: AbortSignal) =>
    apiClient.get<StorageSourceSummary>(
      `/storage-locations/${id}/source-summary?limit=20&offset=${offset}&jobs_limit=20&jobs_offset=${jobsOffset}`,
      { signal },
    ),
  source: (id: string, signal?: AbortSignal) =>
    apiClient.get<DatasetSourceOut>(`/dataset-sources/${id}`, { signal }),
  scan: (id: string, signal?: AbortSignal) =>
    apiClient.get<DatasetScanOut>(`/dataset-scans/${id}`, { signal }),
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
  update: (id: string, payload: DatasetSourceUpdate) =>
    apiClient.patch<DatasetSourceOut>(`/dataset-sources/${id}`, payload),
  updateProtection: (id: string, payload: SourceExternalProtectionUpdate) =>
    apiClient.put<DatasetSourceOut>(`/dataset-sources/${id}/external-protection`, payload),
  impact: (id: string) =>
    apiClient.get<SourceLifecycleImpact>(`/dataset-sources/${id}/lifecycle-impact`),
  lifecycle: (id: string, payload: SourceLifecycleChange) =>
    apiClient.post<DatasetSourceOut>(`/dataset-sources/${id}/lifecycle`, payload),
  activeRegistration: (sourceId: string, signal?: AbortSignal) =>
    apiClient.get<DatasetRegistrationOut | null>(
      `/dataset-sources/${sourceId}/active-registration`,
      { signal },
    ),
  registration: (scanId: string, signal?: AbortSignal) =>
    apiClient.get<DatasetRegistrationOut | null>(`/dataset-scans/${scanId}/registration`, {
      signal,
    }),
  preflight: (scanId: string) =>
    apiClient.post<DatasetRegistrationOut>(`/dataset-scans/${scanId}/registration`),
  confirmRegistration: (id: string, scope: DatasetRegistrationConfirm["scope"] = "all_safe") =>
    apiClient.post<DatasetRegistrationOut>(`/dataset-registrations/${id}/confirm`, { scope }),
  moveCandidates: (id: string, digest: string, offset: number, signal?: AbortSignal) =>
    apiClient.get<SourceMoveCandidateList>(
      `/dataset-registrations/${id}/changes/${digest}/move-candidates?limit=20&offset=${offset}`,
      { signal },
    ),
  relocate: (id: string, digest: string, payload: SourceRevisionAccept) =>
    apiClient.post<SourceRevisionResult>(
      `/dataset-registrations/${id}/changes/${digest}/relocate`,
      payload,
    ),
  revalidate: (id: string, digest: string, payload: SourceRevisionAccept) =>
    apiClient.post<SourceRevisionResult>(
      `/dataset-registrations/${id}/changes/${digest}/revalidate`,
      payload,
    ),
  acceptRevision: (id: string, digest: string, payload: SourceRevisionAccept) =>
    apiClient.post<SourceRevisionResult>(
      `/dataset-registrations/${id}/changes/${digest}/accept-revision`,
      payload,
    ),
  changes: (id: string, kind: string, offset: number, signal?: AbortSignal) =>
    apiClient.get<DatasetSourceChangeList>(
      `/dataset-registrations/${id}/changes?limit=20&offset=${offset}${kind ? `&kind=${kind}` : ""}`,
      { signal },
    ),
  resumeRegistration: (id: string) =>
    apiClient.post<DatasetRegistrationOut>(`/dataset-registrations/${id}/resume`),
  retryRegistrationEntry: (id: string, path: string) =>
    apiClient.post<DatasetRegistrationOut>(`/dataset-registrations/${id}/entries/${path}/retry`),
  registrationEntries: (id: string, offset: number, errorsOnly: boolean, signal?: AbortSignal) =>
    apiClient.get<DatasetRegistrationEntryList>(
      `/dataset-registrations/${id}/entries?limit=20&offset=${offset}${errorsOnly ? "&state=error" : ""}`,
      { signal },
    ),
  scans: (
    sourceId: string,
    signal?: AbortSignal,
    options: { offset?: number; complete?: boolean; active_only?: boolean; limit?: number } = {},
  ) =>
    apiClient.get<DatasetScanList>(
      `/dataset-sources/${sourceId}/scans?${new URLSearchParams({ limit: "20", ...Object.fromEntries(Object.entries(options).map(([key, value]) => [key, String(value)])) })}`,
      { signal },
    ),
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

import { apiClient } from "./client";
import type {
  ReferenceReplicaCandidateList,
  ReferenceReplicaList,
  ReferenceReplicaOut,
} from "./generated/types.gen";

export type { ReferenceReplicaCandidate, ReferenceReplicaOut } from "./generated/types.gen";

type Page = { limit: number; offset: number };
const base = (itemId: string) => `/dataset-items/${encodeURIComponent(itemId)}`;
const pageQuery = ({ limit, offset }: Page) =>
  new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
  });

export const referenceReplicasApi = {
  list: (itemId: string, page: Page, signal?: AbortSignal) =>
    apiClient.silentGet<ReferenceReplicaList>(
      `${base(itemId)}/reference-replicas?${pageQuery(page)}`,
      { signal },
    ),
  candidates: (itemId: string, page: Page, signal?: AbortSignal) =>
    apiClient.silentGet<ReferenceReplicaCandidateList>(
      `${base(itemId)}/reference-replica-candidates?${pageQuery(page)}`,
      { signal },
    ),
  add: (itemId: string, replicaItemId: string, priority: number, signal?: AbortSignal) =>
    apiClient.silentPost<ReferenceReplicaOut>(
      `${base(itemId)}/reference-replicas`,
      {
        replica_item_id: replicaItemId,
        priority,
      },
      { signal },
    ),
  update: (itemId: string, replicaId: string, priority: number, signal?: AbortSignal) =>
    apiClient.patch<ReferenceReplicaOut>(
      `${base(itemId)}/reference-replicas/${encodeURIComponent(replicaId)}`,
      { priority },
      { signal },
    ),
  remove: (itemId: string, replicaId: string, signal?: AbortSignal) =>
    apiClient.silentDelete<void>(
      `${base(itemId)}/reference-replicas/${encodeURIComponent(replicaId)}`,
      { signal },
    ),
};

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { storageApi } from "../api/storage";
import type { VideoAssetRetryPayload } from "../api/storage";
import { useAuthStore } from "@/stores/authStore";
import { useSessionRevision } from "./useSessionGuard";
import { getAuthSessionRevision } from "@/stores/authSession";

export function useStorageHealth() {
  return useQuery({
    queryKey: ["storage-health"],
    queryFn: storageApi.health,
    retry: false,
  });
}

export function useStorageBuckets(enabled = true) {
  const owner = useAuthStore((s) => `${s.user?.id}/${s.sessionId}/${s.user?.role}`);
  const revision = useSessionRevision();
  return useQuery({
    queryKey: ["storage-buckets", owner, revision],
    queryFn: ({ signal }) => storageApi.buckets(signal),
    retry: false,
    enabled,
    refetchInterval: (query) =>
      ["pending", "running"].includes(query.state.data?.inventory_status ?? "") ? 3000 : false,
  });
}

export function useStartStorageInventory() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: storageApi.startInventory,
    onMutate: () => getAuthSessionRevision(),
    onSuccess: (_data, _variables, revision) => {
      if (getAuthSessionRevision() !== revision) return;
      void qc.invalidateQueries({ queryKey: ["storage-buckets"] });
      void qc.invalidateQueries({ queryKey: ["async-jobs"] });
    },
  });
}

export function useVideoAssetFailures(limit = 50, offset = 0) {
  const owner = useAuthStore((s) => `${s.user?.id}/${s.sessionId}/${s.user?.role}`);
  const revision = useSessionRevision();
  return useQuery({
    queryKey: ["storage-video-asset-failures", owner, revision, limit, offset],
    queryFn: ({ signal }) => storageApi.videoAssetFailures(limit, offset, signal),
    retry: false,
  });
}

export function useRetryVideoAsset() {
  const qc = useQueryClient();
  return useMutation({
    onMutate: () => getAuthSessionRevision(),
    mutationFn: (payload: VideoAssetRetryPayload) => storageApi.retryVideoAsset(payload),
    onSuccess: (_data, _variables, revision) => {
      if (getAuthSessionRevision() !== revision) return;
      qc.invalidateQueries({ queryKey: ["storage-video-asset-failures"] });
      void qc.invalidateQueries({ queryKey: ["async-jobs"] });
    },
  });
}

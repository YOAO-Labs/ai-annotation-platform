/**
 * ADR-0044 · superadmin 全局 ML backend 注册表 mutation hooks。
 *
 * 全局 backend (project_id=null) 走 /admin/ml-integrations/registry，与项目作用域的
 * useMLBackends（/projects/{id}/ml-backends）解耦。成功后同时失效全局列表 (all) 与
 * 模型市场总览 (overview)，让两处视图一起刷新。
 */
import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import {
  adminMlIntegrationsApi,
  type CapabilityDriftAcceptRequest,
  type MLBackendRegistryCreatePayload,
  type MLBackendRegistryUpdatePayload,
  type ServicePoolCreateRequest,
  type ServicePoolPatchRequest,
  type ServicePoolMemberPutRequest,
} from "@/api/adminMlIntegrations";

/**
 * Invalidate every query that reflects registry / pool state.
 *
 * extended to also drop `topology` + `runtime-snapshot` so pool /
 * member mutations (drain / resume / edit / remove) refresh the new role-aware
 * registry views, not just the legacy `/all` list. Plan §9.3 requires precise
 * invalidation — no global `invalidateQueries({})`.
 */
export function invalidateRegistryQueries(qc: QueryClient) {
  qc.invalidateQueries({ queryKey: ["admin", "ml-integrations", "all"] });
  qc.invalidateQueries({ queryKey: ["admin", "ml-integrations", "overview"] });
  qc.invalidateQueries({ queryKey: ["admin", "ml-integrations", "gpu-resources"] });
  qc.invalidateQueries({ queryKey: ["admin", "ml-integrations", "observe"] });
  // registry views are driven by topology + runtime snapshot.
  qc.invalidateQueries({ queryKey: ["admin", "ml-integrations", "topology"] });
  qc.invalidateQueries({ queryKey: ["admin", "ml-integrations", "runtime-snapshot"] });
}

export function useCreateRegistry() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: MLBackendRegistryCreatePayload) =>
      adminMlIntegrationsApi.createRegistry(payload),
    onSuccess: () => invalidateRegistryQueries(qc),
  });
}

export function useUpdateRegistry() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: MLBackendRegistryUpdatePayload }) =>
      adminMlIntegrationsApi.updateRegistry(id, payload),
    onSuccess: () => invalidateRegistryQueries(qc),
  });
}

export function useDeleteRegistry() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => adminMlIntegrationsApi.deleteRegistry(id),
    onSuccess: () => invalidateRegistryQueries(qc),
  });
}

export function useRegistryHealth() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => adminMlIntegrationsApi.registryHealth(id),
    onSuccess: () => invalidateRegistryQueries(qc),
  });
}

export function useRegistryUnload() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => adminMlIntegrationsApi.registryUnload(id),
    onSuccess: () => invalidateRegistryQueries(qc),
  });
}

// ── service-pool / member mutations (ADR-0050) ────────────────
// Plan §8 + §10: drain / resume / unload gate at the member scope; pool edits
// and member add/remove use the same invalidation envelope as registry CRUD.

export function useCreateServicePool() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: ServicePoolCreateRequest) =>
      adminMlIntegrationsApi.createServicePool(payload),
    onSuccess: () => invalidateRegistryQueries(qc),
  });
}

export function usePatchServicePool() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ poolId, payload }: { poolId: string; payload: ServicePoolPatchRequest }) =>
      adminMlIntegrationsApi.patchServicePool(poolId, payload),
    onSuccess: () => invalidateRegistryQueries(qc),
  });
}

export function useDeleteServicePool() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (poolId: string) => adminMlIntegrationsApi.deleteServicePool(poolId),
    onSuccess: () => invalidateRegistryQueries(qc),
  });
}

export function usePutPoolMember() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      poolId,
      registryId,
      payload,
    }: {
      poolId: string;
      registryId: string;
      payload: ServicePoolMemberPutRequest;
    }) => adminMlIntegrationsApi.addOrUpdatePoolMember(poolId, registryId, payload),
    onSuccess: () => invalidateRegistryQueries(qc),
  });
}

export function useRemovePoolMember() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ poolId, registryId }: { poolId: string; registryId: string }) =>
      adminMlIntegrationsApi.removePoolMember(poolId, registryId),
    onSuccess: () => invalidateRegistryQueries(qc),
  });
}

/** Member active → draining (stop accepting new leases). Idempotent. */
export function useDrainPoolMember() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ poolId, registryId }: { poolId: string; registryId: string }) =>
      adminMlIntegrationsApi.drainPoolMember(poolId, registryId),
    onSuccess: () => invalidateRegistryQueries(qc),
  });
}

/** Member draining → active (resume routing). Idempotent. */
export function useResumePoolMember() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ poolId, registryId }: { poolId: string; registryId: string }) =>
      adminMlIntegrationsApi.resumePoolMember(poolId, registryId),
    onSuccess: () => invalidateRegistryQueries(qc),
  });
}

export function useCapabilityDriftPreview(poolId: string, registryId: string, enabled: boolean) {
  return useQuery({
    queryKey: ["admin", "ml-integrations", "capability-drift", poolId, registryId],
    queryFn: () => adminMlIntegrationsApi.previewCapabilityDrift(poolId, registryId),
    enabled,
    staleTime: 0,
  });
}

export function useAcceptCapabilityDrift() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      poolId,
      registryId,
      payload,
    }: {
      poolId: string;
      registryId: string;
      payload: CapabilityDriftAcceptRequest;
    }) => adminMlIntegrationsApi.acceptCapabilityDrift(poolId, registryId, payload),
    onSuccess: (_data, variables) => {
      invalidateRegistryQueries(qc);
      qc.removeQueries({
        queryKey: [
          "admin",
          "ml-integrations",
          "capability-drift",
          variables.poolId,
          variables.registryId,
        ],
      });
    },
  });
}

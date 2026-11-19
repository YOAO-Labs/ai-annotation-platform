import { useQuery, useMutation, useQueryClient, type QueryClient } from "@tanstack/react-query";
import {
  mlBackendsApi,
  type MLBackendVariant,
  type ProjectMLBackendEnablementPayload,
} from "@/api/ml-backends";

function invalidateBackendQueries(qc: QueryClient, projectId: string) {
  qc.invalidateQueries({ queryKey: ["ml-backends", projectId] });
  qc.invalidateQueries({ queryKey: ["projects"] });
  qc.invalidateQueries({ queryKey: ["project", projectId] });
  qc.invalidateQueries({ queryKey: ["admin", "ml-integrations", "overview"] });
}

export function useMLBackends(projectId: string | undefined) {
  return useQuery({
    queryKey: ["ml-backends", projectId],
    queryFn: () => mlBackendsApi.list(projectId!),
    enabled: !!projectId,
  });
}

// ADR-0044 · 全部全局 backend + 本项目启用态/覆盖 (项目设置启用清单)。
// query key 复用 ["ml-backends", projectId] 前缀, 让 invalidateBackendQueries 一并失效。
export function useAvailableMLBackends(projectId: string | undefined) {
  return useQuery({
    queryKey: ["ml-backends", projectId, "available"],
    queryFn: () => mlBackendsApi.listAvailable(projectId!),
    enabled: !!projectId,
  });
}

export function useSetMLBackendEnablement(projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      registryId,
      payload,
    }: {
      registryId: string;
      payload: ProjectMLBackendEnablementPayload;
    }) => mlBackendsApi.setEnablement(projectId, registryId, payload),
    onSuccess: () => invalidateBackendQueries(qc, projectId),
  });
}

export function useMLBackendHealth(projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (backendId: string) => mlBackendsApi.health(projectId, backendId),
    onSuccess: () => invalidateBackendQueries(qc, projectId),
  });
}

export function useMLBackendUnload(projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (backendId: string) => mlBackendsApi.unload(projectId, backendId),
    onSuccess: () => invalidateBackendQueries(qc, projectId),
  });
}

export function useMLBackendReload(projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    // variant 可选; 缺省预热默认变体 (旧「重载」按钮行为不变).
    // taskType 可选; "video" 预热独立 video tracker 池.
    mutationFn: ({
      backendId,
      variant,
      taskType,
    }: {
      backendId: string;
      variant?: MLBackendVariant;
      taskType?: "image" | "video";
    }) => mlBackendsApi.reload(projectId, backendId, variant, taskType),
    onSuccess: () => invalidateBackendQueries(qc, projectId),
  });
}

export function useMLBackendWarmup(projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ backendId, body }: { backendId: string; body?: Record<string, unknown> }) =>
      mlBackendsApi.warmup(projectId, backendId, body),
    onSuccess: () => invalidateBackendQueries(qc, projectId),
  });
}

export function useInteractiveAnnotate(projectId: string, backendId: string | undefined) {
  return useMutation({
    mutationFn: (payload: { task_id: string; context: Record<string, unknown> }) => {
      if (!backendId) throw new Error("No interactive backend available");
      return mlBackendsApi.interactiveAnnotate(projectId, backendId, payload);
    },
  });
}

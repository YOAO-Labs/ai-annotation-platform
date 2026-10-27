import { getAuthSessionRevision } from "@/stores/authSession";
import { useSessionRevision } from "./useSessionGuard";
import { useAuthStore } from "@/stores/authStore";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  storageConnectionsApi,
  type StorageConnectionCreatePayload,
  type StorageConnectionUpdatePayload,
} from "@/api/storageConnections";

function mutationOwner() {
  const state = useAuthStore.getState();
  return {
    revision: getAuthSessionRevision(),
    session: `${state.user?.id}/${state.sessionId}/${state.user?.role}`,
  };
}
function mutationIsCurrent(context: ReturnType<typeof mutationOwner> | undefined) {
  const current = mutationOwner();
  return !!context && context.revision === current.revision && context.session === current.session;
}

export function useStorageConnections() {
  const session = useAuthStore((s) => `${s.user?.id}/${s.sessionId}/${s.user?.role}`);
  const canManage = useAuthStore(
    (s) => s.user?.role === "super_admin" || s.user?.role === "project_admin",
  );
  const revision = useSessionRevision();
  return useQuery({
    queryKey: ["storage-connections", session, revision],
    queryFn: ({ signal }) => storageConnectionsApi.list(signal),
    enabled: canManage,
  });
}

export function useConnectorAllowlist() {
  const session = useAuthStore((s) => `${s.user?.id}/${s.sessionId}/${s.user?.role}`);
  const revision = useSessionRevision();
  return useQuery({
    queryKey: ["storage-connections", "allowlist", session, revision],
    queryFn: ({ signal }) => storageConnectionsApi.getAllowlist(signal),
  });
}

export function useUpdateConnectorAllowlist() {
  const qc = useQueryClient();
  return useMutation({
    onMutate: mutationOwner,
    mutationFn: (entries: string[]) => storageConnectionsApi.updateAllowlist(entries),
    onSuccess: (data, _variables, context) => {
      if (!mutationIsCurrent(context) || !context) return;
      qc.setQueryData(
        ["storage-connections", "allowlist", context.session, context.revision],
        data,
      );
    },
  });
}

export function useResetConnectorAllowlist() {
  const qc = useQueryClient();
  return useMutation({
    onMutate: mutationOwner,
    mutationFn: storageConnectionsApi.resetAllowlist,
    onSuccess: (data, _variables, context) => {
      if (!mutationIsCurrent(context) || !context) return;
      qc.setQueryData(
        ["storage-connections", "allowlist", context.session, context.revision],
        data,
      );
    },
  });
}

export function useDeploymentSftpPreset(enabled: boolean) {
  const session = useAuthStore((s) => `${s.user?.id}/${s.sessionId}/${s.user?.role}`);
  const revision = useSessionRevision();
  return useQuery({
    queryKey: ["storage-connections", "deployment-sftp-preset", session, revision],
    queryFn: ({ signal }) => storageConnectionsApi.getDeploymentSftpPreset(signal),
    enabled,
  });
}

export function useCreateStorageConnection() {
  const qc = useQueryClient();
  return useMutation({
    onMutate: mutationOwner,
    mutationFn: (payload: StorageConnectionCreatePayload) => storageConnectionsApi.create(payload),
    onSuccess: (_data, _variables, context) => {
      if (!mutationIsCurrent(context) || !context) return;
      qc.invalidateQueries({
        queryKey: ["storage-connections", context.session, context.revision],
      });
      qc.invalidateQueries({ queryKey: ["storage-source-locations"] });
      qc.invalidateQueries({ queryKey: ["storage-location"] });
    },
  });
}

export function useUpdateStorageConnection() {
  const qc = useQueryClient();
  return useMutation({
    onMutate: mutationOwner,
    mutationFn: ({ id, payload }: { id: string; payload: StorageConnectionUpdatePayload }) =>
      storageConnectionsApi.update(id, payload),
    onSuccess: (_data, _variables, context) => {
      if (!mutationIsCurrent(context) || !context) return;
      qc.invalidateQueries({
        queryKey: ["storage-connections", context.session, context.revision],
      });
      qc.invalidateQueries({ queryKey: ["storage-source-locations"] });
      qc.invalidateQueries({ queryKey: ["storage-location"] });
    },
  });
}

export function useDeleteStorageConnection() {
  const qc = useQueryClient();
  return useMutation({
    onMutate: mutationOwner,
    mutationFn: (id: string) => storageConnectionsApi.delete(id),
    onSuccess: (_data, _variables, context) => {
      if (!mutationIsCurrent(context) || !context) return;
      qc.invalidateQueries({
        queryKey: ["storage-connections", context.session, context.revision],
      });
      qc.invalidateQueries({ queryKey: ["storage-source-locations"] });
      qc.invalidateQueries({ queryKey: ["storage-location"] });
    },
  });
}

export function useTestStorageConnection() {
  return useMutation({
    mutationFn: (id: string) => storageConnectionsApi.test(id),
  });
}

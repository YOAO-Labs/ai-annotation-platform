import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useAuthStore } from "@/stores/authStore";
import {
  usersApi,
  type BulkGroupAssignmentPayload,
  type BulkInviteItemPayload,
  type InvitePayload,
  type OffboardingCommitRequest,
  type UserStatusFilter,
} from "../api/users";

export function useUsers(
  params?: { role?: string; project_id?: string; status?: UserStatusFilter },
  enabled = true,
) {
  const ownerId = useAuthStore((state) => state.user?.id);
  return useQuery({
    queryKey: ["users", ownerId, params],
    queryFn: ({ signal }) => usersApi.list(params, signal),
    enabled,
  });
}

export function useUserPage(params: import("@/api/users").UserPageParams) {
  const ownerId = useAuthStore((state) => state.user?.id);
  return useQuery({
    queryKey: ["users", "page", ownerId, params],
    queryFn: ({ signal }) => usersApi.page(params, signal),
  });
}

export function useOffboardingPreview(userId: string | null, enabled = true) {
  return useQuery({
    queryKey: ["users", "offboarding-preview", userId],
    queryFn: () => usersApi.offboardingPreview(userId as string),
    enabled: enabled && !!userId,
    retry: false,
  });
}

export function useOffboardUser() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ userId, payload }: { userId: string; payload: OffboardingCommitRequest }) =>
      usersApi.offboard(userId, payload),
    onSuccess: (_result, variables) => {
      qc.invalidateQueries({ queryKey: ["users"] });
      qc.invalidateQueries({ queryKey: ["users", "offboarding-preview", variables.userId] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["project-members"] });
      qc.invalidateQueries({ queryKey: ["projects"] });
      qc.invalidateQueries({ queryKey: ["project"] });
      qc.invalidateQueries({ queryKey: ["batches"] });
      qc.invalidateQueries({ queryKey: ["batch"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
}

export function useReactivateUser() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ userId, reason }: { userId: string; reason?: string }) =>
      usersApi.reactivate(userId, reason ? { reason } : undefined),
    onSuccess: (_user, variables) => {
      qc.invalidateQueries({ queryKey: ["users"] });
      qc.invalidateQueries({ queryKey: ["users", "offboarding-preview", variables.userId] });
    },
  });
}

export function useUsersStats(params?: {
  role?: string;
  project_id?: string;
  group_id?: string;
  status?: UserStatusFilter;
  search?: string;
}) {
  const ownerId = useAuthStore((state) => state.user?.id);
  return useQuery({
    queryKey: ["users", "stats", ownerId, params],
    queryFn: ({ signal }) =>
      usersApi.stats(
        {
          role: params?.role,
          project_id: params?.project_id,
          group_id: params?.group_id,
          status: params?.status,
          search: params?.search,
        },
        signal,
      ),
    refetchInterval: 60_000,
  });
}

export function usePreviewBulkInviteUsers() {
  return useMutation({
    mutationFn: (items: BulkInviteItemPayload[]) => usersApi.previewBulkInvite(items),
  });
}

export function useBulkInviteUsers() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (items: BulkInviteItemPayload[]) => usersApi.bulkInvite(items),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["users"] });
      qc.invalidateQueries({ queryKey: ["invitations"] });
    },
  });
}

export function usePreviewBulkUserGroup() {
  return useMutation({
    mutationFn: (payload: BulkGroupAssignmentPayload) => usersApi.previewBulkGroup(payload),
  });
}

export function useBulkUserGroup() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: BulkGroupAssignmentPayload) => usersApi.bulkGroup(payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["users"] });
      qc.invalidateQueries({ queryKey: ["groups"] });
    },
  });
}

export function useInviteUser() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: InvitePayload) => usersApi.invite(payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["users"] });
      qc.invalidateQueries({ queryKey: ["invitations"] });
    },
  });
}

export function useChangeUserRole() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ userId, role }: { userId: string; role: string }) =>
      usersApi.changeRole(userId, role),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["users"] }),
  });
}

export function useDeactivateUser() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (userId: string) => usersApi.deactivate(userId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["users"] }),
  });
}

export interface DeleteUserVariables {
  userId: string;
  transferToUserId?: string;
}

export function useDeleteUser() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ userId, transferToUserId }: DeleteUserVariables) =>
      usersApi.remove(
        userId,
        transferToUserId ? { transfer_to_user_id: transferToUserId } : undefined,
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["users"] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
    },
  });
}

export function useAssignUserGroup() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ userId, groupId }: { userId: string; groupId: string | null }) =>
      usersApi.assignGroup(userId, groupId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["users"] }),
  });
}

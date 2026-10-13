import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  projectsApi,
  type ProjectCreatePayload,
  type ProjectUpdatePayload,
  type ProjectListParams,
  type ProjectPageParams,
  type ProjectRole,
  type ProjectMemberRolePreviewPayload,
  type ProjectMemberRoleChangePayload,
} from "../api/projects";
import { useAuthStore } from "@/stores/authStore";

export function useProjects(params?: ProjectListParams) {
  const userId = useAuthStore((state) => state.user?.id ?? null);
  const tokenEpoch = useAuthStore((state) => state.token);
  return useQuery({
    queryKey: ["projects", params, userId, tokenEpoch],
    queryFn: ({ signal }) => projectsApi.list(params, { signal }),
    enabled: Boolean(userId),
  });
}

export function useProjectPage(params: ProjectPageParams) {
  const userId = useAuthStore((state) => state.user?.id ?? null);
  const tokenEpoch = useAuthStore((state) => state.token);
  return useQuery({
    queryKey: ["projects", "page", params, userId, tokenEpoch],
    queryFn: ({ signal }) => projectsApi.page(params, { signal }),
    staleTime: 0,
    enabled: Boolean(userId),
  });
}

export function useProjectStats() {
  const userId = useAuthStore((state) => state.user?.id ?? null);
  const tokenEpoch = useAuthStore((state) => state.token);
  return useQuery({
    queryKey: ["project-stats", userId, tokenEpoch],
    queryFn: projectsApi.stats,
    enabled: Boolean(userId),
  });
}

export function useProject(id: string) {
  return useQuery({
    queryKey: ["project", id],
    queryFn: () => projectsApi.get(id),
    enabled: !!id,
  });
}

export function useProjectReadiness(id: string | undefined) {
  return useQuery({
    queryKey: ["project-readiness", id],
    refetchInterval: (query) => (query.state.data?.task_creation_active_jobs ? 5_000 : 30_000),
    queryFn: () => projectsApi.getReadiness(id!),
    enabled: Boolean(id),
  });
}

export function useCreateProject() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: ProjectCreatePayload) => projectsApi.create(payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      qc.invalidateQueries({ queryKey: ["project-stats"] });
    },
  });
}

export function useUpdateProject(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: ProjectUpdatePayload) => projectsApi.update(id, payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      qc.invalidateQueries({ queryKey: ["project", id] });
      qc.invalidateQueries({ queryKey: ["project-stats"] });
    },
  });
}

export function useDeleteProject() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => projectsApi.remove(id),
    onSuccess: (_, id) => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      qc.invalidateQueries({ queryKey: ["project", id] });
      qc.invalidateQueries({ queryKey: ["project-stats"] });
    },
  });
}

export function useTransferProject(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (new_owner_id: string) => projectsApi.transfer(id, new_owner_id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      qc.invalidateQueries({ queryKey: ["project", id] });
    },
  });
}

// B-13 · 重命名项目类别 (后端原子改 classes_config + annotations.class_name)
// v0.10.17 · 加 tool_unit_id 限定工具单位 (强隔离下不同 unit 同名是独立记录).
export function useRenameClass(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { old_name: string; new_name: string; tool_unit_id?: string }) =>
      projectsApi.renameClass(id, vars.old_name, vars.new_name, vars.tool_unit_id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["project", id] });
      qc.invalidateQueries({ queryKey: ["projects"] });
      // 重命名会迁移 annotations.class_name → 让工作台 / dashboard 数据失效重拉
      qc.invalidateQueries({ queryKey: ["annotations"] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
    },
  });
}

export function useProjectMembers(id: string) {
  return useQuery({
    queryKey: ["project-members", id],
    queryFn: () => projectsApi.listMembers(id),
    enabled: !!id,
  });
}

/** 讨论区 @ 候选：负责人 + 超管 + 成员；仅需项目可见权限。
 *  挂在 `["project", id]` 下，成员增删 / 负责人转移已有的项目失效会自动带上它。 */
export function useProjectMentionCandidates(id: string) {
  return useQuery({
    queryKey: ["project", id, "mention-candidates"],
    queryFn: () => projectsApi.mentionCandidates(id),
    enabled: !!id,
  });
}

export function useAddProjectMember(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: { user_id: string; role: ProjectRole }) =>
      projectsApi.addMember(id, payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["project-members", id] });
      qc.invalidateQueries({ queryKey: ["project", id] });
      qc.invalidateQueries({ queryKey: ["projects"] });
      qc.invalidateQueries({ queryKey: ["project-access", id] });
    },
  });
}

/**
 * B1 · read-only role-change preview.  Returns blockers, the affected resource
 * snapshot and an opaque token that the write must echo.
 */
export function usePreviewProjectMemberRole(id: string) {
  return useMutation({
    mutationFn: (vars: { memberId: string; payload: ProjectMemberRolePreviewPayload }) =>
      projectsApi.previewMemberRole(id, vars.memberId, vars.payload),
  });
}

export function useChangeProjectMemberRole(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { memberId: string; payload: ProjectMemberRoleChangePayload }) =>
      projectsApi.changeMemberRole(id, vars.memberId, vars.payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["project-members", id] });
      qc.invalidateQueries({ queryKey: ["project", id] });
      qc.invalidateQueries({ queryKey: ["projects"] });
      qc.invalidateQueries({ queryKey: ["project-access", id] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["batches"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
}

export function useRemoveProjectMember(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (memberId: string) => projectsApi.removeMember(id, memberId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["project-members", id] });
      qc.invalidateQueries({ queryKey: ["project", id] });
      qc.invalidateQueries({ queryKey: ["projects"] });
    },
  });
}

import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { projectsApi, type ProjectAccessResponse, type ProjectCapability } from "@/api/projects";
import { useAuthStore } from "@/stores/authStore";

export function projectAccessQueryKey(projectId: string | undefined, userId: string | null) {
  return ["project-access", projectId ?? null, userId] as const;
}

export interface ProjectAccessState {
  access: ProjectAccessResponse | undefined;
  capabilities: ReadonlySet<ProjectCapability>;
  hasCapability: (...capabilities: ProjectCapability[]) => boolean;
  projectRole: ProjectAccessResponse["project_role"];
  membershipVersion: number | null;
  isManager: boolean;
  isPending: boolean;
  isLoading: boolean;
  isError: boolean;
  error: unknown;
}

const NO_CAPABILITIES: ReadonlySet<ProjectCapability> = new Set<ProjectCapability>();

export function useProjectAccess(
  projectId: string | undefined,
  options?: { enabled?: boolean },
): ProjectAccessState & Pick<UseQueryResult<ProjectAccessResponse>, "refetch"> {
  const userId = useAuthStore((state) => state.user?.id ?? null);
  const enabled = (options?.enabled ?? true) && Boolean(projectId) && Boolean(userId);

  const query = useQuery({
    queryKey: projectAccessQueryKey(projectId, userId),
    queryFn: ({ signal }) => projectsApi.getAccess(projectId!, { signal }),
    enabled,
    staleTime: 30_000,
  });

  const candidate = query.isSuccess && !query.isError ? query.data : undefined;
  const access =
    enabled && candidate && candidate.project_id === projectId && candidate.user_id === userId
      ? candidate
      : undefined;
  const capabilities: ReadonlySet<ProjectCapability> = access?.capabilities
    ? new Set(access.capabilities)
    : NO_CAPABILITIES;

  return {
    access,
    capabilities,
    hasCapability: (...required: ProjectCapability[]) =>
      required.every((capability) => capabilities.has(capability)),
    projectRole: access?.project_role ?? null,
    membershipVersion: access?.membership_version ?? null,
    isManager: access?.is_manager ?? false,
    isPending: query.isPending,
    isLoading: query.isLoading,
    isError: query.isError,
    error: query.error,
    refetch: query.refetch,
  };
}

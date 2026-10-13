import { useQuery } from "@tanstack/react-query";
import { useAuthStore } from "@/stores/authStore";
import {
  projectPerformanceApi,
  type ProjectPerformanceQuery,
  type ProjectMemberPerformanceDetail,
  type ProjectMemberPerformanceEventsResponse,
  type ProjectMembersPerformanceResponse,
} from "@/api/projectPerformance";

function performanceKey(query: ProjectPerformanceQuery) {
  return {
    from: query.from ?? "",
    to: query.to ?? "",
    timezone: query.timezone ?? "",
    work_type: query.work_type,
    account_status: query.account_status,
    include_historical: query.include_historical,
    q: query.q ?? "",
    sort: query.sort ?? "",
    cursor: query.cursor ?? null,
    limit: query.limit ?? null,
  };
}

export function useProjectMembersPerformance(
  projectId: string | undefined,
  query: ProjectPerformanceQuery,
  enabled = true,
) {
  const ownerId = useAuthStore((state) => state.user?.id);
  return useQuery<ProjectMembersPerformanceResponse>({
    queryKey: ["project-performance", "members", ownerId, projectId, performanceKey(query)],
    queryFn: ({ signal }) => projectPerformanceApi.getMembers(projectId!, query, signal),
    enabled: Boolean(projectId && ownerId && enabled),
  });
}

export function useProjectMemberPerformance(
  projectId: string | undefined,
  userId: string | null,
  query: ProjectPerformanceQuery,
  enabled = true,
) {
  const ownerId = useAuthStore((state) => state.user?.id);
  return useQuery<ProjectMemberPerformanceDetail>({
    queryKey: ["project-performance", "member", ownerId, projectId, userId, performanceKey(query)],
    queryFn: ({ signal }) => projectPerformanceApi.getMember(projectId!, userId!, query, signal),
    enabled: Boolean(projectId && userId && ownerId && enabled),
  });
}

export function useProjectMemberPerformanceEvents(
  projectId: string | undefined,
  userId: string | null,
  query: ProjectPerformanceQuery,
  enabled = true,
) {
  const ownerId = useAuthStore((state) => state.user?.id);
  return useQuery<ProjectMemberPerformanceEventsResponse>({
    queryKey: [
      "project-performance",
      "member-events",
      ownerId,
      projectId,
      userId,
      performanceKey(query),
    ],
    queryFn: ({ signal }) =>
      projectPerformanceApi.getMemberEvents(projectId!, userId!, query, signal),
    enabled: Boolean(projectId && userId && ownerId && enabled),
  });
}

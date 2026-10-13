import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  taskViewsApi,
  type DataManagerEntityQueryPayload,
  type DataManagerEntityScope,
  type ProjectTaskQueryPayload,
  type ProjectTaskViewPayload,
  type ProjectTaskViewUpdatePayload,
} from "@/api/taskViews";
import { useAuthStore } from "@/stores/authStore";

export function useTaskViews(
  projectId: string | undefined,
  entityScope: DataManagerEntityScope = "tasks",
) {
  const userId = useAuthStore((state) => state.user?.id ?? null);
  const tokenEpoch = useAuthStore((state) => state.token);
  return useQuery({
    queryKey: ["task-views", projectId, entityScope, userId, tokenEpoch],
    queryFn: ({ signal }) => taskViewsApi.list(projectId!, entityScope, { signal }),
    enabled: !!projectId && !!userId,
  });
}

export function useProjectTaskQuery(
  projectId: string | undefined,
  payload: ProjectTaskQueryPayload,
  enabled = true,
) {
  const userId = useAuthStore((state) => state.user?.id ?? null);
  const tokenEpoch = useAuthStore((state) => state.token);
  return useQuery({
    queryKey: ["project-task-query", projectId, payload, userId, tokenEpoch],
    queryFn: ({ signal }) => taskViewsApi.query(projectId!, payload, { signal }),
    enabled: !!projectId && !!userId && enabled,
    placeholderData: (previousData, previousQuery) =>
      previousQuery?.queryKey[1] === projectId &&
      previousQuery?.queryKey[3] === userId &&
      previousQuery?.queryKey[4] === tokenEpoch
        ? previousData
        : undefined,
  });
}

export function useDataManagerSchema(
  projectId: string | undefined,
  entityScope: DataManagerEntityScope = "tasks",
) {
  const userId = useAuthStore((state) => state.user?.id ?? null);
  const tokenEpoch = useAuthStore((state) => state.token);
  return useQuery({
    queryKey: ["data-manager-schema", projectId, entityScope, userId, tokenEpoch],
    queryFn: ({ signal }) => taskViewsApi.schema(projectId!, entityScope, { signal }),
    enabled: !!projectId && !!userId,
    staleTime: 60_000,
    // The Data Manager renders its schema error with an explicit retry action.
    // Without this, a failed schema query would be retried every time the
    // surrounding page remounts that error branch, cycling loading ↔ error
    // instead of letting the reader press retry.
    retryOnMount: false,
  });
}

export function useDataManagerObjects(
  projectId: string | undefined,
  payload: Omit<DataManagerEntityQueryPayload, "cursor">,
  enabled = true,
) {
  const userId = useAuthStore((state) => state.user?.id ?? null);
  const tokenEpoch = useAuthStore((state) => state.token);
  return useInfiniteQuery({
    queryKey: ["data-manager-objects", projectId, payload, userId, tokenEpoch],
    queryFn: ({ pageParam, signal }) =>
      taskViewsApi.queryObjects(projectId!, { ...payload, cursor: pageParam }, { signal }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled: !!projectId && !!userId && enabled,
  });
}

export function useDataManagerTracks(
  projectId: string | undefined,
  payload: Omit<DataManagerEntityQueryPayload, "cursor">,
  enabled = true,
) {
  const userId = useAuthStore((state) => state.user?.id ?? null);
  const tokenEpoch = useAuthStore((state) => state.token);
  return useInfiniteQuery({
    queryKey: ["data-manager-tracks", projectId, payload, userId, tokenEpoch],
    queryFn: ({ pageParam, signal }) =>
      taskViewsApi.queryTracks(projectId!, { ...payload, cursor: pageParam }, { signal }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled: !!projectId && !!userId && enabled,
  });
}

export function useDataManagerObjectDetail(
  projectId: string | undefined,
  annotationId: string | null,
) {
  const userId = useAuthStore((state) => state.user?.id ?? null);
  const tokenEpoch = useAuthStore((state) => state.token);
  return useQuery({
    queryKey: ["data-manager-object-detail", projectId, annotationId, userId, tokenEpoch],
    queryFn: ({ signal }) => taskViewsApi.objectDetail(projectId!, annotationId!, { signal }),
    enabled: !!projectId && !!annotationId && !!userId,
  });
}

export function useDataManagerTrackDetail(projectId: string | undefined, trackRef: string | null) {
  const userId = useAuthStore((state) => state.user?.id ?? null);
  const tokenEpoch = useAuthStore((state) => state.token);
  return useQuery({
    queryKey: ["data-manager-track-detail", projectId, trackRef, userId, tokenEpoch],
    queryFn: ({ signal }) => taskViewsApi.trackDetail(projectId!, trackRef!, { signal }),
    enabled: !!projectId && !!trackRef && !!userId,
  });
}

export function useDataManagerSummary(
  projectId: string | undefined,
  filterJson: Record<string, unknown>,
  enabled = true,
) {
  const userId = useAuthStore((state) => state.user?.id ?? null);
  const tokenEpoch = useAuthStore((state) => state.token);
  return useQuery({
    queryKey: ["data-manager-summary", projectId, filterJson, userId, tokenEpoch],
    queryFn: ({ signal }) => taskViewsApi.summary(projectId!, filterJson, { signal }),
    enabled: !!projectId && !!userId && enabled,
    placeholderData: (previousData, previousQuery) =>
      previousQuery?.queryKey[1] === projectId &&
      previousQuery?.queryKey[3] === userId &&
      previousQuery?.queryKey[4] === tokenEpoch
        ? previousData
        : undefined,
  });
}

export function useDataManagerMatches(
  projectId: string | undefined,
  taskId: string | null,
  filterJson: Record<string, unknown>,
  enabled = true,
) {
  const userId = useAuthStore((state) => state.user?.id ?? null);
  const tokenEpoch = useAuthStore((state) => state.token);
  return useQuery({
    queryKey: ["data-manager-matches", projectId, taskId, filterJson, userId, tokenEpoch],
    queryFn: ({ signal }) => taskViewsApi.matches(projectId!, taskId!, filterJson, { signal }),
    enabled: !!projectId && !!taskId && !!userId && enabled,
  });
}

export function useCreateTaskView(projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: ProjectTaskViewPayload) => taskViewsApi.create(projectId, payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["task-views", projectId] });
    },
  });
}

export function useUpdateTaskView(projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ viewId, payload }: { viewId: string; payload: ProjectTaskViewUpdatePayload }) =>
      taskViewsApi.update(projectId, viewId, payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["task-views", projectId] });
      qc.invalidateQueries({ queryKey: ["project-task-query", projectId] });
    },
  });
}

export function useDeleteTaskView(projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (viewId: string) => taskViewsApi.remove(projectId, viewId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["task-views", projectId] });
    },
  });
}

export function useCopyTaskView(projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ viewId, name }: { viewId: string; name?: string }) =>
      taskViewsApi.copy(projectId, viewId, { name }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["task-views", projectId] });
    },
  });
}

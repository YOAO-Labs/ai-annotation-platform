import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  useInfiniteQuery,
  useQuery,
  useMutation,
  useQueryClient,
  type MutateOptions,
  type UseMutationResult,
} from "@tanstack/react-query";
import {
  tasksApi,
  type AnnotationPayload,
  type AnnotationUpdatePayload,
  type TaskListParams,
} from "../api/tasks";
import type { AnnotationResponse, TaskResponse } from "@/types";
import { ApiError } from "../api/client";
import { randomId } from "@/utils/id";
import {
  enqueueDurably,
  isOfflineCandidate,
  type OfflineOp,
} from "../pages/Workbench/state/offlineQueue";
import { isCurrentAuthOwner, useAuthStore } from "../stores/authStore";
import {
  assertVideoDerivativeOwner,
  isVideoGeneration,
  retryPreparingVideoMetadata,
  useVideoDerivativeOwnerKey,
  videoMetadataRetryDelay,
} from "@/api/videoDerivatives";

const TASK_PAGE_SIZE = 100;

export class ConflictError extends Error {
  constructor(
    message: string,
    public currentVersion: number,
  ) {
    super(message);
    this.name = "ConflictError";
  }
}

export function useTaskList(projectId: string | undefined, params?: TaskListParams) {
  return useInfiniteQuery({
    queryKey: ["tasks", projectId, params],
    queryFn: ({ pageParam, signal }: { pageParam: string | undefined; signal: AbortSignal }) =>
      tasksApi.listByProject(
        projectId!,
        { ...params, limit: params?.limit ?? TASK_PAGE_SIZE, cursor: pageParam },
        { signal },
      ),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled: !!projectId,
  });
}

export function flattenTaskPages(
  pages: Array<{ items: TaskResponse[] }> | undefined,
): TaskResponse[] {
  if (!pages) return [];
  const seen = new Set<string>();
  const tasks: TaskResponse[] = [];
  for (const page of pages) {
    for (const task of page.items) {
      if (seen.has(task.id)) continue;
      seen.add(task.id);
      tasks.push(task);
    }
  }
  return tasks;
}

export function useNextTask(projectId: string | undefined, batchId?: string) {
  return useMutation({
    mutationFn: () => {
      if (!projectId) throw new Error("No project selected");
      return tasksApi.getNext(projectId, batchId || undefined);
    },
  });
}

export function useTask(id: string) {
  return useQuery({
    queryKey: ["task", id],
    queryFn: ({ signal }) => tasksApi.get(id, { signal }),
    enabled: !!id,
  });
}

export function useMaskCapabilities(taskId: string | null | undefined, enabled = true) {
  return useQuery({
    queryKey: ["task-mask-capabilities", taskId],
    queryFn: ({ signal }) => tasksApi.getMaskCapabilities(taskId!, { signal }),
    enabled: !!taskId && enabled,
    staleTime: 30_000,
  });
}

export function useVideoManifest(taskId: string | undefined, enabled = true) {
  const ownerKey = useVideoDerivativeOwnerKey();
  return useQuery({
    queryKey: ["task-video-manifest", ownerKey, taskId],
    queryFn: async ({ signal }) => {
      assertVideoDerivativeOwner(ownerKey, signal);
      const value = await tasksApi.getVideoManifest(taskId!, { signal });
      assertVideoDerivativeOwner(ownerKey, signal);
      return value;
    },
    enabled: !!taskId && !!ownerKey && enabled,
    retry: retryPreparingVideoMetadata,
    retryDelay: videoMetadataRetryDelay,
    refetchInterval: (query) => (query.state.error ? false : 30_000),
    gcTime: 0,
  });
}

export function useVideoFrameTimetable(
  taskId: string | undefined,
  enabled = true,
  metadataGenerationId?: string | null,
) {
  const ownerKey = useVideoDerivativeOwnerKey();
  const queryClient = useQueryClient();
  return useQuery({
    queryKey: ["task-video-frame-timetable", ownerKey, taskId, metadataGenerationId],
    queryFn: async ({ signal }) => {
      assertVideoDerivativeOwner(ownerKey, signal);
      try {
        const value = await tasksApi.getVideoFrameTimetable(
          taskId!,
          { expected_metadata_generation_id: metadataGenerationId! },
          { signal },
        );
        assertVideoDerivativeOwner(ownerKey, signal);
        if (
          !isVideoGeneration(value.metadata_generation_id) ||
          value.metadata_generation_id !== metadataGenerationId
        ) {
          throw new ApiError(409, "视频时间表版本已变化，请重新获取", {
            code: "video_metadata_changed",
          });
        }
        return value;
      } catch (error) {
        assertVideoDerivativeOwner(ownerKey, signal);
        if (
          error instanceof ApiError &&
          error.status === 409 &&
          typeof error.detailRaw === "object" &&
          error.detailRaw &&
          "code" in error.detailRaw &&
          error.detailRaw.code === "video_metadata_changed"
        ) {
          void queryClient.invalidateQueries({
            queryKey: ["task-video-manifest", ownerKey, taskId],
          });
        }
        throw error;
      }
    },
    enabled: !!taskId && !!ownerKey && isVideoGeneration(metadataGenerationId) && enabled,
    staleTime: 30_000,
    gcTime: 0,
    retry: false,
  });
}

export function useAnnotations(
  taskId: string | undefined,
  videoSegmentId?: string | null,
  enabled = true,
) {
  const queryKey = videoSegmentId
    ? (["annotations", taskId, videoSegmentId] as const)
    : (["annotations", taskId] as const);
  return useQuery({
    queryKey,
    queryFn: ({ signal }) => tasksApi.getAnnotations(taskId!, videoSegmentId, { signal }),
    enabled: !!taskId && enabled,
  });
}

interface CreateAnnotationVariables extends AnnotationMutationOwner {
  readonly taskId: string | undefined;
  readonly videoSegmentId: string | null | undefined;
  readonly payload: AnnotationPayload;
  readonly callbacks?: CreateAnnotationOptions;
}

interface CreateAnnotationContext {
  prev: AnnotationResponse[] | undefined;
  tmpId: string | undefined;
  ownerValid?: boolean;
}

type CreateAnnotationOptions = MutateOptions<
  AnnotationResponse,
  Error,
  AnnotationPayload,
  CreateAnnotationContext
>;

function createAnnotationQueryKey({ taskId, videoSegmentId }: CreateAnnotationVariables) {
  return annotationQueryKey(taskId, videoSegmentId);
}

function annotationQueryKey(taskId: string | undefined, videoSegmentId?: string | null) {
  return videoSegmentId
    ? (["annotations", taskId, videoSegmentId] as const)
    : (["annotations", taskId] as const);
}

export function useCreateAnnotation(taskId: string | undefined, videoSegmentId?: string | null) {
  const qc = useQueryClient();
  const mutation = useMutation<
    AnnotationResponse,
    Error,
    CreateAnnotationVariables,
    CreateAnnotationContext
  >({
    networkMode: "always",
    mutationFn: (variables) => {
      const { taskId, videoSegmentId, payload } = variables;
      if (!taskId) throw new Error("No task selected");
      if (!isCurrentAnnotationMutationOwner(variables)) {
        throw new AnnotationMutationOwnerChangedError();
      }
      return tasksApi.createAnnotation(
        taskId,
        videoSegmentId ? { ...payload, video_segment_id: videoSegmentId } : payload,
      );
    },
    onMutate: async (variables) => {
      const { taskId, videoSegmentId, payload } = variables;
      if (!taskId) return { prev: undefined, tmpId: undefined };
      const queryKey = createAnnotationQueryKey(variables);
      await qc.cancelQueries({ queryKey });
      if (!isCurrentAnnotationMutationOwner(variables)) {
        return { prev: undefined, tmpId: undefined, ownerValid: false };
      }
      const prev = qc.getQueryData<AnnotationResponse[]>(queryKey);
      const tmpId = `tmp_${randomId()}`;
      const optimistic: AnnotationResponse = {
        id: tmpId,
        task_id: taskId,
        video_segment_id: videoSegmentId ?? null,
        project_id: null,
        user_id: null,
        source: "manual",
        annotation_type: payload.annotation_type ?? "bbox",
        class_name: payload.class_name,
        geometry: payload.geometry,
        confidence: payload.confidence ?? 1,
        parent_prediction_id: null,
        parent_annotation_id: null,
        lead_time: null,
        is_active: true,
        ground_truth: false,
        attributes: payload.attributes ?? {},
        created_at: new Date().toISOString(),
        updated_at: null,
        render_key: tmpId,
      };
      qc.setQueryData<AnnotationResponse[]>(queryKey, (old) => [...(old ?? []), optimistic]);
      return { prev, tmpId, ownerValid: true };
    },
    onError: (_err, variables, ctx, mutationContext) => {
      if (ctx?.tmpId && ctx.ownerValid !== false && isCurrentAnnotationMutationOwner(variables)) {
        qc.setQueryData<AnnotationResponse[]>(createAnnotationQueryKey(variables), (old) =>
          old?.filter((annotation) => annotation.id !== ctx.tmpId),
        );
      }
      variables.callbacks?.onError?.(_err, variables.payload, ctx, mutationContext);
    },
    onSuccess: (created, variables, ctx, mutationContext) => {
      if (ctx?.ownerValid !== false && isCurrentAnnotationMutationOwner(variables)) {
        if (ctx?.tmpId) {
          qc.setQueryData<AnnotationResponse[]>(createAnnotationQueryKey(variables), (old = []) => {
            if (old.some((annotation) => annotation.id === created.id))
              return old.filter((annotation) => annotation.id !== ctx.tmpId);
            const optimistic = old.find((annotation) => annotation.id === ctx.tmpId);
            const saved = { ...created, render_key: optimistic?.render_key ?? ctx.tmpId };
            return optimistic
              ? old.map((annotation) => (annotation.id === ctx.tmpId ? saved : annotation))
              : [...old, saved];
          });
        }
        qc.invalidateQueries({ queryKey: ["tasks"] });
        qc.invalidateQueries({ queryKey: ["scene-timeline"] });
        qc.invalidateQueries({ queryKey: ["dashboard"] });
      }
      if (isCurrentAnnotationMutationOwner(variables))
        variables.callbacks?.onSuccess?.(created, variables.payload, ctx, mutationContext);
    },
    onSettled: (created, error, variables, ctx, mutationContext) => {
      variables.callbacks?.onSettled?.(created, error, variables.payload, ctx, mutationContext);
    },
  });
  const { mutateAsync: runMutation } = mutation;
  const mutateAsync = useCallback(
    (payload: AnnotationPayload, options?: CreateAnnotationOptions) =>
      runMutation({
        taskId,
        videoSegmentId,
        payload,
        ...captureAnnotationMutationOwner(),
        callbacks: options,
      }),
    [runMutation, taskId, videoSegmentId],
  );
  const mutate = useCallback(
    (payload: AnnotationPayload, options?: CreateAnnotationOptions) => {
      void mutateAsync(payload, options).catch(() => undefined);
    },
    [mutateAsync],
  );
  return {
    ...mutation,
    variables: mutation.variables?.payload,
    mutate,
    mutateAsync,
  } as UseMutationResult<AnnotationResponse, Error, AnnotationPayload, CreateAnnotationContext>;
}

const offlineQueuedErrors = new WeakSet<object>();

class AnnotationMutationOwnerChangedError extends Error {
  readonly status = 503;

  constructor() {
    super("Annotation mutation owner changed before the request was sent");
    this.name = "AnnotationMutationOwnerChangedError";
  }
}

interface AnnotationMutationOwner {
  ownerUserId: string | undefined;
  ownerSessionId: string | null;
}

function captureAnnotationMutationOwner(): AnnotationMutationOwner {
  const { user, sessionId } = useAuthStore.getState();
  return { ownerUserId: user?.id, ownerSessionId: sessionId };
}

function isCurrentAnnotationMutationOwner(owner: AnnotationMutationOwner): boolean {
  return (
    !!owner.ownerUserId &&
    isCurrentAuthOwner(owner.ownerUserId) &&
    useAuthStore.getState().sessionId === owner.ownerSessionId
  );
}

function markOfflineMutationQueued(error: unknown): void {
  if (error && typeof error === "object") offlineQueuedErrors.add(error);
}

export function isOfflineMutationQueued(error: unknown): boolean {
  return !!error && typeof error === "object" && offlineQueuedErrors.has(error);
}

type DeleteAnnotationInput = string;
type DeleteAnnotationContext = {
  prev: AnnotationResponse[] | undefined;
  queryKey: readonly [string, string | undefined] | readonly [string, string | undefined, string];
  ownerValid?: boolean;
};
type DeleteAnnotationOptions = MutateOptions<
  void,
  Error,
  DeleteAnnotationInput,
  DeleteAnnotationContext
>;

interface SubmittedDeleteAnnotation extends AnnotationMutationOwner {
  annotationId: string;
  taskId: string | undefined;
  videoSegmentId: string | null | undefined;
  callbacks?: DeleteAnnotationOptions;
  queueOffline: boolean;
}

export function useDeleteAnnotation(taskId: string | undefined, videoSegmentId?: string | null) {
  const qc = useQueryClient();
  const mutation = useMutation<void, Error, SubmittedDeleteAnnotation, DeleteAnnotationContext>({
    mutationKey: ["annotation-write", taskId],
    networkMode: "always",
    mutationFn: (variables) => {
      const { taskId: submittedTaskId, annotationId } = variables;
      if (!submittedTaskId) throw new Error("No task selected");
      if (!isCurrentAnnotationMutationOwner(variables)) {
        throw new AnnotationMutationOwnerChangedError();
      }
      return tasksApi.deleteAnnotation(submittedTaskId, annotationId);
    },
    onMutate: async (variables) => {
      const { annotationId, taskId: submittedTaskId, videoSegmentId } = variables;
      const queryKey = annotationQueryKey(submittedTaskId, videoSegmentId);
      await qc.cancelQueries({ queryKey });
      if (!isCurrentAnnotationMutationOwner(variables)) {
        return { prev: undefined, queryKey, ownerValid: false };
      }
      const prev = qc.getQueryData<AnnotationResponse[]>(queryKey);
      qc.setQueryData<AnnotationResponse[]>(queryKey, (old) =>
        (old ?? []).filter((a) => a.id !== annotationId),
      );
      return { prev, queryKey, ownerValid: true };
    },
    onError: async (err, variables, ctx, mutationContext) => {
      if (
        variables.queueOffline &&
        isOfflineCandidate(err) &&
        variables.taskId &&
        variables.ownerUserId
      ) {
        const op: OfflineOp = {
          kind: "delete",
          id: randomId(),
          taskId: variables.taskId,
          videoSegmentId: variables.videoSegmentId,
          projectId:
            ctx?.prev?.find((annotation) => annotation.id === variables.annotationId)?.project_id ??
            undefined,
          annotationId: variables.annotationId,
          ts: Date.now(),
        };
        try {
          await enqueueDurably(op, { userId: variables.ownerUserId });
          markOfflineMutationQueued(err);
          return;
        } catch {
          // Storage failure must leave the mutation rejected and restore the
          // cache.  The caller can retry after storage becomes available.
        }
      }
      if (
        ctx?.prev !== undefined &&
        ctx.ownerValid !== false &&
        isCurrentAnnotationMutationOwner(variables)
      )
        qc.setQueryData(ctx.queryKey, ctx.prev);
      await variables.callbacks?.onError?.(err, variables.annotationId, ctx, mutationContext);
    },
    onSuccess: (_data, variables, ctx, mutationContext) => {
      if (isCurrentAnnotationMutationOwner(variables))
        variables.callbacks?.onSuccess?.(_data, variables.annotationId, ctx, mutationContext);
    },
    onSettled: (_data, _err, variables, ctx, mutationContext) => {
      const queryKey =
        ctx?.queryKey ?? annotationQueryKey(variables.taskId, variables.videoSegmentId);
      if (isCurrentAnnotationMutationOwner(variables) && !isOfflineCandidate(_err)) {
        qc.invalidateQueries({ queryKey });
        qc.invalidateQueries({ queryKey: ["tasks"] });
        qc.invalidateQueries({ queryKey: ["dashboard"] });
        qc.invalidateQueries({ queryKey: ["scene-timeline"] });
      }
      variables.callbacks?.onSettled?.(_data, _err, variables.annotationId, ctx, mutationContext);
    },
  });

  const { mutateAsync: runMutation } = mutation;
  const mutateAsync = useCallback(
    (annotationId: DeleteAnnotationInput, options?: DeleteAnnotationOptions) =>
      runMutation({
        annotationId,
        taskId,
        videoSegmentId,
        ...captureAnnotationMutationOwner(),
        callbacks: options,
        queueOffline: !options?.onError,
      }),
    [runMutation, taskId, videoSegmentId],
  );
  const mutate = useCallback(
    (annotationId: DeleteAnnotationInput, options?: DeleteAnnotationOptions) => {
      void mutateAsync(annotationId, options).catch(() => undefined);
    },
    [mutateAsync],
  );
  return {
    ...mutation,
    variables: mutation.variables?.annotationId,
    mutate,
    mutateAsync,
  } as UseMutationResult<void, Error, DeleteAnnotationInput, DeleteAnnotationContext>;
}

type UpdateAnnotationInput = {
  annotationId: string;
  payload: AnnotationUpdatePayload;
  etag?: string;
};
type UpdateAnnotationContext = {
  prev: AnnotationResponse[] | undefined;
  queryKey: readonly [string, string | undefined] | readonly [string, string | undefined, string];
  ownerValid?: boolean;
};
type UpdateAnnotationOptions = MutateOptions<
  AnnotationResponse,
  Error,
  UpdateAnnotationInput,
  UpdateAnnotationContext
> & {
  queueOffline?: boolean;
  optimistic?: boolean;
};

interface SubmittedUpdateAnnotation extends UpdateAnnotationInput, AnnotationMutationOwner {
  taskId: string | undefined;
  videoSegmentId: string | null | undefined;
  callbacks?: UpdateAnnotationOptions;
  queueOffline: boolean;
}

export function useUpdateAnnotation(
  taskId: string | undefined,
  onConflict?: (annotationId: string, currentVersion: number) => void,
  onSettledAnnotation?: (annotationId: string) => void,
  videoSegmentId?: string | null,
) {
  const qc = useQueryClient();
  const activeScope = useRef({ taskId, videoSegmentId });
  activeScope.current.taskId = taskId;
  activeScope.current.videoSegmentId = videoSegmentId;
  const mutation = useMutation<
    AnnotationResponse,
    Error,
    SubmittedUpdateAnnotation,
    UpdateAnnotationContext
  >({
    mutationKey: ["annotation-write", taskId],
    networkMode: "always",
    mutationFn: (variables) => {
      const { annotationId, payload, etag, taskId: submittedTaskId } = variables;
      if (!submittedTaskId) throw new Error("No task selected");
      if (!isCurrentAnnotationMutationOwner(variables)) {
        throw new AnnotationMutationOwnerChangedError();
      }
      return tasksApi.updateAnnotation(submittedTaskId, annotationId, payload, etag);
    },
    onMutate: async (variables) => {
      const { annotationId, payload, taskId: submittedTaskId, videoSegmentId } = variables;
      const queryKey = annotationQueryKey(submittedTaskId, videoSegmentId);
      await qc.cancelQueries({ queryKey });
      if (!isCurrentAnnotationMutationOwner(variables)) {
        return { prev: undefined, queryKey, ownerValid: false };
      }
      if (variables.callbacks?.optimistic === false)
        return { prev: undefined, queryKey, ownerValid: true };
      const prev = qc.getQueryData<AnnotationResponse[]>(queryKey);
      qc.setQueryData<AnnotationResponse[]>(queryKey, (old) =>
        (old ?? []).map((a) =>
          a.id === annotationId
            ? {
                ...a,
                ...(payload.geometry ? { geometry: payload.geometry } : {}),
                ...(payload.class_name ? { class_name: payload.class_name } : {}),
                ...(payload.attributes !== undefined ? { attributes: payload.attributes } : {}),
              }
            : a,
        ),
      );
      return { prev, queryKey, ownerValid: true };
    },
    onError: async (err, variables, ctx, mutationContext) => {
      if (err instanceof ApiError && err.status === 409) {
        const detail = err.detailRaw as { current_version?: number } | undefined;
        const annotationId = variables.annotationId;
        if (
          detail?.current_version &&
          onConflict &&
          activeScope.current.taskId === variables.taskId &&
          activeScope.current.videoSegmentId === variables.videoSegmentId &&
          isCurrentAnnotationMutationOwner(variables)
        ) {
          onConflict(annotationId, detail.current_version);
        }
      }
      if (
        variables.queueOffline &&
        isOfflineCandidate(err) &&
        variables.taskId &&
        variables.ownerUserId
      ) {
        const op: OfflineOp = {
          kind: "update",
          id: randomId(),
          taskId: variables.taskId,
          videoSegmentId: variables.videoSegmentId,
          projectId:
            ctx?.prev?.find((annotation) => annotation.id === variables.annotationId)?.project_id ??
            undefined,
          annotationId: variables.annotationId,
          payload: variables.payload,
          etag: variables.etag,
          ts: Date.now(),
        };
        try {
          await enqueueDurably(op, { userId: variables.ownerUserId });
          markOfflineMutationQueued(err);
          return;
        } catch {
          // Storage failure must keep the mutation failed and roll back the
          // optimistic cache instead of claiming a local success.
        }
      }
      if (
        ctx?.prev !== undefined &&
        ctx.ownerValid !== false &&
        isCurrentAnnotationMutationOwner(variables)
      )
        qc.setQueryData(ctx.queryKey, ctx.prev);
      await variables.callbacks?.onError?.(err, variables, ctx, mutationContext);
    },
    onSuccess: (annotation, variables, ctx, mutationContext) => {
      if (ctx?.ownerValid !== false && isCurrentAnnotationMutationOwner(variables)) {
        qc.setQueryData<AnnotationResponse[]>(ctx.queryKey, (old) =>
          (old ?? []).map((item) => (item.id === annotation.id ? annotation : item)),
        );
        variables.callbacks?.onSuccess?.(annotation, variables, ctx, mutationContext);
      }
    },
    onSettled: (_data, _err, vars, ctx, mutationContext) => {
      const queryKey = ctx?.queryKey ?? annotationQueryKey(vars.taskId, vars.videoSegmentId);
      if (isCurrentAnnotationMutationOwner(vars) && !isOfflineCandidate(_err)) {
        qc.invalidateQueries({ queryKey });
        qc.invalidateQueries({ queryKey: ["scene-timeline"] });
      }
      const annotationId = vars?.annotationId;
      if (annotationId && onSettledAnnotation && isCurrentAnnotationMutationOwner(vars))
        onSettledAnnotation(annotationId);
      vars.callbacks?.onSettled?.(_data, _err, vars, ctx, mutationContext);
    },
  });

  const { mutateAsync: runMutation } = mutation;
  const mutateAsync = useCallback(
    (input: UpdateAnnotationInput, options?: UpdateAnnotationOptions) =>
      runMutation({
        ...input,
        taskId,
        videoSegmentId,
        ...captureAnnotationMutationOwner(),
        callbacks: options,
        queueOffline: options?.queueOffline ?? !options?.onError,
      }),
    [runMutation, taskId, videoSegmentId],
  );
  const mutate = useCallback(
    (input: UpdateAnnotationInput, options?: UpdateAnnotationOptions) => {
      void mutateAsync(input, options).catch(() => undefined);
    },
    [mutateAsync],
  );
  return {
    ...mutation,
    variables: mutation.variables
      ? {
          annotationId: mutation.variables.annotationId,
          payload: mutation.variables.payload,
          etag: mutation.variables.etag,
        }
      : undefined,
    mutate,
    mutateAsync,
  } as Omit<
    UseMutationResult<AnnotationResponse, Error, UpdateAnnotationInput, UpdateAnnotationContext>,
    "mutate" | "mutateAsync"
  > & { mutate: typeof mutate; mutateAsync: typeof mutateAsync };
}

export function useSubmitTask() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (taskId: string) => tasksApi.submit(taskId),
    onSuccess: (_, taskId) => {
      qc.invalidateQueries({ queryKey: ["task", taskId] });
      qc.invalidateQueries({ queryKey: ["annotations", taskId] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
}

export function useSkipTask() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      taskId,
      reason,
      note,
    }: {
      taskId: string;
      reason: "image_corrupt" | "no_target" | "unclear" | "other";
      note?: string;
    }) => tasksApi.skip(taskId, { reason, note }),
    onSuccess: (_, { taskId }) => {
      qc.invalidateQueries({ queryKey: ["task", taskId] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
}

export function useApproveTask() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (taskId: string) => tasksApi.approve(taskId),
    onSuccess: (_, taskId) => {
      qc.invalidateQueries({ queryKey: ["task", taskId] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
}

export function useRejectTask() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      taskId,
      reason_type,
      reason,
    }: {
      taskId: string;
      reason_type: "missing" | "extra" | "wrong_label" | "wrong_geometry";
      reason?: string;
    }) => tasksApi.reject(taskId, { reason_type, reason }),
    onSuccess: (_, { taskId }) => {
      qc.invalidateQueries({ queryKey: ["task", taskId] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
}

export function useWithdrawTask() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (taskId: string) => tasksApi.withdraw(taskId),
    onSuccess: (_, taskId) => {
      qc.invalidateQueries({ queryKey: ["task", taskId] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
}

export function useReopenTask() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (taskId: string) => tasksApi.reopen(taskId),
    onSuccess: (_, taskId) => {
      qc.invalidateQueries({ queryKey: ["task", taskId] });
      qc.invalidateQueries({ queryKey: ["annotations", taskId] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
}

export function useAcceptRejection() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (taskId: string) => tasksApi.acceptRejection(taskId),
    onSuccess: (_, taskId) => {
      qc.invalidateQueries({ queryKey: ["task", taskId] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
}

function reviewClaimReason(error: unknown): string | undefined {
  const detail = error instanceof ApiError ? error.detailRaw : undefined;
  if (
    detail &&
    typeof detail === "object" &&
    "reason" in detail &&
    typeof detail.reason === "string"
  ) {
    return detail.reason;
  }
  return undefined;
}

function isReviewClaimBusy(error: unknown): boolean {
  if (!(error instanceof ApiError) || error.status !== 409) return false;
  const reason = reviewClaimReason(error);
  return reason === "batch_busy" || reason === "task_busy";
}

export interface ReviewClaimState {
  canDecide: boolean;
  ownsClaim: () => boolean;
  initialSettled: boolean;
  message: string | null;
  retry: (() => void) | undefined;
}

export function useReviewClaim(taskId: string | undefined): ReviewClaimState {
  const qc = useQueryClient();
  const userId = useAuthStore((s) => s.user?.id);
  const sessionId = useAuthStore((s) => s.sessionId);
  const scope = useMemo(
    () => (taskId && userId && sessionId ? { taskId, userId, sessionId } : null),
    [taskId, userId, sessionId],
  );
  type Scope = NonNullable<typeof scope>;
  const currentScope = useRef<Scope | null>(null);
  const lastStartedScope = useRef<Scope | null>(null);
  const [initialSettledScope, setInitialSettledScope] = useState<Scope | null>(null);
  useLayoutEffect(() => {
    currentScope.current = scope;
    return () => {
      currentScope.current = null;
    };
  }, [scope]);

  const ownsScope = useCallback(
    (owner: Scope) =>
      currentScope.current === owner &&
      useAuthStore.getState().sessionId === owner.sessionId &&
      isCurrentAuthOwner(owner.userId),
    [],
  );
  const { mutate, variables, data, error } = useMutation({
    mutationFn: (owner: Scope) => {
      if (!ownsScope(owner)) throw new Error("Review claim context changed");
      return tasksApi.reviewClaim(owner.taskId);
    },
    retry: (attempt, error) => attempt < 1 && isReviewClaimBusy(error),
    onSuccess: (_, owner) => {
      if (ownsScope(owner)) void qc.invalidateQueries({ queryKey: ["task", owner.taskId] });
    },
    onSettled: (_data, _error, owner) => {
      if (currentScope.current === owner) setInitialSettledScope(owner);
    },
  });
  useEffect(() => {
    if (!scope || lastStartedScope.current === scope) return;
    lastStartedScope.current = scope;
    mutate(scope);
  }, [mutate, scope]);

  const current = scope !== null && variables === scope;
  const claim = current && data?.task_id === scope.taskId ? data : null;
  const canDecide = claim?.is_self === true && claim.reviewer_id === scope?.userId;
  const ownsClaim = useCallback(
    () => canDecide && scope !== null && ownsScope(scope),
    [canDecide, ownsScope, scope],
  );
  const currentError = current ? error : null;
  const busy = isReviewClaimBusy(currentError);
  const canRetry =
    busy ||
    currentError instanceof TypeError ||
    (currentError instanceof ApiError &&
      (currentError.status === 429 || currentError.status >= 500));
  let message: string | null = null;
  if (scope && !canDecide) {
    if (claim && !claim.is_self) {
      message = `已被其他审核员认领（${new Date(claim.reviewer_claimed_at).toLocaleString("zh-CN")}），无法提交审核结果`;
    } else if (busy) {
      message = "任务正忙，未能认领，请重试";
    } else if (reviewClaimReason(currentError) === "task_review_assigned_to_other") {
      message = "任务已分配给其他审核员，无法认领";
    } else if (canRetry) {
      message = "未能认领审核任务，请重试";
    } else if (currentError || (current && data)) {
      message = "无法认领审核任务";
    } else {
      message = "正在认领审核任务，请稍候";
    }
  }
  return {
    canDecide,
    ownsClaim,
    initialSettled: !scope || initialSettledScope === scope,
    message,
    retry:
      scope && canRetry
        ? () => {
            if (ownsScope(scope)) mutate(scope);
          }
        : undefined,
  };
}

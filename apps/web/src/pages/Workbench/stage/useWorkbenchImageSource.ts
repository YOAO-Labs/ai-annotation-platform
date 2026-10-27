import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { tasksApi } from "@/api/tasks";
import { ApiError } from "@/api/client";
import { useVerifiedImage } from "@/hooks/useVerifiedImage";
import { useOriginalImage } from "@/hooks/useOriginalImage";
import { originalOwnerKey } from "@/api/originals";
import type { TaskResponse } from "@/types";
import { useAuthStore } from "@/stores/authStore";
import {
  imageTileDeviceBudget,
  parseImagePyramidManifest,
  singleImageFitsDecodedBudget,
  type ImagePyramidResponse,
  type WorkbenchImageSource,
} from "./imagePyramid";

export const LARGE_IMAGE_TILES_ENABLED =
  import.meta.env.VITE_EXPERIMENTAL_LARGE_IMAGE_TILES !== "false";

function singleSource(
  task: TaskResponse,
  identity: string,
  width = task.image_width,
  height = task.image_height,
): WorkbenchImageSource | null {
  if (!task.file_url) return null;
  return {
    kind: "single",
    identity,
    url: task.file_url,
    width: width ?? undefined,
    height: height ?? undefined,
    thumbnailUrl: task.thumbnail_url ?? undefined,
    blurhash: task.blurhash ?? undefined,
  };
}

function imageReferenceMatchesTask(task: TaskResponse, response: ImagePyramidResponse): boolean {
  const summary = task.image_pyramid;
  return (
    response.task_id === task.id &&
    (summary?.source_snapshot ?? null) === (response.source_snapshot ?? null) &&
    (summary?.profile_version ?? null) === (response.profile_version ?? null) &&
    (summary?.width == null || !("width" in response) || summary.width === response.width) &&
    (summary?.height == null || !("height" in response) || summary.height === response.height)
  );
}

export function resolveWorkbenchImageSource(
  task: TaskResponse | undefined,
  response: ImagePyramidResponse | undefined,
  options: {
    enabled?: boolean;
    mediaIdentity?: string | null;
    maxSingleDecodedBytes?: number;
  } = {},
): WorkbenchImageSource | null {
  if (!task || task.file_type !== "image") return null;
  const mediaKey = options.mediaIdentity ?? task.dataset_item_id ?? task.id;
  const summary = task.image_pyramid;
  const enabled = options.enabled ?? LARGE_IMAGE_TILES_ENABLED;
  if (!summary) return singleSource(task, mediaKey);
  const identity = summary.source_snapshot
    ? `${mediaKey}/source/${summary.source_snapshot}`
    : mediaKey;
  const pyramidIdentity = `${identity}/pyramid${summary.profile_version ? `/${summary.profile_version}` : ""}`;
  const matches = !response || imageReferenceMatchesTask(task, response);
  const currentResponse = matches ? response : undefined;
  const width =
    currentResponse && "width" in currentResponse
      ? (currentResponse.width ?? 0)
      : (summary.width ?? task.image_width ?? 0);
  const height =
    currentResponse && "height" in currentResponse
      ? (currentResponse.height ?? 0)
      : (summary.height ?? task.image_height ?? 0);
  const required = currentResponse?.required ?? summary.required;
  const generation = currentResponse ? currentResponse.generation : summary.generation;
  const maxSingleDecodedBytes = options.maxSingleDecodedBytes ?? 128 * 1024 * 1024;
  const singleAllowed = singleImageFitsDecodedBudget(width, height, maxSingleDecodedBytes);
  const fallbackState = (
    kind: "pyramid-pending" | "pyramid-failed",
    retryable: boolean,
    errorCode?: string,
  ): WorkbenchImageSource => ({
    kind,
    taskId: task.id,
    identity: `${pyramidIdentity}/${generation ?? "missing"}`,
    width,
    height,
    thumbnailUrl: task.thumbnail_url ?? undefined,
    blurhash: task.blurhash ?? undefined,
    retryable,
    errorCode,
  });

  if (!matches) return fallbackState("pyramid-failed", false, "source_changed");

  if (!enabled) {
    return required
      ? fallbackState("pyramid-failed", false, "client_gate_disabled")
      : singleAllowed
        ? singleSource(task, identity, width, height)
        : fallbackState("pyramid-failed", false, "single_decode_budget_exceeded");
  }

  if (
    currentResponse?.status === "ready" &&
    currentResponse.manifest &&
    currentResponse.generation != null
  ) {
    try {
      const manifest = parseImagePyramidManifest(currentResponse.manifest);
      if (
        manifest.generation !== currentResponse.generation ||
        (width > 0 && manifest.width !== width) ||
        (height > 0 && manifest.height !== height)
      ) {
        throw new Error("image pyramid identity mismatch");
      }
      return {
        kind: "pyramid",
        taskId: task.id,
        identity: `${pyramidIdentity}/${manifest.generation}/${manifest.sourceFingerprint}`,
        generation: manifest.generation,
        manifest,
        overviewUrl: currentResponse.overview?.url,
        thumbnailUrl: task.thumbnail_url ?? undefined,
        blurhash: task.blurhash ?? undefined,
      };
    } catch {
      return required
        ? fallbackState("pyramid-failed", true, "invalid_manifest")
        : singleAllowed
          ? singleSource(task, identity, width, height)
          : fallbackState("pyramid-failed", true, "invalid_manifest");
    }
  }

  if (currentResponse?.status === "pending" || currentResponse?.status === "building") {
    return required
      ? fallbackState("pyramid-pending", false)
      : singleAllowed
        ? singleSource(task, identity, width, height)
        : fallbackState("pyramid-pending", false);
  }

  const responseFailed =
    currentResponse &&
    ["failed", "stale", "inconsistent", "missing", "not_available"].includes(
      currentResponse.status,
    );
  if (
    responseFailed ||
    ["failed", "stale", "inconsistent", "missing", "not_available"].includes(summary.status)
  ) {
    return required
      ? fallbackState(
          "pyramid-failed",
          currentResponse?.retryable ?? summary.retryable ?? true,
          currentResponse?.error_code ?? summary.error_code ?? "pyramid_unavailable",
        )
      : singleAllowed
        ? singleSource(task, identity, width, height)
        : fallbackState(
            "pyramid-failed",
            currentResponse?.retryable ?? summary.retryable ?? true,
            currentResponse?.error_code ?? summary.error_code ?? "single_decode_budget_exceeded",
          );
  }

  const status = currentResponse?.status ?? summary.status;
  if (status === "ready" || status === "pending" || status === "building") {
    return required || status === "ready"
      ? fallbackState("pyramid-pending", false)
      : singleAllowed
        ? singleSource(task, identity, width, height)
        : fallbackState("pyramid-pending", false);
  }
  return singleSource(task, identity, width, height);
}

export function workbenchImagePreviewUrl(source: WorkbenchImageSource | null): string | null {
  if (!source) return null;
  if (source.kind === "single") return source.thumbnailUrl ?? source.url;
  return source.overviewUrl ?? source.thumbnailUrl ?? null;
}

export function useWorkbenchImageSource(
  incomingTask: TaskResponse | undefined,
  mediaIdentity?: string | null,
  requestedTaskId?: string | null,
): {
  source: WorkbenchImageSource | null;
  /** Refresh authenticated bytes or derived metadata without rebuilding the pyramid. */
  refresh: () => Promise<void>;
  retry: (() => Promise<void>) | undefined;
  contentBlockedReason?: string | null;
  contentPending?: boolean;
} {
  const userId = useAuthStore((state) => state.user?.id);
  const sessionId = useAuthStore((state) => state.sessionId);
  const ownerKey = originalOwnerKey();
  // A query may briefly retain its old object after login changes. Never lend
  // that object to the new session, even when it names the same requested Task.
  const taskOwners = useRef(new WeakMap<TaskResponse, string>());
  const retained = useRef<{ owner: string; task?: TaskResponse }>({ owner: ownerKey });
  if (retained.current.owner !== ownerKey) retained.current = { owner: ownerKey };
  if (incomingTask) {
    const previousOwner = taskOwners.current.get(incomingTask);
    if (!previousOwner) taskOwners.current.set(incomingTask, ownerKey);
    if (
      (!previousOwner || previousOwner === ownerKey) &&
      (!requestedTaskId || incomingTask.id === requestedTaskId)
    ) {
      retained.current.task = incomingTask;
    } else retained.current.task = undefined;
  } else if (!requestedTaskId || retained.current.task?.id !== requestedTaskId) {
    retained.current.task = undefined;
  }
  const task = retained.current.task;
  const reference = task?.storage_mode === "reference";
  const verified = useVerifiedImage(
    reference && task?.content_sha256
      ? `/files/tasks/${task.id}/content?sha256=${task.content_sha256}`
      : null,
  );
  const queryClient = useQueryClient();
  const actorId = useAuthStore((state) => state.user?.id ?? null);
  const [retryWindow, setRetryWindow] = useState<{ scope: string; untilMs: number } | null>(null);
  const retryAttempt = useRef<{ scope: string; key: string } | null>(null);
  const summary = task?.image_pyramid;
  const scope = JSON.stringify([
    actorId,
    task?.id,
    summary?.source_snapshot,
    summary?.profile_version,
  ]);
  const queryKey = useMemo(
    () =>
      [
        "image-pyramid",
        ownerKey,
        task?.id,
        summary?.source_snapshot,
        summary?.profile_version,
        summary?.generation,
        summary?.width,
        summary?.height,
        summary?.status,
        summary?.required,
      ] as const,
    [
      ownerKey,
      task?.id,
      summary?.source_snapshot,
      summary?.profile_version,
      summary?.generation,
      summary?.width,
      summary?.height,
      summary?.status,
      summary?.required,
    ],
  );
  const retryUntilMs = retryWindow?.scope === scope ? retryWindow.untilMs : 0;
  const maxSingleDecodedBytes = useMemo(() => {
    if (typeof navigator === "undefined") return imageTileDeviceBudget(null).retainedBytes;
    const value = (navigator as Navigator & { deviceMemory?: unknown }).deviceMemory;
    return imageTileDeviceBudget(typeof value === "number" ? value : null).retainedBytes;
  }, []);
  const shouldQuery = LARGE_IMAGE_TILES_ENABLED && task?.file_type === "image" && summary != null;
  const query = useQuery({
    queryKey,
    queryFn: ({ signal }) => tasksApi.getImagePyramid(task!.id, { signal }),
    enabled: shouldQuery,
    staleTime: 30_000,
    refetchInterval: (current) => {
      const data = current.state.data;
      if (data?.status === "ready") return false;
      if (data?.status === "pending" || data?.status === "building") {
        return Math.max(1_000, data.retry_after_ms ?? 2_000);
      }
      return Date.now() < retryUntilMs ? 1_000 : false;
    },
    retry: 1,
  });
  const referenceChanged = !!task && !!query.data && !imageReferenceMatchesTask(task, query.data);
  const needsTaskMedia =
    !!task &&
    query.data?.width != null &&
    query.data.height != null &&
    (!task.file_url ||
      task.image_width !== query.data.width ||
      task.image_height !== query.data.height);
  useEffect(() => {
    if ((!referenceChanged && !needsTaskMedia) || !task?.id) return;
    void queryClient.invalidateQueries({ queryKey: ["task", task.id] });
    void queryClient.invalidateQueries({ queryKey: ["tasks"] });
  }, [
    queryClient,
    task?.id,
    referenceChanged,
    needsTaskMedia,
    query.data?.source_snapshot,
    query.data?.profile_version,
    query.data?.width,
    query.data?.height,
  ]);
  const selection = useMemo(
    () =>
      resolveWorkbenchImageSource(
        reference
          ? verified.url
            ? { ...task!, file_url: verified.url }
            : undefined
          : task
            ? { ...task, file_url: "original-selection" }
            : undefined,
        query.data,
        {
          mediaIdentity: reference
            ? `${mediaIdentity ?? task?.id}/${verified.url}`
            : `${userId}/${sessionId}/${mediaIdentity ?? task?.dataset_item_id ?? task?.id}`,
          maxSingleDecodedBytes,
        },
      ),
    [
      maxSingleDecodedBytes,
      mediaIdentity,
      query.data,
      task,
      reference,
      verified.url,
      userId,
      sessionId,
    ],
  );
  const needsOriginal = !reference && selection?.kind === "single";
  const original = useOriginalImage(
    needsOriginal && task ? { kind: "task", taskId: task.id } : null,
    mediaIdentity ?? task?.dataset_item_id ?? task?.id ?? null,
  );
  const source = needsOriginal
    ? original.url && selection?.kind === "single"
      ? {
          ...selection,
          url: original.url,
          identity: `${selection.identity}/address/${original.version}`,
        }
      : null
    : selection;
  const refetch = query.refetch;
  const retryContent = verified.retry;
  const retryOriginal = original.retry;
  const refresh = useCallback(async () => {
    if (originalOwnerKey() !== ownerKey) return;
    if (reference) retryContent();
    if (needsOriginal) retryOriginal();
    if (shouldQuery) await refetch();
  }, [refetch, shouldQuery, reference, retryContent, needsOriginal, retryOriginal, ownerKey]);
  const retry = useCallback(async () => {
    if (!task?.id || originalOwnerKey() !== ownerKey) return;
    const key =
      retryAttempt.current?.scope === scope ? retryAttempt.current.key : crypto.randomUUID();
    retryAttempt.current = { scope, key };
    let result;
    try {
      result = await tasksApi.retryImagePyramid(task.id, { idempotencyKey: key });
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        const current = await refetch();
        await Promise.all([
          queryClient.invalidateQueries({ queryKey: ["task", task.id] }),
          queryClient.invalidateQueries({ queryKey: ["tasks"] }),
        ]);
        if (current.data?.status === "ready" && imageReferenceMatchesTask(task, current.data)) {
          if (retryAttempt.current?.key === key) retryAttempt.current = null;
          return;
        }
      }
      throw error;
    }
    if (originalOwnerKey() !== ownerKey) return;
    if (retryAttempt.current?.key === key) retryAttempt.current = null;
    setRetryWindow({ scope, untilMs: Date.now() + 30_000 });
    queryClient.setQueryData<ImagePyramidResponse>(queryKey, (current) => ({
      task_id: task.id,
      status: result.status === "building" ? "building" : "pending",
      required: current?.required ?? summary?.required ?? true,
      source_snapshot: summary?.source_snapshot ?? null,
      profile_version: summary?.profile_version ?? null,
      width: current?.width ?? summary?.width ?? null,
      height: current?.height ?? summary?.height ?? null,
      retryable: false,
      retry_after_ms: 1_000,
      generation: current?.generation ?? summary?.generation ?? null,
      building_generation: current?.building_generation ?? null,
      building_status: result.status === "building" ? "building" : "pending",
      error_code: null,
      manifest: null,
      overview: current?.overview ?? null,
    }));
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["task", task.id] }),
      queryClient.invalidateQueries({ queryKey: ["tasks"] }),
    ]);
  }, [ownerKey, queryClient, queryKey, refetch, scope, summary, task]);
  return {
    source,
    refresh,
    contentPending: reference
      ? !verified.ready && !verified.error
      : needsOriginal && !original.ready && !original.error,
    contentBlockedReason:
      incomingTask?.file_type === "image" && !task
        ? "正在当前会话重新读取任务图片，请稍候"
        : reference && !verified.ready
          ? (verified.error ?? "正在核验并读取原图，请稍候")
          : needsOriginal && !original.ready
            ? (original.error ?? "正在核验并读取原图，请稍候")
            : null,
    retry:
      reference || needsOriginal
        ? refresh
        : source?.kind === "pyramid-failed" && source.retryable
          ? retry
          : undefined,
  };
}

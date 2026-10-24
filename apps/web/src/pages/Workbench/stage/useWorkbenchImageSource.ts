import { useCallback, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { tasksApi } from "@/api/tasks";
import { useVerifiedImage } from "@/hooks/useVerifiedImage";
import { useOriginalImage } from "@/hooks/useOriginalImage";
import { originalOwnerKey } from "@/api/originals";
import { useAuthStore } from "@/stores/authStore";
import type { TaskResponse } from "@/types";
import {
  imageTileDeviceBudget,
  parseImagePyramidManifest,
  singleImageFitsDecodedBudget,
  type ImagePyramidResponse,
  type WorkbenchImageSource,
} from "./imagePyramid";

export const LARGE_IMAGE_TILES_ENABLED =
  import.meta.env.VITE_EXPERIMENTAL_LARGE_IMAGE_TILES !== "false";

function singleSource(task: TaskResponse, identity: string): WorkbenchImageSource | null {
  if (!task.file_url) return null;
  return {
    kind: "single",
    identity,
    url: task.file_url,
    width: task.image_width ?? undefined,
    height: task.image_height ?? undefined,
    thumbnailUrl: task.thumbnail_url ?? undefined,
    blurhash: task.blurhash ?? undefined,
  };
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
  const identity = options.mediaIdentity ?? task.dataset_item_id ?? task.id;
  const summary = task.image_pyramid;
  const enabled = options.enabled ?? LARGE_IMAGE_TILES_ENABLED;
  if (!summary) return singleSource(task, identity);
  const width = summary.width ?? task.image_width ?? 0;
  const height = summary.height ?? task.image_height ?? 0;
  const maxSingleDecodedBytes = options.maxSingleDecodedBytes ?? 128 * 1024 * 1024;
  const singleAllowed = singleImageFitsDecodedBudget(width, height, maxSingleDecodedBytes);
  const fallbackState = (
    kind: "pyramid-pending" | "pyramid-failed",
    retryable: boolean,
    errorCode?: string,
  ): WorkbenchImageSource => ({
    kind,
    taskId: task.id,
    identity: `${identity}/pyramid/${summary.generation}`,
    width,
    height,
    thumbnailUrl: task.thumbnail_url ?? undefined,
    blurhash: task.blurhash ?? undefined,
    retryable,
    errorCode,
  });

  if (!enabled) {
    return summary.required
      ? fallbackState("pyramid-failed", false, "client_gate_disabled")
      : singleAllowed
        ? singleSource(task, identity)
        : fallbackState("pyramid-failed", false, "single_decode_budget_exceeded");
  }

  if (response?.status === "ready" && response.manifest && response.generation != null) {
    try {
      const manifest = parseImagePyramidManifest(response.manifest);
      if (
        manifest.generation !== response.generation ||
        (summary.width != null && manifest.width !== summary.width) ||
        (summary.height != null && manifest.height !== summary.height)
      ) {
        throw new Error("image pyramid identity mismatch");
      }
      return {
        kind: "pyramid",
        taskId: task.id,
        identity: `${identity}/pyramid/${manifest.generation}/${manifest.sourceFingerprint}`,
        generation: manifest.generation,
        manifest,
        overviewUrl: response.overview?.url,
        thumbnailUrl: task.thumbnail_url ?? undefined,
        blurhash: task.blurhash ?? undefined,
      };
    } catch {
      return summary.required
        ? fallbackState("pyramid-failed", true, "invalid_manifest")
        : singleAllowed
          ? singleSource(task, identity)
          : fallbackState("pyramid-failed", true, "invalid_manifest");
    }
  }

  if (response?.status === "pending" || response?.status === "building") {
    return response.required || summary.required
      ? fallbackState("pyramid-pending", false)
      : singleAllowed
        ? singleSource(task, identity)
        : fallbackState("pyramid-pending", false);
  }

  const responseFailed =
    response &&
    ["failed", "stale", "inconsistent", "missing", "not_available"].includes(response.status);
  if (responseFailed || summary.status === "failed") {
    return summary.required
      ? fallbackState(
          "pyramid-failed",
          response?.retryable ?? true,
          response?.error_code ?? "pyramid_unavailable",
        )
      : singleAllowed
        ? singleSource(task, identity)
        : fallbackState(
            "pyramid-failed",
            response?.retryable ?? true,
            response?.error_code ?? "single_decode_budget_exceeded",
          );
  }

  if (summary.status === "ready" || summary.status === "pending" || summary.status === "building") {
    return summary.required || summary.status === "ready"
      ? fallbackState("pyramid-pending", false)
      : singleAllowed
        ? singleSource(task, identity)
        : fallbackState("pyramid-pending", false);
  }
  return singleSource(task, identity);
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
  const [retryWindow, setRetryWindow] = useState<{ taskId: string; untilMs: number } | null>(null);
  const summary = task?.image_pyramid;
  const queryKey = useMemo(
    () => ["image-pyramid", ownerKey, task?.id, summary?.generation] as const,
    [ownerKey, summary?.generation, task?.id],
  );
  const retryUntilMs = retryWindow && retryWindow.taskId === task?.id ? retryWindow.untilMs : 0;
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
    const result = await tasksApi.retryImagePyramid(task.id);
    if (originalOwnerKey() !== ownerKey) return;
    setRetryWindow({ taskId: task.id, untilMs: Date.now() + 30_000 });
    queryClient.setQueryData<ImagePyramidResponse>(queryKey, (current) => ({
      task_id: task.id,
      status: result.status === "building" ? "building" : "pending",
      required: current?.required ?? summary?.required ?? true,
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
  }, [queryClient, queryKey, summary?.generation, summary?.required, task?.id, ownerKey]);
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

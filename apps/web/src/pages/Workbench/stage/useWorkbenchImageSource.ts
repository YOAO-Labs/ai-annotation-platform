import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { tasksApi } from "@/api/tasks";
import { ApiError } from "@/api/client";
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
  task: TaskResponse | undefined,
  mediaIdentity?: string | null,
): {
  source: WorkbenchImageSource | null;
  /** Read fresh metadata and signed URLs without requesting a pyramid rebuild. */
  refresh: () => Promise<void>;
  retry: (() => Promise<void>) | undefined;
} {
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
        actorId,
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
      actorId,
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
  const source = useMemo(
    () =>
      resolveWorkbenchImageSource(task, query.data, {
        mediaIdentity,
        maxSingleDecodedBytes,
      }),
    [maxSingleDecodedBytes, mediaIdentity, query.data, task],
  );
  const refetch = query.refetch;
  const refresh = useCallback(async () => {
    if (shouldQuery) await refetch();
  }, [refetch, shouldQuery]);
  const retry = useCallback(async () => {
    if (!task?.id) return;
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
  }, [queryClient, queryKey, refetch, scope, summary, task]);
  return {
    source,
    refresh,
    retry: source?.kind === "pyramid-failed" && source.retryable ? retry : undefined,
  };
}

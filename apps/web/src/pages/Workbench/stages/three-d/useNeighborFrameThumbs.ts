import { pointCloudManifestQueryKey } from "@/api/pointCloudOriginals";
import { useOriginalAssetOwner } from "./useOriginalAssetOwner";
import { useMemo } from "react";
import { useQueries } from "@tanstack/react-query";
import { tasksApi } from "@/api/tasks";
import { useFrameNeighbors } from "@/hooks/useFrameNeighbors";
import type { PointCloudCameraOut } from "@/api/generated/types.gen";
import { cameraAnchor } from "./geometry/cameraAnchor";

export interface FrameThumb {
  frameIndex: number;
  imageUrl: string | null;
  isCurrent: boolean;
}

function frontCameraImage(cameras: PointCloudCameraOut[]): string | null {
  const front = cameras.find(
    (c) => c.image_url && cameraAnchor(c.calibration, c.role || c.name) === "top",
  );
  if (front) return front.image_url ?? null;
  return cameras.find((c) => c.image_url)?.image_url ?? null;
}

export function useNeighborFrameThumbs(
  taskId: string,
  frameIndex: number | null,
  k: number,
  enabled: boolean,
): FrameThumb[] {
  useOriginalAssetOwner();
  const { data: neighbors } = useFrameNeighbors(enabled ? taskId : null, k);

  const frames = useMemo<{ taskId: string; frameIndex: number; isCurrent: boolean }[]>(() => {
    if (!enabled || frameIndex == null) return [];
    return [
      ...(neighbors?.prev ?? []).map((n) => ({
        taskId: n.task_id,
        frameIndex: n.frame_index,
        isCurrent: false,
      })),
      { taskId, frameIndex, isCurrent: true },
      ...(neighbors?.next ?? []).map((n) => ({
        taskId: n.task_id,
        frameIndex: n.frame_index,
        isCurrent: false,
      })),
    ].sort((a, b) => a.frameIndex - b.frameIndex);
  }, [enabled, taskId, frameIndex, neighbors]);

  const results = useQueries({
    queries: frames.map((f) => ({
      queryKey: pointCloudManifestQueryKey(f.taskId),
      queryFn: ({ signal }: { signal: AbortSignal }) =>
        tasksApi.getPointCloudManifest(f.taskId, { signal }),
      enabled,
      staleTime: 5 * 60 * 1000,
    })),
  });

  return frames.map((f, i) => {
    const manifest = results[i]?.data;
    return {
      frameIndex: f.frameIndex,
      imageUrl: manifest ? frontCameraImage(manifest.cameras) : null,
      isCurrent: f.isCurrent,
    };
  });
}

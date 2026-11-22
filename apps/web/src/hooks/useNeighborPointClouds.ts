import { originalOwnerKey } from "@/api/originals";
import { useOriginalAssetOwner } from "@/pages/Workbench/stages/three-d/useOriginalAssetOwner";
import { useQueries } from "@tanstack/react-query";
import { tasksApi } from "@/api/tasks";
import { loadNeighborPcdPositions } from "@/pages/Workbench/stages/three-d/geometry/loadNeighborPcd";
import type { LidarAxisConvention } from "@/pages/Workbench/stages/three-d/geometry/axisConvention";

export interface NeighborPcd {
  taskId: string;
  frameIndex: number;
  positions: Float32Array;
}

export function useNeighborPointClouds(
  neighbors: { taskId: string; frameIndex: number }[],
  convention: LidarAxisConvention,
  targetCount: number,
  enabled: boolean,
): { items: NeighborPcd[]; isLoading: boolean } {
  const owner = useOriginalAssetOwner();
  const list = enabled ? neighbors : [];
  const results = useQueries({
    queries: list.map((n) => ({
      queryKey: ["neighbor-pcd", owner, n.taskId, convention, targetCount],
      queryFn: async ({ signal }: { signal: AbortSignal }): Promise<NeighborPcd | null> => {
        const manifest = await tasksApi.getPointCloudManifest(n.taskId, { signal });
        const url = manifest.point_cloud_url;
        if (!url) return null;
        const positions = await loadNeighborPcdPositions(url, convention, targetCount, signal);
        if (signal.aborted || owner !== originalOwnerKey())
          throw new DOMException("Original owner changed", "AbortError");
        return { taskId: n.taskId, frameIndex: n.frameIndex, positions };
      },
      enabled,
      staleTime: 5 * 60 * 1000,
      gcTime: 5 * 60 * 1000,
    })),
  });

  const items: NeighborPcd[] = [];
  for (const r of results) {
    if (r.data) items.push(r.data);
  }
  return { items, isLoading: results.some((r) => r.isLoading) };
}

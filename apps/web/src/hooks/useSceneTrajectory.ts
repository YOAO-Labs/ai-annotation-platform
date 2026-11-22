import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { scenesApi } from "@/api/scenes";
import type { FramePose } from "@/api/generated/types.gen";

export function useSceneTrajectory(sceneId: string | null | undefined) {
  const query = useQuery({
    queryKey: ["scene-trajectory", sceneId],
    queryFn: ({ signal }) => scenesApi.getTrajectory(sceneId!, { signal }),
    enabled: !!sceneId,
    staleTime: 30 * 60 * 1000,
  });

  const poseByFrame = useMemo<Map<number, FramePose> | null>(() => {
    const poses = query.data?.poses;
    if (!poses || poses.length === 0) return null;
    return new Map(poses.map((p) => [p.frame_index, p]));
  }, [query.data]);

  return { poseByFrame, isLoading: query.isLoading };
}

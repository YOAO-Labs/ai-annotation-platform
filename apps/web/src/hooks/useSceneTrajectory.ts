/**
 * scene 的逐帧 ego 轨迹(overlay ego 对齐消费)。
 *
 * 轨迹随导入固化、基本不变 → staleTime 放长;无位姿 scene 返回 poses=[],
 * 调用方按"无轨迹"降级(参考框退回不对齐叠加)。
 */
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

  // frame_index → pose 查找表;无数据 → null(与"无轨迹"同形)。
  const poseByFrame = useMemo<Map<number, FramePose> | null>(() => {
    const poses = query.data?.poses;
    if (!poses || poses.length === 0) return null;
    return new Map(poses.map((p) => [p.frame_index, p]));
  }, [query.data]);

  return { poseByFrame, isLoading: query.isLoading };
}

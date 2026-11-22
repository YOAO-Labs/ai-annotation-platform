import { loadDecodedPointCloudFrame } from "../pointCloudAssetCache";
import type { LidarAxisConvention } from "./axisConvention";

export async function loadNeighborPcdPositions(
  url: string,
  convention: LidarAxisConvention,
  targetCount: number,
  signal?: AbortSignal,
): Promise<Float32Array> {
  return (await loadDecodedPointCloudFrame(url, convention, targetCount, { signal })).positions;
}

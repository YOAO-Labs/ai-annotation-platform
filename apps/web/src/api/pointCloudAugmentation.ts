import { apiClient } from "./client";
import type { LidarAxisConvention } from "@/types";

export interface PointCloudAugmentationCamera {
  role: string;
  name: string;
  point_count: number;
  byte_size: number;
  sha256: string;
}

export interface PointCloudAugmentationManifest {
  task_id: string;
  enabled: boolean;
  status: "disabled" | "not_available" | "missing" | "building" | "ready" | "failed" | "stale";
  reason: string | null;
  asset_id: string | null;
  generation: number | null;
  profile: string | null;
  input_digest: string | null;
  coordinate_frame: "primary_pointcloud";
  axis_convention: LidarAxisConvention | null;
  cameras: PointCloudAugmentationCamera[];
}

export interface PointCloudAugmentationAssetRequest {
  asset_id: string;
  generation: number;
  camera_role: string;
}

export interface PointCloudAugmentationAsset {
  url: string;
  expires_in: number;
  format: "pcd";
  point_count: number;
  sha256: string;
  generation: number;
  camera_role: string;
  coordinate_frame: "primary_pointcloud";
  axis_convention: LidarAxisConvention;
}

export const pointCloudAugmentationApi = {
  manifest: (taskId: string, signal?: AbortSignal) =>
    apiClient.silentGet<PointCloudAugmentationManifest>(
      `/tasks/${taskId}/point-cloud/augmentation`,
      { signal },
    ),
  asset: (taskId: string, request: PointCloudAugmentationAssetRequest, signal?: AbortSignal) =>
    apiClient.post<PointCloudAugmentationAsset>(
      `/tasks/${taskId}/point-cloud/augmentation/assets`,
      request,
      { signal },
    ),
};

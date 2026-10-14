import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  pointCloudAugmentationApi,
  type PointCloudAugmentationManifest,
} from "@/api/pointCloudAugmentation";
import { isCurrentAuthOwner, useAuthStore } from "@/stores/authStore";
import type { LidarAxisConvention } from "./geometry/axisConvention";
import type { DecodedAuxiliaryPointCloudFrame } from "./geometry/pointcloudFrame";
import {
  augmentationContentKey,
  loadAugmentationAsset,
  PointCloudAugmentationCache,
} from "./pointCloudAugmentationAssets";

export const AUGMENTATION_STATUS_LABELS = {
  disabled: "项目或部署尚未开启 AI 补强实验",
  not_available: "当前任务不支持 AI 补强",
  missing: "当前帧尚未准备 AI 结果",
  building: "AI 结果正在准备，当前仅显示实测",
  failed: "AI 结果准备失败",
  stale: "AI 结果已过期，请管理员重新准备",
  ready: "AI 当前帧补强（实验）",
} as const;

interface AugmentationState {
  key: string;
  owner: object;
  frame: DecodedAuxiliaryPointCloudFrame | null;
  error: string | null;
}

export function defaultAugmentationCameraRole(cameras: readonly { role: string }[]): string | null {
  const roles = cameras.map((camera) => camera.role).sort();
  return roles.find((role) => /^(?:cam(?:era)?[_:-])?front$/i.test(role)) ?? roles[0] ?? null;
}

/** This hook owns display-only data. It never contributes to playback/frame readiness. */
export function usePointCloudAugmentation({
  taskId,
  enabled,
  measuredReady,
  cameraRole,
  convention,
  targetCount,
}: {
  taskId: string | null;
  enabled: boolean;
  measuredReady: boolean;
  cameraRole: string | null;
  convention: LidarAxisConvention;
  targetCount: number;
}) {
  const userId = useAuthStore((state) => state.user?.id ?? null);
  const token = useAuthStore((state) => state.token);
  const cache = useRef(new PointCloudAugmentationCache());
  const [state, setState] = useState<AugmentationState | null>(null);
  const [retryAttempt, setRetryAttempt] = useState(0);
  const active = enabled && !!taskId && !!userId && !!token;
  const query = useQuery({
    queryKey: ["point-cloud-augmentation", userId, active ? taskId : null],
    queryFn: ({ signal }) => pointCloudAugmentationApi.manifest(taskId!, signal),
    enabled: active,
    staleTime: 0,
    // Re-entering a frame/source must revalidate access before reusing decoded content.
    gcTime: 0,
    refetchInterval: active ? 30_000 : false,
    refetchOnWindowFocus: "always",
    retry: false,
  });
  const manifest: PointCloudAugmentationManifest | undefined =
    active && query.isSuccess && !query.isError && query.data.task_id === taskId
      ? query.data
      : undefined;
  const camera = manifest?.cameras.find((candidate) => candidate.role === cameraRole);
  const valid =
    manifest?.enabled &&
    manifest.status === "ready" &&
    manifest.asset_id &&
    manifest.generation != null &&
    manifest.input_digest &&
    manifest.coordinate_frame === "primary_pointcloud" &&
    manifest.axis_convention === convention;
  const key =
    valid && camera && userId && measuredReady
      ? augmentationContentKey(userId, manifest, camera, targetCount)
      : null;
  const identity = useMemo(
    () => ({ userId, token, key, retryAttempt }),
    [userId, token, key, retryAttempt],
  );
  const currentOwner = useRef(identity);
  currentOwner.current = identity;

  useEffect(() => {
    const ownedCache = cache.current;
    ownedCache.clear();
    return () => ownedCache.clear();
  }, [userId, token]);

  useEffect(() => {
    if (!key || !camera || !manifest || !userId) return;
    const controller = new AbortController();
    const owner = identity;
    const isOwner = () =>
      !controller.signal.aborted &&
      currentOwner.current === owner &&
      isCurrentAuthOwner(userId) &&
      useAuthStore.getState().token === token;
    const cached = cache.current.get(key);
    if (cached) {
      setState({ key, owner, frame: cached, error: null });
      return () => controller.abort();
    }
    setState({ key, owner, frame: null, error: null });
    void loadAugmentationAsset(
      manifest,
      camera,
      convention,
      targetCount,
      () =>
        pointCloudAugmentationApi.asset(
          taskId!,
          {
            asset_id: manifest.asset_id!,
            generation: manifest.generation!,
            camera_role: camera.role,
          },
          controller.signal,
        ),
      controller.signal,
    )
      .then((frame) => {
        if (!isOwner()) return;
        cache.current.put(key, frame);
        setState({ key, owner, frame, error: null });
      })
      .catch((error: unknown) => {
        if (!isOwner()) return;
        setState({
          key,
          owner,
          frame: null,
          error: error instanceof Error ? error.message : "辅助点云加载失败",
        });
      });
    return () => controller.abort();
    // Manifest polling retains a stable content key; only changed identity restarts asset work.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [identity, convention, targetCount, taskId]);

  const ownedState = key && state?.key === key && state.owner === identity ? state : null;
  const frame = ownedState?.frame ?? null;
  let message: string;
  if (!enabled) message = "当前实测点云";
  else if (query.isError) message = "AI 状态读取失败，当前仅显示实测";
  else if (!manifest) message = "读取 AI 补强状态…";
  else if (!manifest.enabled || manifest.status !== "ready")
    message = AUGMENTATION_STATUS_LABELS[manifest.status];
  else if (!valid) message = "AI 资产坐标或来源不匹配，当前仅显示实测";
  else if (!camera) message = "所选相机方向没有 AI 结果";
  else if (!measuredReady) message = "等待当前实测点云，AI 不阻塞切帧";
  else if (ownedState?.error) message = ownedState.error;
  else if (!frame) message = "加载 AI 辅助点…当前实测可用";
  else message = AUGMENTATION_STATUS_LABELS.ready;
  return {
    frame,
    manifest,
    camera,
    message,
    loading:
      active &&
      !frame &&
      !ownedState?.error &&
      (query.isPending || (valid && !!camera && measuredReady)),
    reason: manifest?.reason ?? null,
    retry: () => {
      setRetryAttempt((attempt) => attempt + 1);
      void query.refetch();
    },
    identityKey: key,
  };
}

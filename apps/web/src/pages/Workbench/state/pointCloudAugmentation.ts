import type { PointCloudAugmentationSource, WorkbenchPointcloudPreferences } from "@/api/auth";
import type { WorkbenchConfigPatch } from "./useWorkbenchConfig";

export const POINT_CLOUD_AUGMENTATION_OPTIONS = [
  { value: "none", label: "关闭（仅当前实测）" },
  { value: "neighbors", label: "邻帧实测补强" },
  { value: "vggt_omega", label: "AI 当前帧补强（实验）" },
] satisfies Array<{ value: PointCloudAugmentationSource; label: string }>;

export function isPointCloudAugmentationSource(
  value: unknown,
): value is PointCloudAugmentationSource {
  return value === "none" || value === "neighbors" || value === "vggt_omega";
}

/** Resolve old saved intent without hydrating a new preference or enabling hidden neighbors. */
export function resolvePointCloudAugmentation(
  preferences: Pick<
    WorkbenchPointcloudPreferences,
    "augmentationSource" | "displayStyle" | "neighborPointOverlay" | "neighborPointOverlayK"
  >,
) {
  const explicit = isPointCloudAugmentationSource(preferences.augmentationSource)
    ? preferences.augmentationSource
    : null;
  const legacySurface =
    explicit === null &&
    !preferences.neighborPointOverlay &&
    preferences.displayStyle === "surface";
  const source: PointCloudAugmentationSource =
    explicit ?? (preferences.neighborPointOverlay || legacySurface ? "neighbors" : "none");
  const radius =
    preferences.neighborPointOverlayK === 2 || preferences.neighborPointOverlayK === 3
      ? preferences.neighborPointOverlayK
      : 1;
  return {
    source,
    legacySurface,
    neighborPlan: {
      frameCount: source === "neighbors" ? (legacySurface ? 1 : radius) : 0,
      displayMode:
        explicit === null && !preferences.neighborPointOverlay ? "surface" : "diagnostic",
    } as { frameCount: 0 | 1 | 2 | 3; displayMode: "surface" | "diagnostic" },
  };
}

/** Both settings surfaces and the viewport submit the same compound preference. */
export function buildPointCloudAugmentationPatch(
  source: PointCloudAugmentationSource,
): WorkbenchConfigPatch {
  return {
    pointcloud: { augmentationSource: source, neighborPointOverlay: source === "neighbors" },
  };
}

/** Keep old enable-only writers compatible in optimistic state and full-tree saves. */
export function normalizePointCloudPreferencePatch(
  patch: Partial<WorkbenchPointcloudPreferences> | undefined,
): Partial<WorkbenchPointcloudPreferences> | undefined {
  if (!patch) return patch;
  if (isPointCloudAugmentationSource(patch.augmentationSource)) {
    return { ...patch, neighborPointOverlay: patch.augmentationSource === "neighbors" };
  }
  if (
    Object.prototype.hasOwnProperty.call(patch, "neighborPointOverlay") &&
    !Object.prototype.hasOwnProperty.call(patch, "augmentationSource")
  ) {
    return { ...patch, augmentationSource: null };
  }
  return patch;
}

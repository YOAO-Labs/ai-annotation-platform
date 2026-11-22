import type { WorkbenchConfigPatch } from "../../state/useWorkbenchConfig";

export const POINT_MASK_SELECT_MODES = ["rect", "lasso", "polygon"] as const;
export type PointMaskSelectMode = (typeof POINT_MASK_SELECT_MODES)[number];

export const CROSS_FRAME_OVERLAY_K_VALUES = [0, 1, 3, 5, 7] as const;
export type CrossFrameOverlayK = (typeof CROSS_FRAME_OVERLAY_K_VALUES)[number];

export const LEGACY_POINT_MASK_MODE_KEY = "workbench.pointMaskSelectMode";
export const LEGACY_CROSS_FRAME_OVERLAY_K_KEY = "workbench.crossFrameOverlayK";

type StorageLike = Pick<Storage, "getItem" | "setItem" | "removeItem">;

export interface PointcloudLegacyMigration {
  patch: WorkbenchConfigPatch | null;
  migratedKey: string;
  staleKeys: string[];
}

export function isPointMaskSelectMode(value: unknown): value is PointMaskSelectMode {
  return (
    typeof value === "string" && POINT_MASK_SELECT_MODES.includes(value as PointMaskSelectMode)
  );
}

export function isCrossFrameOverlayK(value: unknown): value is CrossFrameOverlayK {
  return (
    typeof value === "number" && CROSS_FRAME_OVERLAY_K_VALUES.includes(value as CrossFrameOverlayK)
  );
}

export function pointcloudLegacyMigratedKey(userId: string): string {
  return `workbench.${userId}.pcd.migrated`;
}

export function buildPointcloudLegacyMigration(
  userId: string,
  storage: StorageLike,
): PointcloudLegacyMigration | null {
  const migratedKey = pointcloudLegacyMigratedKey(userId);
  if (storage.getItem(migratedKey) === "1") return null;

  const staleKeys: string[] = [];
  const pointcloud: WorkbenchConfigPatch["pointcloud"] = {};
  const common: WorkbenchConfigPatch["common"] = {};

  const rawMode = storage.getItem(LEGACY_POINT_MASK_MODE_KEY);
  if (isPointMaskSelectMode(rawMode)) {
    pointcloud.pointMaskSelectMode = rawMode;
    staleKeys.push(LEGACY_POINT_MASK_MODE_KEY);
  }

  const rawOverlayK = storage.getItem(LEGACY_CROSS_FRAME_OVERLAY_K_KEY);
  const overlayK = rawOverlayK == null ? NaN : Number(rawOverlayK);
  if (isCrossFrameOverlayK(overlayK)) {
    common.crossFrameOverlayEnabled = overlayK > 0;
    common.crossFrameOverlayK = overlayK > 0 ? overlayK : 1;
    staleKeys.push(LEGACY_CROSS_FRAME_OVERLAY_K_KEY);
  }

  const patch: WorkbenchConfigPatch = {};
  if (Object.keys(pointcloud).length > 0) patch.pointcloud = pointcloud;
  if (Object.keys(common).length > 0) patch.common = common;

  return {
    patch: Object.keys(patch).length > 0 ? patch : null,
    migratedKey,
    staleKeys,
  };
}

export function finishPointcloudLegacyMigration(
  migration: PointcloudLegacyMigration,
  storage: StorageLike,
): void {
  storage.setItem(migration.migratedKey, "1");
  for (const key of migration.staleKeys) storage.removeItem(key);
}

type PointcloudStickyToggle = "colorizeOn" | "depthOn";

function pointcloudStickyKey(userId: string, name: PointcloudStickyToggle): string {
  return `workbench.${userId}.pcd.${name}`;
}

export function readPointcloudStickyToggle(
  userId: string,
  name: PointcloudStickyToggle,
  storage: StorageLike,
): boolean {
  const raw = storage.getItem(pointcloudStickyKey(userId, name));
  return raw === "1";
}

export function writePointcloudStickyToggle(
  userId: string,
  name: PointcloudStickyToggle,
  value: boolean,
  storage: StorageLike,
): void {
  storage.setItem(pointcloudStickyKey(userId, name), value ? "1" : "0");
}

export interface PsrPanelUiState {
  expanded: boolean;
  dx: number;
  dy: number;
}

const PSR_PANEL_DEFAULT: PsrPanelUiState = { expanded: false, dx: 0, dy: 0 };

function psrPanelKey(userId: string): string {
  return `workbench.${userId}.pcd.psrPanel`;
}

export function readPsrPanelUiState(userId: string, storage: StorageLike): PsrPanelUiState {
  const raw = storage.getItem(psrPanelKey(userId));
  if (!raw) return { ...PSR_PANEL_DEFAULT };
  try {
    const parsed = JSON.parse(raw) as Partial<PsrPanelUiState>;
    return {
      expanded: parsed.expanded === true,
      dx: Number.isFinite(parsed.dx) ? Number(parsed.dx) : 0,
      dy: Number.isFinite(parsed.dy) ? Number(parsed.dy) : 0,
    };
  } catch {
    return { ...PSR_PANEL_DEFAULT };
  }
}

export function writePsrPanelUiState(
  userId: string,
  state: PsrPanelUiState,
  storage: StorageLike,
): void {
  storage.setItem(psrPanelKey(userId), JSON.stringify(state));
}

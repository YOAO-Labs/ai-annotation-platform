import type { Viewport } from "../state/useViewportTransform";

export interface ImagePyramidSummary {
  status: ImagePyramidResponse["status"];
  generation: number | null;
  width: number | null;
  height: number | null;
  tile_size: number;
  format: string | null;
  required: boolean;
  source_snapshot?: string | null;
  profile_version?: string | null;
  building_generation?: number | null;
  building_status?: "pending" | "building" | null;
  retryable?: boolean;
  retry_after_ms?: number | null;
  error_code?: string | null;
}

export interface ImagePyramidLevel {
  level: number;
  scaleFactor: number;
  width: number;
  height: number;
  columns: number;
  rows: number;
}

export interface ImagePyramidManifestV1 {
  schema: "aap-image-pyramid/v1";
  generation: number;
  sourceFingerprint: string;
  normalizationVersion: string;
  width: number;
  height: number;
  tileSize: number;
  overlap: number;
  format: "webp";
  levels: ImagePyramidLevel[];
  overview: {
    width: number;
    height: number;
    contentDigest: string;
  };
}

export interface ImagePyramidResponse {
  task_id: string;
  status:
    | "not_available"
    | "missing"
    | "pending"
    | "building"
    | "ready"
    | "failed"
    | "stale"
    | "inconsistent";
  required: boolean;
  source_snapshot?: string | null;
  profile_version?: string | null;
  width?: number | null;
  height?: number | null;
  retryable: boolean;
  retry_after_ms: number | null;
  generation: number | null;
  building_generation: number | null;
  building_status: "pending" | "building" | null;
  error_code: string | null;
  manifest: ImagePyramidManifestV1 | null;
  overview: { url: string; expires_at: string } | null;
}

export type ImagePyramidAssetRequest =
  | { kind: "overview"; generation: number }
  | { kind: "tile"; generation: number; level: number; x: number; y: number };

export interface ImagePyramidAssetUrlsResponse {
  task_id: string;
  generation: number;
  expires_at: string;
  items: Array<{
    kind: "overview" | "tile";
    generation: number;
    level: number | null;
    x: number | null;
    y: number | null;
    url: string;
  }>;
}

export type WorkbenchImageSource =
  | {
      kind: "single";
      identity: string;
      url: string;
      width?: number;
      height?: number;
      thumbnailUrl?: string;
      blurhash?: string;
    }
  | {
      kind: "pyramid";
      taskId: string;
      identity: string;
      generation: number;
      manifest: ImagePyramidManifestV1;
      overviewUrl?: string;
      thumbnailUrl?: string;
      blurhash?: string;
    }
  | {
      kind: "pyramid-pending" | "pyramid-failed";
      taskId: string;
      identity: string;
      width: number;
      height: number;
      overviewUrl?: string;
      thumbnailUrl?: string;
      blurhash?: string;
      retryable: boolean;
      errorCode?: string;
    };

export interface PixelRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface ImageTileCoordinate {
  level: number;
  x: number;
  y: number;
}

export interface ImageTileGeometry extends ImageTileCoordinate {
  key: string;
  world: PixelRect;
  crop: PixelRect;
  decodedWidth: number;
  decodedHeight: number;
  decodedBytes: number;
}

export interface ImageTileDeviceBudget {
  retainedBytes: number;
  concurrency: number;
  overscanTiles: number;
}

const MIB = 1024 * 1024;
const LOD_KEEP_MIN = 0.75;
const LOD_KEEP_MAX = 1.25;

export function singleImageFitsDecodedBudget(
  width: number | null | undefined,
  height: number | null | undefined,
  maxDecodedBytes: number,
): boolean {
  return (
    typeof width === "number" &&
    Number.isFinite(width) &&
    width > 0 &&
    typeof height === "number" &&
    Number.isFinite(height) &&
    height > 0 &&
    Number.isFinite(maxDecodedBytes) &&
    width * height * 4 <= maxDecodedBytes
  );
}

function positiveInteger(value: unknown, field: string): number {
  if (!Number.isInteger(value) || Number(value) <= 0) {
    throw new Error(`invalid image pyramid ${field}`);
  }
  return Number(value);
}

export function parseImagePyramidManifest(value: unknown): ImagePyramidManifestV1 {
  if (!value || typeof value !== "object") throw new Error("invalid image pyramid manifest");
  const input = value as Record<string, unknown>;
  if (input.schema !== "aap-image-pyramid/v1" || input.format !== "webp") {
    throw new Error("unsupported image pyramid manifest");
  }
  const generation = positiveInteger(input.generation, "generation");
  const width = positiveInteger(input.width, "width");
  const height = positiveInteger(input.height, "height");
  const tileSize = positiveInteger(input.tileSize, "tileSize");
  if (!Number.isInteger(input.overlap) || Number(input.overlap) < 0) {
    throw new Error("invalid image pyramid overlap");
  }
  if (!Array.isArray(input.levels) || input.levels.length === 0) {
    throw new Error("invalid image pyramid levels");
  }
  const levels = input.levels.map((candidate, index): ImagePyramidLevel => {
    if (!candidate || typeof candidate !== "object") {
      throw new Error("invalid image pyramid level");
    }
    const level = candidate as Record<string, unknown>;
    const levelNumber = Number(level.level);
    const scaleFactor = positiveInteger(level.scaleFactor, "scaleFactor");
    const levelWidth = positiveInteger(level.width, "level width");
    const levelHeight = positiveInteger(level.height, "level height");
    const columns = positiveInteger(level.columns, "columns");
    const rows = positiveInteger(level.rows, "rows");
    if (
      levelNumber !== index ||
      scaleFactor !== 2 ** index ||
      levelWidth !== Math.ceil(width / scaleFactor) ||
      levelHeight !== Math.ceil(height / scaleFactor) ||
      columns !== Math.ceil(levelWidth / tileSize) ||
      rows !== Math.ceil(levelHeight / tileSize)
    ) {
      throw new Error("inconsistent image pyramid level");
    }
    return {
      level: levelNumber,
      scaleFactor,
      width: levelWidth,
      height: levelHeight,
      columns,
      rows,
    };
  });
  const overview = input.overview as Record<string, unknown> | undefined;
  if (!overview || typeof overview.contentDigest !== "string") {
    throw new Error("invalid image pyramid overview");
  }
  return {
    schema: "aap-image-pyramid/v1",
    generation,
    sourceFingerprint: String(input.sourceFingerprint ?? ""),
    normalizationVersion: String(input.normalizationVersion ?? ""),
    width,
    height,
    tileSize,
    overlap: Number(input.overlap),
    format: "webp",
    levels,
    overview: {
      width: positiveInteger(overview.width, "overview width"),
      height: positiveInteger(overview.height, "overview height"),
      contentDigest: overview.contentDigest,
    },
  };
}

export function visibleImageRect(
  viewport: { width: number; height: number },
  transform: Viewport,
  image: { width: number; height: number },
  overscanScreenPx = 0,
): PixelRect | null {
  if (
    viewport.width <= 0 ||
    viewport.height <= 0 ||
    image.width <= 0 ||
    image.height <= 0 ||
    !Number.isFinite(transform.scale) ||
    transform.scale <= 0
  ) {
    return null;
  }
  const left = Math.max(0, Math.floor((-transform.tx - overscanScreenPx) / transform.scale));
  const top = Math.max(0, Math.floor((-transform.ty - overscanScreenPx) / transform.scale));
  const right = Math.min(
    image.width,
    Math.ceil((viewport.width - transform.tx + overscanScreenPx) / transform.scale),
  );
  const bottom = Math.min(
    image.height,
    Math.ceil((viewport.height - transform.ty + overscanScreenPx) / transform.scale),
  );
  if (right <= left || bottom <= top) return null;
  return { x: left, y: top, width: right - left, height: bottom - top };
}

export function chooseImagePyramidLevel(
  manifest: ImagePyramidManifestV1,
  viewportScale: number,
  devicePixelRatio: number,
  currentLevel?: number | null,
): number {
  const screenScale = Math.max(Number.EPSILON, viewportScale * Math.max(1, devicePixelRatio));
  if (currentLevel != null) {
    const current = manifest.levels[currentLevel];
    if (current) {
      const devicePixelsPerLevelPixel = screenScale * current.scaleFactor;
      if (devicePixelsPerLevelPixel >= LOD_KEEP_MIN && devicePixelsPerLevelPixel <= LOD_KEEP_MAX) {
        return current.level;
      }
    }
  }
  for (let index = manifest.levels.length - 1; index >= 0; index -= 1) {
    const level = manifest.levels[index];
    if (screenScale * level.scaleFactor <= LOD_KEEP_MAX) return level.level;
  }
  return 0;
}

export function imageTileGeometry(
  sourceIdentity: string,
  manifest: ImagePyramidManifestV1,
  coordinate: ImageTileCoordinate,
): ImageTileGeometry {
  const level = manifest.levels[coordinate.level];
  if (
    !level ||
    coordinate.x < 0 ||
    coordinate.x >= level.columns ||
    coordinate.y < 0 ||
    coordinate.y >= level.rows
  ) {
    throw new Error("invalid image tile coordinate");
  }
  const coreX = coordinate.x * manifest.tileSize;
  const coreY = coordinate.y * manifest.tileSize;
  const coreWidth = Math.min(manifest.tileSize, level.width - coreX);
  const coreHeight = Math.min(manifest.tileSize, level.height - coreY);
  const leftOverlap = coordinate.x > 0 ? manifest.overlap : 0;
  const rightOverlap = coordinate.x + 1 < level.columns ? manifest.overlap : 0;
  const topOverlap = coordinate.y > 0 ? manifest.overlap : 0;
  const bottomOverlap = coordinate.y + 1 < level.rows ? manifest.overlap : 0;
  const decodedWidth = coreWidth + leftOverlap + rightOverlap;
  const decodedHeight = coreHeight + topOverlap + bottomOverlap;
  const worldX = coreX * level.scaleFactor;
  const worldY = coreY * level.scaleFactor;
  const worldRight = Math.min(manifest.width, (coreX + coreWidth) * level.scaleFactor);
  const worldBottom = Math.min(manifest.height, (coreY + coreHeight) * level.scaleFactor);
  return {
    ...coordinate,
    key: `${sourceIdentity}/${coordinate.level}/${coordinate.x}/${coordinate.y}/${manifest.format}`,
    world: {
      x: worldX,
      y: worldY,
      width: worldRight - worldX,
      height: worldBottom - worldY,
    },
    crop: {
      x: leftOverlap,
      y: topOverlap,
      width: coreWidth,
      height: coreHeight,
    },
    decodedWidth,
    decodedHeight,
    decodedBytes: decodedWidth * decodedHeight * 4,
  };
}

export function imageTilesForRect(
  manifest: ImagePyramidManifestV1,
  levelNumber: number,
  rect: PixelRect,
  overscanTiles = 0,
): ImageTileCoordinate[] {
  const level = manifest.levels[levelNumber];
  if (!level) return [];
  const scale = level.scaleFactor;
  const left = Math.max(
    0,
    Math.floor(rect.x / scale / manifest.tileSize) - Math.max(0, overscanTiles),
  );
  const top = Math.max(
    0,
    Math.floor(rect.y / scale / manifest.tileSize) - Math.max(0, overscanTiles),
  );
  const right = Math.min(
    level.columns - 1,
    Math.floor((rect.x + rect.width - 1) / scale / manifest.tileSize) + Math.max(0, overscanTiles),
  );
  const bottom = Math.min(
    level.rows - 1,
    Math.floor((rect.y + rect.height - 1) / scale / manifest.tileSize) + Math.max(0, overscanTiles),
  );
  const output: ImageTileCoordinate[] = [];
  for (let y = top; y <= bottom; y += 1) {
    for (let x = left; x <= right; x += 1) output.push({ level: levelNumber, x, y });
  }
  return output;
}

export function imageTileDeviceBudget(deviceMemory?: number | null): ImageTileDeviceBudget {
  if (
    deviceMemory != null &&
    Number.isFinite(deviceMemory) &&
    deviceMemory > 0 &&
    deviceMemory <= 2
  ) {
    return { retainedBytes: 32 * MIB, concurrency: 2, overscanTiles: 0 };
  }
  if (deviceMemory != null && Number.isFinite(deviceMemory) && deviceMemory >= 8) {
    return { retainedBytes: 128 * MIB, concurrency: 6, overscanTiles: 1 };
  }
  return { retainedBytes: 64 * MIB, concurrency: 4, overscanTiles: 1 };
}

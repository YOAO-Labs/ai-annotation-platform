import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

export interface SourceGifVariant {
  target: string;
  fps?: number;
  maxWidth?: number;
  maxColors?: number;
  startSec?: number;
  durationSec?: number;
}

export interface FlowArtifactProvenance {
  repoRoot: string;
  targetPath: string;
  assetId: string;
  role: "docs-gif" | "docs-video" | "poster" | "home-video" | "home-poster";
  source: string;
  testTitle: string;
  seedRevision: string | null;
  capturedCommit: string;
  sourceWorktreeDirty: boolean;
  watchPaths?: string[];
  manifestPath?: string;
  capture?: Record<string, unknown>;
  sourceAsset?: {
    runId: string;
    assetId: string;
    sha256: string;
    quality?: "standard" | "marketing";
    clip?: { start: number; duration: number };
    poster_time_seconds?: number;
  };
}

interface FlowManifest {
  schema_version: 1;
  updated_at: string;
  entries: Record<string, Record<string, unknown>>;
}

export function sha256File(filePath: string): string {
  const hash = createHash("sha256");
  const fd = fs.openSync(filePath, "r");
  const buffer = Buffer.allocUnsafe(1024 * 1024);
  try {
    let count: number;
    while ((count = fs.readSync(fd, buffer)) > 0) hash.update(buffer.subarray(0, count));
    return hash.digest("hex");
  } finally {
    fs.closeSync(fd);
  }
}

function mediaFacts(filePath: string): Record<string, unknown> | null {
  try {
    const output = execFileSync(
      "ffprobe",
      [
        "-v",
        "error",
        "-show_entries",
        "format=duration,format_name:stream=codec_name,width,height,avg_frame_rate,r_frame_rate",
        "-of",
        "json",
        filePath,
      ],
      { encoding: "utf8" },
    );
    const parsed = JSON.parse(output) as {
      format?: { duration?: string; format_name?: string };
      streams?: Array<Record<string, unknown>>;
    };
    const video = parsed.streams?.find(
      (stream) => typeof stream.width === "number" && typeof stream.height === "number",
    );
    return {
      container: parsed.format?.format_name ?? null,
      codec: video?.codec_name ?? null,
      width: video?.width ?? null,
      height: video?.height ?? null,
      fps: video?.avg_frame_rate ?? video?.r_frame_rate ?? null,
      duration_seconds: parsed.format?.duration ? Number(parsed.format.duration) : null,
    };
  } catch {
    return null;
  }
}

function readManifest(manifestPath: string): FlowManifest {
  if (!fs.existsSync(manifestPath)) {
    return { schema_version: 1, updated_at: new Date(0).toISOString(), entries: {} };
  }
  const parsed = JSON.parse(fs.readFileSync(manifestPath, "utf8")) as Partial<FlowManifest>;
  if (parsed.schema_version !== 1 || !parsed.entries) {
    throw new Error(`[flow-manifest] 不支持的 manifest：${manifestPath}`);
  }
  return parsed as FlowManifest;
}

export function recordFlowArtifact(input: FlowArtifactProvenance): string {
  const manifestPath =
    input.manifestPath ??
    path.join(input.repoRoot, "apps/web/e2e/screenshots/outputs/flow-manifest.json");
  const absoluteTarget = path.resolve(input.targetPath);
  const relativeTarget = path.relative(input.repoRoot, absoluteTarget).replaceAll("\\", "/");
  if (relativeTarget.startsWith("../") || path.isAbsolute(relativeTarget)) {
    throw new Error(`[flow-manifest] 资产必须位于仓库内：${absoluteTarget}`);
  }
  if (!fs.existsSync(absoluteTarget)) {
    throw new Error(`[flow-manifest] 资产不存在：${relativeTarget}`);
  }

  const manifest = readManifest(manifestPath);
  const generatedAt = new Date().toISOString();
  manifest.updated_at = generatedAt;
  manifest.entries[relativeTarget] = {
    asset_id: input.assetId,
    role: input.role,
    generated_at: generatedAt,
    captured_commit: input.capturedCommit,
    source_worktree_dirty: input.sourceWorktreeDirty,
    seed_revision: input.seedRevision,
    source: input.source,
    test_title: input.testTitle,
    watch_paths: [...new Set([input.source, ...(input.watchPaths ?? [])])].sort(),
    sha256: sha256File(absoluteTarget),
    bytes: fs.statSync(absoluteTarget).size,
    media: mediaFacts(absoluteTarget),
    ...(input.capture ? { capture: input.capture } : {}),
    ...(input.sourceAsset ? { source_asset: input.sourceAsset } : {}),
  };
  manifest.entries = Object.fromEntries(
    Object.entries(manifest.entries).sort(([left], [right]) => left.localeCompare(right)),
  );

  fs.mkdirSync(path.dirname(manifestPath), { recursive: true });
  const tempPath = `${manifestPath}.${process.pid}.tmp`;
  fs.writeFileSync(tempPath, `${JSON.stringify(manifest, null, 2)}\n`);
  fs.renameSync(tempPath, manifestPath);
  return manifestPath;
}

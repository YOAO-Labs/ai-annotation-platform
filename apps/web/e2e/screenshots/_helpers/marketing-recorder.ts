import { createHash, randomUUID } from "node:crypto";
import { execFileSync, spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import type { Video } from "@playwright/test";
import type { MarketingAssetSpec } from "./marketing-assets";
import type { SourceGifVariant } from "./flow-manifest";

export const MARKETING_PROJECT_NAME = "marketing-master";
export const MARKETING_CAPTURE_SIZE = { width: 3840, height: 2160 } as const;
export const MARKETING_CAPTURE_FPS = 60;

export interface MarketingCaptureCadence {
  sample_duration_ms: number;
  captured_frames: number;
  unique_frames: number;
  effective_unique_fps: number;
  unique_frame_ratio: number;
}

export interface MarketingRunContext {
  runId: string;
  createdAt: string;
  sourceCommit: string;
  sourceWorktreeDirty: boolean;
}

interface MarketingRecordingEvidence {
  clock: "live" | "unverified";
  backend_requirements: string | null;
  inference: "live" | "none" | "unverified";
  inference_evidence?: unknown;
  behavior_evidence?: unknown;
}

interface MarketingCaptureMetadata {
  assetId: string;
  source: string;
  watchPaths?: string[];
  testTitle: string;
  projectName: string;
  seedRevision: string | null;
  viewport: { width: number; height: number } | null;
  browser: { name: string; version: string };
  evidence?: MarketingRecordingEvidence;
}

interface MarketingArchiveOptions extends MarketingCaptureMetadata {
  archiveRoot: string;
  run: MarketingRunContext;
  video: Pick<Video, "saveAs">;
  captureExtension?: "webm" | "mkv";
  captureDriver?: "playwright" | "x11grab" | "gpu-screen-recorder" | "screencapturekit";
  deviceScaleFactor?: number;
  sourcePhysicalSize?: { width: number; height: number };
  captureCadence?: MarketingCaptureCadence;
  assetSpec: MarketingAssetSpec;
  capturedAt?: string;
  universalClip?: UniversalClipRequest;
  gifVariants?: SourceGifVariant[];
}

type UniversalClipRequest =
  | { startSeconds: number; durationSeconds: number }
  | { tailSeconds: number };

export function clipFromEpochWindow(
  recordingStartEpochMs: number,
  window: { startEpochMs: number; endEpochMs: number },
): { startSeconds: number; durationSeconds: number } {
  if (window.endEpochMs <= window.startEpochMs) {
    throw new Error("[marketing] 录制动作窗口的结束时间必须晚于开始时间");
  }
  const startSeconds = Math.max(0, (window.startEpochMs - recordingStartEpochMs) / 1000);
  return {
    startSeconds: Number(startSeconds.toFixed(3)),
    durationSeconds: Number(((window.endEpochMs - window.startEpochMs) / 1000).toFixed(3)),
  };
}

interface VideoProbe {
  container: string;
  codec: string;
  pixel_format: string | null;
  width: number;
  height: number;
  fps: number | null;
  duration_ms: number | null;
}

interface MarketingManifestEntry {
  asset_id: string;
  evidence?: MarketingRecordingEvidence;
  watch_paths?: string[];
  gif_variants?: SourceGifVariant[];
  source: string;
  test_title: string;
  project: string;
  seed_revision: string | null;
  viewport: { width: number; height: number } | null;
  browser: { name: string; version: string };
  captured_at: string;
  files: {
    capture_source: MarketingManifestFile;
    universal_mp4: MarketingManifestFile;
  };
  capture: {
    driver: "playwright" | "x11grab" | "gpu-screen-recorder" | "screencapturekit";
    fps: number;
    physical_size: { width: number; height: number };
    logical_viewport: { width: number; height: number } | null;
    device_scale_factor: number | null;
    source_physical_size: { width: number; height: number };
    resampling: "none" | "lanczos";
    cadence: MarketingCaptureCadence | null;
  };
  content: {
    title: string;
    theme: string;
    objective: string;
    duration_policy_seconds: {
      minimum: number;
      target: number;
      maximum: number;
    };
    shots: string[];
    editing_notes: string[];
  };
  review_status: "pending";
}

interface MarketingManifestFile {
  file: string;
  storage_key: string;
  sha256: string;
  bytes: number;
  media: VideoProbe;
  source_clip_seconds?: { start: number; requested_duration: number };
}

interface MarketingManifest {
  schema_version: 4;
  run: {
    id: string;
    created_at: string;
    source_commit: string;
    source_worktree_dirty: boolean;
    archive_policy: "local-private-master";
  };
  entries: Record<string, MarketingManifestEntry>;
}

const runContexts = new Map<string, MarketingRunContext>();

function pathForManifest(value: string): string {
  return value.split(path.sep).join("/");
}

function normalizedAssetId(assetId: string): string {
  const segments = assetId.split("/");
  if (
    assetId.length === 0 ||
    path.isAbsolute(assetId) ||
    segments.some((segment) => segment === "" || segment === "." || segment === "..")
  ) {
    throw new Error(`[marketing] 非法 asset id: ${assetId}`);
  }

  return segments
    .map((segment) => segment.replace(/[^a-zA-Z0-9._-]+/g, "-").replace(/^-+|-+$/g, ""))
    .map((segment) => {
      if (!segment || segment === "." || segment === "..") {
        throw new Error(`[marketing] 非法 asset id: ${assetId}`);
      }
      return segment;
    })
    .join("/");
}

function runId(createdAt: string, sourceCommit: string): string {
  const timestamp = createdAt.replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z");
  return `${timestamp}-${sourceCommit.slice(0, 12)}`;
}

export function getMarketingRunContext(repoRoot: string): MarketingRunContext {
  const cached = runContexts.get(repoRoot);
  if (cached) return cached;

  const sourceCommit = execFileSync("git", ["rev-parse", "HEAD"], {
    cwd: repoRoot,
    encoding: "utf8",
  }).trim();
  const sourceWorktreeDirty =
    execFileSync("git", ["status", "--porcelain", "--untracked-files=normal"], {
      cwd: repoRoot,
      encoding: "utf8",
    }).trim().length > 0;
  const explicitRunId = process.env.MARKETING_RUN_ID;
  const explicitCreatedAt = process.env.MARKETING_RUN_CREATED_AT;
  if (Boolean(explicitRunId) !== Boolean(explicitCreatedAt)) {
    throw new Error("[marketing] MARKETING_RUN_ID 与 MARKETING_RUN_CREATED_AT 必须同时提供");
  }
  const createdAt = explicitCreatedAt ?? new Date().toISOString();
  if (!Number.isFinite(Date.parse(createdAt))) {
    throw new Error(`[marketing] 非法 MARKETING_RUN_CREATED_AT: ${createdAt}`);
  }
  const expectedRunId = runId(createdAt, sourceCommit);
  if (explicitRunId && explicitRunId !== expectedRunId) {
    throw new Error(
      `[marketing] MARKETING_RUN_ID 与录制时间/当前提交不匹配: ` +
        `${explicitRunId}，期望 ${expectedRunId}`,
    );
  }
  const context = {
    runId: explicitRunId ?? expectedRunId,
    createdAt,
    sourceCommit,
    sourceWorktreeDirty,
  };
  runContexts.set(repoRoot, context);
  return context;
}

async function sha256File(filePath: string): Promise<string> {
  const hash = createHash("sha256");
  await new Promise<void>((resolve, reject) => {
    const input = fs.createReadStream(filePath);
    input.on("data", (chunk) => hash.update(chunk));
    input.on("error", reject);
    input.on("end", resolve);
  });
  return hash.digest("hex");
}

function parseFrameRate(value: unknown): number | null {
  if (typeof value !== "string") return null;
  const [numerator, denominator = "1"] = value.split("/");
  const numeratorValue = Number(numerator);
  const denominatorValue = Number(denominator);
  if (
    !Number.isFinite(numeratorValue) ||
    !Number.isFinite(denominatorValue) ||
    denominatorValue === 0
  ) {
    return null;
  }
  return Number((numeratorValue / denominatorValue).toFixed(3));
}

function probeVideo(filePath: string): VideoProbe {
  const result = spawnSync(
    "ffprobe",
    [
      "-v",
      "error",
      "-select_streams",
      "v:0",
      "-show_entries",
      "stream=codec_name,pix_fmt,width,height,avg_frame_rate:format=format_name,duration",
      "-of",
      "json",
      filePath,
    ],
    { encoding: "utf8" },
  );
  if (result.error || result.status !== 0) {
    const reason = result.error?.message ?? result.stderr.trim() ?? `exit ${result.status}`;
    throw new Error(`[marketing] ffprobe 无法校验母版: ${reason}`);
  }

  const payload = JSON.parse(result.stdout) as {
    streams?: Array<{
      codec_name?: string;
      pix_fmt?: string;
      width?: number;
      height?: number;
      avg_frame_rate?: string;
    }>;
    format?: { format_name?: string; duration?: string };
  };
  const stream = payload.streams?.[0];
  if (!stream?.codec_name || !stream.width || !stream.height) {
    throw new Error("[marketing] ffprobe 未返回有效的视频流信息");
  }
  const durationSeconds = Number(payload.format?.duration);
  return {
    container: payload.format?.format_name ?? "unknown",
    codec: stream.codec_name,
    pixel_format: stream.pix_fmt ?? null,
    width: stream.width,
    height: stream.height,
    fps: parseFrameRate(stream.avg_frame_rate),
    duration_ms: Number.isFinite(durationSeconds) ? Math.round(durationSeconds * 1000) : null,
  };
}

function validateExternalCaptureSignal(filePath: string, spec: MarketingAssetSpec): void {
  const result = spawnSync(
    "ffmpeg",
    [
      "-hide_banner",
      "-loglevel",
      "error",
      "-i",
      filePath,
      "-vf",
      "fps=1,signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-",
      "-f",
      "null",
      "-",
    ],
    { encoding: "utf8" },
  );
  if (result.error || result.status !== 0) {
    const reason = result.error?.message ?? result.stderr.trim() ?? `exit ${result.status}`;
    throw new Error(`[marketing] 无法检查 ${spec.assetId} 的画面信号: ${reason}`);
  }
  const averages = [...result.stdout.matchAll(/lavfi\.signalstats\.YAVG=([\d.]+)/g)].map((match) =>
    Number(match[1]),
  );
  if (averages.length === 0 || Math.max(...averages) <= 16.5) {
    throw new Error(`[marketing] ${spec.assetId} 采集源为全黑画面，拒绝归档`);
  }
}

function emptyManifest(run: MarketingRunContext): MarketingManifest {
  return {
    schema_version: 4,
    run: {
      id: run.runId,
      created_at: run.createdAt,
      source_commit: run.sourceCommit,
      source_worktree_dirty: run.sourceWorktreeDirty,
      archive_policy: "local-private-master",
    },
    entries: {},
  };
}

function readManifest(manifestPath: string, run: MarketingRunContext): MarketingManifest {
  if (!fs.existsSync(manifestPath)) return emptyManifest(run);
  const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8")) as MarketingManifest;
  if (
    manifest.schema_version !== 4 ||
    manifest.run?.id !== run.runId ||
    manifest.run?.source_commit !== run.sourceCommit ||
    !manifest.entries
  ) {
    throw new Error(`[marketing] manifest 与当前录制运行不匹配: ${manifestPath}`);
  }
  return manifest;
}

function validateCaptureSource(media: VideoProbe, spec: MarketingAssetSpec): void {
  if (
    media.width !== MARKETING_CAPTURE_SIZE.width ||
    media.height !== MARKETING_CAPTURE_SIZE.height
  ) {
    throw new Error(
      `[marketing] ${spec.assetId} 分辨率不符合高清母版要求: ` +
        `${media.width}×${media.height}，期望 ${MARKETING_CAPTURE_SIZE.width}×${MARKETING_CAPTURE_SIZE.height}`,
    );
  }
  if (media.duration_ms === null) {
    throw new Error(`[marketing] ${spec.assetId} 无法读取录制时长`);
  }
  if (media.fps === null || Math.abs(media.fps - MARKETING_CAPTURE_FPS) > 0.01) {
    throw new Error(
      `[marketing] ${spec.assetId} 帧率不符合母版要求: ${media.fps ?? "unknown"}fps，` +
        `期望 ${MARKETING_CAPTURE_FPS}fps`,
    );
  }
}

function validateContentDuration(media: VideoProbe, spec: MarketingAssetSpec): void {
  if (media.duration_ms === null) {
    throw new Error(`[marketing] ${spec.assetId} 无法读取录制时长`);
  }
  const actualSeconds = media.duration_ms / 1000;
  if (actualSeconds < spec.duration.minSeconds || actualSeconds > spec.duration.maxSeconds) {
    throw new Error(
      `[marketing] ${spec.assetId} 录制时长 ${actualSeconds.toFixed(2)}s 不在允许范围 ` +
        `${spec.duration.minSeconds}–${spec.duration.maxSeconds}s 内（建议 ${spec.duration.targetSeconds}s）`,
    );
  }
}

function transcodeUniversalMp4(
  sourcePath: string,
  targetPath: string,
  clip?: { startSeconds: number; durationSeconds: number },
): void {
  const clipArgs = clip
    ? ["-ss", clip.startSeconds.toFixed(3), "-t", clip.durationSeconds.toFixed(3)]
    : [];
  const result = spawnSync(
    "ffmpeg",
    [
      "-hide_banner",
      "-loglevel",
      "error",
      "-y",
      "-i",
      sourcePath,
      ...clipArgs,
      "-map",
      "0:v:0",
      "-an",
      "-vf",
      `fps=${MARKETING_CAPTURE_FPS}`,
      "-c:v",
      "libx264",
      "-preset",
      "medium",
      "-crf",
      "16",
      "-profile:v",
      "high",
      "-level:v",
      "5.1",
      "-pix_fmt",
      "yuv420p",
      "-tag:v",
      "avc1",
      "-movflags",
      "+faststart",
      targetPath,
    ],
    { encoding: "utf8" },
  );
  if (result.error || result.status !== 0) {
    const reason = result.error?.message ?? result.stderr.trim() ?? `exit ${result.status}`;
    throw new Error(`[marketing] ffmpeg 无法生成 MP4 通用母版: ${reason}`);
  }
}

function transcodeCaptureSource(
  sourcePath: string,
  targetPath: string,
  clip: { startSeconds: number; durationSeconds: number },
  hardwareEncode: boolean,
): void {
  const encoderArgs = hardwareEncode
    ? ["-c:v", "h264_nvenc", "-preset", "p2", "-rc", "constqp", "-qp", "12", "-bf", "0"]
    : ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "12"];
  const result = spawnSync(
    "ffmpeg",
    [
      "-hide_banner",
      "-loglevel",
      "error",
      "-y",
      "-i",
      sourcePath,
      "-ss",
      clip.startSeconds.toFixed(3),
      "-t",
      clip.durationSeconds.toFixed(3),
      "-map",
      "0:v:0",
      "-an",
      "-vf",
      `fps=${MARKETING_CAPTURE_FPS}`,
      ...encoderArgs,
      "-pix_fmt",
      "yuv420p",
      "-g",
      String(MARKETING_CAPTURE_FPS * 2),
      "-f",
      "matroska",
      targetPath,
    ],
    { encoding: "utf8" },
  );
  if (result.error || result.status !== 0) {
    const reason = result.error?.message ?? result.stderr.trim() ?? `exit ${result.status}`;
    throw new Error(`[marketing] ffmpeg 无法生成去除加载段的 MKV 采集源: ${reason}`);
  }
}

function resolveUniversalClip(
  request: UniversalClipRequest | undefined,
  captureMedia: VideoProbe,
): { startSeconds: number; durationSeconds: number } | undefined {
  if (!request || "startSeconds" in request) return request;
  if (captureMedia.duration_ms === null) {
    throw new Error("[marketing] 无法按尾部时长生成通用母版：采集源时长未知");
  }
  const captureSeconds = captureMedia.duration_ms / 1000;
  const durationSeconds = Math.min(request.tailSeconds, captureSeconds);
  return {
    startSeconds: Math.max(0, captureSeconds - durationSeconds),
    durationSeconds,
  };
}

function validateUniversalMp4(media: VideoProbe, spec: MarketingAssetSpec): void {
  validateCaptureSource(media, spec);
  validateContentDuration(media, spec);
  if (media.codec !== "h264" || !media.container.split(",").includes("mp4")) {
    throw new Error(
      `[marketing] ${spec.assetId} 通用母版必须是 MP4/H.264，实际为 ${media.container}/${media.codec}`,
    );
  }
}

function writeManifest(manifestPath: string, manifest: MarketingManifest): void {
  const temporary = `${manifestPath}.tmp-${randomUUID()}`;
  try {
    fs.writeFileSync(temporary, `${JSON.stringify(manifest, null, 2)}\n`);
    fs.renameSync(temporary, manifestPath);
  } finally {
    fs.rmSync(temporary, { force: true });
  }
}

export async function archiveMarketingMaster(options: MarketingArchiveOptions): Promise<{
  capturePath: string;
  masterPath: string;
  manifestPath: string;
  captureStorageKey: string;
  masterStorageKey: string;
}> {
  const assetId = normalizedAssetId(options.assetId);
  const captureExtension = options.captureExtension ?? "webm";
  if (options.assetSpec.assetId !== assetId) {
    throw new Error(
      `[marketing] 资产规格与归档目标不一致: ${options.assetSpec.assetId} != ${assetId}`,
    );
  }
  const runRoot = path.join(options.archiveRoot, options.run.runId);
  const incomingDir = path.join(runRoot, ".incoming");
  const incomingOriginalPath = path.join(
    incomingDir,
    `${assetId.replaceAll("/", "--")}-${randomUUID()}.untrimmed.${captureExtension}`,
  );
  const incomingCapturePath = path.join(
    incomingDir,
    `${assetId.replaceAll("/", "--")}-${randomUUID()}.${captureExtension}`,
  );
  const incomingMasterPath = path.join(
    incomingDir,
    `${assetId.replaceAll("/", "--")}-${randomUUID()}.mp4`,
  );
  const manifestPath = path.join(runRoot, "manifest.json");
  fs.mkdirSync(incomingDir, { recursive: true });

  try {
    await options.video.saveAs(incomingOriginalPath);
    const originalMedia = probeVideo(incomingOriginalPath);
    validateCaptureSource(originalMedia, options.assetSpec);
    const isExternalCapture =
      options.captureDriver === "x11grab" ||
      options.captureDriver === "gpu-screen-recorder" ||
      options.captureDriver === "screencapturekit";
    if (isExternalCapture) {
      validateExternalCaptureSignal(incomingOriginalPath, options.assetSpec);
    }
    const universalClip = resolveUniversalClip(options.universalClip, originalMedia);
    if (universalClip) {
      if (captureExtension !== "mkv") {
        throw new Error("[marketing] 去除加载段的高清采集源必须使用 MKV 容器");
      }
      transcodeCaptureSource(
        incomingOriginalPath,
        incomingCapturePath,
        universalClip,
        options.captureDriver === "x11grab" || options.captureDriver === "gpu-screen-recorder",
      );
    } else {
      fs.renameSync(incomingOriginalPath, incomingCapturePath);
    }
    const [captureSha256, captureMedia] = await Promise.all([
      sha256File(incomingCapturePath),
      Promise.resolve(probeVideo(incomingCapturePath)),
    ]);
    validateCaptureSource(captureMedia, options.assetSpec);
    validateContentDuration(captureMedia, options.assetSpec);
    transcodeUniversalMp4(incomingCapturePath, incomingMasterPath);
    const [masterSha256, masterMedia] = await Promise.all([
      sha256File(incomingMasterPath),
      Promise.resolve(probeVideo(incomingMasterPath)),
    ]);
    validateUniversalMp4(masterMedia, options.assetSpec);
    const manifest = readManifest(manifestPath, options.run);
    const previous = manifest.entries[assetId];
    if (previous && previous.files.capture_source.sha256 !== captureSha256) {
      throw new Error(`[marketing] 当次运行已存在不同母版: ${assetId}`);
    }
    const captureDir = path.join(runRoot, "raw", ...assetId.split("/"));
    const masterDir = path.join(runRoot, "masters", ...assetId.split("/"));
    const capturePath = path.join(captureDir, `${captureSha256}.${captureExtension}`);
    const masterPath = path.join(masterDir, `${masterSha256}.mp4`);
    fs.mkdirSync(captureDir, { recursive: true });
    fs.mkdirSync(masterDir, { recursive: true });
    if (fs.existsSync(capturePath)) {
      fs.rmSync(incomingCapturePath, { force: true });
    } else {
      fs.renameSync(incomingCapturePath, capturePath);
    }
    if (fs.existsSync(masterPath)) {
      fs.rmSync(incomingMasterPath, { force: true });
    } else {
      fs.renameSync(incomingMasterPath, masterPath);
    }

    const capturedAt = options.capturedAt ?? new Date().toISOString();
    const captureStorageKey = [
      "raw",
      assetId,
      capturedAt.slice(0, 10),
      options.run.sourceCommit,
      `${captureSha256}.${captureExtension}`,
    ].join("/");
    const masterStorageKey = [
      "masters",
      assetId,
      capturedAt.slice(0, 10),
      options.run.sourceCommit,
      `${masterSha256}.mp4`,
    ].join("/");
    manifest.entries[assetId] = {
      asset_id: assetId,
      ...(options.evidence ? { evidence: options.evidence } : {}),
      ...(options.gifVariants?.length
        ? {
            gif_variants: options.gifVariants.map((variant) => {
              // Both archived files are already trimmed to universalClip, unlike the
              // original external capture used by the flow's epoch-based windows.
              const startSec =
                variant.startSec === undefined
                  ? 0
                  : Math.max(0, variant.startSec - (universalClip?.startSeconds ?? 0));
              const remaining = Number(captureMedia.duration_ms) / 1000 - startSec;
              if (remaining <= 0) throw new Error("GIF window starts after the archived capture");
              if (variant.durationSec !== undefined && variant.durationSec > remaining + 0.45) {
                throw new Error("Archived capture is missing more than GIF trailing padding");
              }
              // Flow windows include trailing padding; stopping capture can shorten that padding.
              return {
                ...variant,
                startSec,
                durationSec: Math.min(variant.durationSec ?? remaining, remaining),
              };
            }),
          }
        : {}),
      source: pathForManifest(options.source),
      watch_paths: [...new Set([options.source, ...(options.watchPaths ?? [])])].map(
        pathForManifest,
      ),
      test_title: options.testTitle,
      project: options.projectName,
      seed_revision: options.seedRevision,
      viewport: options.viewport,
      browser: options.browser,
      captured_at: capturedAt,
      files: {
        capture_source: {
          file: pathForManifest(path.relative(runRoot, capturePath)),
          storage_key: captureStorageKey,
          sha256: captureSha256,
          bytes: fs.statSync(capturePath).size,
          media: captureMedia,
          ...(universalClip
            ? {
                source_clip_seconds: {
                  start: universalClip.startSeconds,
                  requested_duration: universalClip.durationSeconds,
                },
              }
            : {}),
        },
        universal_mp4: {
          file: pathForManifest(path.relative(runRoot, masterPath)),
          storage_key: masterStorageKey,
          sha256: masterSha256,
          bytes: fs.statSync(masterPath).size,
          media: masterMedia,
          ...(universalClip
            ? {
                source_clip_seconds: {
                  start: universalClip.startSeconds,
                  requested_duration: universalClip.durationSeconds,
                },
              }
            : {}),
        },
      },
      capture: {
        driver: options.captureDriver ?? "playwright",
        fps: MARKETING_CAPTURE_FPS,
        physical_size: { ...MARKETING_CAPTURE_SIZE },
        logical_viewport: options.viewport,
        device_scale_factor: options.deviceScaleFactor ?? null,
        source_physical_size: options.sourcePhysicalSize ?? { ...MARKETING_CAPTURE_SIZE },
        resampling: options.sourcePhysicalSize ? "lanczos" : "none",
        cadence: options.captureCadence ?? null,
      },
      content: {
        title: options.assetSpec.title,
        theme: options.assetSpec.theme,
        objective: options.assetSpec.objective,
        duration_policy_seconds: {
          minimum: options.assetSpec.duration.minSeconds,
          target: options.assetSpec.duration.targetSeconds,
          maximum: options.assetSpec.duration.maxSeconds,
        },
        shots: options.assetSpec.shots,
        editing_notes: options.assetSpec.editingNotes,
      },
      review_status: "pending",
    };
    manifest.entries = Object.fromEntries(
      Object.entries(manifest.entries).sort(([left], [right]) => left.localeCompare(right)),
    );
    writeManifest(manifestPath, manifest);
    return { capturePath, masterPath, manifestPath, captureStorageKey, masterStorageKey };
  } finally {
    fs.rmSync(incomingOriginalPath, { force: true });
    fs.rmSync(incomingCapturePath, { force: true });
    fs.rmSync(incomingMasterPath, { force: true });
    try {
      fs.rmdirSync(incomingDir);
    } catch {
      // 其它录制仍在使用该暂存目录时保留目录。
    }
  }
}

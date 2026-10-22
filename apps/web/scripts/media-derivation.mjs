import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { recordFlowArtifact, sha256File } from "../e2e/screenshots/_helpers/flow-manifest.ts";

export function checked(command, args) {
  const executable = command === "ffmpeg" ? (process.env.FFMPEG_PATH ?? command) : command;
  const result = spawnSync(executable, args, { encoding: "utf8" });
  if (result.error || result.status !== 0) {
    throw new Error(`[media] ${command}: ${result.error?.message ?? result.stderr}`);
  }
  return result.stdout;
}

let ffmpegWebp;

function probe(file) {
  const data = JSON.parse(
    checked("ffprobe", [
      "-v",
      "error",
      "-select_streams",
      "v:0",
      "-show_entries",
      "stream=codec_name,width,height,avg_frame_rate:format=duration",
      "-of",
      "json",
      file,
    ]),
  );
  const stream = data.streams?.[0];
  const [n, d = 1] = String(stream?.avg_frame_rate).split("/").map(Number);
  const result = {
    width: stream?.width,
    height: stream?.height,
    fps: n / d,
    duration: Number(data.format?.duration),
    codec: stream?.codec_name,
  };
  if (
    ![result.width, result.height, result.fps, result.duration].every(
      (v) => Number.isFinite(v) && v > 0,
    )
  ) {
    throw new Error(`[media] Invalid video facts: ${file}`);
  }
  return result;
}

function inside(root, relative) {
  if (typeof relative !== "string" || !relative || path.isAbsolute(relative))
    throw new Error("Invalid archive path");
  const target = path.resolve(root, relative);
  const realRoot = fs.realpathSync(root);
  const realTarget = fs.realpathSync(target);
  const delta = path.relative(realRoot, realTarget);
  if (delta.startsWith("../") || path.isAbsolute(delta))
    throw new Error("Archive path escapes its run directory");
  return realTarget;
}

export function readArchive(runDirectory) {
  const manifest = JSON.parse(fs.readFileSync(path.join(runDirectory, "manifest.json"), "utf8"));
  const entries = new Map();
  if (![1, 4].includes(manifest.schema_version) || !manifest.entries)
    throw new Error("Unsupported source manifest");
  for (const entry of Object.values(manifest.entries)) {
    const id = entry.asset_id;
    if (!/^[a-z0-9-]+(?:\/[a-z0-9-]+)*$/.test(id) || entries.has(id))
      throw new Error("Invalid or duplicate source asset ID");
    const marketing = manifest.schema_version === 4;
    if (!marketing && entry.capture?.profile !== "docs")
      throw new Error("Not a portable source archive");
    entries.set(id, {
      entry,
      quality: marketing ? "marketing" : "standard",
      runId: marketing ? manifest.run?.id : path.basename(runDirectory),
      commit: marketing ? manifest.run?.source_commit : entry.captured_commit,
      dirty: marketing ? manifest.run?.source_worktree_dirty : entry.source_worktree_dirty,
      main: marketing ? entry.files?.universal_mp4 : { file: `${id}.mp4`, sha256: entry.sha256 },
      raw: marketing ? entry.files?.capture_source : { file: `${id}.mp4`, sha256: entry.sha256 },
      gifs: marketing ? (entry.gif_variants ?? []) : (entry.capture?.gif_variants ?? []),
    });
  }
  return entries;
}

export function parseClip(value) {
  if (value === undefined) return undefined;
  const parts = value.split(":");
  const [start, duration] = parts.map(Number);
  if (
    parts.length !== 2 ||
    !parts.every((p) => p.trim()) ||
    !Number.isFinite(start) ||
    start < 0 ||
    !Number.isFinite(duration) ||
    duration <= 0
  )
    throw new Error("--clip must be start:duration in seconds");
  return { start, duration };
}

export function selectArchiveAssets(archive, requested, format, hasVideo) {
  return (
    requested ??
    [...archive.keys()].filter(
      (id) =>
        (format !== "gif" && hasVideo(id)) ||
        (format !== "video" && archive.get(id).gifs.length > 0),
    )
  );
}

function clipFor(media, clip) {
  const window = clip ?? { start: 0, duration: media.duration };
  if (
    !Number.isFinite(window.start) ||
    window.start < 0 ||
    !Number.isFinite(window.duration) ||
    window.duration <= 0 ||
    window.start + window.duration > media.duration + 0.05
  ) {
    throw new Error("Derivative clip is outside the archived source");
  }
  return window;
}

export function outputGeometry(media, maxWidth = 1280, maxHeight = 720, fps = 30) {
  if (![maxWidth, maxHeight, fps].every((v) => Number.isFinite(v) && v > 0))
    throw new Error("Invalid derivative geometry");
  const scale = Math.min(1, maxWidth / media.width, maxHeight / media.height);
  return {
    width: Math.max(2, Math.floor((media.width * scale) / 2) * 2),
    height: Math.max(2, Math.floor((media.height * scale) / 2) * 2),
    fps: Math.min(fps, media.fps),
  };
}
function privacyMaskFilter(masks, sourceOffsetSeconds) {
  if (masks === undefined) return "";
  if (
    !Array.isArray(masks) ||
    masks.some((mask) => {
      if (!mask || typeof mask !== "object" || Array.isArray(mask)) return true;
      const { x, y, width, height } = mask;
      return (
        ![x, y, width, height].every(Number.isFinite) ||
        x < 0 ||
        y < 0 ||
        width <= 0 ||
        height <= 0 ||
        x + width > 1 ||
        y + height > 1 ||
        (mask.fromSeconds !== undefined &&
          (!Number.isFinite(mask.fromSeconds) || mask.fromSeconds < 0)) ||
        (mask.untilSeconds !== undefined &&
          (!Number.isFinite(mask.untilSeconds) || mask.untilSeconds <= (mask.fromSeconds ?? 0)))
      );
    })
  ) {
    throw new Error("Privacy masks must be normalized rectangles");
  }
  return masks
    .map(({ x, y, width, height, fromSeconds, untilSeconds }) => {
      let condition = "";
      if (fromSeconds !== undefined) condition = `gte(t+${sourceOffsetSeconds},${fromSeconds})`;
      if (untilSeconds !== undefined) {
        condition += `${condition ? "*" : ""}lt(t+${sourceOffsetSeconds},${untilSeconds})`;
      }
      const enable = condition ? `:enable='${condition}'` : "";
      return `drawbox=x=iw*${x}:y=ih*${y}:w=iw*${width}:h=ih*${height}:color=black:t=fill${enable}`;
    })
    .join(",");
}

// Both publishers use this path: validate the complete request, encode to staging,
// then update only selected files and their provenance. No review is implied.
export function deriveMedia({
  repoRoot,
  runDirectory,
  quality = "marketing",
  assets,
  outputs,
  clip,
  posterAt,
  privateOutputDirectory,
}) {
  if (
    privateOutputDirectory !== undefined &&
    (!/^\.artifacts\/demos\/[a-z0-9-]+$/.test(privateOutputDirectory) || assets.length !== 1)
  )
    throw new Error("Private output requires one asset and .artifacts/demos/<name>");
  if (!["standard", "marketing"].includes(quality)) throw new Error("Unknown source quality");
  if (!assets.length || (clip && assets.length !== 1))
    throw new Error("--clip requires exactly one selected asset");
  if (posterAt !== undefined && (assets.length !== 1 || !Number.isFinite(posterAt) || posterAt < 0))
    throw new Error("--poster-at requires one selected asset and finite nonnegative seconds");
  const archive = readArchive(runDirectory);
  const jobs = [];
  const targets = new Set();
  for (const assetId of assets) {
    const item = archive.get(assetId);
    if (!item) throw new Error(`Source archive lacks ${assetId}`);
    if (
      item.quality !== quality ||
      !item.runId ||
      !item.commit ||
      typeof item.dirty !== "boolean"
    ) {
      throw new Error(
        `Source quality/provenance mismatch for ${assetId}; select --quality explicitly`,
      );
    }
    const sources = new Map();
    const recipes = outputs(assetId, item);
    if (clip && recipes.filter((recipe) => recipe.raw && recipe.kind === "gif").length > 1) {
      throw new Error(
        "One --clip cannot replace multiple GIF windows; select --format gif --gif-target, or --format video",
      );
    }
    for (const recipe of recipes) {
      if (!["mp4", "webm", "gif", "webp", "png"].includes(recipe.kind))
        throw new Error("Unsupported derivative format");
      if (
        typeof recipe.target !== "string" ||
        !(privateOutputDirectory
          ? recipe.target.startsWith(`${privateOutputDirectory}/`) &&
            ["mp4", "webp", "png"].includes(recipe.kind)
          : /^(docs-site\/(public|user-guide\/images)\/|docs\/articles\/media\/)/.test(
              recipe.target,
            )) ||
        recipe.target.split(/[\\/]/).includes("..") ||
        path.extname(recipe.target) !== `.${recipe.kind}`
      ) {
        throw new Error("Derivative target must be an enrolled media path");
      }
      if (targets.has(recipe.target))
        throw new Error(`Duplicate derivative target: ${recipe.target}`);
      let ancestor = path.dirname(path.resolve(repoRoot, recipe.target));
      while (!fs.existsSync(ancestor)) ancestor = path.dirname(ancestor);
      const parentDelta = path.relative(fs.realpathSync(repoRoot), fs.realpathSync(ancestor));
      if (parentDelta.startsWith("../") || path.isAbsolute(parentDelta)) {
        throw new Error("Output directory escapes repository");
      }
      targets.add(recipe.target);
      const type = recipe.raw ? "raw" : "main";
      if (!sources.has(type)) {
        const info = item[type];
        if (!/^[a-f0-9]{64}$/.test(info?.sha256 ?? ""))
          throw new Error(`Missing source checksum: ${assetId}`);
        const file = inside(runDirectory, info.file);
        if (sha256File(file) !== info.sha256) {
          throw new Error(`Source checksum mismatch: ${assetId}`);
        }
        const media = probe(file);
        if (quality === "marketing") {
          const cadence = item.entry.capture?.cadence;
          if (
            media.width !== 3840 ||
            media.height !== 2160 ||
            Math.abs(media.fps - 60) > 0.05 ||
            !["x11grab", "gpu-screen-recorder", "screencapturekit"].includes(
              item.entry.capture?.driver,
            ) ||
            !(cadence?.effective_unique_fps >= 55) ||
            !(cadence?.unique_frame_ratio >= 0.9)
          ) {
            throw new Error(`Source lacks qualified 4K60 capture evidence: ${assetId}`);
          }
        }
        sources.set(type, { file, media, sha256: info.sha256 });
      }
      const source = sources.get(type);
      if (quality === "standard" && !clip) {
        throw new Error(
          "Standard publication requires an explicitly reviewed --clip start:duration",
        );
      }
      const window = clipFor(source.media, clip ?? recipe.clip);
      const geometry = outputGeometry(source.media, recipe.width, recipe.height, recipe.fps);
      const imageOutput = ["webp", "png"].includes(recipe.kind);
      if (imageOutput && posterAt !== undefined && posterAt >= window.duration)
        throw new Error("--poster-at must fall inside the selected clip");
      const selectedPosterAt =
        (imageOutput ? posterAt : undefined) ?? recipe.posterAt ?? window.duration * 0.72;
      if (!Number.isFinite(selectedPosterAt) || selectedPosterAt < 0)
        throw new Error("Invalid poster timestamp");
      const colors = recipe.colors ?? 256;
      if (!Number.isInteger(colors) || colors < 4 || colors > 256)
        throw new Error("Invalid GIF palette size");
      const absolutePosterAt =
        window.start + Math.min(selectedPosterAt, Math.max(0, window.duration - 0.1));
      const privacyFilter = privacyMaskFilter(
        recipe.privacyMasks,
        ["webp", "png"].includes(recipe.kind) ? absolutePosterAt : window.start,
      );
      jobs.push({
        assetId,
        item,
        recipe,
        source,
        window,
        geometry,
        colors,
        privacyFilter,
        posterAt: absolutePosterAt,
      });
    }
  }
  if (!jobs.length) throw new Error("No derivatives are registered for this selection");
  const stageParent = path.join(repoRoot, ".artifacts");
  fs.mkdirSync(stageParent, { recursive: true });
  const stage = fs.mkdtempSync(path.join(stageParent, "derive-"));
  try {
    for (const [index, job] of jobs.entries()) {
      const { recipe, source, window, geometry } = job;
      const target = path.join(stage, `${index}.${recipe.kind}`);
      const scaleFilter = `scale=${geometry.width}:${geometry.height}:flags=lanczos`;
      const filter = [job.privacyFilter, `fps=${geometry.fps}`, scaleFilter, "setsar=1"]
        .filter(Boolean)
        .join(",");
      const posterFilter = [job.privacyFilter, scaleFilter].filter(Boolean).join(",");
      const input = [
        "-y",
        "-v",
        "error",
        "-ss",
        String(window.start),
        "-t",
        String(window.duration),
        "-i",
        source.file,
      ];
      if (["webp", "png"].includes(recipe.kind)) {
        if (recipe.kind === "webp")
          ffmpegWebp ??= /\blibwebp\b/.test(checked("ffmpeg", ["-hide_banner", "-encoders"]));
        const useCwebp = recipe.kind === "webp" && !ffmpegWebp;
        const frameTarget = useCwebp ? path.join(stage, `${index}.png`) : target;
        checked("ffmpeg", [
          "-y",
          "-v",
          "error",
          "-ss",
          String(job.posterAt),
          "-i",
          source.file,
          "-frames:v",
          "1",
          "-vf",
          posterFilter,
          "-c:v",
          recipe.kind === "webp" && !useCwebp ? "libwebp" : "png",
          frameTarget,
        ]);
        if (useCwebp) checked("cwebp", ["-quiet", "-q", "84", frameTarget, "-o", target]);
      } else if (recipe.kind === "gif") {
        const palette = path.join(stage, `${index}.palette.png`);
        checked("ffmpeg", [
          ...input,
          "-vf",
          `${filter},palettegen=max_colors=${job.colors}`,
          palette,
        ]);
        checked("ffmpeg", [
          ...input,
          "-i",
          palette,
          "-lavfi",
          `${filter}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle`,
          target,
        ]);
      } else {
        const codec =
          recipe.kind === "mp4"
            ? ["-c:v", "libx264", "-crf", "24", "-preset", "slow", "-movflags", "+faststart"]
            : ["-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0", "-cpu-used", "3", "-row-mt", "1"];
        checked("ffmpeg", [
          ...input,
          "-an",
          "-vf",
          filter,
          ...codec,
          "-pix_fmt",
          "yuv420p",
          target,
        ]);
        const actual = probe(target);
        if (
          actual.width !== geometry.width ||
          actual.height !== geometry.height ||
          Math.abs(actual.fps - geometry.fps) > 0.01 ||
          actual.codec !== (recipe.kind === "mp4" ? "h264" : "vp9")
        ) {
          throw new Error("Encoded derivative does not match requested geometry/codec");
        }
      }
      if (!fs.existsSync(target) || fs.statSync(target).size === 0)
        throw new Error("Empty derivative");
      if (recipe.budgetBytes && fs.statSync(target).size > recipe.budgetBytes)
        throw new Error(`GIF exceeds size budget: ${recipe.target}`);
      job.staged = target;
    }
    for (const job of jobs) {
      const target = path.join(repoRoot, job.recipe.target);
      fs.mkdirSync(path.dirname(target), { recursive: true });
      // Resolve existing parents too: a symlink must not redirect a published output.
      const relative = path.relative(
        fs.realpathSync(repoRoot),
        fs.realpathSync(path.dirname(target)),
      );
      if (relative.startsWith("../") || path.isAbsolute(relative))
        throw new Error("Output directory escapes repository");
      fs.renameSync(job.staged, target);
      recordFlowArtifact({
        repoRoot,
        targetPath: target,
        assetId: job.assetId,
        role: job.recipe.role,
        source: "apps/web/scripts/media-derivation.mjs",
        testTitle: `derive ${job.recipe.kind} › ${job.assetId}`,
        seedRevision: job.item.entry.seed_revision ?? null,
        capturedCommit: job.item.commit,
        sourceWorktreeDirty: job.item.dirty,
        ...(privateOutputDirectory
          ? { manifestPath: path.join(repoRoot, privateOutputDirectory, "manifest.json") }
          : {}),
        watchPaths: [
          job.item.entry.source,
          ...(job.item.entry.watch_paths ?? []),
          "apps/web/scripts/derive-doc-media.mjs",
          ...(job.recipe.watchPaths ?? []),
        ].filter(Boolean),
        sourceAsset: {
          runId: job.item.runId,
          assetId: job.assetId,
          sha256: job.source.sha256,
          quality,
          clip: job.window,
          ...(["webp", "png"].includes(job.recipe.kind)
            ? { poster_time_seconds: job.posterAt }
            : {}),
          ...(job.recipe.kind === "gif"
            ? {
                gif_encoding: {
                  palette_colors: job.colors,
                  dither: "bayer",
                  bayer_scale: 5,
                  diff_mode: "rectangle",
                },
              }
            : {}),
          ...(job.recipe.privacyMasks?.length
            ? { normalized_privacy_masks: job.recipe.privacyMasks }
            : {}),
        },
        capture: {
          ...(privateOutputDirectory ? { visibility: "private" } : {}),
          source_quality: quality,
          source_inference:
            job.item.entry.evidence?.inference ?? job.item.entry.capture?.inference ?? "unverified",
        },
      });
      console.log(`[media] ${quality} ${job.assetId} → ${job.recipe.target}`);
    }
  } finally {
    fs.rmSync(stage, { recursive: true, force: true });
  }
  return jobs.map((job) => job.recipe.target);
}

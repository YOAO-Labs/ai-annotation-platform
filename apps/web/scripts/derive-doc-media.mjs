#!/usr/bin/env node
import { parseArgs } from "node:util";
import {
  checked,
  deriveMedia,
  parseClip,
  readArchive,
  selectArchiveAssets,
} from "./media-derivation.mjs";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "../../..");

const POSTER_AT_SECONDS = new Map([
  ["ai-preannotate", 12.5],
  ["video-tracker-range", 32.8],
  ["video-tracker-cross-frame-points", 26],
  ["video-tracker-positive-negative", 29.3],
  ["video-tracker-box-seed", 20.6],
  ["video-tracker-text-discovery", 14.2],
  ["video-mask-track-edit", 19.2],
  ["video-track-carryover", 10.1],
  ["video-draw", 16.9],
  ["video-timeline-zoom", 9.2],
  ["video-chapter", 14.0],
  ["sam-tools/smart-box", 7.8],
  ["sam-tools/smart-point", 6.2],
  ["ai-assisted-annotation", 7.7],
  ["candidate-review-lifecycle", 14],
  ["smart-scribble", 15.8],
  ["bbox-draw", 6.4],
  ["polyline-draw", 11.8],
  ["polygon-draw", 13],
  ["hotkey-cheatsheet", 2],
  ["workspace-layout-basics", 18.8],
  ["workspace-layout-persistence", 23],
  ["rotated-bbox", 9.3],
  ["mask-draw", 7.6],
  ["ai-tracker-panel", 8],
  ["review-reject", 8],
  ["jobs-retry-recovery", 15],
  ["model-market-runtime-pool", 10],
  ["model-market-video-pool", 10],
  ["video-timeline-prediction-navigation", 8],
  ["model-market-runtime-partial-failure", 7],
  ["model-market-gpu-resource-overview", 10],
  ["platform-overview", 17],
  ["project-actions-menu", 9],
  ["jobs-bell-active", 10],
  ["video-tracker-job-states", 5],
  ["project-ml-routing", 11],
  ["pipeline-apply-project", 33.25],
  ["secondary-inference-attribute", 9],
  ["background-export-download", 16],
  ["project-create-existing-resources", 14],
  ["large-image-pyramid-recovery", 9],
  ["large-image-mask-limit", 11],
  ["pointcloud-view", 0.8],
  ["pointcloud-camera-seed-3d-box", 10],
  ["pointcloud-crossframe-track", 15],
  ["pointcloud-billboard-label", 13],
  ["pointcloud-panel-layout", 28],
  ["storage-connector-create-test", 15],
  ["video-track-batch-propagate", 2.5],
  ["video-track", 19.9],
  ["video-propagate-track-vs-copy", 14],
  ["video-mask-correction-propagate", 25],
  // 下列为本次新登记素材的占位海报时间（约目标时长的 85%），需在成片验收后按实际画面复核。
  ["video-issue-resolution", 27.2],
  ["project-guide-autosave", 20.4],
  ["project-invitation-join", 27.2],
]);

const TARGETS = new Map(
  [
    ["sam-tools/smart-point", "sam/smart-point"],
    ["sam-tools/smart-box", "sam/smart-box"],
    ["sam-tools/exemplar", "sam/exemplar"],
    ["bbox-draw", "workbench/bbox-draw"],
    ["workspace-layout-basics", "workbench/layout-basics"],
    ["workspace-layout-persistence", "workbench/layout-persistence"],
    ["ai-prediction-import", "ai/prediction-import"],
    ["ai-preannotate", "ai/preannotate"],
    ["ai-assisted-annotation", "ai/assisted-annotation"],
    ["candidate-keyboard-review", "ai/candidate-keyboard-review"],
    ["candidate-review-lifecycle", "ai/candidate-review-lifecycle"],
    ["review-reject", "review/reject-flow"],
    ["ocr-real-scene", "ai/ocr-current-task"],
    ["current-task-image-inference", "ai/current-task-image-inference"],
    ["current-frame-video-inference", "ai/current-frame-video-inference"],
    ["secondary-inference-attribute", "ai/secondary-inference-attribute"],
    ["pipeline-template-create", "pipeline/template-create"],
    ["pipeline-apply-project", "pipeline/apply-project"],
    ["jobs-retry-recovery", "workflows/jobs-retry-recovery"],
    ["model-market-runtime-pool", "superadmin/model-market/runtime-pools"],
    ["model-market-video-pool", "superadmin/model-market/video-pool"],
    ["model-market-runtime-partial-failure", "superadmin/model-market/runtime-data-sources"],
    ["model-market-gpu-resource-overview", "superadmin/model-market/gpu-resources"],
    ["platform-overview", "superadmin/platform-overview"],
    ["project-actions-menu", "projects/project-actions-menu"],
    ["jobs-bell-active", "jobs/jobs-bell-active"],
    ["video-tracker-job-states", "jobs/video-tracker-job-states"],
    ["project-ml-routing", "projects/ml-routing"],
    ["background-export-download", "datasets/background-export-download"],
    ["project-create-existing-resources", "projects/create-existing-resources"],
    ["ai-pre-variant-selector", "projects/ai-pre-variant-selector"],
    ["batch-bulk-actions", "projects/batch-bulk-actions"],
    ["hotkey-cheatsheet", "workbench/hotkey-cheatsheet"],
    ["mask-draw", "mask-brush/mask-draw"],
    ["polygon-draw", "polygon/draw"],
    ["polyline-draw", "polyline/draw"],
    ["rotated-bbox", "workbench/rotated-bbox"],
    ["smart-scribble", "sam/smart-scribble"],
    ["video-chapter", "video/video-chapter"],
    ["video-draw", "video/video-draw"],
    ["video-timeline-zoom", "video/video-timeline-zoom"],
    ["video-track-carryover", "video/video-track-carryover"],
    ["ai-tracker-panel", "video/ai-tracker-panel"],
    ["video-track", "video/workbench-overview"],
    ["video-track-batch-propagate", "video/track-batch-propagate"],
    ["video-propagate-track-vs-copy", "video/propagate-track-vs-copy"],
    ["video-timeline-prediction-navigation", "video/prediction-density-navigation"],
    ["video-mask-track-edit", "video/mask-track-edit"],
    ["video-tracker-range", "video/tracker-range"],
    ["video-tracker-cross-frame-points", "video/tracker-cross-frame-points"],
    ["video-tracker-positive-negative", "video/tracker-positive-negative"],
    ["video-tracker-box-seed", "video/tracker-box-seed"],
    ["video-tracker-text-discovery", "video/tracker-text-discovery"],
    ["video-tracker-combo-discovery", "video/tracker-combo-discovery"],
    ["video-mask-correction-propagate", "video/mask-correction-propagate"],
    ["pointcloud-controls", "pointcloud/controls"],
    ["pointcloud-view", "pointcloud/orbit"],
    ["pointcloud-camera-seed-3d-box", "pointcloud/camera-seed-3d-box"],
    ["pointcloud-crossframe-track", "pointcloud/crossframe-track"],
    ["pointcloud-billboard-label", "pointcloud/billboard-label"],
    ["pointcloud-panel-layout", "pointcloud/panel-layout"],
    ["storage-connector-create-test", "datasets/storage-connector-create-test"],
    ["large-image-progressive", "large-image/progressive"],
    ["large-image-pyramid-recovery", "large-image/pyramid-recovery"],
    ["large-image-mask-limit", "large-image/mask-limit"],
    ["video-issue-resolution", "discussion/video-issue-resolution"],
    ["project-guide-autosave", "projects/guide-autosave"],
    ["project-invitation-join", "team/project-invitation-join"],
  ].map(([assetId, target]) => [assetId, target]),
);

const HOME_TARGETS = new Map(
  [
    ["ai-assisted-annotation", "ai-assisted-annotation"],
    ["ocr-real-scene", "ocr-real-scene"],
    ["sam-tools/smart-point", "sam-tools/smart-point"],
    ["sam-tools/smart-box", "sam-tools/smart-box"],
    ["sam-tools/exemplar", "sam-tools/exemplar"],
  ].map(([assetId, target]) => [assetId, target]),
);

const { values } = parseArgs({
  args: process.argv.slice(2).filter((arg) => arg !== "--"),
  options: {
    run: { type: "string" },
    asset: { type: "string", multiple: true },
    quality: { type: "string", default: "marketing" },
    format: { type: "string", default: "all" },
    clip: { type: "string" },
    "gif-target": { type: "string" },
    article: { type: "string" },
  },
});
if (!["standard", "marketing"].includes(values.quality))
  throw new Error("--quality must be standard or marketing");
if (!["all", "video", "gif"].includes(values.format))
  throw new Error("--format must be all, video, or gif");
const archiveRoot = path.join(
  REPO_ROOT,
  values.quality === "marketing" ? ".artifacts/marketing" : ".artifacts/recordings",
);
const run =
  values.run ??
  fs
    .readdirSync(archiveRoot, { withFileTypes: true })
    .filter(
      (entry) =>
        entry.isDirectory() && fs.existsSync(path.join(archiveRoot, entry.name, "manifest.json")),
    )
    .map((entry) => entry.name)
    .sort()
    .at(-1);
if (!run) throw new Error("No source archive; capture first or pass --run");
const runDirectory = path.resolve(archiveRoot, run);
if (values.article) {
  if (values.format !== "all" || values["gif-target"]) {
    throw new Error(
      "Article presets generate paired GIF/cover outputs; omit --format and --gif-target",
    );
  }
  const output = checked(process.execPath, [
    path.join(REPO_ROOT, "scripts/derive-article-media.mjs"),
    "--archive",
    runDirectory,
    "--article",
    values.article,
    "--quality",
    values.quality,
    ...(values.asset ?? []).flatMap((id) => ["--asset", id]),
    ...(values.clip ? ["--clip", values.clip] : []),
  ]);
  process.stdout.write(output);
  process.exit(0);
}
const archive = readArchive(runDirectory);
if (values["gif-target"] && (values.format !== "gif" || values.asset?.length !== 1)) {
  throw new Error("--gif-target requires --format gif and one --asset");
}
const assets = selectArchiveAssets(
  archive,
  values.asset,
  values.format,
  (id) => TARGETS.has(id) || HOME_TARGETS.has(id),
);
deriveMedia({
  repoRoot: REPO_ROOT,
  runDirectory,
  quality: values.quality,
  assets,
  clip: parseClip(values.clip),
  outputs(assetId, item) {
    const outputs = [];
    if (values.format !== "gif") {
      for (const [map, root, videoRole, posterRole] of [
        [TARGETS, "docs-site/public/media", "docs-video", "poster"],
        [HOME_TARGETS, "docs-site/public/home", "home-video", "home-poster"],
      ]) {
        const stem = map.get(assetId);
        if (!stem) continue;
        outputs.push({ target: `${root}/${stem}.mp4`, kind: "mp4", role: videoRole });
        if (map === HOME_TARGETS)
          outputs.push({ target: `${root}/${stem}.webm`, kind: "webm", role: videoRole });
        outputs.push({
          target: `${root}/${stem}-poster.webp`,
          kind: "webp",
          role: posterRole,
          posterAt: POSTER_AT_SECONDS.get(assetId),
        });
      }
    }
    if (values.format !== "video") {
      const gifs = item.gifs.filter(
        (gif) => !values["gif-target"] || gif.target === values["gif-target"],
      );
      if (values["gif-target"] && !gifs.length)
        throw new Error("GIF target is not registered in this source archive");
      for (const gif of gifs) {
        outputs.push({
          target: gif.target,
          kind: "gif",
          role: "docs-gif",
          raw: true,
          width: gif.maxWidth,
          height: Number.MAX_SAFE_INTEGER,
          fps: gif.fps,
          colors: gif.maxColors,
          clip:
            gif.durationSec === undefined
              ? undefined
              : { start: gif.startSec ?? 0, duration: gif.durationSec },
        });
      }
      if (values.format === "gif" && !item.gifs.length)
        throw new Error(`No archived GIF recipes for ${assetId}; recapture the flow`);
    }
    if (!outputs.length) throw new Error(`No registered outputs for ${assetId}`);
    return outputs;
  },
});

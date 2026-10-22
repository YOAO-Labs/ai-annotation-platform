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
  ["project-onboarding", 19],
  ["ai-preannotate", 14.2],
  ["video-tracker-range", 32.8],
  ["video-tracker-cross-frame-points", 26],
  ["video-tracker-positive-negative", 27],
  ["video-tracker-box-seed", 20.6],
  ["video-tracker-text-discovery", 30],
  ["video-mask-track-edit", 17],
  ["video-track-carryover", 9.2],
  ["video-draw", 8],
  ["video-timeline-zoom", 8],
  ["video-chapter", 15],
  ["sam-tools/smart-box", 7.8],
  ["sam-tools/smart-point", 6.2],
  ["ai-assisted-annotation", 7.7],
  ["candidate-review-lifecycle", 14],
  ["smart-scribble", 15.8],
  ["bbox-draw", 6.7],
  ["polyline-draw", 10.2],
  ["polygon-draw", 11],
  ["hotkey-cheatsheet", 7],
  ["batch-bulk-actions", 6.8],
  ["workspace-layout-basics", 1],
  ["workspace-layout-persistence", 16],
  ["workspace-named-preset", 18],
  ["polygon-boundary-trace", 17],
  ["polygon-slice-undo", 18.5],
  ["video-comment-badges", 21],
  ["discussion-mention-return", 18],
  ["mask-slice-instances", 15.5],
  ["discussion-target-drafts", 18],
  ["task-drawing-comment", 14],
  ["rotated-bbox", 10.5],
  ["mask-draw", 8.2],
  ["ai-tracker-panel", 8],
  ["review-reject", 18],
  ["jobs-retry-recovery", 15],
  ["model-market-runtime-pool", 10],
  ["model-market-video-pool", 10],
  ["video-timeline-prediction-navigation", 8],
  ["model-market-runtime-partial-failure", 7],
  ["model-market-gpu-resource-overview", 10],
  ["platform-overview", 17],
  ["project-actions-menu", 8],
  ["jobs-bell-active", 10],
  ["video-tracker-job-states", 1],
  ["project-ml-routing", 12.3],
  ["pipeline-apply-project", 33.25],
  ["secondary-inference-attribute", 9],
  ["background-export-download", 22],
  ["project-create-existing-resources", 13],
  ["large-image-pyramid-recovery", 17.5],
  ["large-image-mask-limit", 12],
  ["large-image-progressive", 11],
  ["pointcloud-controls", 8],
  ["pointcloud-view", 11.5],
  ["pointcloud-camera-seed-3d-box", 17],
  ["pointcloud-crossframe-track", 23],
  ["pointcloud-billboard-label", 14],
  ["pointcloud-panel-layout", 2],
  ["storage-connector-create-test", 16],
  ["video-track-batch-propagate", 2.5],
  ["video-track", 24],
  ["video-propagate-track-vs-copy", 31],
  ["video-mask-correction-propagate", 37],
  ["video-issue-resolution", 23],
  ["project-guide-autosave", 16],
  ["project-invitation-join", 21],
]);
// Masks are reviewed against this immutable source; the saved row appears after the form closes.
const NORMALIZED_PRIVACY_MASKS = new Map([
  [
    "storage-connector-create-test",
    [
      { x: 0.3052, y: 0.4653, width: 0.0862, height: 0.0227, untilSeconds: 13.5 },
      { x: 0.3542, y: 0.3713, width: 0.0797, height: 0.019, fromSeconds: 13.1 },
    ],
  ],
]);

const TARGETS = new Map(
  [
    ["sam-tools/smart-point", "sam/smart-point"],
    ["sam-tools/smart-box", "sam/smart-box"],
    ["sam-tools/exemplar", "sam/exemplar"],
    ["bbox-draw", "workbench/bbox-draw"],
    ["workspace-layout-basics", "workbench/layout-basics"],
    ["workspace-layout-persistence", "workbench/layout-persistence"],
    ["workspace-named-preset", "workbench/named-preset"],
    ["polygon-boundary-trace", "polygon/boundary-trace"],
    ["polygon-slice-undo", "polygon/slice-undo"],
    ["video-comment-badges", "discussion/video-comment-badges"],
    ["discussion-mention-return", "discussion/mention-return"],
    ["mask-slice-instances", "mask/slice-instances"],
    ["discussion-target-drafts", "discussion/target-drafts"],
    ["task-drawing-comment", "discussion/task-drawing"],
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
    ["project-onboarding", "projects/onboarding"],
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
    "poster-at": { type: "string" },
    "gif-target": { type: "string" },
    article: { type: "string" },
    "private-output": { type: "string" },
  },
});
if (
  values["private-output"] !== undefined &&
  (values.asset?.length !== 1 ||
    values.format !== "video" ||
    values.article ||
    !/^\.artifacts\/demos\/[a-z0-9-]+$/.test(values["private-output"]))
)
  throw new Error(
    "--private-output requires one --asset, --format video and .artifacts/demos/<name>",
  );
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
  if (values.format !== "all" || values["gif-target"] || values["poster-at"] !== undefined) {
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
if (
  values["poster-at"] !== undefined &&
  (values.asset?.length !== 1 || values.format === "gif" || !values["poster-at"].trim())
)
  throw new Error("--poster-at requires one --asset and video/poster outputs");
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
  posterAt: values["poster-at"] === undefined ? undefined : Number(values["poster-at"]),
  privateOutputDirectory: values["private-output"],
  outputs(assetId, item) {
    if (values["private-output"]) {
      const stem = `${values["private-output"]}/${assetId.replaceAll("/", "-")}`;
      return [
        {
          target: `${stem}.mp4`,
          kind: "mp4",
          role: "docs-video",
          width: 1920,
          height: 1080,
          fps: 30,
        },
        { target: `${stem}-poster.webp`, kind: "webp", role: "poster", width: 1920, height: 1080 },
      ];
    }
    const outputs = [];
    const privacyMasks = NORMALIZED_PRIVACY_MASKS.get(assetId);
    if (
      privacyMasks &&
      item.main.sha256 !== "4eddd1ffecc3b9216577bffa2ad7f47570639ee0131880c745b20aba26ba7279"
    ) {
      throw new Error("Review connector privacy-mask coordinates and timing for the new source");
    }
    const privacy = privacyMasks ? { privacyMasks } : {};
    if (values.format !== "gif") {
      for (const [map, root, videoRole, posterRole] of [
        [TARGETS, "docs-site/public/media", "docs-video", "poster"],
        [HOME_TARGETS, "docs-site/public/home", "home-video", "home-poster"],
      ]) {
        const stem = map.get(assetId);
        if (!stem) continue;
        outputs.push({
          target: `${root}/${stem}.mp4`,
          kind: "mp4",
          role: videoRole,
          ...privacy,
        });
        if (map === HOME_TARGETS)
          outputs.push({
            target: `${root}/${stem}.webm`,
            kind: "webm",
            role: videoRole,
            ...privacy,
          });
        outputs.push({
          target: `${root}/${stem}-poster.webp`,
          kind: "webp",
          role: posterRole,
          posterAt: POSTER_AT_SECONDS.get(assetId),
          ...privacy,
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
          ...privacy,
        });
      }
      if (values.format === "gif" && !item.gifs.length)
        throw new Error(`No archived GIF recipes for ${assetId}; recapture the flow`);
    }
    if (!outputs.length) throw new Error(`No registered outputs for ${assetId}`);
    return outputs;
  },
});

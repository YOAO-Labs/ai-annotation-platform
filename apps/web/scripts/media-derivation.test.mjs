import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import {
  checked,
  deriveMedia,
  outputGeometry,
  parseClip,
  readArchive,
  selectArchiveAssets,
} from "./media-derivation.mjs";
import { recordFlowArtifact } from "../e2e/screenshots/_helpers/flow-manifest.ts";

function fixture(root) {
  const run = path.join(root, ".artifacts/recordings/run");
  fs.mkdirSync(run, { recursive: true });
  const file = path.join(run, "bbox-draw.mp4");
  checked("ffmpeg", [
    "-y",
    "-v",
    "error",
    "-f",
    "lavfi",
    "-i",
    "testsrc2=s=320x180:r=25:d=2",
    "-c:v",
    "libx264",
    "-threads",
    "2",
    file,
  ]);
  recordFlowArtifact({
    repoRoot: root,
    targetPath: file,
    assetId: "bbox-draw",
    role: "docs-video",
    source: "flow.ts",
    capturedCommit: "test-commit",
    sourceWorktreeDirty: true,
    seedRevision: "seed",
    testTitle: "capture",
    manifestPath: path.join(run, "manifest.json"),
    capture: {
      profile: "docs",
      inference: "none",
      gif_variants: [
        {
          target: "docs-site/user-guide/images/bbox/test.gif",
          fps: 4,
          maxWidth: 640,
          startSec: 0.4,
          durationSec: 0.8,
        },
      ],
    },
  });
  return { run, file };
}

test("private demos retain provenance without writing public assets or the public manifest", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "aap-private-demo-"));
  try {
    const { run } = fixture(root);
    const options = {
      repoRoot: root,
      runDirectory: run,
      quality: "standard",
      assets: ["bbox-draw"],
      clip: { start: 0, duration: 1 },
      privateOutputDirectory: ".artifacts/demos/detection",
      outputs: () => [
        {
          target: ".artifacts/demos/detection/demo.mp4",
          kind: "mp4",
          role: "docs-video",
        },
      ],
    };
    assert.throws(
      () => deriveMedia({ ...options, privateOutputDirectory: ".artifacts/demos/../escape" }),
      /Private output/,
    );
    assert.throws(
      () => deriveMedia({ ...options, privateOutputDirectory: undefined }),
      /enrolled media path/,
    );
    deriveMedia(options);
    assert.ok(fs.statSync(path.join(root, ".artifacts/demos/detection/demo.mp4")).size > 0);
    const manifest = JSON.parse(
      fs.readFileSync(path.join(root, ".artifacts/demos/detection/manifest.json")),
    );
    assert.equal(
      manifest.entries[".artifacts/demos/detection/demo.mp4"].capture.visibility,
      "private",
    );
    assert.equal(
      manifest.entries[".artifacts/demos/detection/demo.mp4"].source_asset.quality,
      "standard",
    );
    assert.equal(
      fs.existsSync(path.join(root, "apps/web/e2e/screenshots/outputs/flow-manifest.json")),
      false,
    );
    assert.equal(fs.existsSync(path.join(root, "docs-site/public")), false);
    assert.throws(
      () =>
        deriveMedia({
          ...options,
          outputs: () => [{ target: ".artifacts/demos/other/demo.mp4", kind: "mp4" }],
        }),
      /enrolled media path/,
    );
    assert.throws(
      () =>
        deriveMedia({
          ...options,
          outputs: () => [{ target: "docs-site/public/media/demo.mp4", kind: "mp4" }],
        }),
      /enrolled media path/,
    );
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("one archive derives every format, preserves unrelated entries, and never upscales standard sources", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "aap-derive-test-"));
  try {
    const { run, file } = fixture(root);
    assert.equal(readArchive(run).get("bbox-draw").quality, "standard");
    const mixed = new Map([
      ["bbox-draw", { gifs: [{}] }],
      ["video-track", { gifs: [] }],
    ]);
    assert.deepEqual(
      selectArchiveAssets(mixed, undefined, "gif", () => true),
      ["bbox-draw"],
    );
    assert.deepEqual(
      selectArchiveAssets(mixed, ["video-track"], "gif", () => true),
      ["video-track"],
    );
    assert.deepEqual(outputGeometry({ width: 320, height: 180, fps: 25 }), {
      width: 320,
      height: 180,
      fps: 25,
    });
    assert.throws(() => parseClip("-1:3"), /--clip/);
    assert.throws(() => parseClip("0:Infinity"), /--clip/);
    const options = {
      repoRoot: root,
      runDirectory: run,
      quality: "standard",
      assets: ["bbox-draw"],
      clip: parseClip("0.2:1"),
      posterAt: 0.6,
      outputs: () => [
        { target: "docs-site/public/media/test.mp4", kind: "mp4", role: "docs-video" },
        { target: "docs-site/public/home/test.webm", kind: "webm", role: "home-video" },
        { target: "docs-site/public/media/test-poster.webp", kind: "webp", role: "poster" },
        { target: "docs/articles/media/test-cover.png", kind: "png", role: "poster" },
        {
          target: "docs-site/user-guide/images/bbox/test.gif",
          kind: "gif",
          role: "docs-gif",
          raw: true,
          fps: 4,
          clip: { start: 0.4, duration: 0.8 },
        },
      ],
    };
    for (const posterAt of [-1, Infinity, 1])
      assert.throws(() => deriveMedia({ ...options, posterAt }), /--poster-at/);
    assert.throws(
      () => deriveMedia({ ...options, assets: ["bbox-draw", "other"] }),
      /--poster-at|--clip/,
    );
    deriveMedia(options);
    const manifestPath = path.join(root, "apps/web/e2e/screenshots/outputs/flow-manifest.json");
    const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
    assert.equal(
      manifest.entries["docs-site/public/media/test-poster.webp"].source_asset.poster_time_seconds,
      0.8,
    );
    const video = manifest.entries["docs-site/public/media/test.mp4"];
    assert.equal(video.media.width, 320);
    assert.equal(video.media.fps, "25/1");
    assert.equal(video.source_asset.quality, "standard");
    assert.equal(video.capture.source_inference, "none");
    assert.deepEqual(video.source_asset.clip, { start: 0.2, duration: 1 });
    assert.deepEqual(
      manifest.entries["docs-site/user-guide/images/bbox/test.gif"].source_asset.clip,
      { start: 0.2, duration: 1 },
    );
    deriveMedia({
      ...options,
      clip: { start: 0.4, duration: 0.8 },
      outputs: () => [options.outputs()[4]],
    });
    assert.deepEqual(
      JSON.parse(fs.readFileSync(manifestPath, "utf8")).entries[
        "docs-site/user-guide/images/bbox/test.gif"
      ].source_asset.clip,
      { start: 0.4, duration: 0.8 },
    );
    assert.deepEqual(
      JSON.parse(fs.readFileSync(manifestPath, "utf8")).entries["docs-site/public/media/test.mp4"],
      video,
    );
    const before = fs.readFileSync(manifestPath, "utf8");
    assert.throws(() => deriveMedia({ ...options, clip: undefined }), /reviewed --clip/);
    assert.throws(() => deriveMedia({ ...options, quality: "marketing" }), /quality\/provenance/);
    assert.throws(() => deriveMedia({ ...options, clip: { start: 1, duration: 5 } }), /outside/);
    assert.throws(() => deriveMedia({ ...options, assets: ["missing"] }), /lacks/);
    assert.throws(
      () =>
        deriveMedia({
          ...options,
          outputs: () => [{ ...options.outputs()[0], target: "../escape.mp4" }],
        }),
      /target/,
    );
    // The complete request is validated before any derivative is installed.
    assert.throws(
      () =>
        deriveMedia({
          ...options,
          outputs: () => [
            options.outputs()[0],
            { ...options.outputs()[4], target: "../escape.gif" },
          ],
        }),
      /target/,
    );
    assert.throws(
      () =>
        deriveMedia({
          ...options,
          outputs: () => [
            options.outputs()[4],
            { ...options.outputs()[4], target: "docs-site/user-guide/images/bbox/another.gif" },
          ],
        }),
      /multiple GIF windows/,
    );
    assert.equal(fs.readFileSync(manifestPath, "utf8"), before);
    fs.appendFileSync(file, "corruption");
    assert.throws(() => deriveMedia(options), /checksum mismatch/);
    assert.equal(fs.readFileSync(manifestPath, "utf8"), before);
    assert.ok(!fs.existsSync(path.join(root, "docs-site/maintainers/media-reviews.json")));
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("schema-4 masters preserve inference provenance and the existing quality gate", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "aap-master-derive-test-"));
  try {
    const run = path.join(root, "master");
    fs.mkdirSync(run);
    const file = path.join(run, "master.mp4");
    checked("ffmpeg", [
      "-y",
      "-v",
      "error",
      "-f",
      "lavfi",
      "-i",
      "color=c=yellow:s=3840x2160:r=60:d=0.5",
      "-c:v",
      "libx264",
      "-preset",
      "ultrafast",
      "-threads",
      "2",
      file,
    ]);
    const info = {
      file: "master.mp4",
      sha256: createHash("sha256").update(fs.readFileSync(file)).digest("hex"),
    };
    const manifest = {
      schema_version: 4,
      run: { id: "master", source_commit: "test", source_worktree_dirty: false },
      entries: {
        "bbox-draw": {
          asset_id: "bbox-draw",
          source: "flow.ts",
          files: { universal_mp4: info, capture_source: info },
          capture: {
            driver: "x11grab",
            cadence: { effective_unique_fps: 60, unique_frame_ratio: 1 },
          },
        },
      },
    };
    fs.writeFileSync(path.join(run, "manifest.json"), JSON.stringify(manifest));
    const options = {
      repoRoot: root,
      runDirectory: run,
      assets: ["bbox-draw"],
      outputs: () => [
        { target: "docs-site/public/media/master.mp4", kind: "mp4", role: "docs-video" },
      ],
    };
    deriveMedia(options);
    const result = JSON.parse(
      fs.readFileSync(
        path.join(root, "apps/web/e2e/screenshots/outputs/flow-manifest.json"),
        "utf8",
      ),
    );
    assert.equal(result.entries["docs-site/public/media/master.mp4"].media.width, 1280);
    assert.equal(
      result.entries["docs-site/public/media/master.mp4"].source_asset.quality,
      "marketing",
    );
    assert.equal(
      result.entries["docs-site/public/media/master.mp4"].capture.source_inference,
      "unverified",
    );
    manifest.entries["bbox-draw"].evidence = {
      inference: "live",
      backend_requirements: "image_interactive",
      clock: "live",
    };
    manifest.entries["bbox-draw"].capture.driver = "screencapturekit";
    fs.writeFileSync(path.join(run, "manifest.json"), JSON.stringify(manifest));
    deriveMedia(options);
    const updated = JSON.parse(
      fs.readFileSync(
        path.join(root, "apps/web/e2e/screenshots/outputs/flow-manifest.json"),
        "utf8",
      ),
    );
    assert.equal(
      updated.entries["docs-site/public/media/master.mp4"].capture.source_inference,
      "live",
    );
    manifest.entries["bbox-draw"].capture.cadence.unique_frame_ratio = 0.1;
    fs.writeFileSync(path.join(run, "manifest.json"), JSON.stringify(manifest));
    assert.throws(() => deriveMedia(options), /qualified 4K60/);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
test("normalized privacy masks protect encoded video, GIF, and poster pixels", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "aap-privacy-mask-test-"));
  try {
    const assetId = "test-privacy-mask";
    const run = path.join(root, ".artifacts/recordings/privacy-run");
    fs.mkdirSync(run, { recursive: true });
    const source = path.join(run, `${assetId}.mp4`);
    checked("ffmpeg", [
      "-y",
      "-v",
      "error",
      "-f",
      "lavfi",
      "-i",
      "color=c=white:s=320x180:r=10:d=1.2",
      "-vf",
      "drawbox=x=100:y=80:w=28:h=10:color=red:t=fill",
      "-c:v",
      "libx264",
      "-threads",
      "2",
      source,
    ]);
    const sha256 = createHash("sha256").update(fs.readFileSync(source)).digest("hex");
    fs.writeFileSync(
      path.join(run, "manifest.json"),
      `${JSON.stringify(
        {
          schema_version: 1,
          entries: {
            [assetId]: {
              asset_id: assetId,
              source: "apps/web/scripts/media-derivation.test.mjs",
              sha256,
              captured_commit: "test-commit",
              source_worktree_dirty: false,
              capture: { profile: "docs", inference: "none" },
            },
          },
        },
        null,
        2,
      )}\n`,
    );
    const privacyMasks = [
      {
        x: 0.3125,
        y: 0.4444444444444444,
        width: 0.0875,
        height: 0.05555555555555555,
        fromSeconds: 0.5,
        untilSeconds: 0.9,
      },
    ];
    const recipes = [
      {
        target: "docs-site/public/media/privacy-mask.mp4",
        kind: "mp4",
        role: "docs-video",
        width: 160,
        height: 90,
        fps: 10,
        privacyMasks,
      },
      {
        target: "docs-site/user-guide/images/privacy-mask.gif",
        kind: "gif",
        role: "docs-gif",
        raw: true,
        width: 160,
        height: 90,
        fps: 10,
        privacyMasks,
      },
      {
        target: "docs-site/public/media/privacy-mask-poster.webp",
        kind: "webp",
        role: "poster",
        width: 160,
        height: 90,
        posterAt: 0.3,
        privacyMasks,
      },
    ];
    deriveMedia({
      repoRoot: root,
      runDirectory: run,
      quality: "standard",
      assets: [assetId],
      clip: { start: 0.25, duration: 0.9 },
      outputs: () => recipes,
    });
    for (const recipe of recipes) {
      const rawFrame = path.join(root, `${recipe.kind}.rgb`);
      const frameTimes = recipe.kind === "webp" ? [0] : [0, 0.3, 0.7];
      for (const frameTime of frameTimes) {
        checked("ffmpeg", [
          "-y",
          "-v",
          "error",
          "-ss",
          String(frameTime),
          "-i",
          path.join(root, recipe.target),
          "-frames:v",
          "1",
          "-f",
          "rawvideo",
          "-pix_fmt",
          "rgb24",
          rawFrame,
        ]);
        const frame = fs.readFileSync(rawFrame);
        assert.equal(frame.length, 160 * 90 * 3);
        const pixelAt = (x, y) => [...frame.subarray((y * 160 + x) * 3, (y * 160 + x + 1) * 3)];
        if (recipe.kind === "webp" || frameTime === 0.3) {
          assert.ok(
            pixelAt(56, 42).every((channel) => channel < 48),
            `${recipe.kind} leaks masked pixels after the source-time boundary`,
          );
        } else {
          assert.ok(
            pixelAt(56, 42)[0] > 180 && pixelAt(56, 42)[1] < 80,
            `${recipe.kind} masks an unrelated earlier or later phase`,
          );
        }
        for (const [x, y] of [
          [49, 42],
          [64, 42],
          [56, 39],
          [56, 45],
        ]) {
          assert.ok(
            pixelAt(x, y).some((channel) => channel > 32),
            `${recipe.kind} over-masks pixels outside the normalized boundary at ${x},${y}`,
          );
        }
      }
    }
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

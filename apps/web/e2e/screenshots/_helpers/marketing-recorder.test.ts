import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { MARKETING_ASSET_SPECS, type MarketingAssetSpec } from "./marketing-assets.ts";
import {
  committedAnnotationFromPayload,
  isAnnotationCommitRequest,
  mediaBoundsFromStageBox,
  movePointerAtRefreshRate,
  normalizedBboxIoU,
} from "../flows/_canvas.ts";
import {
  archiveMarketingMaster,
  clipFromEpochWindow,
  getMarketingRunContext,
  type MarketingRunContext,
} from "./marketing-recorder.ts";
import {
  cadenceCalibrationRgb,
  calibrationBoundsFromRgb,
  captureGeometry,
  correctedOuterSize,
  externalCaptureFfmpegArgs,
  gpuScreenRecorderArgs,
  gpuScreenRecorderFirstFrameEpochMs,
  validateCaptureCadence,
  x11CaptureInput,
  cadenceCaptureFilter,
  normalizationEncoderArgs,
  waitForMacReady,
  stopProcess,
} from "./marketing-external-recorder.ts";

import { macCaptureReady } from "../../../scripts/mac-marketing-capture.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "../../../../..");

const RUN: MarketingRunContext = {
  runId: "20260814T000000Z-0123456789ab",
  createdAt: "2026-08-14T00:00:00.000Z",
  sourceCommit: "0123456789abcdef0123456789abcdef01234567",
  sourceWorktreeDirty: false,
};

const TEST_SPEC: MarketingAssetSpec = {
  assetId: "sam-tools/smart-point",
  title: "智能点分割",
  theme: "单点提示生成对象轮廓",
  objective: "验证归档测试。",
  duration: { minSeconds: 1, targetSeconds: 2, maxSeconds: 3 },
  shots: ["放置正点。", "展示候选。"],
  editingNotes: ["保留点击前后画面。"],
};

function createVideoFixture(
  root: string,
  durationSeconds = 2,
  size = "3840x2160",
  fps = 60,
): string {
  const target = path.join(root, `source-${durationSeconds}-${size}-${fps}fps.webm`);
  execFileSync("ffmpeg", [
    "-hide_banner",
    "-loglevel",
    "error",
    "-f",
    "lavfi",
    "-i",
    `color=c=black:s=${size}:r=${fps}:d=${durationSeconds}`,
    "-c:v",
    "libvpx",
    "-deadline",
    "realtime",
    "-cpu-used",
    "8",
    "-b:v",
    "1M",
    target,
  ]);
  return target;
}

test("registers and documents every independent marketing asset", () => {
  const catalog = fs.readFileSync(
    path.join(REPO_ROOT, "docs-site/dev/reference/marketing-asset-catalog.md"),
    "utf8",
  );
  assert.equal(MARKETING_ASSET_SPECS.size, 66);
  for (const spec of MARKETING_ASSET_SPECS.values()) {
    assert.ok(spec.title.length > 0, `${spec.assetId} missing title`);
    assert.ok(spec.theme.length > 0, `${spec.assetId} missing theme`);
    assert.ok(spec.objective.length > 0, `${spec.assetId} missing objective`);
    assert.ok(spec.shots.length >= 3, `${spec.assetId} needs at least three shots`);
    assert.ok(spec.editingNotes.length > 0, `${spec.assetId} missing editing notes`);
    assert.ok(spec.duration.minSeconds < spec.duration.targetSeconds, spec.assetId);
    assert.ok(spec.duration.targetSeconds < spec.duration.maxSeconds, spec.assetId);
    assert.match(catalog, new RegExp("\\|\\s*`" + spec.assetId + "`\\s*\\|"));
  }
});

test("reuses one explicit run identity across Playwright workers", () => {
  const temporaryRepo = fs.mkdtempSync(path.join(os.tmpdir(), "marketing-run-context-"));
  const previousRunId = process.env.MARKETING_RUN_ID;
  const previousCreatedAt = process.env.MARKETING_RUN_CREATED_AT;
  try {
    execFileSync("git", ["init", "--quiet"], { cwd: temporaryRepo });
    fs.writeFileSync(path.join(temporaryRepo, "fixture.txt"), "fixture\n");
    execFileSync("git", ["add", "fixture.txt"], { cwd: temporaryRepo });
    execFileSync(
      "git",
      [
        "-c",
        "user.name=Recorder Test",
        "-c",
        "user.email=recorder@example.test",
        "commit",
        "--quiet",
        "-m",
        "fixture",
      ],
      { cwd: temporaryRepo },
    );
    const commit = execFileSync("git", ["rev-parse", "HEAD"], {
      cwd: temporaryRepo,
      encoding: "utf8",
    }).trim();
    process.env.MARKETING_RUN_CREATED_AT = "2026-08-14T12:34:56.789Z";
    process.env.MARKETING_RUN_ID = `20260814T123456Z-${commit.slice(0, 12)}`;

    const context = getMarketingRunContext(temporaryRepo);
    assert.equal(context.runId, process.env.MARKETING_RUN_ID);
    assert.equal(context.createdAt, process.env.MARKETING_RUN_CREATED_AT);
    assert.equal(context.sourceCommit, commit);
    assert.equal(context.sourceWorktreeDirty, false);
  } finally {
    if (previousRunId === undefined) delete process.env.MARKETING_RUN_ID;
    else process.env.MARKETING_RUN_ID = previousRunId;
    if (previousCreatedAt === undefined) delete process.env.MARKETING_RUN_CREATED_AT;
    else process.env.MARKETING_RUN_CREATED_AT = previousCreatedAt;
    fs.rmSync(temporaryRepo, { recursive: true, force: true });
  }
});

test("maps an absolute action window to the recorder timebase without startup drift", () => {
  assert.deepEqual(
    clipFromEpochWindow(1_000_000, {
      startEpochMs: 1_002_400,
      endEpochMs: 1_007_900,
    }),
    { startSeconds: 2.4, durationSeconds: 5.5 },
  );
});

test("maps normalized anchors through the rendered media rectangle, not the stage letterbox", () => {
  assert.deepEqual(
    mediaBoundsFromStageBox(
      { x: 100, y: 50, width: 1000, height: 700 },
      { x: 20, y: 80, width: 960, height: 540 },
    ),
    { x: 120, y: 130, width: 960, height: 540 },
  );
});

test("rejects a tracker result that only covers a small vehicle part", () => {
  const target = { x: 0.49, y: 0.455, w: 0.23, h: 0.365 };
  const wholeVehicle = { x: 0.48, y: 0.448, w: 0.221, h: 0.365 };
  const windshieldPart = { x: 0.523, y: 0.543, w: 0.068, h: 0.054 };

  assert.ok(normalizedBboxIoU(wholeVehicle, target) > 0.8);
  assert.ok(normalizedBboxIoU(windshieldPart, target) < 0.1);
});

test("advances the trusted Playwright pointer to the drag endpoint", async () => {
  const moves: Array<{ x: number; y: number }> = [];
  const page = {
    mouse: {
      move: async (x: number, y: number) => {
        moves.push({ x, y });
      },
    },
  };

  await movePointerAtRefreshRate(page as never, { x: 10, y: 20 }, { x: 110, y: 220 }, 1);

  assert.deepEqual(moves, [{ x: 110, y: 220 }]);
});

test("drops stale pointer samples when the page cannot consume 60Hz events", async () => {
  const moves: Array<{ x: number; y: number }> = [];
  const page = {
    mouse: {
      move: async (x: number, y: number) => {
        moves.push({ x, y });
        await new Promise((resolve) => setTimeout(resolve, 40));
      },
    },
  };

  await movePointerAtRefreshRate(page as never, { x: 0, y: 0 }, { x: 100, y: 100 }, 100);

  assert.ok(moves.length < 6, `expected stale samples to be dropped, got ${moves.length}`);
  assert.deepEqual(moves.at(-1), { x: 100, y: 100 });
});

test("recognizes direct and AI-mask annotation commits and normalizes their payload", () => {
  assert.equal(isAnnotationCommitRequest("POST", "http://app/api/v1/tasks/1/annotations"), true);
  assert.equal(
    isAnnotationCommitRequest("POST", "http://app/api/v1/tasks/1/ai-mask-candidates/accept"),
    true,
  );
  assert.equal(isAnnotationCommitRequest("GET", "http://app/api/v1/tasks/1/annotations"), false);
  assert.deepEqual(
    committedAnnotationFromPayload({
      prediction: { id: "prediction-1" },
      annotation: { id: "annotation-1", task_id: 1, class_name: "car" },
    }),
    { id: "annotation-1", task_id: 1, class_name: "car" },
  );
});

test("archives an immutable video and writes a verifiable manifest", async () => {
  const temporaryRoot = fs.mkdtempSync(path.join(os.tmpdir(), "marketing-recorder-"));
  try {
    const sourceVideo = createVideoFixture(temporaryRoot, 4);
    const result = await archiveMarketingMaster({
      archiveRoot: temporaryRoot,
      run: RUN,
      video: {
        saveAs: async (target) => fs.copyFileSync(sourceVideo, target),
      },
      captureExtension: "mkv",
      assetId: "sam-tools/smart-point",
      assetSpec: TEST_SPEC,
      source: "apps/web/e2e/screenshots/flows/flows.spec.ts",
      watchPaths: ["apps/web/e2e/screenshots/flows/project-onboarding.ts"],
      testTitle: "flow recordings › sam-tool-smart-point",
      projectName: "marketing-master",
      seedRevision: "screenshots-test",
      viewport: { width: 1920, height: 1080 },
      deviceScaleFactor: 2,
      browser: { name: "chromium", version: "test" },
      captureCadence: {
        sample_duration_ms: 1_100,
        captured_frames: 66,
        unique_frames: 65,
        effective_unique_fps: 59.09,
        unique_frame_ratio: 0.9848,
      },
      capturedAt: "2026-08-14T01:02:03.000Z",
      universalClip: { startSeconds: 0.2, durationSeconds: 1.5 },
      gifVariants: [
        { target: "docs-site/user-guide/images/test/create.gif", startSec: 0.4, durationSec: 0.3 },
        { target: "docs-site/user-guide/images/test/resize.gif", startSec: 1, durationSec: 0.4 },
        { target: "docs-site/user-guide/images/test/padded.gif", startSec: 1.5, durationSec: 0.5 },
      ],
    });

    assert.equal(fs.existsSync(result.capturePath), true);
    assert.equal(fs.existsSync(result.masterPath), true);
    assert.equal(fs.existsSync(result.manifestPath), true);
    assert.match(result.captureStorageKey, /^raw\/sam-tools\/smart-point\/2026-08-14\/.+\.mkv$/);
    assert.match(result.masterStorageKey, /^masters\/sam-tools\/smart-point\/2026-08-14\/.+\.mp4$/);

    const manifest = JSON.parse(fs.readFileSync(result.manifestPath, "utf8")) as {
      schema_version: number;
      entries: Record<
        string,
        {
          review_status: string;
          watch_paths: string[];
          gif_variants: Array<{ startSec: number; durationSec: number }>;
          files: {
            capture_source: {
              file: string;
              sha256: string;
              source_clip_seconds?: { start: number; requested_duration: number };
              media: {
                width: number;
                height: number;
                codec: string;
                fps: number;
                duration_ms: number;
              };
            };
            universal_mp4: {
              file: string;
              sha256: string;
              media: { width: number; height: number; codec: string; duration_ms: number };
              source_clip_seconds: { start: number; requested_duration: number };
            };
          };
          content: {
            theme: string;
            duration_policy_seconds: { minimum: number; target: number; maximum: number };
            shots: string[];
          };
          capture: {
            source_physical_size: { width: number; height: number };
            resampling: string;
            cadence: {
              sample_duration_ms: number;
              captured_frames: number;
              unique_frames: number;
              effective_unique_fps: number;
              unique_frame_ratio: number;
            } | null;
          };
        }
      >;
    };

    assert.deepEqual(manifest.entries["sam-tools/smart-point"].watch_paths, [
      "apps/web/e2e/screenshots/flows/flows.spec.ts",
      "apps/web/e2e/screenshots/flows/project-onboarding.ts",
    ]);
    const entry = manifest.entries["sam-tools/smart-point"];
    assert.deepEqual(
      entry.gif_variants
        .slice(0, 2)
        .map(({ startSec, durationSec }) => ({ startSec, durationSec })),
      [
        { startSec: 0.2, durationSec: 0.3 },
        { startSec: 0.8, durationSec: 0.4 },
      ],
    );
    assert.equal(manifest.schema_version, 4);
    assert.equal(
      entry.gif_variants[2].durationSec,
      entry.files.capture_source.media.duration_ms / 1000 - entry.gif_variants[2].startSec,
    );
    assert.ok(entry.gif_variants[2].durationSec < 0.5);
    assert.match(entry.files.capture_source.sha256, /^[a-f0-9]{64}$/);
    assert.match(entry.files.universal_mp4.sha256, /^[a-f0-9]{64}$/);
    assert.equal(
      entry.files.capture_source.file,
      `raw/sam-tools/smart-point/${entry.files.capture_source.sha256}.mkv`,
    );
    assert.equal(
      entry.files.universal_mp4.file,
      `masters/sam-tools/smart-point/${entry.files.universal_mp4.sha256}.mp4`,
    );
    assert.equal(entry.review_status, "pending");
    assert.equal(entry.files.capture_source.media.width, 3840);
    assert.equal(entry.files.capture_source.media.height, 2160);
    assert.equal(entry.files.capture_source.media.fps, 60);
    assert.ok(entry.files.capture_source.media.duration_ms >= 1_400);
    assert.ok(entry.files.capture_source.media.duration_ms <= 1_600);
    assert.deepEqual(entry.files.capture_source.source_clip_seconds, {
      start: 0.2,
      requested_duration: 1.5,
    });
    assert.equal(entry.files.universal_mp4.media.width, 3840);
    assert.equal(entry.files.universal_mp4.media.height, 2160);
    assert.equal(entry.files.universal_mp4.media.codec, "h264");
    assert.ok(entry.files.universal_mp4.media.duration_ms >= 1_400);
    assert.ok(entry.files.universal_mp4.media.duration_ms <= 1_600);
    assert.deepEqual(entry.files.universal_mp4.source_clip_seconds, {
      start: 0.2,
      requested_duration: 1.5,
    });
    assert.equal(entry.content.theme, TEST_SPEC.theme);
    assert.deepEqual(entry.content.duration_policy_seconds, {
      minimum: 1,
      target: 2,
      maximum: 3,
    });
    assert.deepEqual(entry.content.shots, TEST_SPEC.shots);
    assert.deepEqual(entry.capture.source_physical_size, { width: 3840, height: 2160 });
    assert.equal(entry.capture.resampling, "none");
    assert.equal(entry.capture.cadence?.effective_unique_fps, 59.09);
    assert.equal(fs.existsSync(path.join(temporaryRoot, RUN.runId, ".incoming")), false);
  } finally {
    fs.rmSync(temporaryRoot, { recursive: true, force: true });
  }
});

test("rejects asset ids that can escape the archive root", async () => {
  const temporaryRoot = fs.mkdtempSync(path.join(os.tmpdir(), "marketing-recorder-"));
  try {
    await assert.rejects(
      archiveMarketingMaster({
        archiveRoot: temporaryRoot,
        run: RUN,
        video: { saveAs: async () => undefined },
        assetId: "../outside",
        assetSpec: { ...TEST_SPEC, assetId: "../outside" },
        source: "test",
        testTitle: "test",
        projectName: "marketing-master",
        seedRevision: null,
        viewport: null,
        browser: { name: "chromium", version: "test" },
      }),
      /非法 asset id/,
    );
  } finally {
    fs.rmSync(temporaryRoot, { recursive: true, force: true });
  }
});

test("rejects masters that do not meet the declared duration", async () => {
  const temporaryRoot = fs.mkdtempSync(path.join(os.tmpdir(), "marketing-recorder-"));
  try {
    const sourceVideo = createVideoFixture(temporaryRoot, 0.5);
    await assert.rejects(
      archiveMarketingMaster({
        archiveRoot: temporaryRoot,
        run: RUN,
        video: { saveAs: async (target) => fs.copyFileSync(sourceVideo, target) },
        assetId: TEST_SPEC.assetId,
        assetSpec: TEST_SPEC,
        source: "test",
        testTitle: "test",
        projectName: "marketing-master",
        seedRevision: null,
        viewport: { width: 1920, height: 1080 },
        browser: { name: "chromium", version: "test" },
      }),
      /录制时长 .* 不在允许范围 1–3s 内/,
    );
  } finally {
    fs.rmSync(temporaryRoot, { recursive: true, force: true });
  }
});

test("rejects masters below the required resolution", async () => {
  const temporaryRoot = fs.mkdtempSync(path.join(os.tmpdir(), "marketing-recorder-"));
  try {
    const sourceVideo = createVideoFixture(temporaryRoot, 2, "1280x720");
    await assert.rejects(
      archiveMarketingMaster({
        archiveRoot: temporaryRoot,
        run: RUN,
        video: { saveAs: async (target) => fs.copyFileSync(sourceVideo, target) },
        assetId: TEST_SPEC.assetId,
        assetSpec: TEST_SPEC,
        source: "test",
        testTitle: "test",
        projectName: "marketing-master",
        seedRevision: null,
        viewport: { width: 1920, height: 1080 },
        browser: { name: "chromium", version: "test" },
      }),
      /分辨率不符合高清母版要求: 1280×720/,
    );
  } finally {
    fs.rmSync(temporaryRoot, { recursive: true, force: true });
  }
});

test("builds a 2.6K60 X11 source capture command", () => {
  assert.equal(x11CaptureInput(":0"), ":0.0");
  assert.equal(x11CaptureInput(":97.0"), ":97.0");
  const args = externalCaptureFfmpegArgs(":0.0", "0x2200004", "/tmp/capture.mkv", {
    x: 22,
    y: 131,
    width: 2592,
    height: 1458,
  });
  assert.equal(args[args.indexOf("-window_id") + 1], "0x2200004");
  assert.equal(args[args.indexOf("-framerate") + 1], "60");
  assert.equal(args[args.indexOf("-c:v") + 1], "h264_nvenc");
  assert.equal(args[args.indexOf("-vf") + 1], "crop=2592:1458:22:131,fps=60");
});

test("builds a GPU-only 60Hz window capture command", () => {
  const args = gpuScreenRecorderArgs("0x2200004", "/tmp/capture.mkv");
  assert.equal(args[args.indexOf("-w") + 1], "0x2200004");
  assert.equal(args[args.indexOf("-f") + 1], "60");
  assert.equal(args[args.indexOf("-fm") + 1], "cfr");
  assert.equal(args[args.indexOf("-encoder") + 1], "gpu");
  assert.equal(args[args.indexOf("-cursor") + 1], "no");
  assert.equal(args[args.indexOf("-write-first-frame-ts") + 1], "yes");
});

test("uses GPU recorder first-frame realtime as the exact clipping origin", () => {
  assert.equal(
    gpuScreenRecorderFirstFrameEpochMs(
      "monotonic_microsec\\trealtime_microsec\n123456789\t1786737600123456\n",
    ),
    1_786_737_600_123.456,
  );
  assert.throws(() => gpuScreenRecorderFirstFrameEpochMs("invalid\n"), /首帧时间戳/);
});

test("uses the measured browser content rectangle instead of bottom-aligning the crop", () => {
  assert.deepEqual(
    captureGeometry(
      {
        innerWidth: 1440,
        innerHeight: 810,
        outerWidth: 1464,
        outerHeight: 907,
        deviceScaleFactor: 1.8,
        screenX: 0,
        screenY: 0,
      },
      {
        x: 22,
        y: 131,
        width: 2592,
        height: 1458,
      },
    ),
    { x: 22, y: 131, width: 2592, height: 1458 },
  );
  assert.throws(
    () =>
      captureGeometry(
        {
          innerWidth: 3840,
          innerHeight: 2160,
          outerWidth: 3840,
          outerHeight: 2247,
          deviceScaleFactor: 1,
          screenX: 0,
          screenY: 0,
        },
        { x: 0, y: 0, width: 3840, height: 2160 },
      ),
    /逻辑 viewport 必须为 1440×810/,
  );
});

test("corrects fractional-DPR window rounding from the measured inner viewport", () => {
  const metrics = {
    innerWidth: 1441,
    innerHeight: 810,
    outerWidth: 1473,
    outerHeight: 939,
    deviceScaleFactor: 8 / 3,
    screenX: 0,
    screenY: 0,
  };
  const first = correctedOuterSize(metrics);
  assert.deepEqual(first, { width: 1472, height: 939 });
  assert.deepEqual(correctedOuterSize(metrics, first), { width: 1471, height: 939 });
});

test("detects the browser content rectangle from a calibration frame", () => {
  const width = 8;
  const height = 7;
  const rgb = Buffer.alloc(width * height * 3);
  for (let y = 2; y < 6; y += 1) {
    for (let x = 1; x < 7; x += 1) {
      const offset = (y * width + x) * 3;
      rgb[offset] = 22;
      rgb[offset + 1] = 199;
      rgb[offset + 2] = 132;
    }
  }
  assert.deepEqual(calibrationBoundsFromRgb(rgb, width, height, [22, 199, 132]), {
    x: 1,
    y: 2,
    width: 6,
    height: 4,
  });
});

test("rejects nominal 60fps capture when the calibration contains repeated frames", () => {
  assert.doesNotThrow(() =>
    validateCaptureCadence({ frameCount: 61, uniqueFrameCount: 59, durationMs: 1_000 }),
  );
  assert.throws(
    () => validateCaptureCadence({ frameCount: 60, uniqueFrameCount: 12, durationMs: 1_000 }),
    /有效 12\.00fps/,
  );
  assert.throws(
    () => validateCaptureCadence({ frameCount: 60, uniqueFrameCount: 54, durationMs: 1_000 }),
    /有效 54\.00fps/,
  );
});

test("uses high-contrast colors for every frame in the 60Hz calibration window", () => {
  const colors = Array.from({ length: 70 }, (_, index) =>
    cadenceCalibrationRgb(index + 1).join(","),
  );
  assert.equal(new Set(colors).size, colors.length);
});

test("rejects a 4K source that is not 60fps", async () => {
  const temporaryRoot = fs.mkdtempSync(path.join(os.tmpdir(), "marketing-recorder-"));
  try {
    const sourceVideo = createVideoFixture(temporaryRoot, 2, "3840x2160", 25);
    await assert.rejects(
      archiveMarketingMaster({
        archiveRoot: temporaryRoot,
        run: RUN,
        video: { saveAs: async (target) => fs.copyFileSync(sourceVideo, target) },
        assetId: TEST_SPEC.assetId,
        assetSpec: TEST_SPEC,
        source: "test",
        testTitle: "test",
        projectName: "marketing-master",
        seedRevision: null,
        viewport: { width: 1920, height: 1080 },
        browser: { name: "chromium", version: "test" },
      }),
      /帧率不符合母版要求: 25fps，期望 60fps/,
    );
  } finally {
    fs.rmSync(temporaryRoot, { recursive: true, force: true });
  }
});

test("archives and trims a Mac master without NVIDIA encoding", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "marketing-mac-archive-"));
  try {
    const source = path.join(root, "synthetic.mkv");
    execFileSync("ffmpeg", [
      "-v",
      "error",
      "-f",
      "lavfi",
      "-i",
      "color=c=yellow:s=3840x2160:r=60:d=2",
      "-c:v",
      "libx264",
      "-preset",
      "ultrafast",
      source,
    ]);
    const result = await archiveMarketingMaster({
      archiveRoot: root,
      run: RUN,
      video: { saveAs: async (target) => fs.copyFileSync(source, target) },
      captureDriver: "screencapturekit",
      captureExtension: "mkv",
      assetId: TEST_SPEC.assetId,
      assetSpec: TEST_SPEC,
      source: "synthetic-test",
      testTitle: "Mac archive",
      projectName: "marketing-master",
      seedRevision: null,
      viewport: { width: 1440, height: 810 },
      deviceScaleFactor: 2,
      sourcePhysicalSize: { width: 2880, height: 1620 },
      browser: { name: "chromium", version: "test" },
      captureCadence: {
        sample_duration_ms: 1100,
        captured_frames: 66,
        unique_frames: 65,
        effective_unique_fps: 59.09,
        unique_frame_ratio: 65 / 66,
      },
      universalClip: { startSeconds: 0.2, durationSeconds: 1.2 },
    });
    const entry = JSON.parse(fs.readFileSync(result.manifestPath, "utf8")).entries[
      TEST_SPEC.assetId
    ];
    assert.equal(entry.capture.driver, "screencapturekit");
    assert.equal(entry.capture.resampling, "lanczos");
    assert.deepEqual(entry.capture.source_physical_size, { width: 2880, height: 1620 });
    for (const file of [entry.files.capture_source, entry.files.universal_mp4]) {
      assert.equal(file.media.codec, "h264");
      assert.equal(file.media.width, 3840);
      assert.equal(file.media.height, 2160);
      assert.equal(file.media.fps, 60);
      assert.ok(Math.abs(file.media.duration_ms - 1200) <= 20);
      assert.deepEqual(file.source_clip_seconds, { start: 0.2, requested_duration: 1.2 });
    }
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("rejects an all-black external capture", async () => {
  const temporaryRoot = fs.mkdtempSync(path.join(os.tmpdir(), "marketing-recorder-"));
  try {
    const sourceVideo = createVideoFixture(temporaryRoot, 2);
    for (const captureDriver of ["x11grab", "screencapturekit"] as const)
      await assert.rejects(
        archiveMarketingMaster({
          archiveRoot: temporaryRoot,
          run: RUN,
          video: { saveAs: async (target) => fs.copyFileSync(sourceVideo, target) },
          captureDriver,
          assetId: TEST_SPEC.assetId,
          assetSpec: TEST_SPEC,
          source: "test",
          testTitle: "test",
          projectName: "marketing-master",
          seedRevision: null,
          viewport: { width: 1920, height: 1080 },
          browser: { name: "chromium", version: "test" },
        }),
        /采集源为全黑画面，拒绝归档/,
      );
  } finally {
    fs.rmSync(temporaryRoot, { recursive: true, force: true });
  }
});

test("Mac native capture retains Retina content detail and qualified timestamps", () => {
  const metrics = {
    innerWidth: 1440,
    innerHeight: 810,
    outerWidth: 1440,
    outerHeight: 953,
    deviceScaleFactor: 2,
    screenX: 0,
    screenY: 0,
  };
  const geometry = { x: 0, y: 286, width: 2880, height: 1620 };
  assert.deepEqual(captureGeometry(metrics, geometry, "screencapturekit"), geometry);
  assert.throws(
    () => captureGeometry(metrics, { ...geometry, width: 1440 }, "screencapturekit"),
    /2880×1620/,
  );
  assert.throws(
    () => captureGeometry({ ...metrics, deviceScaleFactor: NaN }, geometry, "screencapturekit"),
    /设备像素倍率/,
  );
  const identity = { window_id: 42, pid: 123, width: 2880, height: 1906 };
  const ready = { ...identity, event: "ready", first_frame_epoch_ms: 1_789_232_181_000 };
  assert.deepEqual(
    clipFromEpochWindow(macCaptureReady(ready, identity), {
      startEpochMs: ready.first_frame_epoch_ms + 1250,
      endEpochMs: ready.first_frame_epoch_ms + 3250,
    }),
    { startSeconds: 1.25, durationSeconds: 2 },
  );
  for (const patch of [
    { pid: 124 },
    { window_id: 41 },
    { width: 1440 },
    { first_frame_epoch_ms: NaN },
    { first_frame_epoch_ms: 0 },
  ]) {
    assert.throws(() => macCaptureReady({ ...ready, ...patch }, identity), /identity or geometry/);
  }
  assert.ok(normalizationEncoderArgs("screencapturekit").includes("libx264"));
  assert.ok(!normalizationEncoderArgs("screencapturekit").includes("h264_nvenc"));
  assert.ok(normalizationEncoderArgs("x11grab").includes("h264_nvenc"));
  for (const sample of [
    { frameCount: 66, uniqueFrameCount: 63, durationMs: NaN },
    { frameCount: 66, uniqueFrameCount: 67, durationMs: 1100 },
  ]) {
    assert.throws(() => validateCaptureCadence(sample), /校准失败/);
  }
});

test("Mac cadence excludes motion in browser chrome and normalizes the source timebase", () => {
  // Leave an input tail for fps-filter end-of-stream rounding; still inspect exactly 66 frames.
  const text = execFileSync(
    "ffmpeg",
    [
      "-v",
      "error",
      "-f",
      "lavfi",
      "-i",
      "testsrc2=s=320x60:r=60:d=1.2",
      "-f",
      "lavfi",
      "-i",
      "color=c=black:s=320x180:r=60:d=1.2",
      "-filter_complex",
      `[0:v][1:v]vstack,${cadenceCaptureFilter("screencapturekit", { x: 0, y: 60, width: 320, height: 180 })}`,
      "-frames:v",
      "66",
      "-f",
      "framemd5",
      "pipe:1",
    ],
    { encoding: "utf8" },
  );
  const hashes = text
    .split("\n")
    .filter((line) => /^\d/.test(line))
    .map((line) => line.split(",").at(-1));
  assert.equal(hashes.length, 66);
  assert.equal(new Set(hashes).size, 1);
  assert.throws(
    () =>
      validateCaptureCadence({
        frameCount: hashes.length,
        uniqueFrameCount: new Set(hashes).size,
        durationMs: 1100,
      }),
    /校准失败/,
  );
});

test("native protocol accepts split ready output and waits for graceful finalization", async () => {
  const identity = {
    window_id: 42,
    pid: 123,
    width: 2880,
    height: 1906,
    point_width: 1440,
    point_height: 953,
  };
  const message = JSON.stringify({
    ...identity,
    event: "ready",
    first_frame_epoch_ms: 1_789_232_181_000,
  });
  const child = spawn(
    process.execPath,
    [
      "-e",
      `const line = ${JSON.stringify(message)}; process.stdout.write(line.slice(0, 12)); setTimeout(() => process.stdout.write(line.slice(12) + "\\n"), 15); process.stdin.on('data', data => { if (data.toString().includes('q')) setTimeout(() => process.exit(0), 20); });`,
    ],
    { stdio: ["pipe", "pipe", "pipe"] },
  );
  const completion = new Promise<number>((resolve) =>
    child.once("close", (code) => resolve(code ?? 1)),
  );
  try {
    assert.equal(await waitForMacReady(child, identity), 1_789_232_181_000);
    await stopProcess({
      process: child,
      completion,
      driver: "screencapturekit",
      stderr: "",
    } as Parameters<typeof stopProcess>[0]);
    assert.equal(child.exitCode, 0);
  } finally {
    if (child.exitCode === null) child.kill("SIGKILL");
    await completion;
  }
});

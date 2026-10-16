import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";
import {
  RECORDING_FLOWS,
  recordingInference,
  recordingPlan,
  marketingCaptureDriver,
  screenshotCatalogPath,
  assertRecordingMode,
} from "./recording-plan.mjs";

test("selection scopes AI without changing capture quality or legacy catalog defaults", () => {
  assert.equal(recordingPlan(["bbox-draw"]).backendRequirements, "none");
  const output = execFileSync(
    process.execPath,
    [
      fileURLToPath(new URL("../../scripts/run-recording-capture.mjs", import.meta.url)),
      "--",
      "--flow",
      "bbox-draw",
      "--plan",
    ],
    { encoding: "utf8" },
  );
  assert.equal(JSON.parse(output).backendRequirements, "none");
  assert.equal(JSON.parse(output).profile, "marketing");
  assert.equal(
    JSON.parse(output).captureDriver,
    process.platform === "darwin" ? "screencapturekit" : "x11grab",
  );
  assert.equal(recordingPlan(["bbox-draw"]).profile, "marketing");
  assert.equal(recordingPlan(["ocr-inference", "bbox-draw"]).backendRequirements, "ocr");
  assert.equal(
    recordingPlan(["sam-interactive"], "marketing").backendRequirements,
    "image_interactive",
  );
  assert.equal(
    recordingPlan(["candidate-keyboard-review"]).backendRequirements,
    "image_interactive",
  );
  assert.throws(() => recordingPlan(["typo"]), /Unregistered/);
  assert.throws(
    () => recordingPlan(["pointcloud-billboard-label"], "docs"),
    /requires --profile marketing/,
  );
  assert.equal(
    recordingPlan(["pointcloud-billboard-label"], "marketing").backendRequirements,
    "none",
  );
  for (const id of ["pointcloud-controls", "pointcloud-view", "pointcloud-panel-layout"]) {
    assert.throws(() => recordingPlan([id], "docs"), /requires --profile marketing/);
    assert.equal(recordingPlan([id], "marketing").backendRequirements, "none");
  }
  assert.throws(() => recordingPlan([]), /Select/);
  assert.throws(() => recordingPlan(["bbox-draw"], "4k-mac"), /Unknown profile/);
  const grep = new RegExp(recordingPlan(["bbox-draw"]).grep);
  assert.ok(grep.test("flows flow recordings bbox-draw — title"));
  assert.ok(!grep.test("flows flow recordings rotated-bbox-draw — title"));
  assert.equal(
    screenshotCatalogPath("none"),
    "/api/v1/__test/seed/catalog?profile=screenshots&backend_requirements=none",
  );
  assert.equal(screenshotCatalogPath(undefined), "/api/v1/__test/seed/catalog?profile=screenshots");
  const spec = fs.readFileSync(new URL("./flows/flows.spec.ts", import.meta.url), "utf8");
  for (const id of Object.keys(RECORDING_FLOWS)) {
    assert.ok(id.startsWith("sam-tool-") || spec.includes(`test("${id} —`), `No flow for ${id}`);
  }
});

test("capability-only panels and live inference retain separate recording evidence", () => {
  assert.deepEqual(RECORDING_FLOWS["ai-tracker-panel"], ["video_tracker"]);
  assert.equal(recordingPlan(["ai-tracker-panel"]).backendRequirements, "video_tracker");
  assert.equal(recordingInference("ai-tracker-panel"), "none");

  assert.deepEqual(RECORDING_FLOWS["current-task-image-inference"], ["ocr"]);
  assert.equal(recordingPlan(["current-task-image-inference"]).backendRequirements, "ocr");
  assert.equal(recordingInference("current-task-image-inference"), "live");
  assert.deepEqual(RECORDING_FLOWS["secondary-inference-attribute"], ["ocr"]);
  assert.deepEqual(RECORDING_FLOWS["jobs-retry-recovery"], ["ocr"]);
  assert.deepEqual(RECORDING_FLOWS["project-ml-routing"], ["image_interactive"]);
  assert.equal(recordingInference("ai-preannotate"), "live");
  assert.equal(recordingInference("pipeline-apply-project"), "live");
  assert.equal(recordingPlan(["ai-preannotate"]).backendRequirements, "none");
  assert.equal(recordingPlan(["pipeline-apply-project"]).backendRequirements, "none");
  assert.equal(recordingPlan(["ai-preannotate", "bbox-draw"]).backendRequirements, "none");
  assert.equal(recordingPlan(["ai-preannotate", "ocr-inference"]).backendRequirements, "ocr");

  for (const id of [
    "sam-tool-smart-point",
    "sam-tool-smart-box",
    "sam-tool-exemplar",
    "sam-interactive",
    "ocr-inference",
    "candidate-keyboard-review",
    "candidate-review-lifecycle",
    "smart-scribble",
    "video-tracker-range",
    "video-tracker-cross-frame-points",
    "video-tracker-positive-negative",
    "video-tracker-box-seed",
    "video-tracker-text-discovery",
  ]) {
    assert.equal(recordingInference(id), "live", id);
  }
  for (const [id, requirements] of Object.entries(RECORDING_FLOWS)) {
    if (requirements.length === 0) {
      assert.ok(["none", "live"].includes(recordingInference(id)), id);
    }
  }
  for (const id of ["typo", "toString", "__proto__"]) {
    assert.throws(() => recordingInference(id), /Unregistered/);
  }
});

test("capture list distinguishes flow-owned live inference from manual flows", () => {
  const output = execFileSync(
    process.execPath,
    [fileURLToPath(new URL("../../scripts/run-recording-capture.mjs", import.meta.url)), "--list"],
    { encoding: "utf8" },
  );
  assert.match(
    output,
    /^ai-preannotate\tlive inference \(flow setup\)\tmarketing \/ docs rehearsal$/m,
  );
  assert.match(
    output,
    /^pipeline-apply-project\tlive inference \(flow setup\)\tmarketing \/ docs rehearsal$/m,
  );
  assert.match(output, /^bbox-draw\tmanual \(no ML backend\)\tmarketing \/ docs rehearsal$/m);
});

test("mixed selection combines capability requirements without widening individual inference", () => {
  const plan = recordingPlan([
    "ai-tracker-panel",
    "current-task-image-inference",
    "bbox-draw",
    "ai-tracker-panel",
  ]);
  assert.deepEqual(plan.flows, ["ai-tracker-panel", "current-task-image-inference", "bbox-draw"]);
  assert.equal(plan.backendRequirements, "ocr,video_tracker");
  assert.deepEqual(plan.flows.map(recordingInference), ["none", "live", "none"]);
  const grep = new RegExp(plan.grep);
  for (const id of plan.flows) assert.ok(grep.test(`flows flow recordings ${id} — title`));
  assert.ok(!grep.test("flows flow recordings video-tracker-range — title"));
});

test("video tracking recordings require a live tracker without widening to other backends", () => {
  const flows = [
    "video-tracker-range",
    "video-tracker-cross-frame-points",
    "video-tracker-positive-negative",
    "video-tracker-box-seed",
    "video-tracker-text-discovery",
    "current-frame-video-inference",
    "video-timeline-prediction-navigation",
    "video-mask-correction-propagate",
    "video-propagate-track-vs-copy",
    "video-track-batch-propagate",
    "video-tracker-combo-discovery",
  ];
  assert.equal(recordingPlan(flows).backendRequirements, "video_tracker");
  assert.deepEqual(
    flows.map(recordingInference),
    flows.map(() => "live"),
  );
  assert.equal(
    recordingPlan(["video-tracker-combo-discovery"]).backendRequirements,
    "video_tracker",
  );
});

test("Mac marketing selects only registered non-ML operations before runtime setup", () => {
  assert.equal(
    marketingCaptureDriver("darwin", undefined, ["bbox-draw", "hotkey-cheatsheet"]),
    "screencapturekit",
  );
  assert.equal(
    marketingCaptureDriver("darwin", "screencapturekit", ["pointcloud-panel-layout"]),
    "screencapturekit",
  );
  for (const flows of [
    ["ocr-inference"],
    ["ai-tracker-panel"],
    ["project-ml-routing"],
    ["ai-preannotate"],
    ["pipeline-apply-project", "bbox-draw"],
  ]) {
    assert.throws(
      () => marketingCaptureDriver("darwin", undefined, flows),
      /no ML capabilities or inference/,
    );
  }
  assert.throws(() => marketingCaptureDriver("darwin", undefined, []), /Select/);
  assert.throws(() => marketingCaptureDriver("darwin", undefined, ["unknown"]), /Unregistered/);
  assert.throws(() => marketingCaptureDriver("darwin", "x11grab", ["bbox-draw"]), /Unsupported/);
  assert.throws(
    () => marketingCaptureDriver("linux", "screencapturekit", ["bbox-draw"]),
    /Unsupported/,
  );
  assert.throws(() => marketingCaptureDriver("win32", undefined, ["bbox-draw"]), /Unsupported/);
  assert.equal(marketingCaptureDriver("linux", undefined), "x11grab");
  assert.equal(
    marketingCaptureDriver("linux", "gpu-screen-recorder", ["ocr-inference"]),
    "gpu-screen-recorder",
  );
});

test("standard rehearsal cannot record production sources, including legacy flow entry", () => {
  assert.throws(() => assertRecordingMode("docs", false), /rehearsal only/);
  assert.doesNotThrow(() => assertRecordingMode("docs", true));
  assert.doesNotThrow(() => assertRecordingMode("marketing", false));
  const launcher = fileURLToPath(
    new URL("../../scripts/run-recording-capture.mjs", import.meta.url),
  );
  assert.throws(
    () =>
      execFileSync(process.execPath, [launcher, "--flow", "bbox-draw", "--profile", "docs"], {
        encoding: "utf8",
        stdio: "pipe",
      }),
    /rehearsal only/,
  );
  const preview = JSON.parse(
    execFileSync(process.execPath, [launcher, "--flow", "bbox-draw", "--validate-only", "--plan"], {
      encoding: "utf8",
    }),
  );
  assert.equal(preview.profile, "docs");
  assert.equal(preview.captureDriver, undefined);
});

test("direct legacy Playwright recording refuses before catalog or database setup", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "aap-rehearsal-guard-"));
  try {
    const config = path.join(root, "playwright.config.mjs");
    fs.writeFileSync(
      config,
      `export default ${JSON.stringify({
        testDir: fileURLToPath(new URL(".", import.meta.url)),
        testMatch: "**/flows/flows.spec.ts",
        workers: 1,
        projects: [{ name: "flows" }],
        reporter: "list",
        outputDir: path.join(root, "results"),
      })}`,
    );
    const env = { ...process.env, SCREENSHOT_VALIDATE_ONLY: "0" };
    for (const key of [
      "SCREENSHOT_DATABASE_URL",
      "SCREENSHOT_CATALOG_JSON",
      "SCREENSHOT_BACKEND_REQUIREMENTS",
      "SCREENSHOT_RECORDING_FLOWS",
      "SCREENSHOT_RECORDING_PROFILE",
    ])
      delete env[key];
    assert.throws(
      () =>
        execFileSync(
          process.execPath,
          [
            fileURLToPath(new URL("../../node_modules/@playwright/test/cli.js", import.meta.url)),
            "test",
            "--config",
            config,
            "--grep",
            "bbox-draw —",
          ],
          { env, encoding: "utf8", stdio: "pipe" },
        ),
      (error) => {
        assert.match(error.stdout, /rehearsal only/);
        assert.doesNotMatch(error.stdout, /缺少 SCREENSHOT_DATABASE_URL|catalog.*初始化/);
        return true;
      },
    );
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("management recordings declare catalog-only dependencies without SAM inference", () => {
  const flows = [
    "ai-pre-variant-selector",
    "pipeline-template-create",
    "model-market-runtime-pool",
    "model-market-video-pool",
    "model-market-runtime-partial-failure",
    "model-market-gpu-resource-overview",
  ];
  for (const flow of flows) {
    assert.equal(recordingPlan([flow]).backendRequirements, "none");
    assert.equal(recordingInference(flow), "none");
  }
});

import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { copyFileSync, mkdtempSync, rmSync, writeFileSync, mkdirSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { planE2ESuites, selectSuites, shadowPlan, SUITE_CONTRACT } from "./plan-e2e-suites.mjs";
import { auditE2ERequirements } from "./audit-e2e-requirements.mjs";
import { buildSummary } from "./summarize-e2e-results.mjs";

const names = (event, paths) => planE2ESuites(event, paths).include.map(({ suite }) => suite);
const readShadow = (cwd, outputs) =>
  JSON.parse(readFileSync(resolve(cwd, outputs.shadow_path), "utf8"));
const core = Array.from(
  { length: 16 },
  (_, index) => `default-${String(index + 1).padStart(2, "0")}`,
);
const video = ["video-pipeline-1", "video-pipeline-2", "video-pipeline-3", "video-pipeline-4"];
const pointcloud = ["pointcloud-1", "pointcloud-2", "pointcloud-3", "pointcloud-4"];
const maskSpecialties = [
  "mask-readonly",
  "mask-native",
  "mask-native-editor",
  "mask-ai-native",
  "mask-toolbar-native",
];
const specialties = [...maskSpecialties, "ai-request-worker"];
const functional = [...core, ...specialties, ...pointcloud];

test("every legacy PR retains all functional shards, Mask and AI suites, and point-cloud", () => {
  assert.deepEqual(names("pull_request", []), functional);
  assert.deepEqual(
    names("pull_request", ["docs-site/user-guide/workbench/index.md", "README.md"]),
    functional,
  );
});

const broadened = ["smoke", ...core, ...specialties, ...video, ...pointcloud, "layout-stress"];

const { jobSeconds } = JSON.parse(
  readFileSync(new URL("./e2e-job-durations.json", import.meta.url), "utf8"),
);

function assertFullDispatch(matrix) {
  const { include } = JSON.parse(matrix);
  const bySuite = (a, b) => a.suite.localeCompare(b.suite);
  assert.deepEqual(
    include.toSorted(bySuite),
    SUITE_CONTRACT.filter(({ scope }) => scope !== "targeted")
      .map(({ suite, command, built, requireAllTests }) => ({
        suite,
        command,
        built,
        ...(requireAllTests ? { requireAllTests: true } : {}),
        ...(core.includes(suite) ? { dedicatedVideo: true } : {}),
      }))
      .toSorted(bySuite),
  );
  assert.equal(include[0].suite, "smoke");
  const seconds = include.slice(1).map(({ suite }) => jobSeconds[suite] ?? -1);
  assert.deepEqual(
    seconds,
    seconds.toSorted((a, b) => b - a),
  );
}

const withWorkerReceipt = (suites) =>
  suites.flatMap((suite) => (suite === "mask-native" ? [suite, "mask-worker"] : [suite]));

test("the full gate has unique jobs with complete Chromium and video partitions", () => {
  assert.equal(broadened.length, 32);
  assert.equal(withWorkerReceipt(broadened).length, 33);
  assert.equal(SUITE_CONTRACT.filter(({ scope }) => scope !== "targeted").length, broadened.length);
  assert.equal(new Set(SUITE_CONTRACT.map(({ suite }) => suite)).size, SUITE_CONTRACT.length);

  for (const [prefix, ids, command, scope, legacyGate] of [
    ["default-", core, "test:e2e --project=chromium --fully-parallel", "full", true],
    [
      "video-pipeline-",
      video,
      "test:e2e:video --project=chromium --fully-parallel e2e/tests/video- e2e/tests/workbench-video-",
      "specialty",
      false,
    ],
    ["pointcloud-", pointcloud, "test:e2e --project=pointcloud", "specialty", true],
  ]) {
    const entries = SUITE_CONTRACT.filter(({ suite }) => suite.startsWith(prefix));
    assert.deepEqual(
      entries.map(({ suite }) => suite),
      ids,
    );
    assert.deepEqual(
      entries.map(({ command }) => command),
      ids.map((_, index) => `${command} --shard=${index + 1}/${ids.length}`),
    );
    assert.ok(
      entries.every(
        (entry) => entry.built && entry.scope === scope && entry.legacyGate === legacyGate,
      ),
      prefix,
    );
  }
});

test("the two strict native product groups retain their separate commands and counts", () => {
  for (const [suite, collects] of [
    ["mask-native", 11],
    ["mask-native-editor", 10],
  ]) {
    const entry = SUITE_CONTRACT.find((entry) => entry.suite === suite);
    assert.equal(entry.command, `test:e2e:${suite}`);
    assert.equal(entry.built, false);
    assert.equal(entry.legacyGate, true);
    assert.equal(entry.scope, "specialty");
    assert.equal(entry.runtime.collects, collects);
    assert.equal(entry.runtime.skipped, 0);
  }
  for (const path of [
    "apps/web/e2e/tests/raster-mask-native.spec.ts",
    "apps/web/e2e/tests/mask-advanced-operations.spec.ts",
    "apps/web/e2e/tests/mask-editor.spec.ts",
    "apps/web/e2e/tests/native-mask-ai.spec.ts",
    "apps/web/e2e/browser-integration/mask-worker.spec.ts",
  ]) {
    assert.deepEqual(selectSuites("pull_request", [path]).planned, ["smoke", ...maskSpecialties]);
  }
});

test("native AI toolbar and async AI requests have independent required launchers", () => {
  const webPackage = JSON.parse(
    readFileSync(new URL("../apps/web/package.json", import.meta.url), "utf8"),
  );
  assert.equal(
    webPackage.scripts["test:e2e:mask-toolbar-native"],
    "PLAYWRIGHT_RASTER_MASK_MATRIX=native PLAYWRIGHT_WEB_PREVIEW=1 playwright test --project=chromium e2e/tests/workbench-ai-toolbar-layers.spec.ts",
  );
  const workflow = readFileSync(
    new URL("../.github/workflows/e2e-run.yml", import.meta.url),
    "utf8",
  );
  assert.match(workflow, /^\s*max-parallel: 11$/m);
  assert.match(workflow, /E2E_REQUIRE_ALL_TESTS:.*matrix\.requireAllTests/);
  assert.match(
    workflow,
    /if \[ "\$\{\{ matrix\.suite \}\}" = "ai-request-worker" \]; then\n\s+PLAYWRIGHT_AI_REQUEST_WORKER=1 PREANNOTATE_GPU_QUEUE=ml\.cpu PREANNOTATE_CPU_QUEUE=ml\.cpu \\\n\s+pnpm dev:worktree -- exec --mode e2e --with-worker -- pnpm --filter web \$\{\{ matrix\.command \}\}/,
  );
  assert.equal(workflow.match(/PLAYWRIGHT_AI_REQUEST_WORKER/g)?.length, 1);

  for (const [suite, spec, command, collects] of [
    ["mask-toolbar-native", "workbench-ai-toolbar-layers", "test:e2e:mask-toolbar-native", 6],
    [
      "ai-request-worker",
      "workbench-ai-inspector-phases",
      "test:e2e --project=chromium e2e/tests/workbench-ai-inspector-phases.spec.ts",
      4,
    ],
  ]) {
    const entry = SUITE_CONTRACT.find((entry) => entry.suite === suite);
    assert.equal(entry.command, command);
    assert.equal(entry.built, true);
    assert.equal(entry.legacyGate, true);
    assert.equal(entry.scope, "specialty");
    assert.equal(entry.requireAllTests, true);
    assert.deepEqual(entry.runtime, { collects, skipped: 0 });
    assert.deepEqual(selectSuites("pull_request", [`apps/web/e2e/tests/${spec}.spec.ts`]).planned, [
      "smoke",
      suite,
    ]);
  }
  for (const path of [
    "apps/web/src/pages/Workbench/shell/AIInspectorPanel.tsx",
    "apps/api/app/workers/frame_preannotate.py",
    "apps/api/app/workers/celery_app.py",
    "apps/inference/src/yoao_inference/execution_pipeline.py",
    "apps/preannotation-results/pyproject.toml",
  ]) {
    assert.ok(selectSuites("pull_request", [path]).planned.includes("ai-request-worker"), path);
  }
});

test("strict browser receipts reject failed, skipped, or zero-case execution", () => {
  const directory = mkdtempSync(join(tmpdir(), "e2e-required-ai-"));
  try {
    const entries = SUITE_CONTRACT.filter(({ requireAllTests }) => requireAllTests);
    const requiredSuites = entries.map(({ suite }) => ({ suite, planned: true }));
    for (const [fixture, outcome, ok] of [
      ["passed", "success", true],
      ["passed", "failure", false],
      ["failed", "success", false],
      ["skipped", "success", false],
      ["empty", "success", false],
    ]) {
      const report = JSON.parse(
        readFileSync(
          new URL(
            `./fixtures/e2e-summary/${fixture === "empty" ? "passed" : fixture}.json`,
            import.meta.url,
          ),
          "utf8",
        ),
      );
      if (fixture === "empty") report.suites = [];
      for (const { suite, requireAllTests } of entries) {
        const { status } = buildSummary({ suite, outcome, report, requireAllTests });
        writeFileSync(join(directory, `${suite}.json`), JSON.stringify(status));
      }
      const audit = auditE2ERequirements({ requiredSuites, statusDir: directory });
      assert.equal(audit.ok, ok, `${fixture}/${outcome}`);
      if (!ok) assert.equal(audit.blockers.length, entries.length);
    }
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("runtime, dependencies and routing changes retain layout stress in the legacy gate", () => {
  for (const path of [
    "apps/web/src/pages/Workbench/layout/workbenchLayoutExecutor.ts",
    "apps/web/src/styles/shadcn.css",
    "apps/web/e2e/tests/deleted.spec.ts",
    "apps/web/playwright.stress.config.ts",
    "apps/api/app/api/v1/_test_seed.py",
    "apps/api/app/api/v1/annotations.py",
    "packages/shared/example.ts",
    "pnpm-lock.yaml",
    "package.json",
    ".env.example",
    "docker-compose.yml",
    ".github/workflows/ci.yml",
    ".github/workflows/e2e-run.yml",
    "scripts/plan-e2e-suites.mjs",
    "scripts/plan-e2e-suites.test.mjs",
  ]) {
    assert.deepEqual(names("pull_request", [path]), [...functional, "layout-stress"], path);
  }
});

test("legacy push retains full coverage; scheduled and manual runs select layout stress only", () => {
  assert.deepEqual(names("push"), [...functional, "layout-stress"]);
  for (const event of ["schedule", "workflow_dispatch"])
    assert.deepEqual(names(event), ["layout-stress"]);
});

test("missing change information or an unsupported event cannot silently omit checks", () => {
  assert.throws(() => planE2ESuites("pull_request"), /changed paths/);
  assert.throws(() => planE2ESuites("unknown", []), /Unsupported/);
  assert.throws(() => selectSuites("unknown", []), /Unsupported/);
  assert.throws(() => selectSuites("pull_request", "not-an-array"), /changed paths/);
});

test("shadow selection classifies docs-only changes and proposes no app suites", () => {
  const docsOnly = [
    "README.md",
    "docs-site/user-guide/workbench/index.md",
    "docs/research/31-repository-optimization-p7-e2e-isolation.md",
  ];
  const shadow = shadowPlan("pull_request", docsOnly);
  assert.deepEqual(shadow.planned, []);
  assert.equal(shadow.classification.docsOnly, true);
  assert.deepEqual(shadow.added, []);
  assert.deepEqual(shadow.removed, functional);
});

test("executable docs examples and docs build config are app code, not docs-only", () => {
  for (const path of [
    "docs-site/dev/examples/protocol-demo.py",
    "docs-site/.vitepress/config.ts",
    "docs-site/dev/examples/data/fixture.json",
    ".github/workflows/ci.yml",
    ".github/actions/example/action.yml",
    "scripts/plan-e2e-suites.mjs",
    "pnpm-lock.yaml",
    ".env.example",
    "docs-site/package.json",
    "docs-site/scripts/check-orphan-images.mjs",
    "docs-site/.vitepress/theme/index.ts",
    "docs-site/dev/examples/echo-ml-backend/README.md",
  ]) {
    const shadow = shadowPlan("pull_request", [path]);
    assert.equal(shadow.classification.docsOnly, false, path);
    assert.deepEqual(shadow.planned, broadened, path);
    assert.equal(shadow.gates.effective.name, "broad", path);
    assert.ok(shadow.gates.effective.reason.trim(), path);
    assert.equal(shadow.timelineLayout, true, path);
  }
});

test("empty diffs and unrecognized paths fail closed to the conservative selection", () => {
  const empty = shadowPlan("pull_request", []);
  assert.deepEqual(empty.planned, broadened);
  assert.equal(empty.classification.emptyDiff, true);
  assert.ok(empty.warnings.some((warning) => /empty diff/.test(warning)));

  const unknown = shadowPlan("pull_request", ["model-configs/weights.yaml"]);
  assert.deepEqual(unknown.planned, broadened);
  assert.ok(unknown.classification.unknownPaths.includes("model-configs/weights.yaml"));
  assert.ok(unknown.warnings.some((warning) => /unrecognized top-level paths/.test(warning)));
});

test("multi-domain changes include layout stress with recorded reasons", () => {
  const shadow = shadowPlan("pull_request", [
    "apps/web/src/pages/Workbench/state/useWorkbenchShellModel.tsx",
    "apps/api/app/services/project_access.py",
  ]);
  assert.deepEqual(shadow.planned, broadened);
  assert.equal(shadow.classification.multiDomain, true);
  assert.ok(shadow.reasons.some((reason) => /multi-domain/.test(reason.because)));
});

test("shared Mask editing code selects native owners and the ordinary Mask workflows", () => {
  const path = "apps/web/src/pages/Workbench/stage/shared/geometry/maskOperations.ts";
  const shadow = shadowPlan("pull_request", [path]);
  assert.deepEqual(shadow.planned, ["smoke", ...maskSpecialties, "mask-workflows"]);
  assert.ok(shadow.reasons.some(({ because }) => because.includes(path)));
  assert.deepEqual(shadow.gates.planned, shadow.gates.effective);
  assert.equal(shadow.gates.effective.name, "risk");
  assert.deepEqual(shadow.gates.broad.suites, broadened);
  assert.deepEqual(shadow.gates.rollback.suites, [...functional, "layout-stress"]);
  assert.notDeepEqual(shadow.gates.broad.suites, shadow.gates.rollback.suites);
  assert.deepEqual(shadow.added, ["smoke", "mask-workflows"]);
  assert.deepEqual(shadow.removed, [...core, "ai-request-worker", ...pointcloud, "layout-stress"]);
});

test("each video owner requires all four video shards and the timeline check", () => {
  for (const path of [
    "apps/web/e2e/tests/video-timeline-seek.spec.ts",
    "apps/web/e2e/tests/workbench-video-tracker.spec.ts",
    "apps/web/src/pages/Workbench/stages/video/VideoWorkbench.tsx",
    "apps/web/src/pages/Workbench/stage/VideoPlaybackOverlay.module.css",
    "apps/web/src/pages/Workbench/stage/useVideoPreciseFrame.ts",
    "apps/web/src/pages/Workbench/stage/useVideoChunkDecoder.ts",
    "apps/web/src/pages/Workbench/stage/frameTimebase.ts",
    "apps/web/src/pages/Workbench/stage/timelineCoords.ts",
    "apps/web/src/pages/Workbench/state/useVideoIssueNavigation.ts",
    "apps/web/src/hooks/useOriginalVideoPlayback.ts",
    "apps/api/app/api/v1/video.py",
    "apps/api/app/services/video.py",
    "apps/api/app/api/v1/tasks/video.py",
    "apps/api/app/workers/video_tracker.py",
  ]) {
    const { planned, reasons, timelineLayout } = selectSuites("pull_request", [path]);
    const expected = /^apps\/api\/app\/(api|services)\//.test(path)
      ? broadened
      : ["smoke", ...video];
    assert.deepEqual([...planned].sort(), [...expected].sort(), path);
    assert.equal(timelineLayout, true, path);
    assert.deepEqual(
      reasons
        .filter(({ suite, because }) => video.includes(suite) && /triggered by /.test(because))
        .map(({ suite }) => suite),
      video,
      path,
    );
  }
});

test("only isolated test changes omit browser jobs without taking the docs bypass", () => {
  for (const paths of [
    ["apps/web/src/pages/Workbench/stage/useVideoPreciseFrame.test.ts"],
    ["apps/web/src/api/client.test.ts", "README.md"],
    ["apps/api/tests/test_pytest_sharding.py"],
    [
      "apps/api/tests/test_pytest_sharding.py",
      "apps/web/src/pages/Workbench/stages/three-d/useScenePlayback.test.ts",
    ],
  ]) {
    const shadow = shadowPlan("pull_request", paths);
    assert.deepEqual(shadow.planned, [], paths.join(", "));
    assert.equal(shadow.classification.docsOnly, false);
    assert.equal(shadow.classification.testOnly, true);
    assert.equal(shadow.gates.effective.name, "test-only");
    assert.match(shadow.gates.effective.reason, /existing CI gates/);
    assert.equal(shadow.timelineLayout, false);
  }
});

test("test support, build setup, shared auth, and unknown runtime paths still select broad", () => {
  for (const path of [
    "apps/web/src/test/apiRequestGuard.test.ts",
    "apps/web/src/test/renderWithProviders.tsx",
    "apps/web/src/__fixtures__/rasterMaskRle.json",
    "apps/web/vitest.setup.ts",
    "apps/web/vite.config.ts",
    "apps/web/playwright.config.ts",
    "apps/web/e2e/fixtures/video-playback-overlay.tsx",
    "apps/web/e2e/helpers/video-timeline-seek.ts",
    "apps/web/src/stores/authStore.ts",
    "apps/api/tests/test_dataset_content.py",
    "apps/api/tests/conftest.py",
    "apps/web/e2e/tests/mask-session-guard.spec.ts",
    "apps/api/app/workers/new-worker.py",
    "apps/web/src/pages/Login/ForgotPasswordPage.tsx",
    "apps/web/src/pages/Unknown/NewPage.tsx",
    "apps/web/e2e/tests/auth.spec.ts",
  ]) {
    const shadow = shadowPlan("pull_request", [path]);
    assert.deepEqual(shadow.planned, broadened, path);
    assert.equal(shadow.gates.effective.name, "broad", path);
    assert.equal(shadow.timelineLayout, true, path);
  }
});

test("Login uses core, but changed ordinary product behavior uses its owning spec", () => {
  const login = "apps/web/src/pages/Login/LoginPage.tsx";
  const plan = selectSuites("pull_request", [login, "apps/api/tests/test_pytest_sharding.py"]);
  assert.deepEqual(plan.planned, ["smoke"]);
  assert.equal(plan.timelineLayout, false);

  for (const [path, suite, spec] of [
    [
      "apps/web/src/pages/Projects/ProjectDataManagerPage.tsx",
      "data-manager",
      "data-manager-management",
    ],
    [
      "apps/web/src/pages/Datasets/DatasetsPage.tsx",
      "operational-lists",
      "filter-operational-lists",
    ],
    [
      "apps/web/src/pages/Projects/sections/AnnotationGuideSection.tsx",
      "annotation-guide",
      "guide-autosave",
    ],
    [
      "apps/web/src/pages/Training/TrainingConfigurationEditor.tsx",
      "training-parameters",
      "training-parameter-reset",
    ],
    ["apps/web/e2e/tests/lidar-export-trusted.spec.ts", "lidar-export", "lidar-export-trusted"],
    ["apps/web/e2e/tests/mask-slice.spec.ts", "mask-workflows", "mask-slice"],
  ]) {
    const selected = selectSuites("pull_request", [login, path, "apps/web/src/api/client.test.ts"]);
    assert.deepEqual(selected.planned, ["smoke", suite], path);
    assert.equal(selected.timelineLayout, false, path);
    const entry = SUITE_CONTRACT.find((item) => item.suite === suite);
    assert.ok(entry.command.split(" ").includes(`e2e/tests/${spec}.spec.ts`), path);
  }
});

test("short dock layout follows shared layout code without scheduling long stress", () => {
  for (const path of [
    "apps/web/src/pages/Workbench/layout/WorkbenchDockWorkspace.tsx",
    "apps/web/src/pages/Workbench/layout/workbenchLayoutExecutor.ts",
    "apps/web/src/pages/Workbench/state/useWorkbenchWorkspaceLayout.ts",
    "apps/api/app/schemas/workbench_workspace.py",
  ]) {
    const selected = selectSuites("pull_request", [path]);
    assert.deepEqual(selected.planned, ["smoke", ...pointcloud, "layout-short"], path);
    assert.equal(selected.timelineLayout, true, path);
  }
  const short = selectSuites("pull_request", ["apps/web/e2e/tests/workbench-layout.spec.ts"]);
  assert.deepEqual(short.planned, ["smoke", "layout-short"]);
  assert.equal(short.timelineLayout, true);
  assert.deepEqual(
    selectSuites("pull_request", ["apps/web/e2e/tests/workbench-layout-matrix.spec.ts"]).planned,
    ["smoke", "layout-stress", "layout-short"],
  );
  assert.deepEqual(
    selectSuites("pull_request", ["apps/web/e2e/tests/workbench-pointcloud-layout-matrix.spec.ts"])
      .planned,
    ["smoke", ...pointcloud, "layout-stress"],
  );
});

test("point-cloud rendering and the standalone timeline spec each keep their real browser owner", () => {
  const cloud = selectSuites("pull_request", [
    "apps/web/src/pages/Workbench/stages/three-d/PointCloudTriViewPass.ts",
    "apps/web/e2e/tests/workbench-pointcloud-playback.spec.ts",
  ]);
  assert.deepEqual(cloud.planned, ["smoke", ...pointcloud]);
  assert.equal(cloud.timelineLayout, false);

  const timeline = selectSuites("pull_request", [
    "apps/web/e2e/layout/video-playback-overlay.spec.ts",
  ]);
  assert.deepEqual(timeline.planned, ["smoke"]);
  assert.equal(timeline.timelineLayout, true);
});

test("shadow diff against the legacy gate is recorded for schedule changes", () => {
  const shadow = shadowPlan("schedule");
  assert.deepEqual(shadow.legacy, ["layout-stress"]);
  assert.deepEqual(shadow.planned, broadened);
  assert.deepEqual(shadow.added, ["smoke", ...core, ...specialties, ...video, ...pointcloud]);
  assert.deepEqual(shadow.removed, []);
  assert.ok(
    shadow.warnings.some((warning) => /legacy rollback gate runs extended-only/.test(warning)),
  );
});

test("app-code changes can never produce an empty shadow selection", () => {
  const shadow = shadowPlan("pull_request", ["apps/api/app/services/project_access.py"]);
  assert.ok(shadow.planned.length > 0);
});

test("CLI emits the expected suites and delegates CPU coverage by scope", () => {
  const cwd = mkdtempSync(join(tmpdir(), "e2e-cli-contract-"));
  const script = fileURLToPath(new URL("./plan-e2e-suites.mjs", import.meta.url));
  const git = (...args) =>
    execFileSync("git", args, { cwd, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim();
  const run = (env) =>
    spawnSync(process.execPath, [script], {
      cwd,
      encoding: "utf8",
      env: { ...process.env, ...env },
    });
  try {
    git("init");
    git("config", "user.name", "E2E routing test");
    git("config", "user.email", "test@example.invalid");
    mkdirSync(join(cwd, "placeholder"), { recursive: true });
    writeFileSync(join(cwd, "placeholder", "seed.txt"), "seed\n");
    git("add", ".");
    git("-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "commit", "-m", "seed");

    const dispatched = run({ GITHUB_EVENT_NAME: "workflow_dispatch", E2E_DISPATCH_SCOPE: "full" });
    assert.equal(dispatched.status, 0, dispatched.stderr);
    const outputs = Object.fromEntries(
      dispatched.stdout
        .trim()
        .split("\n")
        .map((line) => [line.slice(0, line.indexOf("=")), line.slice(line.indexOf("=") + 1)]),
    );
    assert.equal(outputs.ml_cpu, "true");
    assert.equal(readShadow(cwd, outputs).mlCpu, true);
    assert.equal(outputs.run_suites, "true");
    assert.equal(outputs.timeline_layout, "true");
    const requiredManifest = JSON.parse(outputs.required);
    assert.equal(requiredManifest.classification, "app-code");
    assert.equal(
      requiredManifest.suites.every((entry) => entry.planned === true),
      true,
    );
    assert.equal(
      requiredManifest.suites.find((entry) => entry.suite === "smoke").flakyPolicy,
      "forbid",
    );
    assert.deepEqual(
      requiredManifest.suites
        .filter(({ suite }) => pointcloud.includes(suite))
        .map(({ suite }) => suite),
      pointcloud,
    );
    assertFullDispatch(outputs.matrix);
    assert.deepEqual(
      JSON.parse(outputs.legacy).include.map((entry) => entry.suite),
      ["layout-stress"],
    );

    const extended = run({
      GITHUB_EVENT_NAME: "workflow_dispatch",
      E2E_DISPATCH_SCOPE: "extended",
    });
    assert.equal(extended.status, 0, extended.stderr);
    const extOutputs = Object.fromEntries(
      extended.stdout
        .trim()
        .split("\n")
        .map((line) => [line.slice(0, line.indexOf("=")), line.slice(line.indexOf("=") + 1)]),
    );
    assert.equal(extOutputs.ml_cpu, "false");
    assert.deepEqual(
      JSON.parse(extOutputs.matrix).include.map(({ suite }) => suite),
      ["layout-stress"],
    );
    assert.deepEqual(
      JSON.parse(extOutputs.required).suites.map(({ suite }) => suite),
      ["layout-stress"],
    );

    const push = run({ GITHUB_EVENT_NAME: "push" });
    assert.equal(push.status, 0, push.stderr);
    const pushOutputs = Object.fromEntries(
      push.stdout
        .trim()
        .split("\n")
        .map((line) => [line.slice(0, line.indexOf("=")), line.slice(line.indexOf("=") + 1)]),
    );
    const pushShadow = readShadow(cwd, pushOutputs);
    assert.equal(pushOutputs.ml_cpu, "true");
    assert.equal(pushShadow.mlCpu, true);
    assert.equal(pushOutputs.run_suites, "true");
    assert.equal(pushOutputs.timeline_layout, "true");
    assertFullDispatch(pushOutputs.matrix);

    const sharedBase = git("rev-parse", "HEAD");
    mkdirSync(join(cwd, "apps/_shared/protocol_v2/src"), { recursive: true });
    writeFileSync(join(cwd, "apps/_shared/protocol_v2/src/schemas.py"), "pass\n");
    git("add", ".");
    git(
      "-c",
      "core.hooksPath=/dev/null",
      "-c",
      "commit.gpgsign=false",
      "commit",
      "-m",
      "shared change",
    );
    const sharedPr = spawnSync(process.execPath, [script, sharedBase], {
      cwd,
      encoding: "utf8",
      env: { ...process.env, GITHUB_EVENT_NAME: "pull_request" },
    });
    assert.equal(sharedPr.status, 0, sharedPr.stderr);
    const sharedOutputs = Object.fromEntries(
      sharedPr.stdout
        .trim()
        .split("\n")
        .map((line) => [line.slice(0, line.indexOf("=")), line.slice(line.indexOf("=") + 1)]),
    );
    assert.equal(sharedOutputs.ml_cpu, "true");
    assert.equal(readShadow(cwd, sharedOutputs).mlCpu, true);
  } finally {
    rmSync(cwd, { recursive: true, force: true });
  }
});

test("CLI dispatch preserves selection order for tied and unmeasured jobs", (t) => {
  const cwd = mkdtempSync(join(tmpdir(), "e2e-partial-history-"));
  t.after(() => rmSync(cwd, { recursive: true, force: true }));
  for (const file of ["plan-e2e-suites.mjs", "test-suites.json"]) {
    copyFileSync(new URL(`./${file}`, import.meta.url), join(cwd, file));
  }
  writeFileSync(
    join(cwd, "e2e-job-durations.json"),
    JSON.stringify({ jobSeconds: { "mask-native": 10, "mask-readonly": 10, "layout-stress": 20 } }),
  );
  const stdout = execFileSync(process.execPath, ["plan-e2e-suites.mjs"], {
    cwd,
    encoding: "utf8",
    env: {
      ...process.env,
      GITHUB_EVENT_NAME: "push",
      E2E_SELECTION_MODE: "planned",
      E2E_SHADOW_PATH: join(cwd, "shadow.json"),
    },
  });
  const matrix = stdout.split("\n").find((line) => line.startsWith("matrix="));
  const suites = JSON.parse(matrix.slice("matrix=".length)).include.map(({ suite }) => suite);
  const first = ["smoke", "layout-stress", "mask-readonly", "mask-native"];
  assert.deepEqual(suites, [...first, ...broadened.filter((suite) => !first.includes(suite))]);
});

test("large PR reports use files while suite dispatch and final audit outputs stay bounded", () => {
  const cwd = mkdtempSync(join(tmpdir(), "e2e-large-plan-"));
  const script = fileURLToPath(new URL("./plan-e2e-suites.mjs", import.meta.url));
  const git = (...args) =>
    execFileSync("git", args, { cwd, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim();
  try {
    git("init");
    git("config", "user.name", "E2E large plan test");
    git("config", "user.email", "test@example.invalid");
    writeFileSync(join(cwd, "README.md"), "base\n");
    git("add", ".");
    git("-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "commit", "-m", "base");
    const base = git("rev-parse", "HEAD");
    const paths = Array.from(
      { length: 1730 },
      (_, index) => `apps/web/changed-${String(index).padStart(4, "0")}-${"x".repeat(120)}.ts`,
    );
    paths.push("apps/_shared/protocol_v2/changed.py");
    for (const path of paths) {
      mkdirSync(resolve(cwd, path, ".."), { recursive: true });
      writeFileSync(join(cwd, path), "fixture\n");
    }
    git("add", ".");
    git("-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "commit", "-m", "large PR");
    const reportDir = join(cwd, "report files");
    mkdirSync(reportDir);
    const shadowPath = join(reportDir, "shadow.json");
    const summaryPath = join(reportDir, "summary.md");
    const result = spawnSync(process.execPath, [script, base, summaryPath], {
      cwd,
      encoding: "utf8",
      env: {
        ...process.env,
        GITHUB_EVENT_NAME: "pull_request",
        E2E_SELECTION_MODE: "planned",
        E2E_SHADOW_PATH: shadowPath,
      },
    });
    assert.equal(result.status, 0, result.stderr);
    const outputs = Object.fromEntries(
      result.stdout
        .trim()
        .split("\n")
        .map((line) => [line.slice(0, line.indexOf("=")), line.slice(line.indexOf("=") + 1)]),
    );
    assert.deepEqual(Object.keys(outputs).sort(), [
      "docs_only",
      "legacy",
      "matrix",
      "ml_cpu",
      "required",
      "run_suites",
      "selection_mode",
      "shadow_path",
      "timeline_layout",
    ]);
    assert.ok(Buffer.byteLength(result.stdout) < 32 * 1024);
    for (const [key, value] of Object.entries(outputs))
      assert.ok(Buffer.byteLength(value) < 16 * 1024, `${key} must not grow with changed paths`);
    assert.equal(outputs.shadow_path, shadowPath);
    assert.ok(Buffer.byteLength(readFileSync(shadowPath)) > 128 * 1024);
    const shadow = readShadow(cwd, outputs);
    assert.deepEqual([...shadow.classification.appCode].sort(), [...paths].sort());
    assert.deepEqual(shadow.planned, broadened);
    assert.equal(shadow.mlCpu, true);
    assert.equal(outputs.ml_cpu, "true");
    assert.equal(outputs.run_suites, "true");
    assertFullDispatch(outputs.matrix);
    assert.deepEqual(
      JSON.parse(outputs.required).suites.map(({ suite }) => suite),
      withWorkerReceipt(broadened),
    );
    assert.equal(
      JSON.parse(outputs.required).suites.find(({ suite }) => suite === "smoke").flakyPolicy,
      "forbid",
    );

    const summary = readFileSync(summaryPath, "utf8");
    assert.ok(summary.includes(broadened.join(", ")));
    assert.ok(Buffer.byteLength(summary) < 16 * 1024);

    const transfer = spawnSync(
      "bash",
      ["-e", "-c", "printf '%s' \"$E2E_REQUIRED\" > required-suites.json"],
      { cwd, encoding: "utf8", env: { ...process.env, E2E_REQUIRED: outputs.required } },
    );
    assert.equal(transfer.status, 0, transfer.stderr);
    assert.equal(readFileSync(join(cwd, "required-suites.json"), "utf8"), outputs.required);
  } finally {
    rmSync(cwd, { recursive: true, force: true });
  }
});
test("CLI handles deleted and renamed paths, and fails on an unavailable base", () => {
  const cwd = mkdtempSync(join(tmpdir(), "e2e-routing-"));
  const script = fileURLToPath(new URL("./plan-e2e-suites.mjs", import.meta.url));
  const git = (...args) =>
    execFileSync("git", args, { cwd, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim();
  const run = (base) =>
    spawnSync(process.execPath, [script, base], {
      cwd,
      encoding: "utf8",
      env: { ...process.env, GITHUB_EVENT_NAME: "pull_request" },
    });
  try {
    git("init");
    git("config", "user.name", "E2E routing test");
    git("config", "user.email", "test@example.invalid");
    mkdirSync(join(cwd, "apps/web"), { recursive: true });
    writeFileSync(join(cwd, "apps/web/old file\n.spec.ts"), "fixture\n");
    git("add", ".");
    git("-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "commit", "-m", "base");
    const base = git("rev-parse", "HEAD");
    git("mv", "apps/web/old file\n.spec.ts", "README.md");
    git(
      "-c",
      "core.hooksPath=/dev/null",
      "-c",
      "commit.gpgsign=false",
      "commit",
      "-m",
      "move out of frontend",
    );
    const result = run(base);
    assert.equal(result.status, 0, result.stderr);
    const lines = result.stdout.trim().split("\n");
    assert.ok(lines.includes("docs_only=false"));
    const matrixLine = lines.find((line) => line.startsWith("matrix="));
    const legacyLine = lines.find((line) => line.startsWith("legacy="));
    const shadowPathLine = lines.find((line) => line.startsWith("shadow_path="));
    assert.ok(
      matrixLine && legacyLine && shadowPathLine,
      "CLI must emit matrix/legacy/shadow_path lines",
    );
    assertFullDispatch(matrixLine.slice("matrix=".length));
    assert.deepEqual(
      JSON.parse(legacyLine.slice("legacy=".length)).include.map(({ suite }) => suite),
      [...functional, "layout-stress"],
    );
    const shadow = readShadow(cwd, {
      shadow_path: shadowPathLine.slice("shadow_path=".length),
    });
    assert.deepEqual(shadow.legacy, [...functional, "layout-stress"]);
    assert.ok(Array.isArray(shadow.added) && Array.isArray(shadow.removed));
    assert.notEqual(run("0".repeat(40)).status, 0);
    assert.notEqual(run("").status, 0);
  } finally {
    rmSync(cwd, { recursive: true, force: true });
  }
});

test("bounded smoke runs without retries and forbids flaky passes", () => {
  const smoke = SUITE_CONTRACT.find((entry) => entry.suite === "smoke");
  assert.ok(smoke, "smoke suite must exist in the contract");
  assert.equal(smoke.flakyPolicy, "forbid");
  assert.match(smoke.command, /--retries=0/);
  assert.doesNotMatch(smoke.command, /健康检查/);
  assert.match(smoke.command, /正确凭证\|错密码\|未登录访问/);
  assert.equal(
    SUITE_CONTRACT.some((entry) => entry.flakyPolicy === "forbid" && entry.suite !== "smoke"),
    false,
  );
});

test("the required manifest audits every effective job and the bundled Worker in each mode", () => {
  const cwd = mkdtempSync(join(tmpdir(), "e2e-required-ids-"));
  const script = fileURLToPath(new URL("./plan-e2e-suites.mjs", import.meta.url));
  const git = (...args) =>
    execFileSync("git", args, { cwd, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim();
  const parse = (stdout) =>
    Object.fromEntries(
      stdout
        .trim()
        .split("\n")
        .filter((line) => line.includes("="))
        .map((line) => [line.slice(0, line.indexOf("=")), line.slice(line.indexOf("=") + 1)]),
    );
  const execute = (env, argv = []) =>
    spawnSync(process.execPath, [script, ...argv], {
      cwd,
      encoding: "utf8",
      env: {
        ...process.env,
        E2E_SELECTION_MODE: undefined,
        E2E_SHADOW_PATH: join(cwd, ".git", "e2e-shadow.json"),
        ...env,
      },
    });
  const run = (env, argv) => {
    const result = execute(env, argv);
    assert.equal(result.status, 0, result.stderr);
    return parse(result.stdout);
  };
  const ids = (value) => JSON.parse(value).include.map(({ suite }) => suite);
  const requiredIds = (value) => JSON.parse(value).suites.map(({ suite }) => suite);

  try {
    git("init");
    git("config", "user.name", "E2E routing test");
    git("config", "user.email", "test@example.invalid");
    mkdirSync(join(cwd, "apps/web/src/pages/Workbench/state"), { recursive: true });
    writeFileSync(
      join(cwd, "apps/web/src/pages/Workbench/state/useWorkbenchShellModel.tsx"),
      "seed\n",
    );
    git("add", ".");
    git("-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "commit", "-m", "seed");
    const appBase = git("rev-parse", "HEAD");
    writeFileSync(
      join(cwd, "apps/web/src/pages/Workbench/state/useWorkbenchShellModel.tsx"),
      "changed\n",
    );
    git("add", ".");
    git(
      "-c",
      "core.hooksPath=/dev/null",
      "-c",
      "commit.gpgsign=false",
      "commit",
      "-m",
      "app change",
    );

    for (const mode of [undefined, "", "  ", "planned", "legacy"]) {
      const env = { GITHUB_EVENT_NAME: "push" };
      if (mode !== undefined) env.E2E_SELECTION_MODE = mode;
      const out = run(env);
      const expected = mode === "legacy" ? [...functional, "layout-stress"] : broadened;
      assert.equal(out.docs_only, "false");
      assert.equal(out.run_suites, "true");
      if (mode === "legacy") {
        assert.deepEqual(ids(out.matrix), expected);
        assert.ok(JSON.parse(out.matrix).include.every((entry) => !("dedicatedVideo" in entry)));
      } else assertFullDispatch(out.matrix);
      assert.deepEqual(requiredIds(out.required), withWorkerReceipt(expected));
      if (mode !== "legacy") {
        const smoke = JSON.parse(out.required).suites.find(({ suite }) => suite === "smoke");
        assert.equal(smoke.flakyPolicy, "forbid");
      }
    }
    const invalid = execute({ GITHUB_EVENT_NAME: "push", E2E_SELECTION_MODE: "sideways" });
    assert.notEqual(invalid.status, 0);
    assert.match(invalid.stderr, /Invalid E2E selection mode: sideways/);

    for (const mode of [undefined, "", "legacy"]) {
      const env = { GITHUB_EVENT_NAME: "pull_request" };
      if (mode !== undefined) env.E2E_SELECTION_MODE = mode;
      const out = run(env, [appBase]);
      assert.equal(out.docs_only, "false");
      const { gates, timelineLayout } = readShadow(cwd, out);
      assert.equal(timelineLayout, true);
      assert.deepEqual(gates.planned.suites, broadened);
      assert.equal(gates.planned.name, "broad");
      assert.equal(gates.effective.name, mode === "legacy" ? "legacy" : "broad");
      assert.deepEqual(ids(out.matrix).toSorted(), gates.effective.suites.toSorted());
      assert.deepEqual(
        requiredIds(out.required).sort(),
        withWorkerReceipt(ids(out.matrix)).sort(),
        `PR app mode=${JSON.stringify(mode)}`,
      );
    }

    for (const event of ["workflow_dispatch", "schedule"]) {
      const out = run({
        GITHUB_EVENT_NAME: event,
        E2E_DISPATCH_SCOPE: "full",
        E2E_SCHEDULE_SCOPE: "full",
      });
      assert.equal(out.docs_only, "false", event);
      assert.deepEqual(requiredIds(out.required), withWorkerReceipt(broadened), event);
      assertFullDispatch(out.matrix);
    }

    const pointcloudBase = git("rev-parse", "HEAD");
    mkdirSync(join(cwd, "apps/web/e2e/tests"), { recursive: true });
    writeFileSync(join(cwd, "apps/web/e2e/tests/workbench-pointcloud-routing.spec.ts"), "test\n");
    git("add", "apps/web/e2e/tests");
    git(
      "-c",
      "core.hooksPath=/dev/null",
      "-c",
      "commit.gpgsign=false",
      "commit",
      "-m",
      "pointcloud",
    );
    const pointcloudPr = run({ GITHUB_EVENT_NAME: "pull_request" }, [pointcloudBase]);
    const [firstSuite, ...selectedPoints] = ids(pointcloudPr.matrix);
    assert.equal(firstSuite, "smoke");
    assert.deepEqual(selectedPoints.toSorted(), pointcloud.toSorted());
    assert.deepEqual(requiredIds(pointcloudPr.required), ["smoke", ...pointcloud]);
    assert.equal(pointcloudPr.timeline_layout, "false");

    const unitBase = git("rev-parse", "HEAD");
    mkdirSync(join(cwd, "apps/api/tests"), { recursive: true });
    writeFileSync(join(cwd, "apps/api/tests/test_pytest_sharding.py"), "pass\n");
    git("add", "apps/api/tests");
    git("-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "commit", "-m", "unit");
    const unit = run({ GITHUB_EVENT_NAME: "pull_request" }, [unitBase]);
    assert.equal(unit.docs_only, "false");
    assert.equal(unit.ml_cpu, "true");
    assert.equal(unit.run_suites, "false");
    assert.equal(unit.timeline_layout, "false");
    assert.deepEqual(ids(unit.matrix), []);
    const unitManifest = JSON.parse(unit.required);
    assert.equal(unitManifest.classification, "test-only");
    assert.deepEqual(unitManifest.suites, []);
    assert.ok(unitManifest.reason.trim());
    assert.equal(auditE2ERequirements({ requiredSuites: unitManifest, statusDir: cwd }).ok, true);
    const unitLegacy = run({ GITHUB_EVENT_NAME: "pull_request", E2E_SELECTION_MODE: "legacy" }, [
      unitBase,
    ]);
    assert.equal(unitLegacy.docs_only, "false");
    assert.equal(unitLegacy.timeline_layout, "true");
    assert.equal(unitLegacy.run_suites, "true");
    assert.deepEqual(ids(unitLegacy.matrix), [...functional, "layout-stress"]);
    assert.deepEqual(requiredIds(unitLegacy.required), withWorkerReceipt(ids(unitLegacy.matrix)));

    const docsBase = git("rev-parse", "HEAD");
    assert.equal(run({ GITHUB_EVENT_NAME: "pull_request" }, [docsBase]).docs_only, "false");
    mkdirSync(join(cwd, "docs-site/user-guide"), { recursive: true });
    writeFileSync(join(cwd, "docs-site/user-guide/index.md"), "# docs\n");
    mkdirSync(join(cwd, "ROADMAP/research"), { recursive: true });
    for (const path of ["DEV.md", "ROADMAP.md", "ROADMAP/next.md", "ROADMAP/research/notes.md"])
      writeFileSync(join(cwd, path), "# plan\n");
    git("add", ".");
    git("-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "commit", "-m", "docs");
    const docsPlanned = run({ GITHUB_EVENT_NAME: "pull_request", E2E_SELECTION_MODE: "" }, [
      docsBase,
    ]);
    assert.deepEqual(ids(docsPlanned.matrix), []);
    assert.equal(docsPlanned.docs_only, "true");
    assert.equal(docsPlanned.ml_cpu, "false");
    assert.equal(docsPlanned.run_suites, "false");
    assert.equal(docsPlanned.timeline_layout, "false");
    const docsManifest = JSON.parse(docsPlanned.required);
    assert.equal(docsManifest.classification, "docs-only");
    assert.deepEqual(docsManifest.suites, []);
    assert.ok(docsManifest.reason.trim());
    assert.deepEqual(ids(docsPlanned.legacy), functional);
    const docsLegacy = run({ GITHUB_EVENT_NAME: "pull_request", E2E_SELECTION_MODE: "legacy" }, [
      docsBase,
    ]);
    assert.equal(docsLegacy.run_suites, "true");
    assert.equal(docsLegacy.docs_only, "true");
    assert.equal(docsLegacy.timeline_layout, "false");
    assert.deepEqual(ids(docsLegacy.matrix), [...functional]);
    assert.deepEqual(requiredIds(docsLegacy.required), withWorkerReceipt(ids(docsLegacy.matrix)));

    const unknownBase = git("rev-parse", "HEAD");
    writeFileSync(join(cwd, "ROADMAP/check.mjs"), "export {};\n");
    git("add", "ROADMAP/check.mjs");
    git("-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "commit", "-m", "script");
    assert.equal(run({ GITHUB_EVENT_NAME: "pull_request" }, [unknownBase]).docs_only, "false");
  } finally {
    rmSync(cwd, { recursive: true, force: true });
  }
});

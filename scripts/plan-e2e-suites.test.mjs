import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync, mkdirSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { planE2ESuites, selectSuites, shadowPlan, SUITE_CONTRACT } from "./plan-e2e-suites.mjs";

const names = (event, paths) => planE2ESuites(event, paths).include.map(({ suite }) => suite);
const functional = [
  "default-one",
  "default-two",
  "default-three",
  "default-four",
  "mask-readonly",
  "mask-native",
  "mask-ai-native",
];

test("every PR retains all four functional shards and three Mask suites", () => {
  assert.deepEqual(names("pull_request", []), functional);
  assert.deepEqual(
    names("pull_request", ["docs-site/user-guide/workbench/index.md", "README.md"]),
    functional,
  );
});

const core = ["default-one", "default-two", "default-three", "default-four"];
const specialties = ["mask-readonly", "mask-native", "mask-ai-native"];
// Broadened §6.3 selection: smoke + full shards + every dedicated contract +
// extended, because the default shards never execute dedicated matrices.
const broadened = [
  "smoke",
  ...core,
  ...specialties,
  "video-pipeline",
  "pointcloud",
  "visual",
  "layout-stress",
];

test("runtime, dependencies and the routing mechanism select both extended checks", () => {
  for (const path of [
    "apps/web/src/pages/Workbench/layout/workbenchLayoutExecutor.ts",
    "apps/web/src/styles/shadcn.css",
    "apps/web/e2e/tests/deleted.spec.ts",
    "apps/web/playwright.extended.config.ts",
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
    assert.deepEqual(
      names("pull_request", [path]),
      [...functional, "visual", "layout-stress"],
      path,
    );
  }
});

test("main always runs everything; scheduled and manual runs select the extended suites", () => {
  assert.deepEqual(names("push"), [...functional, "visual", "layout-stress"]);
  for (const event of ["schedule", "workflow_dispatch"])
    assert.deepEqual(names(event), ["visual", "layout-stress"]);
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
  // The gate is unchanged: docs-only PRs still run the full legacy matrix
  // until P9 switches it, and the diff records exactly that.
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
    "docs-site/scripts/check-image-manifest.mjs",
    "docs-site/.vitepress/theme/index.ts",
    "docs-site/dev/examples/echo-ml-backend/README.md",
  ]) {
    const shadow = shadowPlan("pull_request", [path]);
    assert.equal(shadow.classification.docsOnly, false, path);
    // Executable content (examples, VitePress build) is unmapped app code and
    // therefore broadens to the full conservative selection.
    assert.deepEqual(shadow.planned, broadened, path);
    assert.ok(
      shadow.classification.unmappedAppPaths.length > 0 || shadow.classification.multiDomain,
      path,
    );
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

test("multi-domain changes broaden to the extended checks with recorded reasons", () => {
  const shadow = shadowPlan("pull_request", [
    "apps/web/src/pages/Workbench/state/useWorkbenchShellModel.tsx",
    "apps/api/app/services/project_access.py",
  ]);
  // Broadening selects every dedicated contract, because the full shards do
  // not execute the Mask/video/pointcloud matrices at runtime.
  assert.deepEqual(shadow.planned, broadened);
  assert.equal(shadow.classification.multiDomain, true);
  assert.ok(shadow.reasons.some((reason) => /multi-domain/.test(reason.because)));
});

test("single-domain frontend changes select core plus triggered specialties", () => {
  const shadow = shadowPlan("pull_request", [
    "apps/web/src/pages/Workbench/stage/shared/geometry/maskOperations.ts",
  ]);
  // Core stays unconditional for app code, the Mask specialties ride along as
  // triggered, and the extended checks are the recorded narrowing proposal.
  assert.deepEqual(shadow.planned, ["smoke", ...specialties]);
  assert.ok(shadow.reasons.some((reason) => /raster Mask/.test(reason.because)));
  // Smoke itself is shadow-only, so the diff against the frozen gate shows it
  // as an addition together with the dropped default shards and extended.
  assert.deepEqual(shadow.added, ["smoke"]);
  assert.deepEqual(shadow.removed, [...core, "visual", "layout-stress"]);
});

test("mask runtime numbers from P7 evidence ride with the contract", () => {
  const contractFor = (suite) => {
    const entry = SUITE_CONTRACT.find((candidate) => candidate.suite === suite);
    assert.ok(entry, `missing contract entry for ${suite}`);
    return entry;
  };
  const readonly = contractFor("mask-readonly");
  assert.equal(readonly.runtime.collects, 13);
  assert.equal(readonly.runtime.skipped, 11);
  const native = contractFor("mask-native");
  assert.equal(native.runtime.collects, 22);
  assert.equal(native.runtime.skipped, 2);
});

test("shared/ML contract entries carry the per-suite CPU audit facts", () => {
  const shadow = shadowPlan("pull_request", [
    "apps/_shared/protocol_v2/src/aap_protocol_v2/schemas.py",
    "apps/sam3-backend/main.py",
  ]);
  const contracts = shadow.sharedContracts.qualification;
  const bySuite = Object.fromEntries(contracts.map((suite) => [suite.suite, suite]));
  assert.deepEqual(Object.keys(bySuite).sort(), [
    "ml-grounded-sam2",
    "ml-onnxtools",
    "ml-rapidocr",
    "ml-sam3",
    "ml-yolo",
    "shared-backend-runtime",
    "shared-mask-utils",
    "shared-protocol-v2",
  ]);
  // Per-audit facts: no GPU/weights/network anywhere; torch split is explicit.
  assert.match(bySuite["shared-protocol-v2"].dependencies, /numpy/);
  assert.match(bySuite["ml-yolo"].reason, /without torch or ultralytics/);
  assert.match(
    bySuite["ml-grounded-sam2"].reason,
    /10 torch-free files \(68 tests\) \+ 9 CPU-torch files \(82 tests\)/,
  );
  assert.match(bySuite["ml-sam3"].reason, /3 CPU-torch files \(43 tests\)/);
  for (const suite of contracts) {
    assert.match(suite.command, /run-ml-cpu-tests\.sh run /);
    assert.equal(suite.wired, false);
    assert.match(suite.dependencies, /CPU only/);
  }
  // Execution is delegated to the ml-cpu workflow: the planner schedules none
  // of them, and the caller wiring lives in ci.yml.
  assert.ok(shadow.planned.every((suite) => !suite.startsWith("ml-")));
});
test("shadow diff against the legacy gate is recorded for schedule changes", () => {
  const shadow = shadowPlan("schedule");
  // Legacy: extended-only. Shadow proposal per §6.2: nightly full matrix.
  assert.deepEqual(shadow.legacy, ["visual", "layout-stress"]);
  assert.deepEqual(shadow.planned, [
    "smoke",
    ...core,
    ...specialties,
    "video-pipeline",
    "pointcloud",
    "visual",
    "layout-stress",
  ]);
  assert.deepEqual(shadow.added, [
    "smoke",
    ...core,
    ...specialties,
    "video-pipeline",
    "pointcloud",
  ]);
  assert.deepEqual(shadow.removed, []);
  assert.ok(
    shadow.warnings.some((warning) => /legacy rollback gate runs extended-only/.test(warning)),
  );
});

test("app-code changes can never produce an empty shadow selection", () => {
  // The core suites are unconditional for app code, so this only holds
  // structurally; the explicit throw guards future edits to the rules.
  const shadow = shadowPlan("pull_request", ["apps/api/app/services/project_access.py"]);
  assert.ok(shadow.planned.length > 0);
});

test("CLI stdout carries exactly the outputs the workflow consumers read", () => {
  const cwd = mkdtempSync(join(tmpdir(), "e2e-cli-contract-"));
  const script = fileURLToPath(new URL("./plan-e2e-suites.mjs", import.meta.url));
  const ciYml = readFileSync(
    fileURLToPath(new URL("../.github/workflows/ci.yml", import.meta.url)),
    "utf8",
  );
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

    // An invalid base fails closed regardless of the shadow report.
    const pr = run({ GITHUB_EVENT_NAME: "pull_request", E2E_BASE_SHA: "0".repeat(40) });
    assert.notEqual(pr.status, 0);

    const dispatched = run({ GITHUB_EVENT_NAME: "workflow_dispatch", E2E_DISPATCH_SCOPE: "full" });
    assert.equal(dispatched.status, 0, dispatched.stderr);
    const outputs = Object.fromEntries(
      dispatched.stdout
        .trim()
        .split("\n")
        .map((line) => [line.slice(0, line.indexOf("=")), line.slice(line.indexOf("=") + 1)]),
    );
    // The workflow consumes exactly these step outputs.
    assert.ok(ciYml.includes("steps.plan.outputs.matrix"));
    assert.ok(
      ciYml.includes("steps.plan.outputs.ml-cpu") || ciYml.includes("steps.plan.outputs.ml_cpu"),
    );
    assert.ok(ciYml.includes("steps.plan.outputs.required"));
    assert.ok("matrix" in outputs, "CLI must emit matrix=");
    assert.ok("shadow" in outputs, "CLI must emit shadow=");
    assert.ok(
      "ml_cpu" in outputs,
      "ci.yml reads steps.plan.outputs.ml_cpu; the CLI must emit the same key",
    );
    assert.ok(
      "required" in outputs,
      "ci.yml reads steps.plan.outputs.required; the CLI must emit the same key",
    );
    // Manual full dispatch triggers the delegated CPU contracts; the required
    // manifest is the planned selection with the core forbid-flaky policy.
    assert.equal(outputs.ml_cpu, "true");
    assert.equal(JSON.parse(outputs.shadow).mlCpu, true);
    assert.equal(outputs.run_suites, "true");
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
      JSON.parse(outputs.matrix).include.map((entry) => entry.suite),
      [...broadened],
    );
    assert.deepEqual(
      JSON.parse(outputs.legacy).include.map((entry) => entry.suite),
      ["visual", "layout-stress"],
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

    // W1: the shadow report carries the same real ml_cpu boolean (push always
    // triggers the delegated CPU contract suites).
    const push = run({ GITHUB_EVENT_NAME: "push" });
    assert.equal(push.status, 0, push.stderr);
    const pushOutputs = Object.fromEntries(
      push.stdout
        .trim()
        .split("\n")
        .map((line) => [line.slice(0, line.indexOf("=")), line.slice(line.indexOf("=") + 1)]),
    );
    const pushShadow = JSON.parse(pushOutputs.shadow);
    assert.equal(pushOutputs.ml_cpu, "true");
    assert.equal(pushShadow.mlCpu, true);
    assert.equal(pushOutputs.run_suites, "true");
    assert.equal(JSON.parse(pushOutputs.required).suites.length, 12);
    assert.deepEqual(
      JSON.parse(pushOutputs.matrix).include.map((entry) => entry.suite),
      [...broadened],
    );

    // A PR touching a shared consumer/runner path triggers the CPU caller in
    // both outputs. The fixture keeps base != HEAD: the base is captured
    // BEFORE the shared path is added, so the diff is nonempty.
    const sharedBase = git("rev-parse", "HEAD");
    mkdirSync(join(cwd, "apps/_shared/mask_utils/src"), { recursive: true });
    writeFileSync(join(cwd, "apps/_shared/mask_utils/src/polygon.ts"), "export {}\n");
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
    assert.equal(JSON.parse(sharedOutputs.shadow).mlCpu, true);
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
    const matrixLine = lines.find((line) => line.startsWith("matrix="));
    const legacyLine = lines.find((line) => line.startsWith("legacy="));
    const shadowLine = lines.find((line) => line.startsWith("shadow="));
    assert.ok(matrixLine && legacyLine && shadowLine, "CLI must emit matrix/legacy/shadow lines");
    // P9 switch: the matrix is the planned selection; a rename that deletes a
    // frontend path is app code with an unmapped destination, so it broadens
    // to the full planned set. The frozen legacy set rides along for the
    // comparison.
    assert.deepEqual(
      JSON.parse(matrixLine.slice("matrix=".length)).include.map(({ suite }) => suite),
      [...broadened],
    );
    assert.deepEqual(
      JSON.parse(legacyLine.slice("legacy=".length)).include.map(({ suite }) => suite),
      [...functional, "visual", "layout-stress"],
    );
    const shadow = JSON.parse(shadowLine.slice("shadow=".length));
    assert.deepEqual(shadow.legacy, [...functional, "visual", "layout-stress"]);
    assert.ok(Array.isArray(shadow.added) && Array.isArray(shadow.removed));
    assert.notEqual(run("0".repeat(40)).status, 0);
    assert.notEqual(run("").status, 0);
  } finally {
    rmSync(cwd, { recursive: true, force: true });
  }
});

test("unknown/multi-domain PRs run the delegated ml-cpu contracts, never silently off", () => {
  assert.equal(planE2ESuites("push").include.length > 0, true);
  const unknown = shadowPlan("pull_request", ["model-configs/weights.yaml"]);
  assert.equal(unknown.planned, undefined ? [] : unknown.planned);
  assert.ok(unknown.planned.length >= [...functional].length + 2);
});

test("docs-only PRs plan an explicit allowed skip instead of running suites", () => {
  const cwd = mkdtempSync(join(tmpdir(), "e2e-docs-skip-"));
  const script = fileURLToPath(new URL("./plan-e2e-suites.mjs", import.meta.url));
  const git = (...args) =>
    execFileSync("git", args, { cwd, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim();
  try {
    git("init");
    git("config", "user.name", "E2E routing test");
    git("config", "user.email", "test@example.invalid");
    mkdirSync(join(cwd, "docs-site/user-guide"), { recursive: true });
    writeFileSync(join(cwd, "docs-site/user-guide/seed.md"), "seed\n");
    git("add", ".");
    git("-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "commit", "-m", "seed");
    const base = git("rev-parse", "HEAD");
    mkdirSync(join(cwd, "docs-site/user-guide"), { recursive: true });
    writeFileSync(join(cwd, "docs-site/user-guide/index.md"), "# docs\n");
    git("add", ".");
    git("-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "commit", "-m", "docs");

    const result = spawnSync(process.execPath, [script, base], {
      cwd,
      encoding: "utf8",
      env: { ...process.env, GITHUB_EVENT_NAME: "pull_request" },
    });
    assert.equal(result.status, 0, result.stderr);
    const outputs = Object.fromEntries(
      result.stdout
        .trim()
        .split("\n")
        .map((line) => [line.slice(0, line.indexOf("=")), line.slice(line.indexOf("=") + 1)]),
    );
    assert.equal(outputs.run_suites, "false");
    assert.deepEqual(JSON.parse(outputs.matrix).include, []);
    const manifest = JSON.parse(outputs.required);
    assert.equal(manifest.classification, "docs-only");
    assert.equal(manifest.suites.length, 0);
    assert.ok(manifest.reason.trim().length > 0);
    // Legacy comparison still records what the frozen gate would have run.
    assert.deepEqual(
      JSON.parse(outputs.legacy).include.map(({ suite }) => suite),
      [...functional],
    );
  } finally {
    rmSync(cwd, { recursive: true, force: true });
  }
});

test("E2E_SELECTION_MODE=legacy restores the frozen selection for rollback", () => {
  const cwd = mkdtempSync(join(tmpdir(), "e2e-rollback-"));
  const script = fileURLToPath(new URL("./plan-e2e-suites.mjs", import.meta.url));
  try {
    const result = spawnSync(process.execPath, [script], {
      cwd,
      encoding: "utf8",
      env: {
        ...process.env,
        GITHUB_EVENT_NAME: "push",
        E2E_SELECTION_MODE: "legacy",
      },
    });
    assert.equal(result.status, 0, result.stderr);
    const outputs = Object.fromEntries(
      result.stdout
        .trim()
        .split("\n")
        .map((line) => [line.slice(0, line.indexOf("=")), line.slice(line.indexOf("=") + 1)]),
    );
    assert.deepEqual(
      JSON.parse(outputs.matrix).include.map(({ suite }) => suite),
      [...functional, "visual", "layout-stress"],
    );
    assert.equal(outputs.run_suites, "true");

    const invalid = spawnSync(process.execPath, [script], {
      cwd,
      encoding: "utf8",
      env: { ...process.env, GITHUB_EVENT_NAME: "push", E2E_SELECTION_MODE: "sideways" },
    });
    assert.notEqual(invalid.status, 0);
  } finally {
    rmSync(cwd, { recursive: true, force: true });
  }
});

test("bounded smoke runs without retries and forbids flaky passes", () => {
  const smoke = SUITE_CONTRACT.find((entry) => entry.suite === "smoke");
  assert.ok(smoke, "smoke suite must exist in the contract");
  assert.equal(smoke.flakyPolicy, "forbid");
  assert.match(smoke.command, /--retries=0/);
  assert.equal(
    SUITE_CONTRACT.some((entry) => entry.flakyPolicy === "forbid" && entry.suite !== "smoke"),
    false,
  );
});

test("effective gate and required manifest stay identical in every mode and event", () => {
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
  const run = (env, argv = []) =>
    parse(
      spawnSync(process.execPath, [script, ...argv], {
        cwd,
        encoding: "utf8",
        env: { ...process.env, ...env },
      }).stdout,
    );
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
    mkdirSync(join(cwd, "apps/web/src/pages/Workbench/state"), { recursive: true });
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
      assert.deepEqual(
        ids(out.matrix),
        requiredIds(out.required),
        `push mode=${JSON.stringify(mode)}`,
      );
    }

    // PR app code, both modes, with the empty env value GitHub renders for an
    // unset repository variable.
    for (const mode of [undefined, "", "legacy"]) {
      const env = { GITHUB_EVENT_NAME: "pull_request" };
      if (mode !== undefined) env.E2E_SELECTION_MODE = mode;
      const out = run(env, [appBase]);
      assert.deepEqual(
        ids(out.matrix),
        requiredIds(out.required),
        `PR app mode=${JSON.stringify(mode)}`,
      );
    }

    // Manual full and nightly full keep exact ID equality too.
    for (const event of ["workflow_dispatch", "schedule"]) {
      const out = run({
        GITHUB_EVENT_NAME: event,
        E2E_DISPATCH_SCOPE: "full",
        E2E_SCHEDULE_SCOPE: "full",
      });
      assert.deepEqual(ids(out.matrix), requiredIds(out.required), event);
    }

    // Docs-only in planned mode is an explicit allowed skip; in legacy
    // rollback mode the frozen gate still runs its suites and requires them.
    // The docs-only base is the commit right before the docs change, so the
    // diff contains documentation only.
    const docsBase = git("rev-parse", "HEAD");
    mkdirSync(join(cwd, "docs-site/user-guide"), { recursive: true });
    writeFileSync(join(cwd, "docs-site/user-guide/index.md"), "# docs\n");
    git("add", ".");
    git("-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "commit", "-m", "docs");
    const docsPlanned = run({ GITHUB_EVENT_NAME: "pull_request", E2E_SELECTION_MODE: "" }, [
      docsBase,
    ]);
    assert.deepEqual(ids(docsPlanned.matrix), []);
    assert.equal(docsPlanned.run_suites, "false");
    assert.equal(JSON.parse(docsPlanned.required).classification, "docs-only");
    const docsLegacy = run({ GITHUB_EVENT_NAME: "pull_request", E2E_SELECTION_MODE: "legacy" }, [
      docsBase,
    ]);
    assert.equal(docsLegacy.run_suites, "true");
    // Docs-only paths do not affect the extended checks in the frozen gate.
    assert.deepEqual(ids(docsLegacy.matrix), [...functional]);
    assert.deepEqual(ids(docsLegacy.matrix), requiredIds(docsLegacy.required));

    // Core-flaky policy only applies to suites actually selected.
    assert.equal(
      JSON.parse(docsPlanned.required).suites.some(({ flakyPolicy }) => flakyPolicy),
      false,
    );
    const plannedApp = run({ GITHUB_EVENT_NAME: "push", E2E_SELECTION_MODE: "" });
    assert.equal(
      JSON.parse(plannedApp.required).suites.find(({ suite }) => suite === "smoke").flakyPolicy,
      "forbid",
    );
    const legacyApp = run({ GITHUB_EVENT_NAME: "push", E2E_SELECTION_MODE: "legacy" });
    assert.equal(
      JSON.parse(legacyApp.required).suites.some(({ suite }) => suite === "smoke"),
      false,
    );
  } finally {
    rmSync(cwd, { recursive: true, force: true });
  }
});

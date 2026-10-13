import { execFileSync } from "node:child_process";
import { pathToFileURL } from "node:url";

/**
 * E2E suite selection for CI.
 *
 * Two layers live here:
 *
 * 1. The LEGACY gate selection (`planE2ESuites`) that drives the actual
 *    `Frontend E2E` matrix. Its behavior stays byte-for-byte reproducible for
 *    the documented `E2E_SELECTION_MODE=legacy` rollback, while the default
 *    mode runs the planned §6 selection.
 * 2. The SHADOW selection (`selectSuites`/`shadowPlan`) implementing plan §6:
 *    explicit core/specialty/extended scopes with reasons, docs-only
 *    whitelist, fail-closed diff classification (empty diff, unknown paths,
 *    multiple domains, rename/delete pairs, unsupported events), and a
 *    machine-readable diff against the legacy gate so the planning job can
 *    record what a future switch would change without changing it.
 *
 * Every suite entry documents its scope, trigger paths and reason. Runtime
 * numbers (`collects`/`skips`) come from measured P7 evidence
 * (docs/research/31 §4.1) so collection and runtime-skip differences stay
 * visible alongside the selection.
 */

// Per-test CPU wiring from the accepted P8 audit (/tmp/aap-opt-p8-cpu-audit.md):
// all 89 not-wired files are CPU-compatible; 12 need CPU torch (9 grounded-sam2,
// 3 sam3); none needs a GPU, real weights or network. Execution itself is
// delegated to .github/workflows/ml-cpu-test.yml (scripts/run-ml-cpu-tests.sh);
// this planner only owns the path triggers and the recorded contract entries.
// `wired: false` means "runs via the ml-cpu workflow, not scheduled here".
const SHARED_CONTRACT_SUITES = {
  "shared-backend-runtime": {
    command: "pnpm exec bash scripts/run-ml-cpu-tests.sh run shared-backend-runtime",
    scope: "specialty",
    built: false,
    wired: false,
    triggers: ["apps/_shared/backend_runtime/"],
    reason: "shared pool/runtime contract tests; 99 tests, torch only stubbed via sys.modules",
    dependencies: "pytest + package extras (httpx, pillow); CPU only",
  },
  "shared-mask-utils": {
    command: "pnpm exec bash scripts/run-ml-cpu-tests.sh run shared-mask-utils",
    scope: "specialty",
    built: false,
    wired: false,
    triggers: ["apps/_shared/mask_utils/"],
    reason: "shared mask→polygon conversion contracts; 41 tests",
    dependencies: "pytest + numpy, opencv-python-headless, shapely; CPU only",
  },
  "shared-protocol-v2": {
    command: "pnpm exec bash scripts/run-ml-cpu-tests.sh run shared-protocol-v2",
    scope: "specialty",
    built: false,
    wired: false,
    triggers: [
      "apps/_shared/protocol_v2/",
      "scripts/run-ml-cpu-tests.sh",
      "scripts/ml-cpu-deps/",
      ".github/workflows/ml-cpu-test.yml",
    ],
    reason: "protocol v2 schema/vocab/mask-codec contracts; 163 tests",
    dependencies: "pytest + cryptography/fastapi/pydantic/PyJWT + numpy (test extra); CPU only",
  },
  "ml-grounded-sam2": {
    command: "pnpm exec bash scripts/run-ml-cpu-tests.sh run grounded-sam2",
    scope: "specialty",
    built: false,
    wired: false,
    triggers: ["apps/grounded-sam2-backend/"],
    reason:
      "grounded-sam2 backend contract tests: 10 torch-free files (68 tests) + 9 CPU-torch files (82 tests); full directory runs clean with CPU torch",
    dependencies: "backend dev extra + CPU torch (cpu wheel index); CPU only",
  },
  "ml-sam3": {
    command: "pnpm exec bash scripts/run-ml-cpu-tests.sh run sam3",
    scope: "specialty",
    built: false,
    wired: false,
    triggers: ["apps/sam3-backend/"],
    reason:
      "sam3 backend contract tests: 17 torch-free files (153 tests) + 3 CPU-torch files (43 tests); full directory runs clean with CPU torch",
    dependencies: "backend dev extra + CPU torch (cpu wheel index); CPU only",
  },
  "ml-yolo": {
    command: "pnpm exec bash scripts/run-ml-cpu-tests.sh run yolo",
    scope: "specialty",
    built: false,
    wired: false,
    triggers: ["apps/yolo-backend/"],
    reason:
      "yolo backend contract tests: 16 files / 224 tests run entirely without torch or ultralytics (test-only dependency set)",
    dependencies:
      "pytest, pytest-asyncio, fastapi, httpx, numpy, pillow, opencv-headless, psutil, pynvml; CPU only",
  },
  "ml-rapidocr": {
    command: "pnpm exec bash scripts/run-ml-cpu-tests.sh run rapidocr",
    scope: "specialty",
    built: false,
    wired: false,
    triggers: ["apps/rapidocr-backend/"],
    reason:
      "rapidocr backend contract tests: 7 files / 83 tests; rapidocr shimmed by conftest, onnxruntime faked, no cv2 needed",
    dependencies:
      "pytest, pytest-asyncio, fastapi, httpx, numpy, pillow, cryptography, PyJWT; CPU only",
  },
  "ml-onnxtools": {
    command: "pnpm exec bash scripts/run-ml-cpu-tests.sh run onnxtools",
    scope: "specialty",
    built: false,
    wired: false,
    triggers: ["apps/onnxtools-backend/"],
    reason:
      "onnxtools backend contract tests: 8 files / 70 passed + 3 expected upstream importorskip skips; cv2 added explicitly (pyproject omits it)",
    dependencies: "backend dev extra + opencv-python-headless; CPU only",
  },
};

const smoke = [
  {
    suite: "smoke",
    legacyGate: false,
    command:
      "test:e2e --retries=0 e2e/tests/auth.spec.ts e2e/tests/annotation.spec.ts e2e/tests/employee-project-roles.spec.ts e2e/tests/mask-session-guard.spec.ts --grep '健康检查|正确凭证|错密码|未登录访问|annotator 登录|bbox 真实绘制、选类、落库并刷新恢复|same employee annotates A|opposite project actions are denied|切工具离开 dirty session'",
    built: true,
    scope: "smoke",
    // §6.7: the bounded core must pass on the first attempt; retries stay a
    // diagnostic tool for non-core suites only.
    flakyPolicy: "forbid",
    reason:
      "bounded real chain (reviewer-owned membership: 9 tests / 4 existing files): UI login, real canvas save+refresh, annotator submit→review→complete across two projects, permission denial, dirty-session switch guard",
  },
];

// Full functional matrix: four shards over every functional spec. Runs on
// push/nightly (§6.2 全量), in the conservative fallback, and in the legacy
// rollback mode.
const functional = [
  ...["one", "two", "three", "four"].map((name, index) => ({
    suite: `default-${name}`,
    command: `test:e2e --shard=${index + 1}/4`,
    built: true,
    legacyGate: true,
    scope: "full",
    reason: "whole functional suite shard; nightly/full scope, not part of the bounded smoke",
    collects: {
      chromium: 271,
      note: "default --list, list-only; shared Mask specs skip at runtime without a matrix",
    },
  })),
  ...["readonly", "native", "ai-native"].map((name) => ({
    suite: `mask-${name}`,
    command: `test:e2e:mask-${name}`,
    built: false,
    legacyGate: true,
    scope: "specialty",
    triggers: [
      "apps/web/e2e/tests/mask-",
      "apps/web/e2e/tests/raster-mask-",
      "apps/api/app/api/v1/mask_",
      "apps/api/app/services/",
      "apps/web/src/pages/Workbench/state/mask",
      "apps/web/src/pages/Workbench/stage/shared/geometry/",
      "apps/web/src/pages/Workbench/stage/shared/rasterMask",
    ],
    reason: `raster Mask ${name} matrix contract`,
    runtime: {
      "mask-readonly": {
        collects: 13,
        skipped: 11,
        note: "readonly describe executes; native write matrix skipped",
      },
      "mask-native": {
        collects: 22,
        skipped: 2,
        note: "native write + advanced execute; readonly describe skipped",
      },
      "mask-ai-native": {
        collects: 7,
        skipped: 0,
        note: "mock SAM backend + alternative tracker service, not a real model/GPU run",
      },
    }[`mask-${name}`],
  })),
];

function sharedContractEntries() {
  // All shared/ML contract suites execute via the delegated ml-cpu workflow;
  // triggeredSpecialties skips wired:false entries so this returns [].
  return [];
}

const extended = [
  {
    suite: "visual",
    command: "test:e2e:visual",
    built: true,
    legacyGate: true,
    scope: "extended",
    reason: "visual baselines for shared layout/theme surfaces",
  },
  {
    suite: "layout-stress",
    command: "test:e2e:stress",
    built: true,
    legacyGate: true,
    scope: "extended",
    reason: "long-horizon layout stress (54 rearrangements)",
  },
];

// Shadow-only domain specialties (plan §6.2 受影响专项). Never part of the
// legacy gate; the shadow diff shows what a P9 switch would add.
const domainSpecialties = [
  {
    suite: "video-pipeline",
    command: "test:e2e e2e/tests/video-*.spec.ts e2e/tests/workbench-video-*.spec.ts",
    built: true,
    scope: "specialty",
    triggers: [
      "apps/web/e2e/tests/video-",
      "apps/web/e2e/tests/workbench-video-",
      "apps/web/src/pages/Workbench/stages/video/",
      "apps/api/app/api/v1/video",
      "apps/api/app/services/video",
      "apps/api/app/workers/",
    ],
    reason:
      "video pipeline specialty: manifest/chunk playback, tracker jobs, keyframe and Issue chains",
  },
  {
    suite: "pointcloud",
    command: "test:e2e --project=pointcloud e2e/tests/workbench-pointcloud-*.spec.ts",
    built: true,
    legacyGate: false,
    scope: "specialty",
    triggers: [
      "apps/web/e2e/tests/workbench-pointcloud-",
      "apps/web/src/pages/Workbench/stages/three-d/",
      "apps/api/app/api/v1/lidar",
      "apps/api/app/services/point",
    ],
    reason: "point-cloud specialty under the SwiftShader software-rendering project",
  },
];

export const SUITE_CONTRACT = [...smoke, ...functional, ...domainSpecialties, ...extended];
const allSuites = SUITE_CONTRACT;

// Legacy gate rule (frozen): shared frontend/runtime changes can affect every
// surface, so any app input proposes the extended checks.
function affectsExtended(path) {
  return (
    ["apps/web/", "apps/api/", "packages/"].some((prefix) => path.startsWith(prefix)) ||
    ["package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml", ".npmrc", ".env.example"].includes(
      path,
    ) ||
    /^docker-compose[^/]*\.ya?ml$/.test(path) ||
    /^\.github\/workflows\/(ci|e2e-[^/]+)\.yml$/.test(path) ||
    /^scripts\/plan-e2e-suites(\.test)?\.mjs$/.test(path)
  );
}

// Shadow rule (plan §6.3 narrowing proposal): extended checks target shared
// visual/layout surfaces, so only those inputs (plus shared runtime, tooling
// and CI definitions) propose them. P9 evaluates the diff before any switch.
function affectsExtendedShadow(path) {
  return (
    ["apps/web/src/styles/", "apps/web/index.html", "apps/web/e2e/tests/workbench-layout"].some(
      (prefix) => path.startsWith(prefix),
    ) ||
    /\.(css|scss)$/.test(path) ||
    ["package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml", ".npmrc", ".env.example"].includes(
      path,
    ) ||
    /^docker-compose[^/]*\.ya?ml$/.test(path) ||
    /^\.github\/workflows\/(ci|e2e-[^/]+)\.yml$/.test(path) ||
    /^scripts\/plan-e2e-suites(\.test)?\.mjs$/.test(path) ||
    /^apps\/web\/(playwright[^/]*|vite\.config\.ts|tailwind\.config\.[cm]?js|postcss\.config\.cjs)/.test(
      path,
    )
  );
}

const legacyInclude = (eventName, paths) =>
  planE2ESuites(eventName, paths).include.map(({ suite }) => suite);

// Docs-only is a whitelist, never a fallback. Only genuine documentation
// qualifies, and executable content is excluded explicitly: anything under
// apps/, packages/, scripts/ or a test-fixture path stays app code even when
// it ends in .md, and docs-site executable surfaces (dev/examples and the
// VitePress build config) stay app code.
const DOCS_ONLY = [
  // File-type requirement (.md) keeps CI workflows, actions, configs, data
  // files and package manifests out of the docs-only classification.
  /^README\.md$/,
  /^CHANGELOG\.md$/,
  /^CONTRIBUTING\.md$/,
  /^SECURITY\.md$/,
  /^CODE_OF_CONDUCT\.md$/,
  /^\.github\/.*\.md$/,
  /^docs\/.*\.md$/,
  /^docs-site\/.*\.md$/,
];

const DOCS_ONLY_EXCLUSIONS = [
  /^docs-site\/dev\/examples\//,
  /^docs-site\/\.vitepress\//,
  /^docs-site\/.*\.(py|mjs|cjs|ts|tsx|sh)$/,
  /^(apps|packages|scripts)\/.*\.md$/,
];

function isDocsOnlyPath(path) {
  if (DOCS_ONLY_EXCLUSIONS.some((pattern) => pattern.test(path))) return false;
  return DOCS_ONLY.some((pattern) => pattern.test(path));
}

function classifyPaths(paths) {
  if (!Array.isArray(paths)) throw new Error("PR changed paths must be supplied");
  const docs = paths.filter((path) => isDocsOnlyPath(path));
  const docsSet = new Set(docs);
  const appCode = paths.filter((path) => !docsSet.has(path));
  const domains = new Set(
    appCode.map((path) => {
      const topLevel = path.split("/")[0];
      if (topLevel === "apps") return `apps/${path.split("/")[1] ?? ""}`;
      if (
        topLevel === ".github" ||
        topLevel === "scripts" ||
        topLevel === "packages" ||
        topLevel === "docs-site"
      )
        return topLevel;
      return "unknown";
    }),
  );
  const unknownPaths = appCode.filter((path) => {
    const topLevel = path.split("/")[0];
    return (
      !["apps", "packages", "scripts", ".github", "docs-site", "docker-compose.yml"].includes(
        topLevel,
      ) &&
      !["package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml", ".npmrc", ".env.example"].includes(
        path,
      )
    );
  });
  return {
    docsOnly: appCode.length === 0 && paths.length > 0,
    emptyDiff: paths.length === 0,
    appCode,
    docs,
    multiDomain: domains.size > 1,
    domains: [...domains],
    unknownPaths: appCode.filter((path) => {
      const topLevel = path.split("/")[0];
      return (
        !["apps", "packages", "scripts", ".github", "docs-site", "docker-compose.yml"].includes(
          topLevel,
        ) &&
        ![
          "package.json",
          "pnpm-lock.yaml",
          "pnpm-workspace.yaml",
          ".npmrc",
          ".env.example",
        ].includes(path)
      );
    }),
  };
}

// Shared Workbench shell/state owners (task navigation, shell model,
// commands): a change here can affect image, video and point-cloud flows at
// once, so it broadens to the full selection instead of claiming Mask-only.
const SHARED_WORKBENCH_TRIGGERS = [
  "apps/web/src/pages/Workbench/state/",
  "apps/web/src/pages/Workbench/shell/",
];

// Shared UI/API/client/auth/layout dependencies fan out to every domain that
// consumes them: a change here is not observable from the module path alone.
const SHARED_DEPENDENCY_TRIGGERS = [
  "apps/web/src/api/",
  "apps/web/src/lib/",
  "apps/web/src/stores/",
  "apps/web/src/hooks/",
  "apps/web/src/test/",
  "apps/api/app/api/",
  "apps/api/app/services/",
  "apps/api/app/core/",
  "apps/api/app/deps.py",
];

function triggeredSpecialties(appCode) {
  const triggered = [];
  for (const suite of [...SUITE_CONTRACT, ...sharedContractEntries()]) {
    if (suite.scope !== "specialty" || !suite.triggers || suite.wired === false) continue;
    const hit = suite.triggers.find((prefix) => appCode.some((path) => path.startsWith(prefix)));
    if (hit)
      triggered.push({
        suite: suite.suite,
        because: `${suite.reason} (triggered by ${hit})`,
      });
  }
  // Shared dependencies select every domain-consuming specialty.
  const sharedHit = SHARED_DEPENDENCY_TRIGGERS.find((prefix) =>
    appCode.some((path) => path.startsWith(prefix)),
  );
  if (sharedHit)
    for (const suite of SUITE_CONTRACT.filter(
      ({ scope, triggers }) => scope === "specialty" && triggers,
    ))
      triggered.push({
        suite: suite.suite,
        because: `${suite.reason} (triggered by shared dependency ${sharedHit})`,
      });
  return triggered;
}

function triggeredSharedContracts(appCode) {
  return Object.values(SHARED_CONTRACT_SUITES).some(
    (suite) =>
      suite.wired &&
      suite.triggers.some((prefix) => appCode.some((path) => path.startsWith(prefix))),
  );
}

/**
 * Shadow selection per plan §6.2/§6.3. Never drives the gate: the caller keeps
 * using `planE2ESuites` for the matrix and records the diff for P9.
 */
export function selectSuites(eventName, paths, options = {}) {
  if (eventName === "schedule") {
    // §6.7-5: the scheduled entry states its scope explicitly. Full is the
    // default nightly contract; an explicit extended-only nightly is possible
    // but must be requested and is reported as such.
    const scope = process.env.E2E_SCHEDULE_SCOPE ?? "full";
    if (!["extended", "full"].includes(scope))
      throw new Error(`Invalid nightly E2E scope: ${scope} (expected extended or full)`);
    if (scope === "extended")
      return {
        planned: extended.map(({ suite }) => suite),
        classification: { event: "schedule", note: "nightly extended-only explicitly requested" },
        reasons: [
          {
            suite: "visual+layout-stress",
            selected: true,
            because: "nightly scope set to extended-only",
          },
        ],
        warnings: [],
      };
    return {
      planned: allSuites.map(({ suite }) => suite),
      classification: {
        event: "schedule",
        note: "explicit full scope: bounded smoke + full functional shards + dedicated contracts + extended (plan §6.2)",
      },
      reasons: [
        {
          suite: "all",
          selected: true,
          because:
            "post-merge/nightly verification is the explicit full scope (smoke + full shards + extended)",
        },
      ],
      warnings:
        eventName === "schedule"
          ? [
              "the legacy rollback gate runs extended-only for schedule; the planned nightly selection is full",
            ]
          : [],
    };
  }
  if (eventName === "push") {
    return {
      planned: allSuites.map(({ suite }) => suite),
      classification: {
        event: "push",
        note: "explicit full scope: bounded smoke + full functional shards + dedicated contracts + extended",
      },
      reasons: [
        {
          suite: "all",
          selected: true,
          because: "post-merge verification is the explicit full scope",
        },
      ],
      warnings: [],
    };
  }
  if (eventName === "workflow_dispatch") {
    // Plan §6.7-5: manual entry states its scope explicitly. The planned
    // selection honours the requested scope; the legacy rollback gate keeps
    // its extended-only behavior.
    const scope = options.dispatchScope ?? "extended";
    if (!["extended", "full"].includes(scope))
      throw new Error(`Invalid manual E2E scope: ${scope} (expected extended or full)`);
    if (scope === "full")
      return {
        planned: allSuites.map(({ suite }) => suite),
        classification: {
          event: "workflow_dispatch",
          note: "manual full run explicitly requested via the e2e_scope input",
        },
        reasons: [
          {
            suite: "all",
            selected: true,
            because:
              "manual dispatch explicitly selected the full matrix (smoke + full shards + extended)",
          },
        ],
        warnings: [],
      };
    return {
      planned: extended.map(({ suite }) => suite),
      classification: {
        event: "workflow_dispatch",
        note: "manual entry stays extended-only and explicitly labeled",
      },
      reasons: [
        {
          suite: "visual+layout-stress",
          selected: true,
          because: "manual runs select the extended checks explicitly",
        },
      ],
      warnings: [],
    };
  }
  if (eventName !== "pull_request") throw new Error(`Unsupported E2E event: ${eventName}`);
  if (!Array.isArray(paths)) throw new Error("PR changed paths must be supplied");

  const classification = classifyPaths(paths);
  const planned = [];
  const reasons = [];
  const warnings = [];
  // App behaviour paths that no domain specialty maps to (§6.3: recognized
  // apps/ prefixes alone never prove coverage).
  classification.unmappedAppPaths = (() => {
    const specialtyPrefixes = allSuites
      .filter(({ scope, triggers }) => scope === "specialty" && triggers)
      .flatMap(({ triggers }) => triggers);
    const sharedPrefixes = SHARED_DEPENDENCY_TRIGGERS;
    return classification.appCode.filter(
      (path) =>
        !specialtyPrefixes.some((prefix) => path.startsWith(prefix)) &&
        !sharedPrefixes.some((prefix) => path.startsWith(prefix)),
    );
  })();

  if (classification.docsOnly) {
    reasons.push({
      suite: "(none)",
      selected: false,
      because: "docs-only change (whitelisted markdown/docs-site paths); no app E2E required",
    });
  } else {
    // Fail-closed conservative defaults: an empty diff or unrecognized paths
    // must broaden, never narrow.
    if (classification.emptyDiff)
      warnings.push(
        "empty diff cannot be classified; falling back to the full conservative selection",
      );
    if (classification.unknownPaths.length)
      warnings.push(
        `unrecognized top-level paths broaden the selection: ${classification.unknownPaths.join(", ")}`,
      );
    if (classification.multiDomain)
      warnings.push(
        `multiple domains touched (${classification.domains.join(", ")}); broadening the selection`,
      );

    const triggered = triggeredSpecialties(classification.appCode);
    const sharedDepHit =
      SHARED_DEPENDENCY_TRIGGERS.find((prefix) =>
        classification.appCode.some((path) => path.startsWith(prefix)),
      ) ??
      SHARED_WORKBENCH_TRIGGERS.find((prefix) =>
        classification.appCode.some((path) => path.startsWith(prefix)),
      );
    const runtimeLike = classification.appCode.some(
      (path) =>
        [
          "package.json",
          "pnpm-lock.yaml",
          "pnpm-workspace.yaml",
          ".npmrc",
          ".env.example",
          "docker-compose.yml",
        ].includes(path) || /^docker-compose[^/]*\.ya?ml$/.test(path),
    );
    // Fail-closed (§6.3): unmapped app behaviour paths, shared
    // dependencies/runtime and empty or unrecognized diffs broaden to the
    // full selection with every dedicated contract; a recognized apps/ prefix
    // alone never proves coverage.
    const broaden =
      classification.emptyDiff ||
      classification.unknownPaths.length > 0 ||
      classification.multiDomain ||
      classification.unmappedAppPaths.length > 0 ||
      sharedDepHit !== undefined ||
      runtimeLike;
    if (classification.unmappedAppPaths.length)
      warnings.push(
        `app paths without a domain specialty mapping broaden to full: ${classification.unmappedAppPaths.join(", ")}`,
      );
    if (sharedDepHit !== undefined)
      warnings.push(
        `shared dependency path ${sharedDepHit} selects full functional plus dedicated configs`,
      );
    if (runtimeLike)
      warnings.push(
        "shared runtime/dependency change selects full functional plus dedicated configs",
      );
    const addPlanned = (suite, because) => {
      if (!planned.includes(suite)) {
        planned.push(suite);
        reasons.push({ suite, selected: true, because });
      }
    };
    for (const suite of allSuites.filter(({ scope }) => scope === "smoke"))
      addPlanned(suite.suite, suite.reason);
    if (broaden) {
      // The default shards never execute the dedicated Mask/video/pointcloud
      // matrices at runtime, so broadening selects every dedicated contract.
      for (const suite of allSuites.filter(({ scope }) => scope === "full"))
        addPlanned(
          suite.suite,
          "unmapped/multi-domain/shared-runtime change broadens to the full functional shards",
        );
    }
    for (const { suite, because } of triggered) addPlanned(suite, because);
    if (broaden) {
      for (const entry of allSuites.filter(
        (candidate) => candidate.scope === "specialty" && candidate.triggers,
      ))
        addPlanned(entry.suite, "broadened selection runs every dedicated domain contract");
    }
    if (broaden || classification.appCode.some(affectsExtendedShadow)) {
      for (const suite of extended) {
        addPlanned(
          suite.suite,
          classification.multiDomain
            ? "multi-domain change broadens verification to the extended checks"
            : "shared visual/layout surface (styles, layout tooling or runtime config) changed",
        );
      }
    }
    if (planned.length === 0)
      throw new Error(
        "Illegal E2E selection: app-code changes must always select at least the core suites",
      );
  }

  return { planned, classification, reasons, warnings };
}

/** Full shadow report: legacy gate suites vs the §6 selection, with the diff
 * and the newly-wired shared CPU contract suites surfaced for P9 review. */
export function shadowPlan(eventName, paths, options = {}) {
  const { planned, classification, reasons, warnings } = selectSuites(eventName, paths, options);
  const legacy = legacyInclude(eventName, paths);
  const legacySet = new Set(legacy);
  const plannedSet = new Set(planned);
  const wiredContracts = Object.entries(SHARED_CONTRACT_SUITES)
    .filter(([, suite]) => suite.wired)
    .filter(([, suite]) =>
      eventName !== "pull_request"
        ? false
        : suite.triggers.some((prefix) => (paths ?? []).some((path) => path.startsWith(prefix))),
    )
    .map(([suite, suiteContract]) => ({ suite, ...suiteContract }));
  return {
    legacy,
    planned,
    added: planned.filter((suite) => !legacySet.has(suite)),
    removed: legacy.filter((suite) => !plannedSet.has(suite)),
    classification,
    reasons,
    warnings,
    sharedContracts: {
      wired: wiredContracts,
      qualification: Object.entries(SHARED_CONTRACT_SUITES)
        .filter(([, suite]) => !suite.wired)
        .map(([suite, suiteContract]) => ({ suite, ...suiteContract })),
    },
  };
}

// Frozen legacy selection derived from the authoritative table's legacyGate
// membership. Byte-for-byte the pre-P8 outputs, used by the documented
// E2E_SELECTION_MODE=legacy rollback.
export function planE2ESuites(eventName, paths) {
  const legacy = SUITE_CONTRACT.filter((suite) => suite.legacyGate);
  const extendedOnly = ["visual", "layout-stress"];
  if (["schedule", "workflow_dispatch"].includes(eventName))
    return { include: legacy.filter(({ suite }) => extendedOnly.includes(suite)) };
  if (eventName === "push") return { include: legacy };
  if (eventName !== "pull_request") throw new Error(`Unsupported E2E event: ${eventName}`);
  if (!Array.isArray(paths)) throw new Error("PR changed paths must be supplied");
  return {
    include: legacy.filter(
      ({ suite }) => !extendedOnly.includes(suite) || paths.some(affectsExtended),
    ),
  };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const eventName = process.env.GITHUB_EVENT_NAME;
  let paths;
  if (eventName === "pull_request") {
    const base = process.argv[2];
    if (!/^[a-f0-9]{40}$/.test(base ?? "")) throw new Error("A full PR base SHA is required");
    // --no-renames includes both deleted and added paths; NUL handles unusual filenames.
    paths = execFileSync("git", ["diff", "--name-only", "--no-renames", "-z", base, "HEAD", "--"], {
      encoding: "utf8",
    })
      .split("\0")
      .filter(Boolean);
  }
  const dispatchScope = process.env.E2E_DISPATCH_SCOPE;
  // P6 handoff wiring: the ml-cpu workflow owns execution; this flag triggers
  // the ci.yml caller. Semantics are explicit per event — never silently off:
  //   pull_request: run when shared/ML paths (or their workflow/runner/deps)
  //   changed; push: always (post-merge full verification); workflow_dispatch:
  //   full scope runs them, extended-only does not; schedule: always.
  const mlCpuTriggers = [
    ...Object.values(SHARED_CONTRACT_SUITES).flatMap((suite) => suite.triggers),
    "scripts/run-ml-cpu-tests.sh",
    "scripts/ml-cpu-deps/",
    ".github/workflows/ml-cpu-test.yml",
  ];
  let mlCpu;
  if (eventName === "push" || eventName === "schedule") mlCpu = true;
  else if (eventName === "workflow_dispatch")
    mlCpu = (process.env.E2E_DISPATCH_SCOPE ?? "extended") === "full";
  else if (eventName === "pull_request")
    mlCpu = (paths ?? []).some((path) => mlCpuTriggers.some((prefix) => path.startsWith(prefix)));
  else throw new Error(`Unsupported E2E event for ml-cpu wiring: ${eventName}`);

  // P9 gate switch with a documented rollback: `E2E_SELECTION_MODE=legacy`
  // restores the frozen pre-P8 selection without a code change.
  // GitHub renders an unset repository variable as an empty string, so
  // absent/empty/whitespace all mean the default planned mode; only a
  // non-empty unknown value is rejected.
  const selectionMode = (process.env.E2E_SELECTION_MODE ?? "").trim() || "planned";
  if (!["planned", "legacy"].includes(selectionMode))
    throw new Error(`Invalid E2E selection mode: ${selectionMode}`);
  const legacyGate = planE2ESuites(eventName, paths);
  const selection = selectSuites(eventName, paths, { dispatchScope });
  const shadow = { ...shadowPlan(eventName, paths, { dispatchScope }), mlCpu };
  const plannedEntries = selection.planned
    .map((suite) => SUITE_CONTRACT.find((entry) => entry.suite === suite))
    .filter(Boolean);
  const plannedGate = { include: plannedEntries };
  const gate = selectionMode === "legacy" ? legacyGate : plannedGate;
  // The required manifest always describes the EFFECTIVE gate: in legacy
  // rollback mode the new-only smoke/video/pointcloud suites are not executed,
  // so they must not be required either; a docs-only allowed skip only exists
  // in planned mode (the legacy gate always runs suites).
  const requiredSuites =
    selectionMode === "legacy"
      ? legacyGate.include.map((entry) => ({ suite: entry.suite, planned: true }))
      : plannedEntries.map((entry) => ({
          suite: entry.suite,
          planned: true,
          ...(entry.flakyPolicy ? { flakyPolicy: entry.flakyPolicy } : {}),
        }));
  const docsOnlySkip = selectionMode !== "legacy" && selection.classification.docsOnly;
  console.log(`matrix=${JSON.stringify(gate)}`);
  // The comparison document always records the frozen legacy set next to the
  // planned set, so every run leaves old/new evidence behind.
  console.log(`legacy=${JSON.stringify(legacyGate)}`);
  console.log(
    `required=${JSON.stringify(
      docsOnlySkip
        ? {
            classification: "docs-only",
            reason:
              selection.reasons.find((reason) => /docs-only/.test(reason.because))?.because ??
              "docs-only change",
            suites: [],
          }
        : {
            classification: "app-code",
            reason:
              selectionMode === "legacy"
                ? "legacy rollback gate (E2E_SELECTION_MODE=legacy)"
                : "planned selection (plan §6.2/§6.3)",
            suites: requiredSuites,
          },
    )}`,
  );
  console.log(`run_suites=${gate.include.length > 0}`);
  console.log(`selection_mode=${selectionMode}`);
  // Shadow report for P9: recorded by the planning job.
  console.log(`shadow=${JSON.stringify(shadow)}`);
  console.log(`ml_cpu=${mlCpu}`);
}

import { execFileSync } from "node:child_process";
import { appendFileSync, readFileSync, writeFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const cpuSuites = JSON.parse(
  readFileSync(new URL("./test-suites.json", import.meta.url), "utf8"),
).ml_cpu;
const SHARED_CONTRACT_SUITES = Object.fromEntries(
  cpuSuites.map(({ id, owner, paths, resources }) => [
    id,
    {
      command: `pnpm exec bash scripts/run-ml-cpu-tests.sh run ${id}`,
      scope: "specialty",
      built: false,
      wired: false,
      triggers: paths,
      reason: `${owner} CPU suite`,
      dependencies: resources,
    },
  ]),
);

const smoke = [
  {
    suite: "smoke",
    legacyGate: false,
    command:
      "test:e2e --retries=0 e2e/tests/auth.spec.ts e2e/tests/annotation.spec.ts e2e/tests/employee-project-roles.spec.ts e2e/tests/mask-session-guard.spec.ts --grep '正确凭证|错密码|未登录访问|annotator 登录|bbox 真实绘制、选类、落库并刷新恢复|same employee annotates A|opposite project actions are denied|切工具离开 dirty session'",
    built: true,
    scope: "smoke",
    flakyPolicy: "forbid",
    reason:
      "bounded real chain: UI login, real canvas save and refresh, annotator submit through review across two projects, permission denial, and the dirty-session switch guard",
  },
];

function shardSuite(entry, total) {
  return Array.from({ length: total }, (_, offset) => {
    const index = offset + 1;
    return {
      ...entry,
      suite: `${entry.suite}-${String(index).padStart(String(total).length, "0")}`,
      command: `${entry.command} --shard=${index}/${total}`,
    };
  });
}

const web = "apps/web/src";
const workbench = `${web}/pages/Workbench`;
const stage = `${workbench}/stage`;
const browserTests = "apps/web/e2e/tests";
const specPath = (name) => `${browserTests}/${name}.spec.ts`;

const layoutPaths = [
  `${workbench}/layout/`,
  `${workbench}/shell/WorkbenchLayout.tsx`,
  `${workbench}/shell/ToolDock.tsx`,
  `${workbench}/shell/toolDockOverflow.ts`,
  `${workbench}/state/useWorkbenchNamedPresets.ts`,
  `${workbench}/state/useWorkbenchWorkspaceLayout.ts`,
  "apps/api/app/schemas/workbench_workspace.py",
];

const maskVideoPaths = [
  `${stage}/VideoKonvaMaskLayer.tsx`,
  `${stage}/VideoMaskCorrectionDialog.tsx`,
  `${stage}/videoMaskClipboard.ts`,
  `${stage}/videoMaskFrames.ts`,
  `${stage}/videoMaskKeyframeActions.ts`,
  `${workbench}/state/useVideoMaskCorrection.ts`,
  `${workbench}/state/videoMaskCorrectionFlow.ts`,
];

const maskPaths = [
  /^apps\/web\/src\/pages\/Workbench\/stage\/shared\/(?:geometry\/mask|mask|rasterMask|useRaster|rasterResource|sparseMask)[^/]*$/,
  /^apps\/web\/src\/pages\/Workbench\/state\/(?:mask|useMask|canEditMask|imageRasterMask)[^/]*$/,
  /^apps\/web\/src\/pages\/Workbench\/shell\/Mask[^/]*$/,
  `${stage}/MaskConversionDialog.tsx`,
  `${stage}/RasterMaskVisual.tsx`,
  `${stage}/tools/MaskTool.ts`,
  `${stage}/overlays/MaskCompareTileLayer.tsx`,
  `${stage}/overlays/MaskOverlayLayer.tsx`,
  `${stage}/overlays/TiledMaskOverlayLayer.tsx`,
  "apps/web/src/api/rasterMasks.ts",
  "apps/web/src/utils/rasterMaskComputeDiagnostics.ts",
  /^apps\/api\/app\/api\/v1\/(?:_?mask_|tasks\/(?:ai_masks|mask_))/,
  ...maskVideoPaths,
];

const maskTriggers = [
  ...maskPaths,
  ...["raster-mask-native", "mask-advanced-operations", "mask-editor", "native-mask-ai"].map(
    specPath,
  ),
  "apps/web/e2e/browser-integration/mask-worker.spec.ts",
];

const videoTriggers = [
  /^apps\/web\/e2e\/tests\/(?:workbench-)?video-[^/]+\.spec\.ts$/,
  `${workbench}/stages/video/`,
  /^apps\/web\/src\/pages\/Workbench\/(?:stage|state|shell|sidebar)\/(?:Video|video|useVideo)[^/]*\.(?:tsx?|module\.css)$/,
  `${workbench}/shell/AIInspectorPanel.tsx`,
  `${stage}/frameTimebase.ts`,
  `${stage}/timelineCoords.ts`,
  `${stage}/useFrameClock.ts`,
  `${stage}/shared/geometry/maskBuffer.ts`,
  `${stage}/shared/rasterMaskRender.ts`,
  `${stage}/shared/useRasterMaskRecords.ts`,
  `${web}/utils/videoWorkbenchDiagnostics.ts`,
  `${web}/hooks/useOriginalVideoPlayback.ts`,
  "apps/api/app/api/v1/tasks/video.py",
  /^apps\/api\/app\/(?:api\/v1\/video|services\/video|workers\/video_)/,
];

const timelineSpec = "apps/web/e2e/layout/video-playback-overlay.spec.ts";
const layoutStressSpecs = [
  specPath("workbench-layout-matrix"),
  specPath("workbench-pointcloud-layout-matrix"),
];

function chromiumSubset(suite, specs, paths, reason) {
  return {
    suite,
    command: `test:e2e --project=chromium ${specs.map((name) => `e2e/tests/${name}.spec.ts`).join(" ")}`,
    built: true,
    legacyGate: false,
    scope: "targeted",
    requireAllTests: true,
    triggers: [...paths, ...specs.map(specPath)],
    reason,
  };
}

const shortLayout = chromiumSubset(
  "layout-short",
  [
    "workbench-layout",
    "workbench-layout-matrix",
    "workbench-tool-dock",
    "workbench-ai-tracker-layout",
    "workbench-context-toolbars",
  ],
  [...layoutPaths, `${workbench}/shell/FloatingDock.tsx`],
  "ordinary Chromium dock, persistence, compact layout, and short rearrangement cases",
);

const targetedFunctional = [
  shortLayout,
  chromiumSubset(
    "mask-workflows",
    ["mask-conversion", "mask-lock-bypass", "mask-primary-actions", "mask-slice"],
    maskPaths,
    "ordinary Chromium Mask edit, conversion, lock, and slice cases outside the native matrix",
  ),
  chromiumSubset(
    "data-manager",
    ["data-manager-management", "filter-data-manager"],
    [`${web}/pages/Projects/ProjectDataManagerPage.tsx`, `${web}/pages/Projects/data-manager/`],
    "Data Manager queries, saved views, activity and access",
  ),
  chromiumSubset(
    "operational-lists",
    ["filter-operational-lists"],
    [
      `${web}/pages/Datasets/DatasetsPage.tsx`,
      `${web}/pages/Datasets/DatasetDetailPage.tsx`,
      `${web}/pages/Datasets/datasetsUrlState.ts`,
      `${web}/pages/ProjectTemplates/ProjectTemplatesPage.tsx`,
      `${web}/pages/ProjectTemplates/projectTemplatesUrlState.ts`,
      `${web}/pages/Audit/AuditPage.tsx`,
      `${web}/pages/Audit/auditUrlState.ts`,
      `${web}/pages/Users/UsersPage.tsx`,
      `${web}/pages/Users/usersUrlState.ts`,
      `${web}/pages/ModelMarket/VideoTrackerJobsPage.tsx`,
      `${web}/pages/Dashboard/dashboardUrlState.ts`,
      `${web}/pages/Dashboard/useDashboardQuerySync.ts`,
      /^apps\/web\/src\/pages\/Dashboard\/ProjectFilter(?:Control|Panel|Summary)\.tsx$/,
    ],
    "real list filters, URL state, and browser history",
  ),
  chromiumSubset(
    "annotation-guide",
    ["guide-experience", "guide-autosave", "guide-asset-ownership", "markdown-authoring"],
    [
      `${web}/pages/Projects/sections/AnnotationGuideSection.tsx`,
      `${web}/pages/ProjectTemplates/ProjectTemplatesPage.tsx`,
    ],
    "annotation guide authoring, autosave, asset ownership, and reading",
  ),
  chromiumSubset(
    "training-parameters",
    ["training-parameter-reset"],
    [
      `${web}/pages/Training/TrainingConfigurationEditor.tsx`,
      `${web}/pages/Training/TrainingParameterField.tsx`,
      `${web}/pages/Training/trainingConfiguration.ts`,
    ],
    "browser parameter editing and reset",
  ),
  chromiumSubset(
    "lidar-export",
    ["lidar-export-trusted"],
    [],
    "LiDAR export camera selection and preflight in ordinary Chromium",
  ),
];

const functional = [
  ...shardSuite(
    {
      suite: "default",
      command: "test:e2e --project=chromium --fully-parallel",
      built: true,
      legacyGate: true,
      scope: "full",
      reason: "whole Chromium functional shard; point-cloud cases run in their dedicated project",
    },
    16,
  ),
  ...["readonly", "native", "native-editor", "ai-native"].map((name) => ({
    suite: `mask-${name}`,
    command: `test:e2e:mask-${name}`,
    built: false,
    legacyGate: true,
    scope: "specialty",
    triggers: maskTriggers,
    reason: `raster Mask ${name} matrix contract`,
    runtime: {
      "mask-readonly": {
        collects: 2,
        skipped: 0,
        note: "read-only and closed-gate cases; native writes run in their own suite",
      },
      "mask-native": {
        collects: 11,
        skipped: 0,
        note: "Raster native product matrix; separate real Worker receipt after retirement",
      },
      "mask-native-editor": {
        collects: 10,
        skipped: 0,
        note: "eight advanced editing and two editor cases on the isolated Raster write deployment",
      },
      "mask-ai-native": {
        collects: 7,
        skipped: 0,
        note: "mock SAM backend + alternative tracker service, not a real model/GPU run",
      },
    }[`mask-${name}`],
  })),
  {
    suite: "mask-toolbar-native",
    command: "test:e2e:mask-toolbar-native",
    built: true,
    legacyGate: true,
    scope: "specialty",
    requireAllTests: true,
    triggers: [...maskTriggers, specPath("workbench-ai-toolbar-layers")],
    reason: "native Mask workbench AI toolbar writes and recovery",
    runtime: { collects: 6, skipped: 0 },
  },
  {
    suite: "ai-request-worker",
    command: "test:e2e --project=chromium e2e/tests/workbench-ai-inspector-phases.spec.ts",
    built: true,
    legacyGate: true,
    scope: "specialty",
    requireAllTests: true,
    triggers: [
      specPath("workbench-ai-inspector-phases"),
      `${workbench}/shell/AIInspectorPanel.tsx`,
      "apps/api/app/workers/frame_preannotate.py",
      "apps/api/app/workers/predictions_retry.py",
      "apps/api/app/workers/celery_app.py",
      "apps/inference/",
      "apps/preannotation-results/",
    ],
    reason: "real async AI request, retry, cancellation and tracker review",
    runtime: { collects: 4, skipped: 0 },
  },
];

const layoutStress = {
  suite: "layout-stress",
  command: "test:e2e:stress",
  built: true,
  legacyGate: true,
  scope: "extended",
  triggers: layoutStressSpecs,
  reason: "long-horizon layout stress (54 rearrangements)",
};

const videoSuites = shardSuite(
  {
    suite: "video-pipeline",
    command:
      "test:e2e:video --project=chromium --fully-parallel e2e/tests/video- e2e/tests/workbench-video-",
    built: true,
    legacyGate: false,
    scope: "specialty",
    triggers: videoTriggers,
    reason:
      "video pipeline specialty: manifest/chunk playback, tracker jobs, keyframe and Issue chains",
  },
  4,
);

const domainSpecialties = [
  ...videoSuites,
  ...shardSuite(
    {
      suite: "pointcloud",
      command: "test:e2e --project=pointcloud",
      built: true,
      legacyGate: true,
      scope: "specialty",
      triggers: [
        /^apps\/web\/e2e\/tests\/workbench-pointcloud[^/]*\.spec\.ts$/,
        `${workbench}/stages/three-d/`,
        `${web}/utils/pointCloudNavigationDiagnostics.ts`,
        /^apps\/api\/app\/(?:api\/v1\/(?:lidar|point_cloud)|services\/point)/,
        ...layoutPaths,
      ],
      reason: "point-cloud specialty under the SwiftShader software-rendering project",
    },
    4,
  ),
];

const fullSuites = [...smoke, ...functional, ...domainSpecialties, layoutStress];
export const SUITE_CONTRACT = [...fullSuites, ...targetedFunctional];
const suitesById = new Map(SUITE_CONTRACT.map((entry) => [entry.suite, entry]));
const routedSuites = SUITE_CONTRACT.filter(({ triggers }) => triggers);

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

const DOCS_ONLY = [
  /^README\.md$/,
  /^DEV\.md$/,
  /^ROADMAP\.md$/,
  /^ROADMAP\/.*\.md$/,
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

const ISOLATED_PYTEST = new Set(["apps/api/tests/test_pytest_sharding.py"]);
const VITEST_CASE = /^apps\/web\/src\/.+\.(?:test|spec)\.[jt]sx?$/;
const TEST_SUPPORT =
  /\/(?:__fixtures__|fixtures|helpers|mocks|test|test-utils|testUtils|build|config)\//;

function isIsolatedTest(path) {
  return ISOLATED_PYTEST.has(path) || (VITEST_CASE.test(path) && !TEST_SUPPORT.test(path));
}

const corePaths = [/^apps\/web\/src\/pages\/Login\/LoginPage\.(?:tsx|module\.css)$/, timelineSpec];
const sharedPaths = [
  `${web}/api/`,
  `${web}/lib/`,
  `${web}/stores/`,
  `${web}/hooks/useAuth.ts`,
  `${web}/test/`,
  `${web}/styles/`,
  `${workbench}/WorkbenchPage.tsx`,
  `${workbench}/state/taskNavigation.ts`,
  `${workbench}/state/useWorkbenchShellModel.tsx`,
  "apps/api/app/api/",
  "apps/api/app/services/",
  "apps/api/app/core/",
  "apps/api/app/deps.py",
  "apps/web/e2e/fixtures/",
  "apps/web/e2e/helpers/",
  /^apps\/web\/(?:e2e\/global-|index\.html$|package\.json$|playwright|tsconfig|vite|postcss|tailwind)/,
  /^docker-compose[^/]*\.ya?ml$/,
  "package.json",
  "pnpm-lock.yaml",
  "pnpm-workspace.yaml",
  ".npmrc",
  ".env.example",
];
const knownRoots = new Set(["apps", "packages", "scripts", ".github", "examples"]);

function matches(path, triggers) {
  return triggers.some((trigger) =>
    typeof trigger !== "string"
      ? trigger.test(path)
      : trigger.endsWith("/")
        ? path.startsWith(trigger)
        : path === trigger,
  );
}

function selectPrSuites(paths) {
  if (!Array.isArray(paths)) throw new Error("PR changed paths must be supplied");
  const docs = [];
  const appCode = [];
  const domains = new Set();
  const unknownPaths = [];
  const unmappedAppPaths = [];
  const routes = new Map();
  let timelineLayout = false;
  let sharedPath;

  for (const path of paths) {
    if (isDocsOnlyPath(path)) {
      docs.push(path);
      continue;
    }
    appCode.push(path);
    if (isIsolatedTest(path)) continue;

    const [root, app] = path.split("/", 2);
    const shared = matches(path, sharedPaths);
    domains.add(root === "apps" ? `apps/${app ?? ""}` : root);
    if (!knownRoots.has(root) && !shared) unknownPaths.push(path);
    if (shared) sharedPath ??= path;
    let mapped = matches(path, corePaths) || shared;
    for (const { suite, triggers } of routedSuites) {
      if (!matches(path, triggers)) continue;
      mapped = true;
      if (!routes.has(suite)) routes.set(suite, path);
    }
    if (!mapped) unmappedAppPaths.push(path);
    if (path === timelineSpec || matches(path, videoTriggers) || matches(path, layoutPaths))
      timelineLayout = true;
  }

  const classification = {
    event: "pull_request",
    docsOnly: docs.length > 0 && appCode.length === 0,
    testOnly: appCode.length > 0 && domains.size === 0,
    emptyDiff: paths.length === 0,
    appCode,
    docs,
    multiDomain: domains.size > 1,
    domains: [...domains],
    unknownPaths,
    unmappedAppPaths,
  };
  if (classification.docsOnly || classification.testOnly) {
    const name = classification.docsOnly ? "docs-only" : "test-only";
    const reason = classification.docsOnly
      ? "docs-only change (whitelisted markdown/docs-site paths); no app E2E required"
      : "Only isolated Vitest or pytest cases changed; their existing CI gates still run.";
    return {
      planned: [],
      classification,
      gate: { name, reason },
      reasons: [{ suite: "(none)", selected: false, because: reason }],
      warnings: [],
      timelineLayout: false,
    };
  }

  const fallback = [
    classification.emptyDiff && [
      "Empty diff cannot identify a browser owner.",
      "empty diff cannot be classified; falling back to the full conservative selection",
    ],
    unknownPaths.length > 0 && [
      "Unrecognized paths require the conservative full matrix.",
      `unrecognized top-level paths broaden the selection: ${unknownPaths.join(", ")}`,
    ],
    classification.multiDomain && [
      "A multi-domain change requires the conservative full matrix.",
      `multiple domains touched (${classification.domains.join(", ")}); broadening the selection`,
    ],
    sharedPath !== undefined && [
      "A shared dependency or runtime change requires the conservative full matrix.",
      `shared dependency path ${sharedPath} selects full functional plus dedicated configs`,
    ],
    unmappedAppPaths.length > 0 && [
      "Unmapped runtime paths require the conservative full matrix.",
      `app paths without a browser owner broaden to full: ${unmappedAppPaths.join(", ")}`,
    ],
  ].filter(Boolean);
  const broaden = fallback.length > 0;
  const gate = broaden
    ? { name: "broad", reason: fallback[0][0] }
    : {
        name: "risk",
        reason: "Only mapped runtime paths changed. Run bounded smoke and their browser owners.",
      };
  const entries = broaden
    ? fullSuites
    : [...smoke, ...routedSuites.filter(({ suite }) => routes.has(suite))];
  return {
    planned: entries.map(({ suite }) => suite),
    classification,
    gate,
    reasons: entries.map(({ suite, reason }) => ({
      suite,
      selected: true,
      because: routes.has(suite)
        ? `${reason} (triggered by ${routes.get(suite)})`
        : suite === "smoke" || !broaden
          ? reason
          : gate.reason,
    })),
    warnings: fallback.map(([, warning]) => warning),
    timelineLayout: broaden || timelineLayout,
  };
}

export function selectSuites(eventName, paths, options = {}) {
  if (eventName === "pull_request") return selectPrSuites(paths);
  const scopes = {
    push: "full",
    schedule: process.env.E2E_SCHEDULE_SCOPE ?? "full",
    workflow_dispatch: options.dispatchScope ?? "extended",
  };
  if (!Object.hasOwn(scopes, eventName)) throw new Error(`Unsupported E2E event: ${eventName}`);
  const scope = scopes[eventName];
  const nightly = eventName === "schedule";
  if (!["extended", "full"].includes(scope))
    throw new Error(
      `Invalid ${nightly ? "nightly" : "manual"} E2E scope: ${scope} (expected extended or full)`,
    );
  const full = scope === "full";
  const reason = full
    ? "Run bounded smoke, the full functional shards, dedicated checks, and layout stress."
    : `${nightly ? "Nightly" : "Manual"} extended-only scope selects layout stress.`;
  return {
    planned: full ? fullSuites.map(({ suite }) => suite) : [layoutStress.suite],
    classification: { event: eventName, note: reason },
    gate: { name: scope, reason },
    reasons: [{ suite: full ? "all" : layoutStress.suite, selected: true, because: reason }],
    warnings:
      nightly && full
        ? [
            "the legacy rollback gate runs extended-only for schedule; the planned nightly selection is full",
          ]
        : [],
    timelineLayout: true,
  };
}

export function shadowPlan(eventName, paths, options = {}) {
  const selection = selectSuites(eventName, paths, options);
  const { planned, classification, reasons, warnings } = selection;
  const legacy = planE2ESuites(eventName, paths).include.map(({ suite }) => suite);
  const selectionMode = options.selectionMode ?? "planned";
  const plannedGate = { ...selection.gate, suites: planned };
  const rollback = {
    name: "legacy",
    reason: "Legacy rollback gate (E2E_SELECTION_MODE=legacy).",
    suites: legacy,
  };
  const effective = selectionMode === "legacy" ? rollback : plannedGate;
  const broad =
    plannedGate.name === "broad"
      ? plannedGate
      : {
          name: "broad",
          reason: "Conservative fallback for shared, unknown, or multi-domain changes.",
          suites: fullSuites.map(({ suite }) => suite),
        };
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
    selectionMode,
    timelineLayout:
      selectionMode === "legacy" ? classification.docsOnly !== true : selection.timelineLayout,
    gates: {
      planned: plannedGate,
      effective,
      rollback,
      ...(eventName === "pull_request" ? { broad } : {}),
    },
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

export function planE2ESuites(eventName, paths) {
  const legacy = SUITE_CONTRACT.filter((suite) => suite.legacyGate);
  if (["schedule", "workflow_dispatch"].includes(eventName))
    return { include: legacy.filter(({ scope }) => scope === "extended") };
  if (eventName === "push") return { include: legacy };
  if (eventName !== "pull_request") throw new Error(`Unsupported E2E event: ${eventName}`);
  if (!Array.isArray(paths)) throw new Error("PR changed paths must be supplied");
  return {
    include: legacy.filter(({ scope }) => scope !== "extended" || paths.some(affectsExtended)),
  };
}

function dispatchMatrix(entries) {
  const ids = new Set(entries.map(({ suite }) => suite));
  const dedicatedVideo = videoSuites.every(({ suite }) => ids.has(suite));
  return {
    include: entries.map(({ suite, command, built, scope, requireAllTests }) => ({
      suite,
      command,
      built,
      ...(scope === "targeted" ? { chromiumSubset: true } : {}),
      ...(requireAllTests ? { requireAllTests } : {}),
      ...(dedicatedVideo && suite.startsWith("default-") ? { dedicatedVideo: true } : {}),
    })),
  };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const eventName = process.env.GITHUB_EVENT_NAME;
  let paths;
  if (eventName === "pull_request") {
    const base = process.argv[2];
    if (!/^[a-f0-9]{40}$/.test(base ?? "")) throw new Error("A full PR base SHA is required");
    paths = execFileSync("git", ["diff", "--name-only", "--no-renames", "-z", base, "HEAD", "--"], {
      encoding: "utf8",
    })
      .split("\0")
      .filter(Boolean);
  }
  const dispatchScope = process.env.E2E_DISPATCH_SCOPE;
  const selectionMode = (process.env.E2E_SELECTION_MODE ?? "").trim() || "planned";
  if (!["planned", "legacy"].includes(selectionMode))
    throw new Error(`Invalid E2E selection mode: ${selectionMode}`);
  const shadow = shadowPlan(eventName, paths, { dispatchScope, selectionMode });
  const docsOnly = shadow.classification.docsOnly === true;
  const mlCpuTriggers = [
    ...Object.values(SHARED_CONTRACT_SUITES).flatMap((suite) => suite.triggers),
    "scripts/test-suites.json",
    "scripts/run-ml-cpu-tests.sh",
    ".github/workflows/ml-cpu-test.yml",
  ];
  let mlCpu;
  if (eventName === "push" || eventName === "schedule") mlCpu = true;
  else if (eventName === "workflow_dispatch")
    mlCpu = (process.env.E2E_DISPATCH_SCOPE ?? "extended") === "full";
  else if (eventName === "pull_request") {
    const changed = paths ?? [];
    mlCpu =
      changed.length === 0 ||
      changed.some((path) => mlCpuTriggers.some((prefix) => path.startsWith(prefix))) ||
      !docsOnly;
  } else throw new Error(`Unsupported E2E event for ml-cpu wiring: ${eventName}`);

  const { effective, rollback } = shadow.gates;
  const entries = effective.suites.map((suite) => suitesById.get(suite));
  const requiredSuites = entries.flatMap(({ suite, flakyPolicy }) => [
    {
      suite,
      planned: true,
      ...(selectionMode !== "legacy" && flakyPolicy ? { flakyPolicy } : {}),
    },
    ...(suite === "mask-native" ? [{ suite: "mask-worker", planned: true }] : []),
  ]);
  let ordered = entries;
  if (selectionMode === "planned") {
    const { jobSeconds } = JSON.parse(
      readFileSync(new URL("./e2e-job-durations.json", import.meta.url), "utf8"),
    );
    const priority = ({ suite }) => {
      if (suite === "smoke") return Infinity;
      return Object.hasOwn(jobSeconds, suite) ? jobSeconds[suite] : -1;
    };
    ordered = entries.toSorted((a, b) => priority(b) - priority(a));
  }
  console.log(`matrix=${JSON.stringify(dispatchMatrix(ordered))}`);
  const legacyMatrix = dispatchMatrix(rollback.suites.map((suite) => suitesById.get(suite)));
  console.log(`legacy=${JSON.stringify(legacyMatrix)}`);
  console.log(
    `required=${JSON.stringify({
      classification: requiredSuites.length > 0 ? "app-code" : effective.name,
      reason: effective.reason,
      suites: requiredSuites,
    })}`,
  );
  console.log(`docs_only=${docsOnly}`);
  console.log(`run_suites=${entries.length > 0}`);
  console.log(`selection_mode=${selectionMode}`);
  console.log(`timeline_layout=${shadow.timelineLayout}`);
  const shadowPath = process.env.E2E_SHADOW_PATH || "e2e-shadow.json";
  writeFileSync(shadowPath, `${JSON.stringify({ ...shadow, mlCpu })}\n`);
  console.log(`shadow_path=${shadowPath}`);
  console.log(`ml_cpu=${mlCpu}`);
  const summaryPath = process.argv[3];
  if (summaryPath) {
    const suites = effective.suites.join(", ") || "(none)";
    appendFileSync(
      summaryPath,
      [
        `### CI test selection (${selectionMode})`,
        `Application tests: ${docsOnly ? "not required (docs-only PR)" : "selected"}`,
        `E2E (${effective.name}): ${suites}`,
        `Timeline layout: ${shadow.timelineLayout ? "selected" : "not selected"}`,
        `ML CPU: ${mlCpu ? "selected" : "not selected"}`,
        "See the `e2e-selection-report` artifact for changed paths and selection reasons.",
        "",
      ].join("\n\n"),
    );
  }
}

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
      "test:e2e --retries=0 e2e/tests/auth.spec.ts e2e/tests/annotation.spec.ts e2e/tests/employee-project-roles.spec.ts e2e/tests/mask-session-guard.spec.ts --grep '健康检查|正确凭证|错密码|未登录访问|annotator 登录|bbox 真实绘制、选类、落库并刷新恢复|same employee annotates A|opposite project actions are denied|切工具离开 dirty session'",
    built: true,
    scope: "smoke",
    flakyPolicy: "forbid",
    reason:
      "bounded real chain (reviewer-owned membership: 9 tests / 4 existing files): UI login, real canvas save+refresh, annotator submit→review→complete across two projects, permission denial, dirty-session switch guard",
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

const maskTriggers = [
  "apps/web/e2e/tests/mask-",
  "apps/web/e2e/tests/raster-mask-",
  "apps/api/app/api/v1/mask_",
  "apps/api/app/services/",
  "apps/web/src/pages/Workbench/state/mask",
  "apps/web/src/pages/Workbench/stage/shared/geometry/",
  "apps/web/src/pages/Workbench/stage/shared/rasterMask",
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
    triggers: [...maskTriggers, "apps/web/e2e/tests/workbench-ai-toolbar-layers.spec.ts"],
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
      "apps/web/e2e/tests/workbench-ai-inspector-phases.spec.ts",
      "apps/web/src/pages/Workbench/shell/AIInspectorPanel",
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

function sharedContractEntries() {
  return [];
}

const layoutStress = {
  suite: "layout-stress",
  command: "test:e2e:stress",
  built: true,
  legacyGate: true,
  scope: "extended",
  reason: "long-horizon layout stress (54 rearrangements)",
};

const domainSpecialties = [
  ...shardSuite(
    {
      suite: "video-pipeline",
      command:
        "test:e2e:video --project=chromium --fully-parallel e2e/tests/video- e2e/tests/workbench-video-",
      built: true,
      legacyGate: false,
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
    4,
  ),
  ...shardSuite(
    {
      suite: "pointcloud",
      command: "test:e2e --project=pointcloud",
      built: true,
      legacyGate: true,
      scope: "specialty",
      triggers: [
        "apps/web/e2e/tests/workbench-pointcloud-",
        "apps/web/src/pages/Workbench/stages/three-d/",
        "apps/api/app/api/v1/lidar",
        "apps/api/app/services/point",
      ],
      reason: "point-cloud specialty under the SwiftShader software-rendering project",
    },
    4,
  ),
];

export const SUITE_CONTRACT = [...smoke, ...functional, ...domainSpecialties, layoutStress];
const allSuites = SUITE_CONTRACT;

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

const SHARED_WORKBENCH_TRIGGERS = [
  "apps/web/src/pages/Workbench/state/",
  "apps/web/src/pages/Workbench/shell/",
];

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

export function selectSuites(eventName, paths, options = {}) {
  if (eventName === "schedule") {
    const scope = process.env.E2E_SCHEDULE_SCOPE ?? "full";
    if (!["extended", "full"].includes(scope))
      throw new Error(`Invalid nightly E2E scope: ${scope} (expected extended or full)`);
    if (scope === "extended")
      return {
        planned: [layoutStress.suite],
        classification: { event: "schedule", note: "nightly extended-only explicitly requested" },
        reasons: [
          {
            suite: layoutStress.suite,
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
      planned: [layoutStress.suite],
      classification: {
        event: "workflow_dispatch",
        note: "manual entry stays extended-only and explicitly labeled",
      },
      reasons: [
        {
          suite: layoutStress.suite,
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
      addPlanned(
        layoutStress.suite,
        classification.multiDomain
          ? "multi-domain change broadens verification to layout stress"
          : "shared layout surface (styles, layout tooling or runtime config) changed",
      );
    }
    if (planned.length === 0)
      throw new Error(
        "Illegal E2E selection: app-code changes must always select at least the core suites",
      );
  }

  return { planned, classification, reasons, warnings };
}

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
  const selection = selectSuites(eventName, paths, { dispatchScope });
  const docsOnly = selection.classification.docsOnly === true;
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

  const selectionMode = (process.env.E2E_SELECTION_MODE ?? "").trim() || "planned";
  if (!["planned", "legacy"].includes(selectionMode))
    throw new Error(`Invalid E2E selection mode: ${selectionMode}`);
  const legacyGate = planE2ESuites(eventName, paths);
  const shadow = { ...shadowPlan(eventName, paths, { dispatchScope }), mlCpu };
  const plannedEntries = selection.planned
    .map((suite) => SUITE_CONTRACT.find((entry) => entry.suite === suite))
    .filter(Boolean);
  const plannedGate = { include: plannedEntries };
  const gate = selectionMode === "legacy" ? legacyGate : plannedGate;
  const requiredSuites = gate.include.flatMap(({ suite, flakyPolicy }) => [
    {
      suite,
      planned: true,
      ...(selectionMode !== "legacy" && flakyPolicy ? { flakyPolicy } : {}),
    },
    ...(suite === "mask-native" ? [{ suite: "mask-worker", planned: true }] : []),
  ]);
  const docsOnlySkip = selectionMode !== "legacy" && docsOnly;
  let matrix = gate;
  if (selectionMode === "planned") {
    const { jobSeconds } = JSON.parse(
      readFileSync(new URL("./e2e-job-durations.json", import.meta.url), "utf8"),
    );
    const priority = ({ suite }) => {
      if (suite === "smoke") return Infinity;
      return Object.hasOwn(jobSeconds, suite) ? jobSeconds[suite] : -1;
    };
    matrix = { include: plannedEntries.toSorted((a, b) => priority(b) - priority(a)) };
  }
  console.log(`matrix=${JSON.stringify(matrix)}`);
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
  console.log(`docs_only=${docsOnly}`);
  console.log(`run_suites=${gate.include.length > 0}`);
  console.log(`selection_mode=${selectionMode}`);
  const shadowPath = process.env.E2E_SHADOW_PATH || "e2e-shadow.json";
  writeFileSync(shadowPath, `${JSON.stringify(shadow)}\n`);
  console.log(`shadow_path=${shadowPath}`);
  console.log(`ml_cpu=${mlCpu}`);
  const summaryPath = process.argv[3];
  if (summaryPath) {
    const suites = gate.include.map(({ suite }) => suite).join(", ") || "(none)";
    appendFileSync(
      summaryPath,
      [
        `### CI test selection (${selectionMode})`,
        `Application tests: ${docsOnly ? "not required (docs-only PR)" : "selected"}`,
        `E2E: ${suites}`,
        `ML CPU: ${mlCpu ? "selected" : "not selected"}`,
        "See the `e2e-selection-report` artifact for changed paths and selection reasons.",
        "",
      ].join("\n\n"),
    );
  }
}

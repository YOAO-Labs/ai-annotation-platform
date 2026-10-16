import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import { withHeadlessDisplay } from "./headless-marketing-display.mjs";
import {
  RECORDING_FLOWS,
  MARKETING_ONLY_FLOWS,
  recordingInference,
  recordingPlan,
  marketingCaptureDriver,
  assertRecordingMode,
} from "../e2e/screenshots/recording-plan.mjs";

const { values } = parseArgs({
  args: process.argv.slice(2).filter((arg) => arg !== "--"),
  options: {
    flow: { type: "string", multiple: true },
    profile: { type: "string" },
    list: { type: "boolean" },
    plan: { type: "boolean" },
    "validate-only": { type: "boolean" },
    "resize-display": { type: "boolean" },
    "headless-display": { type: "boolean" },
  },
});
if (values.list) {
  for (const [id, requirements] of Object.entries(RECORDING_FLOWS)) {
    const inference = recordingInference(id);
    const scope =
      requirements.join(",") ||
      (inference === "live" ? "live inference (flow setup)" : "manual (no ML backend)");
    console.log(
      `${id}\t${scope}\t${MARKETING_ONLY_FLOWS.includes(id) ? "marketing only" : "marketing / docs rehearsal"}`,
    );
  }
  process.exit(0);
}
const profile = values.profile ?? (values["validate-only"] ? "docs" : "marketing");
const plan = recordingPlan(values.flow ?? [], profile);
const captureDriver =
  plan.profile === "marketing"
    ? marketingCaptureDriver(
        process.platform,
        values["headless-display"] ? "x11grab" : process.env.MARKETING_CAPTURE_DRIVER,
        plan.flows,
      )
    : undefined;
if (values["headless-display"] && (process.platform !== "linux" || plan.profile !== "marketing")) {
  throw new Error("--headless-display requires the Linux marketing profile.");
}
console.log(
  JSON.stringify(
    {
      ...plan,
      ...(captureDriver ? { captureDriver } : {}),
      ...(values["headless-display"] ? { headlessDisplay: true } : {}),
    },
    null,
    2,
  ),
);
if (values["resize-display"] && plan.profile !== "marketing") {
  throw new Error("--resize-display only applies to the marketing profile.");
}
if (values["resize-display"] && captureDriver === "screencapturekit") {
  throw new Error("--resize-display applies only to Linux X11 capture.");
}
if (values.plan) process.exit(0);
assertRecordingMode(plan.profile, values["validate-only"]);
if (values["headless-display"]) {
  // Re-enter the same selector on our owned display, before any database/fixture operation.
  process.exitCode = await withHeadlessDisplay(process.execPath, [
    fileURLToPath(import.meta.url),
    ...process.argv.slice(2).filter((arg) => arg !== "--headless-display"),
  ]);
  process.exit(process.exitCode);
}

const webRoot = fileURLToPath(new URL("..", import.meta.url));
const apiRoot = path.resolve(webRoot, "../api");
const databaseUrl = process.env.SCREENSHOT_DATABASE_URL;
if (!databaseUrl || !/(_test|_e2e)$/.test(new URL(databaseUrl).pathname)) {
  throw new Error("SCREENSHOT_DATABASE_URL must select an isolated *_test or *_e2e database.");
}
const redisUrl = process.env.REDIS_URL;
if (!redisUrl || !process.env.CELERY_BROKER_URL) {
  throw new Error(
    "Set REDIS_URL and CELERY_BROKER_URL to the isolated capture Redis before seeding.",
  );
}
const redis = new URL(redisUrl);
const broker = new URL(process.env.CELERY_BROKER_URL);
if (!/^\/[1-9]\d*$/.test(redis.pathname) || redis.href !== broker.href) {
  throw new Error(
    "Capture requires the same explicit nonzero Redis DB for REDIS_URL and CELERY_BROKER_URL.",
  );
}
const env = {
  ...process.env,
  DATABASE_URL: databaseUrl,
  MIGRATION_DATABASE_URL: databaseUrl,
  PYTHONPATH: ".",
  SCREENSHOT_BACKEND_REQUIREMENTS: plan.backendRequirements,
  SCREENSHOT_RECORDING_FLOWS: plan.flows.join(","),
  SCREENSHOT_RECORDING_PROFILE: plan.profile,
  SCREENSHOT_RECORDING_RUN: `${new Date().toISOString().replace(/[:.]/g, "-")}-${process.pid}`,
  SCREENSHOT_VALIDATE_ONLY: values["validate-only"] ? "1" : "0",
  ...(captureDriver ? { MARKETING_CAPTURE_DRIVER: captureDriver } : {}),
};
const requiresLiveInference = plan.flows.some((id) => recordingInference(id) === "live");
if (plan.backendRequirements !== "none" || requiresLiveInference) {
  const response = await fetch(
    new URL("/health", process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010"),
    { signal: AbortSignal.timeout(15_000) },
  );
  if (!response.ok) throw new Error(`Capture API health failed: ${response.status}`);
  const health = await response.json();
  const workers = health?.checks?.celery?.workers ?? [];
  if (!workers.length || workers.some((worker) => !worker.name?.startsWith("screenshots@"))) {
    throw new Error("Live recording requires only screenshots@ workers on the capture API broker.");
  }
}
function run(command, args, cwd) {
  const result = spawnSync(command, args, { cwd, env, stdio: "inherit" });
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status ?? 1);
}
// Fail read-only capture checks before fixture reconciliation can mutate the database.
if (plan.profile === "marketing") {
  run(
    process.execPath,
    [
      "scripts/run-marketing-capture.mjs",
      "--preflight-only",
      ...(values["resize-display"] ? ["--resize-display"] : []),
    ],
    webRoot,
  );
}
// Probe live registered backends during reconciliation, never silently start a stub.
run(
  path.join(apiRoot, ".venv/bin/python"),
  [
    "scripts/seed.py",
    "--profile",
    "screenshots",
    "--offline",
    "--repair",
    "--ml-backend-mode",
    "live",
    "--backend-requirements",
    plan.backendRequirements,
  ],
  apiRoot,
);
if (plan.profile === "marketing") {
  run(
    process.execPath,
    [
      "scripts/run-marketing-capture.mjs",
      "--grep",
      plan.grep,
      ...(values["resize-display"] ? ["--resize-display"] : []),
    ],
    webRoot,
  );
} else {
  run(
    path.join(webRoot, "node_modules/.bin/playwright"),
    ["test", "--config=playwright.screenshots.config.ts", "--project=flows", "--grep", plan.grep],
    webRoot,
  );
}

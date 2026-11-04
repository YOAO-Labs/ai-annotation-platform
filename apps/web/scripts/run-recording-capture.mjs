import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import { cleanupScreenshotRun } from "../e2e/screenshots/fixture-run.mjs";
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
if (
  !["test", "e2e"].includes(process.env.AAP_WORKTREE_MODE ?? "") ||
  !process.env.DATABASE_URL ||
  !process.env.PLAYWRIGHT_E2E_DATABASE_URL
) {
  throw new Error("Capture requires pnpm dev:worktree exec --mode e2e --with-worker.");
}
const redisUrl = process.env.REDIS_URL;
if (!redisUrl || !process.env.CELERY_BROKER_URL) {
  throw new Error(
    "Set REDIS_URL and CELERY_BROKER_URL to the isolated capture Redis before seeding.",
  );
}
const redis = new URL(redisUrl);
const broker = new URL(process.env.CELERY_BROKER_URL);
if (
  redis.protocol !== "redis:" ||
  redis.hostname !== "127.0.0.1" ||
  redis.pathname !== "/0" ||
  redis.href !== broker.href
) {
  throw new Error("Capture requires the isolated worktree Redis and matching Celery broker.");
}
const env = {
  ...process.env,
  SCREENSHOT_FIXTURE_RUN_ID: randomUUID(),
  SCREENSHOT_ASSET_OFFLINE: "1",
  SCREENSHOT_DEFER_HEALTH: "1",
  SCREENSHOT_BACKEND_REQUIREMENTS: plan.backendRequirements,
  SCREENSHOT_RECORDING_FLOWS: plan.flows.join(","),
  SCREENSHOT_RECORDING_PROFILE: plan.profile,
  SCREENSHOT_RECORDING_RUN: `${new Date().toISOString().replace(/[:.]/g, "-")}-${process.pid}`,
  SCREENSHOT_VALIDATE_ONLY: values["validate-only"] ? "1" : "0",
  ...(captureDriver ? { MARKETING_CAPTURE_DRIVER: captureDriver } : {}),
};
let child;
let interrupted;
const interrupt = () => {
  interrupted = "SIGINT";
  child?.kill("SIGINT");
};
const terminate = () => {
  interrupted = "SIGTERM";
  child?.kill("SIGTERM");
};
async function run(command, args, cwd) {
  child = spawn(command, args, { cwd, env, stdio: "inherit" });
  const result = await new Promise((resolve, reject) => {
    child.once("error", reject);
    child.once("close", (code, signal) => resolve({ code, signal }));
  });
  child = undefined;
  if (result.code !== 0) throw new Error(`${command} failed: ${result.signal ?? result.code}`);
}
// Fail read-only capture checks before fixture reconciliation can mutate the database.
if (plan.profile === "marketing") {
  await run(
    process.execPath,
    [
      "scripts/run-marketing-capture.mjs",
      "--preflight-only",
      ...(values["resize-display"] ? ["--resize-display"] : []),
    ],
    webRoot,
  );
}
let failure;
process.on("SIGINT", interrupt);
process.on("SIGTERM", terminate);
try {
  if (plan.profile === "marketing") {
    await run(
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
    await run(
      path.join(webRoot, "node_modules/.bin/playwright"),
      ["test", "--config=playwright.screenshots.config.ts", "--project=flows", "--grep", plan.grep],
      webRoot,
    );
  }
} catch (error) {
  failure = error;
}
try {
  await cleanupScreenshotRun(env.SCREENSHOT_FIXTURE_RUN_ID, { allowMissing: true });
} catch (error) {
  if (failure) failure = new AggregateError([failure, error], "Capture and fixture cleanup failed");
  else failure = error;
} finally {
  process.removeListener("SIGINT", interrupt);
  process.removeListener("SIGTERM", terminate);
}
if (failure) throw failure;
if (interrupted) process.exitCode = interrupted === "SIGINT" ? 130 : 143;

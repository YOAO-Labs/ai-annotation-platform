import { worktreeMode as readWorktreeMode } from "../e2e/helpers/worktree-mode.mjs";
import { randomUUID } from "node:crypto";
import { closeFixtureTransport } from "../e2e/helpers/seed-owner-transport.mjs";
import {
  cleanupScreenshotRun,
  prepareScreenshotCapture,
  requireScreenshotCaptureClosed,
  seedScreenshotCapture,
} from "../e2e/screenshots/fixture-run.mjs";

const args = process.argv.slice(2);
if (args.length === 1 && args[0] === "--help") {
  console.log(
    "Usage: node scripts/run-screenshot-native.mjs [--seed -- <seed options>]\nWithout --seed, Playwright owns seeding and teardown. With --seed, Ctrl-C retires this run and its API.",
  );
  process.exit(0);
}
const standalone = args[0] === "--seed";
if (args.length && !standalone) throw new Error("Unknown screenshot launcher option");
const seedArgs = standalone ? args.slice(args[1] === "--" ? 2 : 1) : [];
if (seedArgs.some((arg) => /^(?:--profile|--run-id|--admission-id|--help)(?:=|$)/.test(arg))) {
  throw new Error("The screenshot launcher owns the profile and admission identities");
}
if (
  !["test", "e2e"].includes(readWorktreeMode()) ||
  !process.env.DATABASE_URL ||
  !process.env.PLAYWRIGHT_E2E_DATABASE_URL ||
  process.env.AAP_PRIVATE_FIXTURE_SUPERVISED_SUITE ||
  process.env.AAP_PRIVATE_FIXTURE_AUTHORITY_NATIVE ||
  process.env.AAP_PRIVATE_FIXTURE_RASTER_OWNER ||
  process.env.PLAYWRIGHT_RASTER_OFFLINE === "1"
) {
  throw new Error("Screenshot capture requires its verified worktree and shared private authority");
}
const runId = process.env.SCREENSHOT_FIXTURE_RUN_ID ?? randomUUID();
const port = process.env.PLAYWRIGHT_ISOLATED_API_PORT ?? "8010";
if (!/^[1-9]\d{0,4}$/.test(port) || Number(port) > 65535) {
  throw new Error("Invalid isolated screenshot API port");
}
process.env.SCREENSHOT_FIXTURE_RUN_ID = runId;
process.env.PLAYWRIGHT_ISOLATED_API_PORT = port;
const apiOrigin = `http://127.0.0.1:${port}`;
let stop;
const stopping = new Promise((resolve) => {
  stop = resolve;
});
let interrupted = false;
const seedCancellation = new AbortController();
const healthCancellation = new AbortController();
const interrupt = (signal) => {
  if (interrupted) return;
  interrupted = true;
  seedCancellation.abort(signal);
  healthCancellation.abort();
  stop();
};
const interruptInt = () => interrupt("SIGINT");
const interruptTerm = () => interrupt("SIGTERM");
process.on("SIGINT", interruptInt);
process.on("SIGTERM", interruptTerm);
// Signal listeners alone do not keep Node alive after the owner client closes.
const keepAlive = setInterval(() => {}, 2_147_483_647);
const failures = [];
try {
  await prepareScreenshotCapture(runId, Number(port));
  if (standalone && !interrupted) {
    await seedScreenshotCapture(runId, apiOrigin, seedArgs, seedCancellation.signal);
    if (!interrupted) {
      try {
        const response = await fetch(`${apiOrigin}/health/db`, {
          signal: AbortSignal.any([healthCancellation.signal, AbortSignal.timeout(15_000)]),
        });
        if (!response.ok) throw new Error(`Screenshot API health failed: ${response.status}`);
      } catch (failure) {
        if (!interrupted || failure.name !== "AbortError") throw failure;
      }
    }
  }
  await closeFixtureTransport();
  if (!interrupted) {
    console.log(`Owned screenshot API: ${apiOrigin}; run: ${runId}`);
    await stopping;
  }
} catch (failure) {
  failures.push(failure);
}
try {
  // Playwright normally retires first; the same durable operation also covers
  // startup failures before its global teardown was registered.
  await cleanupScreenshotRun(runId);
  requireScreenshotCaptureClosed(runId);
} catch (failure) {
  failures.push(failure);
} finally {
  clearInterval(keepAlive);
  process.removeListener("SIGINT", interruptInt);
  process.removeListener("SIGTERM", interruptTerm);
  try {
    await closeFixtureTransport();
  } catch (failure) {
    failures.push(failure);
  }
}
if (failures.length === 1) throw failures[0];
if (failures.length > 1)
  throw new AggregateError(failures, "Screenshot capture and cleanup failed");

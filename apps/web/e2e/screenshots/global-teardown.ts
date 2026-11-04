import type { FullConfig } from "@playwright/test";
import { cleanupScreenshotRun } from "./fixture-run.mjs";

export default async function screenshotGlobalTeardown(_config: FullConfig): Promise<void> {
  const runId = process.env.SCREENSHOT_FIXTURE_RUN_ID;
  if (!runId) throw new Error("Screenshot fixture run identity is required for teardown");
  await cleanupScreenshotRun(runId);
}

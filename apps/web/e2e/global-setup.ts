import type { FullConfig } from "@playwright/test";
import { closeFixtureTransport, prepareFixtureSuite } from "./helpers/seed-owner-transport.mjs";

export default async function globalSetup(_config: FullConfig): Promise<void> {
  let failure: unknown;
  let failed = false;
  try {
    const suiteId = process.env.PLAYWRIGHT_FIXTURE_RUN_ID;
    if (!suiteId) throw new Error("Fixture suite identity is required for setup");
    await prepareFixtureSuite(suiteId, "owned");
  } catch (error) {
    failed = true;
    failure = error;
  }
  try {
    await closeFixtureTransport();
  } catch (error) {
    if (failed)
      throw new AggregateError([failure, error], "Fixture setup and transport shutdown failed");
    throw error;
  }
  if (failed) throw failure;
}

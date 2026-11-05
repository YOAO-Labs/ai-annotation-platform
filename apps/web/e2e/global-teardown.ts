/// <reference lib="es2021.promise" />

import type { FullConfig } from "@playwright/test";
import {
  closeFixtureTransport,
  postFixture,
  shutdownFixtureTransport,
} from "./helpers/seed-owner-transport.mjs";

export default async function globalTeardown(_config: FullConfig): Promise<void> {
  const apiBase = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";
  let failure: unknown;
  let failed = false;
  try {
    const suiteId = process.env.PLAYWRIGHT_FIXTURE_RUN_ID;
    if (!suiteId) throw new Error("Fixture suite identity is required for teardown");
    const response = await postFixture(`${apiBase}/api/v1/__test/seed/cleanup`, {
      data: { suite_id: suiteId },
      timeout: 5_000,
    });
    if (!response.ok()) {
      throw new Error(`seed/cleanup failed: ${response.status()} ${await response.text()}`);
    }
    if (process.env.PLAYWRIGHT_RASTER_OFFLINE !== "1") {
      await shutdownFixtureTransport(suiteId);
    }
  } catch (error) {
    failed = true;
    failure = error;
  }
  try {
    await closeFixtureTransport();
  } catch (error) {
    if (failed) {
      throw new AggregateError([failure, error], "Fixture cleanup and transport shutdown failed");
    }
    throw error;
  }
  if (failed) throw failure;
}

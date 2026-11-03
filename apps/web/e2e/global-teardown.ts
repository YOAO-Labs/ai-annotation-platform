import type { FullConfig } from "@playwright/test";
import { closeFixtureTransport, postFixture } from "./helpers/seed-owner-transport.mjs";

export default async function globalTeardown(_config: FullConfig): Promise<void> {
  const apiBase = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";
  try {
    const response = await postFixture(`${apiBase}/api/v1/__test/seed/cleanup`, { timeout: 5_000 });
    if (!response.ok()) {
      throw new Error(`seed/cleanup failed: ${response.status()} ${await response.text()}`);
    }
  } finally {
    await closeFixtureTransport();
  }
}

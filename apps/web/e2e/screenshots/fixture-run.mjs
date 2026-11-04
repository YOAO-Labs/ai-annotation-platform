import { closeFixtureTransport, postFixture } from "../helpers/seed-owner-transport.mjs";

export async function cleanupScreenshotRun(runId, { allowMissing = false } = {}) {
  let failure;
  try {
    const response = await postFixture(
      "http://fixture.local/api/v1/__test/seed/screenshot-cleanup",
      {
        data: { run_id: runId },
      },
    );
    if (!response.ok() && !(allowMissing && response.status() === 404)) {
      throw new Error(`Screenshot cleanup failed: ${response.status()} ${await response.text()}`);
    }
  } catch (error) {
    failure = error;
  }
  try {
    await closeFixtureTransport();
  } catch (error) {
    if (failure)
      throw new AggregateError([failure, error], "Screenshot cleanup and transport close failed");
    throw error;
  }
  if (failure) throw failure;
}

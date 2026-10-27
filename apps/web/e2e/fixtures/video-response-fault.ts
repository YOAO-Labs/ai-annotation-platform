import type { Page } from "@playwright/test";

/** Damage only a browser response; persisted output receipts remain valid. */
export async function injectVideoResponseFault(
  page: Page,
  itemId: string,
  expectations: Record<string, unknown>,
): Promise<void> {
  const fault = expectations.response_fault as
    | { target?: string; mutation?: string }
    | null
    | undefined;
  if (!fault) return;
  if (
    fault.target !== "chunk-samples" ||
    !["unsupported-config", "malformed-samples"].includes(fault.mutation ?? "")
  ) {
    throw new Error("Unknown video fixture response fault");
  }
  await page.route(`**/api/v1/videos/${itemId}/chunks/*/samples*`, async (route) => {
    const response = await route.fetch();
    if (!response.ok()) {
      await route.fulfill({ response });
      return;
    }
    const body = await response.json();
    if (fault.mutation === "unsupported-config") {
      body.codec_string = "avc1.ffff00";
    } else {
      if (!body.samples?.length) throw new Error("Expected a real owned sample table");
      body.samples[0].offset_in_chunk = 2 ** 40;
    }
    await route.fulfill({ response, json: body });
  });
}

import { afterEach, describe, expect, it, vi } from "vitest";
import { getRecentApiCalls, patchFetchForBugCapture, sanitizeApiCalls } from "./bugReportCapture";

afterEach(() => vi.unstubAllGlobals());

describe("API diagnostics preserve request failures", () => {
  it.each([new TypeError("Failed to fetch"), new DOMException("aborted", "AbortError")])(
    "records a failed request without changing %s",
    async (error) => {
      vi.stubGlobal("fetch", vi.fn().mockRejectedValue(error));
      patchFetchForBugCapture();
      await expect(window.fetch("/api/v1/tasks/task/annotations", { method: "POST" })).rejects.toBe(
        error,
      );
      const calls = getRecentApiCalls();
      expect(calls[calls.length - 1]).toMatchObject({ method: "POST", status: 0 });
    },
  );

  it("captures x-request-id on API responses and keeps it through sanitization", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: { code: "storage_original_busy" } }), {
          status: 409,
          headers: { "X-Request-ID": "req-capture-409" },
        }),
      ),
    );
    patchFetchForBugCapture();
    await window.fetch("/api/v1/files/tasks/t1/original-identity?token=secret");
    const calls = sanitizeApiCalls(getRecentApiCalls());
    expect(calls[calls.length - 1]).toMatchObject({
      method: "GET",
      status: 409,
      requestId: "req-capture-409",
      url: "/api/v1/files/tasks/t1/original-identity?token=***",
    });
  });
});

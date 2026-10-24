import { afterEach, describe, expect, it, vi } from "vitest";
import { bugReportsApi, uploadBugAttachment, uploadBugScreenshot } from "./bug-reports";
import { useAuthStore } from "@/stores/authStore";

afterEach(() => {
  vi.unstubAllGlobals();
  useAuthStore.getState().logout();
});

describe("bug report attachment transfer", () => {
  it("posts authenticated raw bytes and returns the server receipt unchanged", async () => {
    useAuthStore.getState().setAuth("upload-token", { id: "owner" } as never);
    const file = new Blob(["original bytes"], { type: "image/webp" });
    const receipt = {
      storageKey: "registered-key",
      fileName: "safe-name.webp",
      mimeType: "image/webp",
      size: 14,
    };
    const fetch = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => receipt });
    vi.stubGlobal("fetch", fetch);

    expect(await uploadBugAttachment(file, "截图 & copy.webp")).toBe(receipt);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/bug_reports/screenshot/upload?file_name=%E6%88%AA%E5%9B%BE%20%26%20copy.webp",
      expect.objectContaining({
        method: "POST",
        body: file,
        headers: { "Content-Type": "image/webp", Authorization: "Bearer upload-token" },
      }),
    );
    expect(await uploadBugScreenshot(file)).toBe("registered-key");
  });

  it("uses authenticated blob GET with the report key and preserves download failures", async () => {
    useAuthStore.getState().setAuth("read-token", { id: "owner" } as never);
    const blob = new Blob(["png bytes"], { type: "image/png" });
    const controller = new AbortController();
    const fetch = vi.fn().mockResolvedValueOnce({ ok: true, status: 200, blob: async () => blob });
    vi.stubGlobal("fetch", fetch);
    expect(
      await bugReportsApi.downloadAttachment("report-1", "key/a b.png", controller.signal),
    ).toBe(blob);
    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/bug_reports/report-1/attachments/download?key=key%2Fa%20b.png",
      expect.objectContaining({
        signal: controller.signal,
        headers: expect.objectContaining({ Authorization: "Bearer read-token" }),
      }),
    );
    fetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: "Attachment expired" }), { status: 410 }),
    );
    await expect(bugReportsApi.downloadAttachment("report-1", "key")).rejects.toMatchObject({
      status: 410,
    });
  });

  it("preserves upload errors instead of manufacturing a client receipt", async () => {
    useAuthStore.getState().setAuth("upload-token", { id: "owner" } as never);
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response(JSON.stringify({ detail: "Not an image" }), { status: 422 }),
        ),
    );
    await expect(
      uploadBugAttachment(new Blob(["bad"], { type: "image/png" })),
    ).rejects.toMatchObject({ status: 422, message: "Not an image" });
  });
});

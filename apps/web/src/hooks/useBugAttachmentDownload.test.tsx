import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "@/stores/authStore";
import { bugReportsApi } from "@/api/bug-reports";
import { useBugAttachmentDownload } from "./useBugAttachmentDownload";

vi.mock("@/api/bug-reports", () => ({ bugReportsApi: { downloadAttachment: vi.fn() } }));
const download = vi.mocked(bugReportsApi.downloadAttachment);
const attachment = {
  storageKey: "owner/image.png",
  fileName: "image.png",
  mimeType: "image/png",
  size: 3,
};
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

let createUrl: ReturnType<typeof vi.fn>;
let revokeUrl: ReturnType<typeof vi.fn>;
let anchorClick: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  useAuthStore.getState().setAuth("download-token", { id: "owner" } as never);
  download.mockReset();
  createUrl = vi.fn().mockReturnValue("blob:download");
  revokeUrl = vi.fn();
  vi.stubGlobal(
    "URL",
    Object.assign(class extends URL {}, { createObjectURL: createUrl, revokeObjectURL: revokeUrl }),
  );
  anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
});
afterEach(() => {
  anchorClick.mockRestore();
  vi.unstubAllGlobals();
});

describe("useBugAttachmentDownload", () => {
  it("guards repeated clicks, downloads one blob, removes the anchor and revokes its URL", async () => {
    const response = deferred<Blob>();
    download.mockReturnValue(response.promise);
    const { result } = renderHook(() => useBugAttachmentDownload("report"));
    let action!: Promise<void>;
    act(() => {
      action = result.current.download(attachment);
      void result.current.download(attachment);
    });
    expect(download).toHaveBeenCalledTimes(1);
    expect(result.current.pendingKeys.has(attachment.storageKey)).toBe(true);
    const blob = new Blob(["png"], { type: "image/png" });
    await act(async () => {
      response.resolve(blob);
      await action;
    });
    expect(createUrl).toHaveBeenCalledWith(blob);
    expect(anchorClick).toHaveBeenCalledTimes(1);
    expect(revokeUrl).toHaveBeenCalledWith("blob:download");
    expect(document.querySelector("a[download]")).toBeNull();
    expect(result.current.pendingKeys.size).toBe(0);
  });

  it("shows a current attachment error and lets it be retried", async () => {
    download.mockRejectedValueOnce({ status: 410 }).mockResolvedValueOnce(new Blob(["png"]));
    const { result } = renderHook(() => useBugAttachmentDownload("report"));
    await act(async () => {
      await result.current.download(attachment);
    });
    expect(result.current.errors[attachment.storageKey]).toBe("附件已过期");
    await act(async () => {
      await result.current.download(attachment);
    });
    expect(result.current.errors[attachment.storageKey]).toBeUndefined();
    expect(anchorClick).toHaveBeenCalledTimes(1);
  });

  it.each(["report", "account", "same-account-login", "unmount", "local-token"])(
    "discards late bytes after %s replacement",
    async (change) => {
      const response = deferred<Blob>();
      download.mockReturnValue(response.promise);
      const { result, rerender, unmount } = renderHook(({ id }) => useBugAttachmentDownload(id), {
        initialProps: { id: "first-report" },
      });
      let action!: Promise<void>;
      act(() => {
        action = result.current.download(attachment);
      });
      const signal = download.mock.calls[0][2];
      act(() => {
        if (change === "report") rerender({ id: "second-report" });
        if (change === "account")
          useAuthStore.getState().setAuth("other-token", { id: "other" } as never);
        if (change === "same-account-login") {
          useAuthStore.getState().logout();
          useAuthStore.getState().setAuth("download-token", { id: "owner" } as never);
        }
        if (change === "local-token") localStorage.setItem("token", "other-token");
        if (change === "unmount") unmount();
      });
      await act(async () => {
        response.resolve(new Blob(["png"]));
        await action;
      });
      expect(createUrl).not.toHaveBeenCalled();
      expect(anchorClick).not.toHaveBeenCalled();
      if (change !== "local-token") expect(signal?.aborted).toBe(true);
      expect(result.current.errors).toEqual({});
    },
  );

  it("does not apply a late failure to a different report", async () => {
    const response = deferred<Blob>();
    download.mockReturnValue(response.promise);
    const { result, rerender } = renderHook(({ id }) => useBugAttachmentDownload(id), {
      initialProps: { id: "old" },
    });
    let action!: Promise<void>;
    act(() => {
      action = result.current.download(attachment);
    });
    rerender({ id: "new" });
    await act(async () => {
      response.reject(new Error("late"));
      await action;
    });
    expect(result.current.errors).toEqual({});
    expect(result.current.pendingKeys.size).toBe(0);
  });

  it("releases the object URL even when invoking the browser download fails", async () => {
    download.mockResolvedValue(new Blob(["png"]));
    anchorClick.mockImplementation(() => {
      throw new Error("blocked");
    });
    const { result } = renderHook(() => useBugAttachmentDownload("report"));
    await act(async () => {
      await result.current.download(attachment);
    });
    expect(revokeUrl).toHaveBeenCalledWith("blob:download");
    expect(document.querySelector("a[download]")).toBeNull();
    expect(result.current.errors[attachment.storageKey]).toBe("下载失败，请重试");
  });
});

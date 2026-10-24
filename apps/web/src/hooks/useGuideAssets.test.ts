import { createElement, StrictMode } from "react";
import { act, cleanup, render, renderHook, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MeResponse } from "@/api/auth";
import { useAuthStore } from "@/stores/authStore";
import { MarkdownView } from "@/components/markdown/MarkdownView";

const mocks = vi.hoisted(() => ({ content: vi.fn(), upload: vi.fn(), remove: vi.fn() }));
vi.mock("@/api/projects", () => ({ projectsApi: { guideAssets: mocks } }));
import { useGuideAssets } from "./useGuideAssets";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const receipt = {
  key: "projects/origin/guide/image-id",
  original_name: "服务器原名.png",
  content_type: "image/png",
  size: 5,
  uploaded_at: "2026-10-24T00:00:00Z",
};
const png = () => new Blob(["image"], { type: "image/png" });
const file = () => new File(["image"], "边界 %.png", { type: "image/png" });
const createAddress = vi.fn();
const revokeAddress = vi.fn();

function login(user = "alice", token = "alice-token") {
  act(() => useAuthStore.getState().setAuth(token, { id: user } as MeResponse));
}

describe("useGuideAssets authenticated image ownership", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.content.mockReset().mockImplementation(async () => png());
    mocks.upload.mockReset().mockResolvedValue(receipt);
    mocks.remove.mockReset().mockResolvedValue({ deleted: receipt.key });
    let sequence = 0;
    createAddress.mockImplementation(() => `blob:guide-${++sequence}`);
    vi.stubGlobal(
      "URL",
      class extends URL {
        static createObjectURL = createAddress;
        static revokeObjectURL = revokeAddress;
      },
    );
    login();
  });
  afterEach(() => {
    cleanup();
    useAuthStore.getState().logout();
    vi.unstubAllGlobals();
  });

  it("deduplicates repeated exact Unicode keys and caches authenticated content", async () => {
    const gate = deferred<Blob>();
    mocks.content.mockReturnValue(gate.promise);
    const { result } = renderHook(() => useGuideAssets("project-a"));
    const key = "projects/origin/guide/中文 空格%25.png";
    const requests = Array.from({ length: 20 }, () =>
      result.current.resolveImage(`guide-asset:${key}`),
    );
    expect(mocks.content).toHaveBeenCalledTimes(1);
    expect(mocks.content).toHaveBeenCalledWith("project-a", key, {
      signal: expect.any(AbortSignal),
    });
    gate.resolve(png());
    expect(await Promise.all(requests)).toEqual(Array(20).fill({ url: "blob:guide-1" }));
    expect(await result.current.signAsset(key)).toBe("blob:guide-1");
    expect(createAddress).toHaveBeenCalledTimes(1);
  });

  it("retains every refreshed address until the owning scope retires", async () => {
    const view = renderHook(() => useGuideAssets("project-a"));
    expect(await view.result.current.signAsset("key")).toBe("blob:guide-1");
    expect(await view.result.current.signAsset("key", { refresh: true })).toBe("blob:guide-2");
    expect(revokeAddress).not.toHaveBeenCalled();
    view.unmount();
    expect(revokeAddress.mock.calls).toEqual([["blob:guide-1"], ["blob:guide-2"]]);
  });

  it("keeps exact SVG bytes in an opaque-origin data URL without creating a Blob URL", async () => {
    const svg = '<svg xmlns="http://www.w3.org/2000/svg"><text>中文%</text></svg>';
    mocks.content.mockResolvedValue(new Blob([svg], { type: "image/svg+xml" }));
    const view = renderHook(() => useGuideAssets("project-a"));
    const address = await view.result.current.signAsset("svg-key");
    expect(address).toBe(`data:image/svg+xml;base64,${Buffer.from(svg).toString("base64")}`);
    expect(await view.result.current.signAsset("svg-key")).toBe(address);
    expect(createAddress).not.toHaveBeenCalled();
    view.unmount();
    expect(revokeAddress).not.toHaveBeenCalled();
  });

  it("rejects late previous-project bytes and retired callbacks before creating an address", async () => {
    const gate = deferred<Blob>();
    mocks.content.mockReturnValueOnce(gate.promise);
    const view = renderHook(({ project }) => useGuideAssets(project), {
      initialProps: { project: "project-a" },
    });
    const oldResolver = view.result.current.resolveImage;
    const oldRequest = oldResolver("guide-asset:key").catch((error: unknown) => error);
    const signal = mocks.content.mock.calls[0][2].signal as AbortSignal;
    view.rerender({ project: "project-b" });
    expect(signal.aborted).toBe(true);
    gate.resolve(png());
    expect(await oldRequest).toMatchObject({ message: expect.stringContaining("会话已变更") });
    expect(createAddress).not.toHaveBeenCalled();
    await expect(oldResolver("guide-asset:key")).rejects.toThrow("会话已变更");
    expect(mocks.content).toHaveBeenCalledTimes(1);
    await expect(view.result.current.resolveImage("guide-asset:key")).resolves.toEqual({
      url: "blob:guide-1",
    });
  });

  it.each(["account", "session"])("retires same-project images after a new %s", async (change) => {
    const view = renderHook(() => useGuideAssets("project-a"));
    const oldResolver = view.result.current.resolveImage;
    const oldScope = view.result.current.imageScope;
    await oldResolver("guide-asset:key");
    login(change === "account" ? "bob" : "alice", "new-login-token");
    expect(view.result.current.imageScope).not.toBe(oldScope);
    expect(view.result.current.resolveImage).not.toBe(oldResolver);
    expect(revokeAddress).toHaveBeenCalledWith("blob:guide-1");
    await expect(oldResolver("guide-asset:key")).rejects.toThrow("会话已变更");
    await view.result.current.resolveImage("guide-asset:key");
    expect(mocks.content).toHaveBeenCalledTimes(2);
  });

  it("continues the same owner after a token refresh", async () => {
    const view = renderHook(() => useGuideAssets("project-a"));
    const resolver = view.result.current.resolveImage;
    await resolver("guide-asset:key");
    act(() => useAuthStore.getState().setToken("refreshed-token"));
    expect(view.result.current.resolveImage).toBe(resolver);
    await resolver("guide-asset:key");
    expect(mocks.content).toHaveBeenCalledTimes(1);
    expect(revokeAddress).not.toHaveBeenCalled();
  });

  it("rejects cached reads and new writes when shared credentials changed before render", async () => {
    const view = renderHook(() => useGuideAssets("project-a"));
    await view.result.current.resolveImage("guide-asset:key");
    localStorage.setItem("token", "another-tab-token");
    await expect(view.result.current.signAsset("key")).rejects.toThrow("会话已变更");
    await expect(view.result.current.uploadAsset(file())).rejects.toThrow("会话已变更");
    await expect(view.result.current.deleteAsset("key")).rejects.toThrow("会话已变更");
    expect(mocks.upload).not.toHaveBeenCalled();
    expect(mocks.remove).not.toHaveBeenCalled();
  });

  it.each(["content", "upload", "remove"] as const)(
    "aborts pending %s on unmount and rejects its late result",
    async (operation) => {
      const gate = deferred<unknown>();
      mocks[operation].mockReturnValueOnce(gate.promise);
      const view = renderHook(() => useGuideAssets("project-a"));
      const request =
        operation === "content"
          ? view.result.current.resolveImage("guide-asset:key")
          : operation === "upload"
            ? view.result.current.uploadAsset(file())
            : view.result.current.deleteAsset("key");
      const outcome = request.catch((error: unknown) => error);
      const signal = mocks[operation].mock.calls[0][2].signal as AbortSignal;
      view.unmount();
      expect(signal.aborted).toBe(true);
      gate.resolve(operation === "content" ? png() : receipt);
      expect(await outcome).toMatchObject({ message: expect.stringContaining("会话已变更") });
      expect(createAddress).not.toHaveBeenCalled();
    },
  );

  it("rejects a late upload after re-login without sending any follow-up request", async () => {
    const gate = deferred<typeof receipt>();
    mocks.upload.mockReturnValueOnce(gate.promise);
    const view = renderHook(() => useGuideAssets("project-a"));
    const outcome = view.result.current.uploadAsset(file()).catch((error: unknown) => error);
    login("alice", "new-session-token");
    gate.resolve(receipt);
    expect(await outcome).toMatchObject({ message: expect.stringContaining("会话已变更") });
    expect(mocks.upload).toHaveBeenCalledTimes(1);
    expect(mocks.content).not.toHaveBeenCalled();
  });

  it.each(["image/png", "image/jpeg", "image/webp", "image/gif", "image/svg+xml"])(
    "uploads %s directly and returns the server receipt",
    async (type) => {
      const view = renderHook(() => useGuideAssets("project-a"));
      const image = new File(["x"], "original", { type });
      expect(await view.result.current.uploadAsset(image)).toEqual({
        src: `guide-asset:${receipt.key}`,
        alt: receipt.original_name,
        asset: receipt,
      });
      expect(mocks.upload.mock.calls[0][1]).toBe(image);
      expect(mocks.upload.mock.calls[0][2]).toEqual({ signal: expect.any(AbortSignal) });
    },
  );

  it("accepts 5MiB and rejects zero, oversized and non-image files before transport", async () => {
    const view = renderHook(() => useGuideAssets("project-a"));
    await view.result.current.uploadAsset(
      new File([new Uint8Array(5 * 1024 * 1024)], "max.png", { type: "image/png" }),
    );
    for (const image of [
      new File([], "empty.png", { type: "image/png" }),
      new File([new Uint8Array(5 * 1024 * 1024 + 1)], "large.png", { type: "image/png" }),
      new File(["text"], "text.txt", { type: "text/plain" }),
    ]) {
      await expect(view.result.current.uploadAsset(image)).rejects.toThrow();
    }
    expect(mocks.upload).toHaveBeenCalledTimes(1);
  });

  it("invalidates deleted content without revoking addresses still used by images", async () => {
    const view = renderHook(() => useGuideAssets("project-a"));
    await view.result.current.signAsset("key");
    const gate = deferred<Blob>();
    mocks.content.mockReturnValueOnce(gate.promise);
    const late = view.result.current.signAsset("key", { refresh: true }).catch((e: unknown) => e);
    await view.result.current.deleteAsset("key");
    gate.resolve(png());
    expect(await late).toBeInstanceOf(Error);
    expect(createAddress).toHaveBeenCalledTimes(1);
    expect(revokeAddress).not.toHaveBeenCalled();
    await view.result.current.signAsset("key");
    expect(mocks.content).toHaveBeenCalledTimes(3);
  });

  it("preserves actionable errors and permits a later retry", async () => {
    const error = Object.assign(new Error("图片尚未保存归属，请重新上传"), { status: 409 });
    mocks.content.mockRejectedValueOnce(error);
    const view = renderHook(() => useGuideAssets("project-a"));
    await expect(view.result.current.signAsset("legacy")).rejects.toBe(error);
    await expect(view.result.current.signAsset("legacy", { refresh: true })).resolves.toBe(
      "blob:guide-1",
    );
  });

  it("rejects invalid response MIME and empty bytes before creating an image address", async () => {
    mocks.content.mockResolvedValueOnce(new Blob(["html"], { type: "text/html" }));
    mocks.content.mockResolvedValueOnce(new Blob([], { type: "image/png" }));
    const view = renderHook(() => useGuideAssets("project-a"));
    await expect(view.result.current.signAsset("bad-type")).rejects.toThrow("重新上传");
    await expect(view.result.current.signAsset("empty")).rejects.toThrow("重新上传");
    expect(createAddress).not.toHaveBeenCalled();
  });

  it("remains live after StrictMode effect replay", async () => {
    const view = renderHook(() => useGuideAssets("project-a"), {
      wrapper: ({ children }) => createElement(StrictMode, null, children),
    });
    await expect(view.result.current.signAsset("key")).resolves.toBe("blob:guide-1");
    expect(view.result.current.isCurrent()).toBe(true);
  });

  it("allows child preview effects to reload after StrictMode retires the first request", async () => {
    function Reader() {
      const images = useGuideAssets("project-a");
      return createElement(MarkdownView, {
        content: "![StrictMode guide](guide-asset:key)",
        resolveImage: images.resolveImage,
        imageScope: images.imageScope,
      });
    }
    render(createElement(StrictMode, null, createElement(Reader)));
    expect(await screen.findByRole("img", { name: "StrictMode guide" })).toHaveAttribute(
      "src",
      "blob:guide-1",
    );
    expect(mocks.content).toHaveBeenCalledTimes(2);
    expect(createAddress).toHaveBeenCalledTimes(1);
  });

  it("keeps separately mounted owners independent in the same project", async () => {
    const first = renderHook(() => useGuideAssets("project-a"));
    const second = renderHook(() => useGuideAssets("project-a"));
    await first.result.current.signAsset("key");
    await second.result.current.signAsset("key");
    first.unmount();
    expect(revokeAddress.mock.calls).toEqual([["blob:guide-1"]]);
    expect(await second.result.current.signAsset("key")).toBe("blob:guide-2");
  });
});

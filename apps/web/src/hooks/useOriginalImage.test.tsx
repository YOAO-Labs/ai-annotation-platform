import { StrictMode } from "react";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "@/stores/authStore";
import { readOriginalImage } from "@/utils/originalImageCache";
import { createOriginalImageAddress } from "@/utils/originalImageDisplay";
import { useOriginalImage } from "./useOriginalImage";

vi.mock("@/utils/originalImageCache", () => ({ readOriginalImage: vi.fn() }));
vi.mock("@/utils/originalImageDisplay", () => ({ createOriginalImageAddress: vi.fn() }));
const target = { kind: "task" as const, taskId: "task-one" };
const decodes: Array<{ resolve(): void; reject(error: Error): void }> = [];
const releases: Array<ReturnType<typeof vi.fn>> = [];
const user = { id: "reader" } as Parameters<ReturnType<typeof useAuthStore.getState>["setAuth"]>[1];
beforeEach(() => {
  useAuthStore.getState().setAuth("one", user);
  decodes.length = 0;
  releases.length = 0;
  vi.mocked(readOriginalImage)
    .mockReset()
    .mockResolvedValue({ identity: {} as never, blob: new Blob(["image"]) });
  vi.mocked(createOriginalImageAddress)
    .mockReset()
    .mockImplementation(async () => {
      const release = vi.fn();
      releases.push(release);
      return { url: `blob:image-${releases.length}`, release };
    });
  vi.stubGlobal(
    "Image",
    class {
      src = "";
      decode() {
        return new Promise<void>((resolve, reject) => decodes.push({ resolve, reject }));
      }
    },
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
describe("owned original image", () => {
  it("does no original read while gated off", () => {
    renderHook(() => useOriginalImage(null));
    expect(readOriginalImage).not.toHaveBeenCalled();
  });
  it("keeps a ready address during token refresh and retires it on a new session", async () => {
    const { result } = renderHook(() => useOriginalImage(target));
    await waitFor(() => expect(decodes).toHaveLength(1));
    await act(async () => decodes[0].resolve());
    const version = result.current.version;
    act(() => useAuthStore.getState().setToken("refreshed"));
    expect(result.current.url).toBe("blob:image-1");
    expect(result.current.version).toBe(version);
    expect(readOriginalImage).toHaveBeenCalledTimes(1);
    act(() => useAuthStore.getState().setAuth("new-login", user));
    expect(result.current.url).toBeNull();
    expect(releases[0]).toHaveBeenCalled();
  });
  it("discards late decode on task switch, then refreshes with a new address identity", async () => {
    const { result, rerender, unmount } = renderHook(
      ({ taskId }) => useOriginalImage({ kind: "task", taskId }),
      { initialProps: { taskId: "first" } },
    );
    await waitFor(() => expect(decodes).toHaveLength(1));
    rerender({ taskId: "second" });
    await waitFor(() => expect(decodes).toHaveLength(2));
    await act(async () => decodes[0].resolve());
    expect(result.current.url).toBeNull();
    await act(async () => decodes[1].resolve());
    const version = result.current.version;
    act(() => result.current.retry());
    expect(result.current.url).toBeNull();
    await waitFor(() => expect(decodes).toHaveLength(3));
    await act(async () => decodes[2].resolve());
    expect(result.current.version).toBeGreaterThan(version);
    unmount();
    expect(releases.every((release) => release.mock.calls.length > 0)).toBe(true);
  });
  it("never creates an address or reports a retired request after unmount", async () => {
    let reject!: (error: Error) => void;
    vi.mocked(readOriginalImage).mockReturnValue(
      new Promise((_, failure) => {
        reject = failure;
      }),
    );
    const { unmount } = renderHook(() => useOriginalImage(target));
    const signal = vi.mocked(readOriginalImage).mock.calls[0][1].signal;
    unmount();
    await act(async () => reject(new Error("old account error")));
    expect(signal.aborted).toBe(true);
    expect(createOriginalImageAddress).not.toHaveBeenCalled();
  });
  it("distinguishes browser decode failure from a server format refusal", async () => {
    const { result } = renderHook(() => useOriginalImage(target));
    await waitFor(() => expect(decodes).toHaveLength(1));
    await act(async () => decodes[0].reject(new Error("Unsupported TIFF")));
    expect(result.current.error).toContain("浏览器无法解码");
    expect(releases[0]).toHaveBeenCalled();
  });
  it("re-describes an explicit content binding change on the same Task without returning old bytes", async () => {
    const { result, rerender } = renderHook(({ item }) => useOriginalImage(target, item), {
      initialProps: { item: "first-item" },
    });
    await waitFor(() => expect(decodes).toHaveLength(1));
    await act(async () => decodes[0].resolve());
    expect(result.current.url).toBe("blob:image-1");
    rerender({ item: "second-item" });
    expect(result.current.url).toBeNull();
    expect(releases[0]).toHaveBeenCalled();
    await waitFor(() => expect(decodes).toHaveLength(2));
    expect(vi.mocked(readOriginalImage).mock.calls.map(([resource]) => resource)).toEqual([
      target,
      target,
    ]);
    await act(async () => decodes[1].resolve());
    expect(result.current.url).toBe("blob:image-2");
  });
  it("fences the first StrictMode setup while publishing the live one", async () => {
    const { result } = renderHook(() => useOriginalImage(target), { wrapper: StrictMode });
    await waitFor(() => expect(decodes).toHaveLength(1));
    await act(async () => decodes[0].resolve());
    expect(result.current.ready).toBe(true);
  });
});

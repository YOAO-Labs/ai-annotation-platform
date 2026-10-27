import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "@/stores/authStore";
const acquire = vi.hoisted(() => vi.fn());
vi.mock("./pointCloudAssetCache", () => ({ acquireCameraImageAddress: acquire }));
import { useCameraOriginalImage } from "./useCameraOriginalImage";
function deferred() {
  let resolve!: (value: { url: string; release(): void }) => void;
  const promise = new Promise<{ url: string; release(): void }>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
describe("camera original address ownership", () => {
  beforeEach(() => {
    acquire.mockReset();
    useAuthStore.getState().setAuth("first", { id: "account" } as never);
  });
  it("hides and releases an old address before the next source resolves", async () => {
    const first = deferred(),
      next = deferred();
    acquire.mockReturnValueOnce(first.promise).mockReturnValueOnce(next.promise);
    const release = vi.fn();
    const hook = renderHook(({ source }) => useCameraOriginalImage(source), {
      initialProps: { source: "/first" },
    });
    await act(async () => {
      first.resolve({ url: "blob:first", release });
    });
    expect(hook.result.current.url).toBe("blob:first");
    hook.rerender({ source: "/second" });
    expect(hook.result.current.url).toBeUndefined();
    expect(release).toHaveBeenCalledTimes(1);
    expect(acquire.mock.calls[0][1].aborted).toBe(true);
    hook.unmount();
  });
  it("releases ignored-abort late addresses and never publishes a previous session", async () => {
    const first = deferred(),
      next = deferred();
    acquire.mockReturnValueOnce(first.promise).mockReturnValueOnce(next.promise);
    const release = vi.fn();
    const hook = renderHook(() => useCameraOriginalImage("/same-path"));
    act(() => useAuthStore.getState().setAuth("new-login", { id: "account" } as never));
    await act(async () => {
      first.resolve({ url: "blob:old-login", release });
    });
    expect(release).toHaveBeenCalledTimes(1);
    expect(hook.result.current.url).toBeUndefined();
    const nextRelease = vi.fn();
    await act(async () => {
      next.resolve({ url: "blob:new-login", release: nextRelease });
    });
    expect(hook.result.current.url).toBe("blob:new-login");
    hook.unmount();
    expect(nextRelease).toHaveBeenCalledTimes(1);
  });
  it("keeps the image address through token refresh in the same session", async () => {
    const release = vi.fn();
    acquire.mockResolvedValue({ url: "blob:stable", release });
    const hook = renderHook(() => useCameraOriginalImage("/same-content"));
    await waitFor(() => expect(hook.result.current.url).toBe("blob:stable"));
    act(() => useAuthStore.getState().setToken("refreshed"));
    expect(hook.result.current.url).toBe("blob:stable");
    expect(acquire).toHaveBeenCalledTimes(1);
    hook.unmount();
    expect(release).toHaveBeenCalledTimes(1);
  });
});

import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiClient, ApiError } from "@/api/client";
import { useAuthStore } from "@/stores/authStore";
import { useVerifiedImage } from "./useVerifiedImage";
import { useSessionGuard } from "./useSessionGuard";

vi.mock("@/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/client")>();
  return { ...actual, apiClient: { ...actual.apiClient, silentGetBlob: vi.fn() } };
});
const decodes: Array<() => void> = [];
const revoke = vi.fn();
const user = { id: "reader" } as Parameters<ReturnType<typeof useAuthStore.getState>["setAuth"]>[1];

beforeEach(() => {
  vi.mocked(apiClient.silentGetBlob)
    .mockReset()
    .mockResolvedValue(new Blob(["image"]));
  useAuthStore.getState().setAuth("token-one", user);
  decodes.length = 0;
  revoke.mockClear();
  let sequence = 0;
  vi.stubGlobal(
    "Image",
    class {
      src = "";
      decode() {
        return new Promise<void>((resolve) => decodes.push(resolve));
      }
    },
  );
  vi.stubGlobal(
    "URL",
    Object.assign(class extends URL {}, {
      createObjectURL: vi.fn(() => `blob:test-${++sequence}`),
      revokeObjectURL: revoke,
    }),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("verified original ownership", () => {
  it("bounds grid preview requests and removes a cancelled queued preview", async () => {
    const releases: Array<(value: Blob) => void> = [];
    vi.mocked(apiClient.silentGetBlob).mockImplementation(
      () => new Promise<Blob>((resolve) => releases.push(resolve)),
    );
    const first = renderHook(() => useVerifiedImage("/one/preview"));
    const second = renderHook(() => useVerifiedImage("/two/preview"));
    const cancelled = renderHook(() => useVerifiedImage("/cancelled/preview"));
    const queued = renderHook(() => useVerifiedImage("/queued/preview"));
    expect(apiClient.silentGetBlob).toHaveBeenCalledTimes(2);
    cancelled.unmount();
    await act(async () => releases[0](new Blob(["first"])));
    await waitFor(() => expect(apiClient.silentGetBlob).toHaveBeenCalledTimes(3));
    expect(apiClient.silentGetBlob).toHaveBeenLastCalledWith("/queued/preview", {
      signal: expect.any(AbortSignal),
    });
    await act(async () => {
      releases[1](new Blob(["second"]));
      releases[2](new Blob(["queued"]));
    });
    expect(apiClient.silentGetBlob).toHaveBeenCalledTimes(3);
    first.unmount();
    second.unmount();
    queued.unmount();
  });

  it("retries a busy preview within a bounded budget and decodes the recovered image", async () => {
    vi.mocked(apiClient.silentGetBlob).mockRejectedValueOnce(
      new ApiError(429, "Busy", { code: "snapshot_capacity_exceeded" }),
    );
    const { result } = renderHook(() => useVerifiedImage("/one/preview"));
    await waitFor(() => expect(decodes).toHaveLength(1));
    expect(apiClient.silentGetBlob).toHaveBeenCalledTimes(2);
    await act(async () => decodes[0]());
    expect(result.current.ready).toBe(true);
  });

  it("does not retry authorization failures or send a retired preview retry", async () => {
    vi.mocked(apiClient.silentGetBlob).mockRejectedValueOnce(new ApiError(403, "Forbidden"));
    const forbidden = renderHook(() => useVerifiedImage("/forbidden/preview"));
    await waitFor(() => expect(forbidden.result.current.error).toBe("Forbidden"));
    expect(apiClient.silentGetBlob).toHaveBeenCalledTimes(1);
    forbidden.unmount();
    vi.mocked(apiClient.silentGetBlob).mockRejectedValueOnce(
      new ApiError(429, "Busy", { code: "snapshot_capacity_exceeded" }),
    );
    const retired = renderHook(() => useVerifiedImage("/retired/preview"));
    await act(async () => {});
    retired.unmount();
    await new Promise((resolve) => setTimeout(resolve, 550));
    expect(apiClient.silentGetBlob).toHaveBeenCalledTimes(2);
  });

  it("stops a persistently busy preview after three attempts", async () => {
    vi.mocked(apiClient.silentGetBlob).mockRejectedValue(
      new ApiError(429, "Busy", { code: "snapshot_capacity_exceeded" }),
    );
    const { result } = renderHook(() => useVerifiedImage("/busy/preview"));
    await waitFor(() => expect(result.current.error).toBe("原图读取繁忙，请稍后重试"), {
      timeout: 2500,
    });
    expect(apiClient.silentGetBlob).toHaveBeenCalledTimes(3);
    expect(result.current.url).toBeNull();
  });

  it("discards a late decode after a task switch and revokes both owned URLs", async () => {
    const { result, rerender, unmount } = renderHook(({ path }) => useVerifiedImage(path), {
      initialProps: { path: "/one" },
    });
    await waitFor(() => expect(decodes).toHaveLength(1));
    const firstSignal = vi.mocked(apiClient.silentGetBlob).mock.calls[0][1]?.signal;
    rerender({ path: "/two" });
    await waitFor(() => expect(decodes).toHaveLength(2));
    expect(firstSignal?.aborted).toBe(true);
    await act(async () => decodes[0]());
    expect(result.current.url).toBeNull();
    await act(async () => decodes[1]());
    expect(result.current.url).toBe("blob:test-2");
    unmount();
    expect(revoke).toHaveBeenCalledWith("blob:test-1");
    expect(revoke).toHaveBeenCalledWith("blob:test-2");
  });

  it("retires content on retry but keeps the same session across token refresh", async () => {
    const { result } = renderHook(() => useVerifiedImage("/one"));
    await waitFor(() => expect(decodes).toHaveLength(1));
    await act(async () => decodes[0]());
    expect(result.current.ready).toBe(true);
    act(() => result.current.retry());
    expect(result.current.ready).toBe(false);
    await waitFor(() => expect(decodes).toHaveLength(2));
    await act(async () => decodes[1]());
    expect(result.current.url).toBe("blob:test-2");
    act(() => useAuthStore.getState().setToken("token-two"));
    expect(result.current.url).toBe("blob:test-2");
    expect(decodes).toHaveLength(2);
  });

  it("never publishes an unresolved request after logout", async () => {
    let resolve!: (value: Blob) => void;
    vi.mocked(apiClient.silentGetBlob).mockImplementation(
      () =>
        new Promise<Blob>((r) => {
          resolve = r;
        }),
    );
    const { result } = renderHook(() => useVerifiedImage("/one"));
    act(() => useAuthStore.getState().logout());
    await act(async () => resolve(new Blob(["late"])));
    expect(result.current.url).toBeNull();
    expect(URL.createObjectURL).not.toHaveBeenCalled();
  });

  it("retires multi-step actions even if auth returns to the original values", () => {
    const { result, unmount } = renderHook(() => useSessionGuard());
    const current = result.current();
    act(() => {
      useAuthStore.getState().setToken("other");
      useAuthStore.getState().setToken("token-one");
    });
    expect(current()).toBe(false);
    const next = result.current();
    expect(next()).toBe(true);
    unmount();
    expect(next()).toBe(false);
  });
});

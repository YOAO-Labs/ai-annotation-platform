import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiClient } from "@/api/client";
import { useAuthStore } from "@/stores/authStore";
import { useVerifiedImage } from "./useVerifiedImage";
import { useSessionGuard } from "./useSessionGuard";

vi.mock("@/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/client")>();
  return { ...actual, apiClient: { ...actual.apiClient, getBlob: vi.fn() } };
});
const decodes: Array<() => void> = [];
const revoke = vi.fn();
const user = { id: "reader" } as Parameters<ReturnType<typeof useAuthStore.getState>["setAuth"]>[1];

beforeEach(() => {
  vi.mocked(apiClient.getBlob)
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
  it("discards a late decode after a task switch and revokes both owned URLs", async () => {
    const { result, rerender, unmount } = renderHook(({ path }) => useVerifiedImage(path), {
      initialProps: { path: "/one" },
    });
    await waitFor(() => expect(decodes).toHaveLength(1));
    const firstSignal = vi.mocked(apiClient.getBlob).mock.calls[0][1]?.signal;
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

  it("retires displayed content immediately on retry or token refresh", async () => {
    const { result } = renderHook(() => useVerifiedImage("/one"));
    await waitFor(() => expect(decodes).toHaveLength(1));
    await act(async () => decodes[0]());
    expect(result.current.ready).toBe(true);
    act(() => result.current.retry());
    expect(result.current.ready).toBe(false);
    await waitFor(() => expect(decodes).toHaveLength(2));
    act(() => useAuthStore.getState().setToken("token-two"));
    await act(async () => decodes[1]());
    expect(result.current.ready).toBe(false);
    await waitFor(() => expect(decodes).toHaveLength(3));
    await act(async () => decodes[2]());
    expect(result.current.url).toBe("blob:test-3");
  });

  it("never publishes an unresolved request after logout", async () => {
    let resolve!: (value: Blob) => void;
    vi.mocked(apiClient.getBlob).mockImplementation(
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

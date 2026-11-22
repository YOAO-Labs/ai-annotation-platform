import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";

import { useAuthStore } from "@/stores/authStore";
import { useInteractiveBackendPref } from "./useInteractiveBackendPref";

const getPreferencesMock = vi.fn();
const updatePreferencesMock = vi.fn();

let queryClient: QueryClient;
const wrapper = ({ children }: { children: ReactNode }) =>
  createElement(QueryClientProvider, { client: queryClient }, children);

vi.mock("@/api/auth", () => ({
  authApi: {
    getPreferences: (...args: unknown[]) => getPreferencesMock(...args),
    updatePreferences: (...args: unknown[]) => updatePreferencesMock(...args),
  },
}));

function setUser(id: string | null) {
  if (id) useAuthStore.setState({ user: { id } as never });
  else useAuthStore.setState({ user: null });
}

describe("useInteractiveBackendPref", () => {
  beforeEach(() => {
    getPreferencesMock.mockReset();
    updatePreferencesMock.mockReset();
    setUser(null);
    queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    });
  });
  afterEach(() => setUser(null));

  it("登出 (userId → null) 清 byProject, 下一个用户首渲染不读到旧值 (P1 回归)", async () => {
    setUser("u1");
    getPreferencesMock.mockResolvedValueOnce({
      ai: { interactive_backend_by_project: { p1: "b1" } },
    });
    const { result, rerender } = renderHook(() => useInteractiveBackendPref("p1"), { wrapper });
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(result.current.savedBackendId).toBe("b1");

    act(() => setUser(null));
    rerender();
    expect(result.current.savedBackendId).toBeUndefined();
    expect(result.current.loaded).toBe(false);

    getPreferencesMock.mockResolvedValueOnce({ ai: {} });
    act(() => setUser("u2"));
    rerender();
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(result.current.savedBackendId).toBeUndefined();
  });

  it("unmount 时 pending 节流写 fire-and-forget flush (P1 回归)", async () => {
    setUser("u1");
    getPreferencesMock.mockResolvedValueOnce({ ai: {} });
    updatePreferencesMock.mockResolvedValue(undefined);

    const { result, unmount } = renderHook(() => useInteractiveBackendPref("p1"), { wrapper });
    await waitFor(() => expect(result.current.loaded).toBe(true));

    act(() => result.current.save("b9"));
    expect(updatePreferencesMock).not.toHaveBeenCalled();

    unmount();
    expect(updatePreferencesMock).toHaveBeenCalledTimes(1);
    expect(updatePreferencesMock).toHaveBeenCalledWith({
      ai: { interactive_backend_by_project: { p1: "b9" } },
    });
  });

  it("无 pending 时 unmount 不发多余写", async () => {
    setUser("u1");
    getPreferencesMock.mockResolvedValueOnce({
      ai: { interactive_backend_by_project: { p1: "b1" } },
    });
    const { result, unmount } = renderHook(() => useInteractiveBackendPref("p1"), { wrapper });
    await waitFor(() => expect(result.current.loaded).toBe(true));
    unmount();
    expect(updatePreferencesMock).not.toHaveBeenCalled();
  });

  it("save(null) 删 projectId 条目", async () => {
    setUser("u1");
    getPreferencesMock.mockResolvedValueOnce({
      ai: { interactive_backend_by_project: { p1: "b1", p2: "b2" } },
    });
    updatePreferencesMock.mockResolvedValue(undefined);
    const { result, unmount } = renderHook(() => useInteractiveBackendPref("p1"), { wrapper });
    await waitFor(() => expect(result.current.loaded).toBe(true));

    act(() => result.current.save(null));
    unmount();
    expect(updatePreferencesMock).toHaveBeenCalledWith({
      ai: { interactive_backend_by_project: { p2: "b2" } },
    });
  });
});

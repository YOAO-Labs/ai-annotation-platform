import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";

vi.mock("@/api/me", () => ({
  meApi: {
    heartbeat: vi.fn(async () => undefined),
  },
}));

const useAuthStoreMock = vi.fn();
vi.mock("@/stores/authStore", () => ({
  useAuthStore: (selector: (s: { token: string | null }) => unknown) =>
    selector({ token: useAuthStoreMock() }),
}));

import { meApi } from "@/api/me";
import { useHeartbeat } from "./useHeartbeat";

describe("useHeartbeat", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    (meApi.heartbeat as any).mockClear();
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => "visible",
    });
    useAuthStoreMock.mockReturnValue("token-abc");
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("无 token → 不调用 heartbeat", () => {
    useAuthStoreMock.mockReturnValue(null);
    renderHook(() => useHeartbeat(1000));
    vi.advanceTimersByTime(5000);
    expect(meApi.heartbeat).not.toHaveBeenCalled();
  });

  it("已登录 visible → 立即一次 + 每 interval 一次", () => {
    renderHook(() => useHeartbeat(1000));
    expect(meApi.heartbeat).toHaveBeenCalledTimes(1);

    vi.advanceTimersByTime(1000);
    expect(meApi.heartbeat).toHaveBeenCalledTimes(2);

    vi.advanceTimersByTime(2000);
    expect(meApi.heartbeat).toHaveBeenCalledTimes(4);
  });

  it("hidden 时暂停；visible 恢复立即一次 + 周期", () => {
    renderHook(() => useHeartbeat(1000));
    expect(meApi.heartbeat).toHaveBeenCalledTimes(1);

    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => "hidden",
    });
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    vi.advanceTimersByTime(5000);
    expect(meApi.heartbeat).toHaveBeenCalledTimes(1);

    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => "visible",
    });
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(meApi.heartbeat).toHaveBeenCalledTimes(2);

    vi.advanceTimersByTime(1000);
    expect(meApi.heartbeat).toHaveBeenCalledTimes(3);
  });

  it("卸载后不再触发", () => {
    const { unmount } = renderHook(() => useHeartbeat(1000));
    expect(meApi.heartbeat).toHaveBeenCalledTimes(1);
    unmount();
    vi.advanceTimersByTime(10_000);
    expect(meApi.heartbeat).toHaveBeenCalledTimes(1);
  });

  it("heartbeat 失败被静默吞掉", async () => {
    (meApi.heartbeat as any).mockRejectedValueOnce(new Error("network"));
    const { result } = renderHook(() => useHeartbeat(1000));
    expect(result).toBeDefined();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(meApi.heartbeat).toHaveBeenCalled();
  });
});

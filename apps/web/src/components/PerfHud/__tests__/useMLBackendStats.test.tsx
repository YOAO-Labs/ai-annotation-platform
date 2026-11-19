/**
 * useMLBackendStats WS smoke 测试 (PerfHud).
 *
 * 验证 URL 路径 /ws/ml-backend-stats?token=... 拼接正确, visible+token 才建连,
 * 收 backends 帧后 ring buffer 行为, 卸载清理.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useAuthStore } from "@/stores/authStore";

import { useMLBackendStats } from "../useMLBackendStats";
import { usePerfHudStore } from "../usePerfHudStore";

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  url: string;
  onopen: ((e: Event) => void) | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  onclose: ((e: CloseEvent) => void) | null = null;
  onerror: ((e: Event) => void) | null = null;
  readyState = 0;
  closedManually = false;

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }
  close() {
    this.closedManually = true;
  }
  triggerOpen() {
    this.readyState = 1;
    this.onopen?.(new Event("open"));
  }
  triggerMessage(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) } as MessageEvent);
  }
}

let originalWS: typeof globalThis.WebSocket | undefined;

beforeEach(() => {
  MockWebSocket.instances = [];
  vi.useFakeTimers();
  originalWS = globalThis.WebSocket;
  Object.defineProperty(globalThis, "WebSocket", {
    value: MockWebSocket,
    writable: true,
    configurable: true,
  });
  useAuthStore.setState({ token: "tok-stat", user: null });
  usePerfHudStore.setState({ visible: true });
});

afterEach(() => {
  vi.useRealTimers();
  Object.defineProperty(globalThis, "WebSocket", {
    value: originalWS,
    writable: true,
    configurable: true,
  });
  usePerfHudStore.setState({ visible: false });
});

describe("useMLBackendStats", () => {
  it("visible+token 时拼 /ws/ml-backend-stats URL", () => {
    renderHook(() => useMLBackendStats());
    expect(MockWebSocket.instances.length).toBe(1);
    expect(MockWebSocket.instances[0].url).toMatch(/\/ws\/ml-backend-stats/);
    expect(MockWebSocket.instances[0].url).toContain("token=tok-stat");
  });

  it("不可见时不建连", () => {
    usePerfHudStore.setState({ visible: false });
    renderHook(() => useMLBackendStats());
    expect(MockWebSocket.instances.length).toBe(0);
  });

  it("无 token 不建连", () => {
    useAuthStore.setState({ token: null, user: null });
    renderHook(() => useMLBackendStats());
    expect(MockWebSocket.instances.length).toBe(0);
  });

  it("收 backends 帧后 snapshots 反映", () => {
    const { result } = renderHook(() => useMLBackendStats());
    act(() => {
      MockWebSocket.instances[0].triggerOpen();
      MockWebSocket.instances[0].triggerMessage({
        backends: [
          {
            backend_id: "b1",
            backend_name: "sam2",
            state: "connected",
            loaded: false,
            pool: { loaded_variants: [] },
          },
        ],
      });
    });
    expect(result.current.snapshots["b1"]?.backend_name).toBe("sam2");
    expect(result.current.snapshots["b1"]?.loaded).toBe(false);
    expect(result.current.snapshots["b1"]?.pool?.loaded_variants).toEqual([]);
  });

  it("有 physical_key 时按物理 backend 维护 snapshots 和 history", () => {
    const { result } = renderHook(() => useMLBackendStats());
    act(() => {
      MockWebSocket.instances[0].triggerOpen();
      MockWebSocket.instances[0].triggerMessage({
        backends: [
          {
            physical_key: "http://ml.local:9000",
            url_host: "ml.local:9000",
            backend_id: "binding-a",
            backend_name: "sam-a",
            state: "connected",
            gpu_info: { gpu_utilization_percent: 42, memory_used_mb: 2, memory_total_mb: 4 },
            bindings: [
              {
                backend_id: "binding-a",
                backend_name: "sam-a",
                project_display_id: "P-A",
              },
              {
                backend_id: "binding-b",
                backend_name: "sam-b",
                project_display_id: "P-B",
              },
            ],
          },
        ],
      });
    });

    const snap = result.current.snapshots["http://ml.local:9000"];
    expect(snap?.bindings?.map((b) => b.backend_name)).toEqual(["sam-a", "sam-b"]);
    expect(result.current.snapshots["binding-a"]).toBeUndefined();
    expect(result.current.history["http://ml.local:9000"]?.gpuUtil).toEqual([42]);
  });

  it("ping 帧不触发 state 变化", () => {
    const { result } = renderHook(() => useMLBackendStats());
    act(() => {
      MockWebSocket.instances[0].triggerMessage({ type: "ping" });
    });
    expect(Object.keys(result.current.snapshots).length).toBe(0);
  });

  it("卸载主动 close", () => {
    const { unmount } = renderHook(() => useMLBackendStats());
    unmount();
    expect(MockWebSocket.instances[0].closedManually).toBe(true);
  });
});

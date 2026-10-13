/**
 * v0.9.13 · usePreannotationProgress WS smoke 测试.
 *
 * 验证 URL 路径 /ws/projects/{id}/preannotate 拼接正确, 收消息 setProgress, 卸载清理.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";

import { useAuthStore } from "@/stores/authStore";
import { usePreannotationProgress } from "../usePreannotation";

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
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
    this.onclose?.({ code: 1000, wasClean: true } as CloseEvent);
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
  // Auth hydration: a token is required before the root WS URL is built.
  useAuthStore.setState({ token: "test-token", user: { id: "u1" } as never });
});

afterEach(() => {
  vi.useRealTimers();
  Object.defineProperty(globalThis, "WebSocket", {
    value: originalWS,
    writable: true,
    configurable: true,
  });
});

describe("usePreannotationProgress", () => {
  it("projectId 存在时拼 /ws/projects/{id}/preannotate（带 token）", () => {
    renderHook(() => usePreannotationProgress("p-abc"));
    expect(MockWebSocket.instances.length).toBe(1);
    expect(MockWebSocket.instances[0].url).toContain("/ws/projects/p-abc/preannotate");
    expect(MockWebSocket.instances[0].url).toContain("token=test-token");
  });

  it("projectId 为空不建连", () => {
    renderHook(() => usePreannotationProgress(undefined));
    expect(MockWebSocket.instances.length).toBe(0);
  });

  it("未 hydration（无 token）不建无鉴权连接", () => {
    useAuthStore.setState({ token: null });
    renderHook(() => usePreannotationProgress("p-abc"));
    expect(MockWebSocket.instances.length).toBe(0);
  });

  it("收消息后 progress 更新", () => {
    const { result } = renderHook(() => usePreannotationProgress("p-1"));
    act(() => {
      MockWebSocket.instances[0].triggerOpen();
      MockWebSocket.instances[0].triggerMessage({
        current: 3,
        total: 10,
        status: "running",
        error: null,
      });
    });
    expect(result.current.progress?.current).toBe(3);
    expect(result.current.progress?.total).toBe(10);
  });

  it("卸载主动断（已 OPEN 立即关）", () => {
    const { unmount } = renderHook(() => usePreannotationProgress("p-1"));
    act(() => MockWebSocket.instances[0].triggerOpen());
    unmount();
    expect(MockWebSocket.instances[0].closedManually).toBe(true);
  });

  it("卸载时仍在握手 → 延迟到 onopen 再关", () => {
    const { unmount } = renderHook(() => usePreannotationProgress("p-1"));
    // 未 triggerOpen，readyState 仍 CONNECTING：直接关会触发浏览器告警，故延迟
    unmount();
    expect(MockWebSocket.instances[0].closedManually).toBe(false);
    act(() => MockWebSocket.instances[0].triggerOpen());
    expect(MockWebSocket.instances[0].closedManually).toBe(true);
  });
});
